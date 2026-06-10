from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from core import DEFAULT_EXCLUDE_PATTERNS  # noqa: E402
from local_repo_map import build_classic_repo_map, build_optimized_repo_map  # noqa: E402


@dataclass(frozen=True)
class RepoMapTask:
    category: str
    query: str
    relevant_paths: tuple[str, ...]


@dataclass
class RepoMapResult:
    task_id: str
    category: str
    query: str
    variant: str
    latency_ms: float
    size_bytes: int
    depth: int
    fell_back: bool
    strategy: str
    hot_dirs: list[str]
    path_spines: int
    candidate_count: int
    file_recall: float
    deep_cover_recall: float
    file_mrr: float
    deep_cover_mrr: float
    first_file_rank: int | None
    first_cover_rank: int | None


def load_tasks(path: Path) -> tuple[RepoMapTask, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("tasks", raw) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError("task file must be a JSON array or an object with a tasks array")

    tasks: list[RepoMapTask] = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"task #{index} must be an object")
        category = str(row.get("category") or f"task-{index}")
        query = str(row.get("query") or "").strip()
        paths = row.get("relevant_paths")
        if not query:
            raise ValueError(f"task #{index} is missing query")
        if not isinstance(paths, list) or not paths:
            raise ValueError(f"task #{index} is missing relevant_paths")
        tasks.append(RepoMapTask(category, query, tuple(str(path) for path in paths)))
    return tuple(tasks)


def _repo_label(repo_root: Path, label: str) -> str:
    return label or f"{repo_root.name}-redacted"


