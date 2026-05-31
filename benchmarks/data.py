from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

BENCH_CACHE_ROOT = Path.home() / ".cache" / "semble-bench"
DEFAULT_REPOS = ("fastapi", "axios")


def default_dataset_root() -> Path:
    env_root = os.environ.get("SEMBLE_BENCHMARK_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[1]
    return (repo_root.parent / "semble" / "benchmarks").resolve()


@dataclass(frozen=True)
class Target:
    path: str
    start_line: int | None = None
    end_line: int | None = None


@dataclass(frozen=True)
class RepoSpec:
    name: str
    language: str
    url: str
    revision: str
    benchmark_root: str | None = None

    @property
    def checkout_dir(self) -> Path:
        return BENCH_CACHE_ROOT / self.name

    @property
    def benchmark_dir(self) -> Path:
        if not self.benchmark_root:
            return self.checkout_dir
        return self.checkout_dir / self.benchmark_root


@dataclass(frozen=True)
class Task:
    repo: str
    language: str
    category: str
    query: str
    relevant: tuple[Target, ...]
    secondary: tuple[Target, ...]

    @property
    def all_relevant(self) -> tuple[Target, ...]:
        return self.relevant + self.secondary

    @property
    def task_id(self) -> str:
        return f"{self.repo}::{self.category}::{self.query}"


def _coerce_int(value: object) -> int:
    if not isinstance(value, int | str):
        raise TypeError(f"expected int-compatible value, got {type(value).__name__}")
    return int(value)


def _parse_target(raw: str | dict[str, object]) -> Target:
    if isinstance(raw, str):
        return Target(path=raw)
    if not isinstance(raw, dict):
        raise TypeError(f"expected mapping, got {type(raw).__name__}")
    start_line = raw.get("start_line")
    end_line = raw.get("end_line")
    return Target(
        path=str(raw["path"]),
        start_line=_coerce_int(start_line) if start_line is not None else None,
        end_line=_coerce_int(end_line) if end_line is not None else None,
    )


def load_repo_specs(dataset_root: Path, repo_names: list[str]) -> dict[str, RepoSpec]:
    repos_path = dataset_root / "repos.json"
    raw = json.loads(repos_path.read_text(encoding="utf-8"))
    wanted = set(repo_names)
    specs = {item["name"]: RepoSpec(**item) for item in raw if item["name"] in wanted}
    missing = wanted - set(specs)
    if missing:
        raise FileNotFoundError(f"Missing repo specs for: {', '.join(sorted(missing))}")
    return specs


def load_tasks(dataset_root: Path, specs: dict[str, RepoSpec]) -> list[Task]:
    annotations_dir = dataset_root / "annotations"
    tasks: list[Task] = []
    for repo in sorted(specs):
        annotation_path = annotations_dir / f"{repo}.json"
        raw = json.loads(annotation_path.read_text(encoding="utf-8"))
        spec = specs[repo]
        for item in raw:
            tasks.append(
                Task(
                    repo=repo,
                    language=spec.language,
                    category=str(item["category"]),
                    query=str(item["query"]),
                    relevant=tuple(_parse_target(target) for target in item.get("relevant", [])),
                    secondary=tuple(_parse_target(target) for target in item.get("secondary", [])),
                )
            )
    return tasks


def current_revision(path: Path) -> str:
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def ensure_pinned_checkouts(specs: dict[str, RepoSpec]) -> list[dict[str, str | bool]]:
    details: list[dict[str, str | bool]] = []
    for repo in sorted(specs):
        spec = specs[repo]
        if not spec.checkout_dir.exists():
            raise FileNotFoundError(f"Benchmark checkout missing: {spec.checkout_dir}")
        if not spec.benchmark_dir.exists():
            raise FileNotFoundError(f"Benchmark root missing: {spec.benchmark_dir}")
        actual_revision = current_revision(spec.checkout_dir)
        matches = actual_revision == spec.revision
        if not matches:
            raise RuntimeError(
                f"{repo} checkout is not pinned: expected {spec.revision}, got {actual_revision}"
            )
        details.append(
            {
                "repo": repo,
                "language": spec.language,
                "url": spec.url,
                "expected_revision": spec.revision,
                "actual_revision": actual_revision,
                "matches": matches,
                "checkout_dir": str(spec.checkout_dir),
                "benchmark_dir": str(spec.benchmark_dir),
            }
        )
    return details
