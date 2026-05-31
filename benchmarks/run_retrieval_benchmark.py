from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

import core  # noqa: E402
import local_semble  # noqa: E402

from benchmarks.data import (  # noqa: E402
    DEFAULT_REPOS,
    Task,
    default_dataset_root,
    ensure_pinned_checkouts,
    load_repo_specs,
    load_tasks,
)
from benchmarks.metrics import (  # noqa: E402
    bootstrap_mean_ci,
    bootstrap_paired_delta_ci,
    dedupe_paths,
    metrics_for_ranking,
)


@dataclass
class QueryResult:
    task_id: str
    repo: str
    language: str
    category: str
    query: str
    backend: str
    latency_ms: float
    ranked_paths: list[str]
    relevant_paths: list[str]
    ndcg10: float
    recall10: float
    top1: float
    mrr: float
    success: bool
    remote_success: bool
    degraded: bool
    retries: int
    error_code: str | None
    error: str | None


class RemotePacer:
    def __init__(self, min_interval_ms: int, jitter_ms: int, seed: int) -> None:
        self.min_interval_ms = min_interval_ms
        self.jitter_ms = jitter_ms
        self.rng = random.Random(seed)
        self.last_started: float | None = None

    def wait(self) -> None:
        if self.last_started is None:
            return
        target_gap = (self.min_interval_ms + self.rng.uniform(0, self.jitter_ms)) / 1000.0
        elapsed = time.monotonic() - self.last_started
        if elapsed < target_gap:
            time.sleep(target_gap - elapsed)

    def note_start(self) -> None:
        self.last_started = time.monotonic()


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _round_metric(value: float) -> float:
    return round(value, 4)


def _relevant_paths(task: Task) -> list[str]:
    return dedupe_paths([target.path for target in task.all_relevant])


def _local_ranked_paths(payload: dict[str, Any], *, limit: int) -> list[str]:
    paths = []
    for item in payload.get("results") or []:
        chunk = item.get("chunk") or {}
        path = chunk.get("file_path")
        if path:
            paths.append(str(path))
    return dedupe_paths(paths)[:limit]


def _remote_ranked_paths(result: dict[str, Any], *, limit: int) -> list[str]:
    return dedupe_paths([str(item["path"]) for item in result.get("files") or []])[:limit]


def _retryable_error(error_code: str | None, error_message: str | None) -> bool:
    lowered = (error_message or "").lower()
    return error_code in {"RATE_LIMITED", "TIMEOUT", "RESOURCE_EXHAUSTED"} or "resource_exhausted" in lowered


def _sleep_with_jitter(base_ms: int, attempt: int, rng: random.Random) -> None:
    cap_ms = base_ms * (2 ** attempt)
    delay_ms = rng.uniform(base_ms, cap_ms)
    time.sleep(delay_ms / 1000.0)


def _run_local(task: Task, project_root: str, max_results: int) -> QueryResult:
    started = time.perf_counter()
    payload = local_semble.search(
        query=task.query,
        project_root=project_root,
        top_k=max_results,
        content=["code"],
    )
    latency_ms = (time.perf_counter() - started) * 1000
    ranked_paths = _local_ranked_paths(payload, limit=max_results)
    relevant_paths = _relevant_paths(task)
    metrics = metrics_for_ranking(ranked_paths, relevant_paths, k=max_results)
    return QueryResult(
        task_id=task.task_id,
        repo=task.repo,
        language=task.language,
        category=task.category,
        query=task.query,
        backend="local",
        latency_ms=latency_ms,
        ranked_paths=ranked_paths,
        relevant_paths=relevant_paths,
        ndcg10=metrics["ndcg10"],
        recall10=metrics["recall10"],
        top1=metrics["top1"],
        mrr=metrics["mrr"],
        success=bool(ranked_paths),
        remote_success=True,
        degraded=False,
        retries=0,
        error_code=None,
        error=None,
    )