def _normalize(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _path_matches(candidate: str, target: str) -> bool:
    left = _normalize(candidate)
    right = _normalize(target)
    return left == right or left.endswith(f"/{right}") or right.endswith(f"/{left}")


def _is_deep_cover(candidate: str, target: str) -> bool:
    left = _normalize(candidate).rstrip("/")
    right = _normalize(target)
    if "." in Path(left).name:
        return _path_matches(left, right)
    if len([part for part in left.split("/") if part]) < 2:
        return False
    return right == left or right.startswith(f"{left}/")


def _line_level(prefix: str) -> int:
    return prefix.count("│   ") + prefix.count("    ")


def extract_candidate_paths(repo_map: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    root_parts: list[str] = []
    stack: list[str] = []

    def add(path: str) -> None:
        normalized = _normalize(path)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidates.append(normalized)

    for raw_line in repo_map.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- /codebase/"):
            add(stripped.removeprefix("- /codebase/"))
            continue
        if stripped.startswith("/codebase"):
            rel = stripped.removeprefix("/codebase").strip("/")
            root_parts = [part for part in rel.split("/") if part]
            stack = list(root_parts)
            if root_parts:
                add("/".join(root_parts))
            continue
        marker = "├── "
        if marker not in line:
            continue
        prefix, name = line.split(marker, 1)
        name = name.strip()
        level = _line_level(prefix)
        parent_len = len(root_parts) + level
        stack = stack[:parent_len]
        path = "/".join([*stack, name])
        add(path)
        stack = [*stack, name]
    return candidates


def _first_rank(candidates: list[str], targets: tuple[str, ...], *, deep_cover: bool) -> int | None:
    matcher = _is_deep_cover if deep_cover else _path_matches
    for index, candidate in enumerate(candidates, 1):
        if any(matcher(candidate, target) for target in targets):
            return index
    return None


def _recall(candidates: list[str], targets: tuple[str, ...], *, deep_cover: bool) -> float:
    matcher = _is_deep_cover if deep_cover else _path_matches
    matched = {
        target
        for target in targets
        if any(matcher(candidate, target) for candidate in candidates)
    }
    return len(matched) / len(targets) if targets else 0.0


def run_variant(
    task: RepoMapTask,
    repo_root: Path,
    *,
    variant: str,
    tree_depth: int,
    max_bytes: int,
    exclude_paths: list[str],
) -> RepoMapResult:
    started = time.perf_counter()
    if variant == "classic":
        payload = build_classic_repo_map(str(repo_root), tree_depth, exclude_paths, max_bytes)
    elif variant == "hotspot":
        payload = build_optimized_repo_map(str(repo_root), task.query, tree_depth, exclude_paths, max_bytes)
    else:
        raise ValueError(f"unknown variant: {variant}")
    latency_ms = (time.perf_counter() - started) * 1000

    candidates = extract_candidate_paths(str(payload["tree"]))
    first_file_rank = _first_rank(candidates, task.relevant_paths, deep_cover=False)
    first_cover_rank = _first_rank(candidates, task.relevant_paths, deep_cover=True)
    return RepoMapResult(
        task_id=f"{task.category}::{task.query}",
        category=task.category,
        query=task.query,
        variant=variant,
        latency_ms=latency_ms,
        size_bytes=int(payload["size_bytes"]),
        depth=int(payload["depth"]),
        fell_back=bool(payload["fell_back"]),
        strategy=str(payload["strategy"]),
        hot_dirs=list(payload.get("hot_dirs") or []),
        path_spines=len(payload.get("path_spines") or []),
        candidate_count=len(candidates),
        file_recall=_recall(candidates, task.relevant_paths, deep_cover=False),
        deep_cover_recall=_recall(candidates, task.relevant_paths, deep_cover=True),
        file_mrr=(1 / first_file_rank) if first_file_rank else 0.0,
        deep_cover_mrr=(1 / first_cover_rank) if first_cover_rank else 0.0,
        first_file_rank=first_file_rank,
        first_cover_rank=first_cover_rank,
    )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize(results: list[RepoMapResult]) -> dict[str, Any]:
    by_variant: dict[str, list[RepoMapResult]] = {}
    for result in results:
        by_variant.setdefault(result.variant, []).append(result)

    summary: dict[str, Any] = {}
    for variant, rows in sorted(by_variant.items()):
        summary[variant] = {
            "queries": len(rows),
            "file_recall": round(_mean([row.file_recall for row in rows]), 4),
            "deep_cover_recall": round(_mean([row.deep_cover_recall for row in rows]), 4),
            "file_mrr": round(_mean([row.file_mrr for row in rows]), 4),
            "deep_cover_mrr": round(_mean([row.deep_cover_mrr for row in rows]), 4),
            "avg_size_bytes": round(_mean([row.size_bytes for row in rows]), 1),
            "p50_latency_ms": round(statistics.median([row.latency_ms for row in rows]), 2),
            "avg_candidates": round(_mean([row.candidate_count for row in rows]), 1),
        }
    if {"classic", "hotspot"} <= set(by_variant):
        classic = {row.task_id: row for row in by_variant["classic"]}
        hotspot = {row.task_id: row for row in by_variant["hotspot"]}
        shared = sorted(set(classic) & set(hotspot))
        summary["delta_hotspot_minus_classic"] = {
            "queries": len(shared),
            "file_recall": round(_mean([hotspot[key].file_recall - classic[key].file_recall for key in shared]), 4),
            "deep_cover_recall": round(_mean([hotspot[key].deep_cover_recall - classic[key].deep_cover_recall for key in shared]), 4),
            "file_mrr": round(_mean([hotspot[key].file_mrr - classic[key].file_mrr for key in shared]), 4),
            "deep_cover_mrr": round(_mean([hotspot[key].deep_cover_mrr - classic[key].deep_cover_mrr for key in shared]), 4),
            "size_bytes": round(_mean([hotspot[key].size_bytes - classic[key].size_bytes for key in shared]), 1),
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run classic vs hotspot repo-map A/B benchmark.")
    parser.add_argument("--repo", required=True, help="Repository root to benchmark.")
    parser.add_argument(
        "--tasks",
        required=True,
        help="JSON file with tasks: [{category, query, relevant_paths}].",
    )
    parser.add_argument(
        "--repo-label",
        default="",
        help="Optional redacted repo label to store in output instead of an absolute path.",
    )
    parser.add_argument("--tree-depth", type=int, default=3)
    parser.add_argument("--max-bytes", type=int, default=12 * 1024)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    repo_root = Path(args.repo).expanduser().resolve()
    tasks = load_tasks(Path(args.tasks).expanduser().resolve())
    missing = [
        path
        for task in tasks
        for path in task.relevant_paths
        if not (repo_root / path).exists()
    ]
    if missing:
        raise FileNotFoundError(f"missing benchmark targets under {repo_root}: {missing}")

    exclude_paths = list(DEFAULT_EXCLUDE_PATTERNS)
    results: list[RepoMapResult] = []
    for task in tasks:
        for variant in ("classic", "hotspot"):
            results.append(
                run_variant(
                    task,
                    repo_root,
                    variant=variant,
                    tree_depth=args.tree_depth,
                    max_bytes=args.max_bytes,
                    exclude_paths=exclude_paths,
                )
            )

    payload = {
        "repo": _repo_label(repo_root, args.repo_label),
        "tree_depth": args.tree_depth,
        "max_bytes": args.max_bytes,
        "tasks": len(tasks),
        "summary": summarize(results),
        "results": [asdict(result) for result in results],
    }

    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