def _run_remote_with_retries(
    task: Task,
    project_root: str,
    *,
    max_results: int,
    max_turns: int,
    tree_depth: int,
    timeout_ms: int,
    pacer: RemotePacer,
    retry_base_ms: int,
    max_retries: int,
    retry_seed: int,
    local_context: str | None = None,
) -> tuple[dict[str, Any], int]:
    retries = 0
    rng = random.Random(retry_seed)
    while True:
        pacer.wait()
        pacer.note_start()
        try:
            result = core.search(
                query=task.query,
                project_root=project_root,
                max_turns=max_turns,
                max_results=max_results,
                tree_depth=tree_depth,
                timeout_ms=timeout_ms,
                local_context=local_context,
            )
        except Exception as exc:
            result = {
                "files": [],
                "error": str(exc),
                "_meta": {"error_code": "EXCEPTION"},
            }
        if not result.get("error"):
            return result, retries
        meta = result.get("_meta") or {}
        error_code = meta.get("error_code")
        if retries >= max_retries or not _retryable_error(error_code, result.get("error")):
            return result, retries
        retries += 1
        _sleep_with_jitter(retry_base_ms, retries, rng)


def _run_remote(
    task: Task,
    project_root: str,
    *,
    max_results: int,
    max_turns: int,
    tree_depth: int,
    timeout_ms: int,
    pacer: RemotePacer,
    retry_base_ms: int,
    max_retries: int,
    retry_seed: int,
) -> QueryResult:
    started = time.perf_counter()
    result, retries = _run_remote_with_retries(
        task,
        project_root,
        max_results=max_results,
        max_turns=max_turns,
        tree_depth=tree_depth,
        timeout_ms=timeout_ms,
        pacer=pacer,
        retry_base_ms=retry_base_ms,
        max_retries=max_retries,
        retry_seed=retry_seed,
    )
    latency_ms = (time.perf_counter() - started) * 1000
    ranked_paths = _remote_ranked_paths(result, limit=max_results)
    relevant_paths = _relevant_paths(task)
    metrics = metrics_for_ranking(ranked_paths, relevant_paths, k=max_results)
    meta = result.get("_meta") or {}
    return QueryResult(
        task_id=task.task_id,
        repo=task.repo,
        language=task.language,
        category=task.category,
        query=task.query,
        backend="remote",
        latency_ms=latency_ms,
        ranked_paths=ranked_paths,
        relevant_paths=relevant_paths,
        ndcg10=metrics["ndcg10"],
        recall10=metrics["recall10"],
        top1=metrics["top1"],
        mrr=metrics["mrr"],
        success=not result.get("error") and bool(ranked_paths),
        remote_success=not result.get("error"),
        degraded=False,
        retries=retries,
        error_code=meta.get("error_code"),
        error=result.get("error"),
    )


def _run_hybrid(
    task: Task,
    project_root: str,
    *,
    max_results: int,
    max_turns: int,
    tree_depth: int,
    timeout_ms: int,
    pacer: RemotePacer,
    retry_base_ms: int,
    max_retries: int,
    retry_seed: int,
) -> QueryResult:
    started = time.perf_counter()
    local_payload = local_semble.search(
        query=task.query,
        project_root=project_root,
        top_k=max_results,
        content=["code"],
    )
    prompt_context = core._format_semble_prompt_context(  # type: ignore[attr-defined]
        local_payload,
        max_chunks=min(max_results, 5),
    )
    remote_result, retries = _run_remote_with_retries(
        task,
        project_root,
        max_results=max_results,
        max_turns=max_turns,
        tree_depth=tree_depth,
        timeout_ms=timeout_ms,
        pacer=pacer,
        retry_base_ms=retry_base_ms,
        max_retries=max_retries,
        retry_seed=retry_seed,
        local_context=prompt_context,
    )
    latency_ms = (time.perf_counter() - started) * 1000
    relevant_paths = _relevant_paths(task)
    limited_local = core._limit_semble_payload(  # type: ignore[attr-defined]
        local_payload,
        min(max_results, 5),
    )
    local_ranked = _local_ranked_paths(local_payload, limit=max_results)
    remote_ranked = _remote_ranked_paths(remote_result, limit=max_results)
    if remote_result.get("error"):
        ranked_paths = local_ranked
        degraded = True
        success = bool(local_ranked)
    else:
        ranked_paths = dedupe_paths(remote_ranked + _local_ranked_paths(limited_local, limit=max_results))[:max_results]
        degraded = False
        success = bool(ranked_paths)
    metrics = metrics_for_ranking(ranked_paths, relevant_paths, k=max_results)
    meta = remote_result.get("_meta") or {}
    return QueryResult(
        task_id=task.task_id,
        repo=task.repo,
        language=task.language,
        category=task.category,
        query=task.query,
        backend="hybrid",
        latency_ms=latency_ms,
        ranked_paths=ranked_paths,
        relevant_paths=relevant_paths,
        ndcg10=metrics["ndcg10"],
        recall10=metrics["recall10"],
        top1=metrics["top1"],
        mrr=metrics["mrr"],
        success=success,
        remote_success=not remote_result.get("error"),
        degraded=degraded,
        retries=retries,
        error_code=meta.get("error_code"),
        error=remote_result.get("error"),
    )


def _summarize_backend(results: list[QueryResult], *, bootstrap_samples: int, seed: int) -> dict[str, Any]:
    ndcg10 = [result.ndcg10 for result in results]
    recall10 = [result.recall10 for result in results]
    top1 = [result.top1 for result in results]
    mrr = [result.mrr for result in results]
    latencies = [result.latency_ms for result in results]
    by_category: dict[str, dict[str, float | int]] = {}
    for category in sorted({result.category for result in results}):
        grouped = [result for result in results if result.category == category]
        by_category[category] = {
            "count": len(grouped),
            "ndcg10": _round_metric(_mean([result.ndcg10 for result in grouped])),
            "recall10": _round_metric(_mean([result.recall10 for result in grouped])),
            "top1": _round_metric(_mean([result.top1 for result in grouped])),
            "mrr": _round_metric(_mean([result.mrr for result in grouped])),
        }

    ndcg_low, ndcg_high = bootstrap_mean_ci(ndcg10, samples=bootstrap_samples, seed=seed)
    recall_low, recall_high = bootstrap_mean_ci(recall10, samples=bootstrap_samples, seed=seed + 1)
    return {
        "queries": len(results),
        "ndcg10": _round_metric(_mean(ndcg10)),
        "ndcg10_ci95": [_round_metric(ndcg_low), _round_metric(ndcg_high)],
        "recall10": _round_metric(_mean(recall10)),
        "recall10_ci95": [_round_metric(recall_low), _round_metric(recall_high)],
        "top1": _round_metric(_mean(top1)),
        "mrr": _round_metric(_mean(mrr)),
        "latency_ms": {
            "p50": round(statistics.median(latencies), 1) if latencies else 0.0,
            "p90": round(_percentile(latencies, 0.9), 1),
            "mean": round(_mean(latencies), 1),
        },
        "success_rate": _round_metric(_mean([1.0 if result.success else 0.0 for result in results])),
        "remote_success_rate": _round_metric(_mean([1.0 if result.remote_success else 0.0 for result in results])),
        "degraded_rate": _round_metric(_mean([1.0 if result.degraded else 0.0 for result in results])),
        "nonempty_rate": _round_metric(_mean([1.0 if result.ranked_paths else 0.0 for result in results])),
        "retry_rate": _round_metric(_mean([1.0 if result.retries else 0.0 for result in results])),
        "total_retries": sum(result.retries for result in results),
        "error_counts": {
            str(error_code): sum(1 for result in results if result.error_code == error_code)
            for error_code in sorted({result.error_code for result in results if result.error_code})
        },
        "by_category": by_category,
    }


def _paired_deltas(
    results: list[QueryResult],
    *,
    bootstrap_samples: int,
    seed: int,
) -> dict[str, Any]:
    by_backend: dict[str, dict[str, QueryResult]] = {}
    for result in results:
        by_backend.setdefault(result.backend, {})[result.task_id] = result

    comparisons = [("hybrid", "local"), ("hybrid", "remote"), ("local", "remote")]
    payload: dict[str, Any] = {}
    for left_name, right_name in comparisons:
        left = by_backend.get(left_name, {})
        right = by_backend.get(right_name, {})
        task_ids = sorted(set(left) & set(right))
        left_ndcg = [left[task_id].ndcg10 for task_id in task_ids]
        right_ndcg = [right[task_id].ndcg10 for task_id in task_ids]
        left_recall = [left[task_id].recall10 for task_id in task_ids]
        right_recall = [right[task_id].recall10 for task_id in task_ids]
        ndcg_delta, ndcg_low, ndcg_high = bootstrap_paired_delta_ci(
            left_ndcg,
            right_ndcg,
            samples=bootstrap_samples,
            seed=seed,
        )
        recall_delta, recall_low, recall_high = bootstrap_paired_delta_ci(
            left_recall,
            right_recall,
            samples=bootstrap_samples,
            seed=seed + 7,
        )
        payload[f"{left_name}_minus_{right_name}"] = {
            "queries": len(task_ids),
            "ndcg10_delta": _round_metric(ndcg_delta),
            "ndcg10_ci95": [_round_metric(ndcg_low), _round_metric(ndcg_high)],
            "recall10_delta": _round_metric(recall_delta),
            "recall10_ci95": [_round_metric(recall_low), _round_metric(recall_high)],
        }
    return payload


def _results_path(repo_names: list[str]) -> Path:
    date_str = time.strftime("%Y-%m-%d")
    slug = "-".join(repo_names)
    return REPO_ROOT / "benchmarks" / "results" / f"retrieval-{slug}-{date_str}.json"


def _plot_path() -> Path:
    return REPO_ROOT / "assets" / "images" / "retrieval_benchmark_speed_vs_quality.svg"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _write_svg_plot(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backends = ["local", "remote", "hybrid"]
    colors = {
        "local": "#1f6feb",
        "remote": "#d29922",
        "hybrid": "#1a7f37",
    }
    labels = {
        "local": "local",
        "remote": "remote",
        "hybrid": "hybrid",
    }
    latencies = [max(summary["backends"][backend]["latency_ms"]["p50"], 1.0) for backend in backends]
    ndcgs = [summary["backends"][backend]["ndcg10"] for backend in backends]
    min_latency = min(latencies)
    max_latency = max(latencies)
    width = 760
    height = 420
    margin_left = 84
    margin_right = 60
    margin_top = 48
    margin_bottom = 72
    inner_width = width - margin_left - margin_right
    inner_height = height - margin_top - margin_bottom

    def x_pos(latency: float) -> float:
        log_min = math.log10(min_latency)
        log_max = math.log10(max_latency)
        if log_max == log_min:
            return margin_left + (inner_width / 2)
        ratio = (math.log10(latency) - log_min) / (log_max - log_min)
        return margin_left + ratio * inner_width

    def y_pos(ndcg: float) -> float:
        y_min = 0.45
        y_max = 1.0
        ratio = (ndcg - y_min) / (y_max - y_min)
        return margin_top + (1 - ratio) * inner_height

    tick_latencies = sorted({round(value, 1) for value in latencies} | {200.0, 1000.0, 5000.0})
    tick_latencies = [value for value in tick_latencies if min_latency <= value <= max_latency]
    if len(tick_latencies) < 2:
        tick_latencies = [min_latency, max_latency]
    y_ticks = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<style>',
        "text { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }",
        ".axis { stroke: #555; stroke-width: 1; }",
        ".grid { stroke: #e5e7eb; stroke-width: 1; }",
        ".label { fill: #374151; font-size: 12px; }",
        ".title { fill: #111827; font-size: 18px; font-weight: 700; }",
        ".subtitle { fill: #6b7280; font-size: 12px; }",
        "</style>",
        f'<text class="title" x="{margin_left}" y="28">Fast Context Retrieval Benchmark</text>',
        f'<text class="subtitle" x="{margin_left}" y="44">Semble benchmark subset: fastapi + axios, all 40 queries, warm local cache, batch p50 latency on log scale</text>',
    ]

    for tick in y_ticks:
        y = y_pos(tick)
        lines.append(f'<line class="grid" x1="{margin_left}" y1="{y:.1f}" x2="{width - margin_right}" y2="{y:.1f}"/>')
        lines.append(f'<text class="label" x="{margin_left - 12}" y="{y + 4:.1f}" text-anchor="end">{tick:.1f}</text>')

    for tick in tick_latencies:
        x = x_pos(tick)
        lines.append(f'<line class="grid" x1="{x:.1f}" y1="{margin_top}" x2="{x:.1f}" y2="{height - margin_bottom}"/>')
        tick_label = f"{int(tick)} ms" if tick < 1000 else f"{tick / 1000:.1f} s"
        lines.append(f'<text class="label" x="{x:.1f}" y="{height - margin_bottom + 24}" text-anchor="middle">{tick_label}</text>')

    lines.append(
        f'<line class="axis" x1="{margin_left}" y1="{height - margin_bottom}" x2="{width - margin_right}" y2="{height - margin_bottom}"/>'
    )
    lines.append(f'<line class="axis" x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{height - margin_bottom}"/>')
    lines.append(
        f'<text class="label" x="{margin_left + inner_width / 2:.1f}" y="{height - 20}" text-anchor="middle">p50 latency</text>'
    )
    lines.append(
        f'<text class="label" x="20" y="{margin_top + inner_height / 2:.1f}" transform="rotate(-90 20 {margin_top + inner_height / 2:.1f})" text-anchor="middle">NDCG@10</text>'
    )

    for backend in backends:
        latency = summary["backends"][backend]["latency_ms"]["p50"]
        ndcg = summary["backends"][backend]["ndcg10"]
        recall = summary["backends"][backend]["recall10"]
        x = x_pos(latency)
        y = y_pos(ndcg)
        label_right = x < (width - margin_right - 120)
        label_x = x + 12 if label_right else x - 12
        text_anchor = "start" if label_right else "end"
        lines.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8" fill="{colors[backend]}" opacity="0.9"/>')
        lines.append(
            f'<text x="{label_x:.1f}" y="{y - 6:.1f}" class="label" fill="{colors[backend]}" text-anchor="{text_anchor}">{labels[backend]}</text>'
        )
        lines.append(
            f'<text x="{label_x:.1f}" y="{y + 12:.1f}" class="subtitle" fill="{colors[backend]}" text-anchor="{text_anchor}">NDCG {ndcg:.3f}, Recall {recall:.3f}</text>'
        )

    lines.append("</svg>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _print_summary(summary: dict[str, Any]) -> None:
    print(
        f"{'backend':<8} {'ndcg@10':>8} {'recall@10':>10} {'top1':>8} {'mrr':>8} {'p50':>8} {'remote_ok':>10} {'degraded':>10} {'retries':>8}",
        file=sys.stderr,
    )
    print(f"{'-' * 8} {'-' * 8} {'-' * 10} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 10} {'-' * 10} {'-' * 8}", file=sys.stderr)
    for backend in ("local", "remote", "hybrid"):
        item = summary["backends"][backend]
        print(
            f"{backend:<8} {item['ndcg10']:>8.3f} {item['recall10']:>10.3f} {item['top1']:>8.3f} {item['mrr']:>8.3f} "
            f"{item['latency_ms']['p50']:>7.1f}ms {item['remote_success_rate']:>10.3f} {item['degraded_rate']:>10.3f} {item['total_retries']:>8}",
            file=sys.stderr,
        )
    print(file=sys.stderr)
    print("paired deltas (95% bootstrap CI)", file=sys.stderr)
    for name, item in summary["paired_deltas"].items():
        print(
            f"  {name}: ndcg@10 {item['ndcg10_delta']:+.3f} [{item['ndcg10_ci95'][0]:.3f}, {item['ndcg10_ci95'][1]:.3f}]"
            f", recall@10 {item['recall10_delta']:+.3f} [{item['recall10_ci95'][0]:.3f}, {item['recall10_ci95'][1]:.3f}]",
            file=sys.stderr,
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run retrieval benchmark for fast-context.")
    parser.add_argument(
        "--dataset-root",
        default=str(default_dataset_root()),
        help="Path to a Semble benchmark checkout root.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Benchmark repo to include. Defaults to fastapi + axios.",
    )
    parser.add_argument("--max-results", type=int, default=10, help="Top-k results to score.")
    parser.add_argument("--max-turns", type=int, default=2, help="Remote search rounds.")
    parser.add_argument("--tree-depth", type=int, default=3, help="Repo tree depth for remote search.")
    parser.add_argument("--timeout-ms", type=int, default=30000, help="Remote search timeout in ms.")
    parser.add_argument("--remote-sleep-ms", type=int, default=2000, help="Minimum gap between remote request starts.")
    parser.add_argument("--remote-jitter-ms", type=int, default=750, help="Extra random remote pacing jitter.")
    parser.add_argument("--retry-base-ms", type=int, default=5000, help="Base delay for retryable remote errors.")
    parser.add_argument("--max-retries", type=int, default=2, help="Retries for retryable remote errors.")
    parser.add_argument("--bootstrap-samples", type=int, default=5000, help="Bootstrap resamples for CI.")
    parser.add_argument("--seed", type=int, default=20260601, help="Seed for task order and retries.")
    parser.add_argument("--clear-local-cache", action="store_true", help="Clear local Semble cache before warm-up.")
    parser.add_argument("--output", default="", help="Path to write benchmark JSON.")
    parser.add_argument("--plot", default="", help="Path to write SVG plot.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    dataset_root = Path(args.dataset_root).expanduser().resolve()
    repo_names = args.repo or list(DEFAULT_REPOS)
    specs = load_repo_specs(dataset_root, repo_names)
    tasks = load_tasks(dataset_root, specs)
    checkout_details = ensure_pinned_checkouts(specs)

    execution_tasks = list(tasks)
    random.Random(args.seed).shuffle(execution_tasks)

    warmup: dict[str, dict[str, Any]] = {}
    for repo in repo_names:
        spec = specs[repo]
        project_root = str(spec.benchmark_dir.resolve())
        if args.clear_local_cache:
            local_semble.clear_project_cache(project_root)
        started = time.perf_counter()
        _, cache_status = local_semble._build_index(project_root, ["code"])  # type: ignore[attr-defined]
        warmup[repo] = {
            "project_root": project_root,
            "cache_status": cache_status,
            "warmup_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    pacer = RemotePacer(args.remote_sleep_ms, args.remote_jitter_ms, args.seed)
    results: list[QueryResult] = []

    for index, task in enumerate(execution_tasks, 1):
        spec = specs[task.repo]
        project_root = str(spec.benchmark_dir.resolve())
        print(
            f"[{index:02d}/{len(execution_tasks)}] {task.repo:<7} {task.category:<12} local   {task.query}",
            file=sys.stderr,
        )
        results.append(_run_local(task, project_root, args.max_results))
        remote_order = ["remote", "hybrid"] if index % 2 else ["hybrid", "remote"]
        for backend in remote_order:
            print(
                f"[{index:02d}/{len(execution_tasks)}] {task.repo:<7} {task.category:<12} {backend:<7} {task.query}",
                file=sys.stderr,
            )
            if backend == "remote":
                result = _run_remote(
                    task,
                    project_root,
                    max_results=args.max_results,
                    max_turns=args.max_turns,
                    tree_depth=args.tree_depth,
                    timeout_ms=args.timeout_ms,
                    pacer=pacer,
                    retry_base_ms=args.retry_base_ms,
                    max_retries=args.max_retries,
                    retry_seed=args.seed + index,
                )
            else:
                result = _run_hybrid(
                    task,
                    project_root,
                    max_results=args.max_results,
                    max_turns=args.max_turns,
                    tree_depth=args.tree_depth,
                    timeout_ms=args.timeout_ms,
                    pacer=pacer,
                    retry_base_ms=args.retry_base_ms,
                    max_retries=args.max_retries,
                    retry_seed=args.seed + index + 101,
                )
            results.append(result)

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dataset_root": str(dataset_root),
        "repos": repo_names,
        "task_count": len(tasks),
        "config": {
            "max_results": args.max_results,
            "max_turns": args.max_turns,
            "tree_depth": args.tree_depth,
            "timeout_ms": args.timeout_ms,
            "remote_sleep_ms": args.remote_sleep_ms,
            "remote_jitter_ms": args.remote_jitter_ms,
            "retry_base_ms": args.retry_base_ms,
            "max_retries": args.max_retries,
            "bootstrap_samples": args.bootstrap_samples,
            "seed": args.seed,
            "clear_local_cache": args.clear_local_cache,
        },
        "checkouts": checkout_details,
        "warmup": warmup,
        "backends": {
            backend: _summarize_backend(
                [result for result in results if result.backend == backend],
                bootstrap_samples=args.bootstrap_samples,
                seed=args.seed + offset,
            )
            for offset, backend in enumerate(("local", "remote", "hybrid"))
        },
        "paired_deltas": _paired_deltas(
            results,
            bootstrap_samples=args.bootstrap_samples,
            seed=args.seed + 50,
        ),
        "per_query": [asdict(result) for result in results],
    }

    output_path = Path(args.output).expanduser().resolve() if args.output else _results_path(repo_names)
    plot_path = Path(args.plot).expanduser().resolve() if args.plot else _plot_path()
    _write_json(output_path, summary)
    _write_svg_plot(plot_path, summary)
    _print_summary(summary)
    print(f"wrote {output_path}", file=sys.stderr)
    print(f"wrote {plot_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
