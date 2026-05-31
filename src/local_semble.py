from __future__ import annotations

import json
import shutil
import time
from functools import cache
from pathlib import Path
from typing import Any


class SembleUnavailable(RuntimeError):
    """Raised when local Semble search cannot run."""


def _normalize_content(content: list[str] | None) -> list[str]:
    values = content or ["code"]
    if "all" in values:
        return ["all"]
    allowed = {"code", "docs", "config"}
    normalized = [item for item in values if item in allowed]
    return normalized or ["code"]


@cache
def _load_semble_api() -> tuple[Any, Any, Any, Any, Any]:
    try:
        from semble.cache import save_index_to_cache
        from semble.index import SembleIndex
        from semble.types import ContentType
        from semble.utils import format_results, resolve_chunk
    except ImportError as exc:
        raise SembleUnavailable("semble dependency is not installed; run `uv sync` first") from exc

    return SembleIndex, ContentType, format_results, resolve_chunk, save_index_to_cache


@cache
def _load_semble_cache_api() -> tuple[Any, Any]:
    try:
        from semble.cache import find_index_from_cache_folder, resolve_cache_folder
    except ImportError as exc:
        raise SembleUnavailable("semble dependency is not installed; run `uv sync` first") from exc

    return find_index_from_cache_folder, resolve_cache_folder


def _content_types(content: list[str]) -> list[Any]:
    _, ContentType, _, _, _ = _load_semble_api()
    return (
        [ContentType.CODE, ContentType.DOCS, ContentType.CONFIG]
        if "all" in content
        else [ContentType(item) for item in content]
    )


def _build_index(
    project_root: str,
    content: list[str],
) -> tuple[Any, str]:
    SembleIndex, _, _, _, save_index_to_cache = _load_semble_api()

    try:
        index = SembleIndex.from_path(project_root, content=_content_types(content))
    except Exception as exc:
        raise SembleUnavailable(f"semble indexing failed: {exc}") from exc

    if getattr(index, "loaded_from_disk", False):
        return index, "hit"

    try:
        save_index_to_cache(index, project_root)
    except Exception:
        return index, "save_failed"

    return index, "saved"


def _path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0

    total = 0
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return total


def _entry_time(entry: Path, metadata: dict[str, Any] | None) -> float:
    if metadata and isinstance(metadata.get("time"), (int, float)):
        return float(metadata["time"])
    try:
        return entry.stat().st_mtime
    except OSError:
        return 0.0


def _stale_cache_reason(entry: Path, min_timestamp: float) -> tuple[str, str | None] | None:
    metadata_path = entry / "index" / "metadata.json"
    metadata: dict[str, Any] | None = None
    root_path: str | None = None

    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = None
            reason = "invalid-metadata"
        else:
            root_value = metadata.get("root_path")
            root_path = str(root_value) if root_value else None
            reason = "missing-root-path" if root_path is None else ""
    else:
        reason = "missing-metadata"

    if _entry_time(entry, metadata) > min_timestamp:
        return None

    if reason in {"missing-metadata", "invalid-metadata"}:
        return reason, root_path
    if reason == "missing-root-path":
        return None
    if root_path and not Path(root_path).exists():
        return "missing-root-path", root_path
    return None


def clear_project_cache(project_root: str, dry_run: bool = False) -> dict[str, Any]:
    find_index_from_cache_folder, _ = _load_semble_cache_api()

    resolved_root = str(Path(project_root).expanduser().resolve())
    cache_entry = find_index_from_cache_folder(resolved_root).parent
    existed = cache_entry.exists()
    size_bytes = _path_size(cache_entry)
    if existed and not dry_run:
        shutil.rmtree(cache_entry)

    return {
        "project_root": resolved_root,
        "cache_path": str(cache_entry),
        "existed": existed,
        "removed": existed and not dry_run,
        "bytes": size_bytes,
        "dry_run": dry_run,
    }


def gc_stale_caches(dry_run: bool = False, min_age_days: float = 0.0) -> dict[str, Any]:
    if min_age_days < 0:
        raise ValueError("min_age_days must be >= 0")

    _, resolve_cache_folder = _load_semble_cache_api()
    cache_root = resolve_cache_folder()
    min_timestamp = time.time() - (min_age_days * 86400)
    entries = []
    removed_bytes = 0

    for entry in sorted(cache_root.iterdir()):
        if not entry.is_dir():
            continue
        stale = _stale_cache_reason(entry, min_timestamp)
        if stale is None:
            continue

        reason, root_path = stale
        size_bytes = _path_size(entry)
        if not dry_run:
            shutil.rmtree(entry, ignore_errors=True)
        removed_bytes += size_bytes
        entries.append(
            {
                "cache_path": str(entry),
                "root_path": root_path,
                "reason": reason,
                "bytes": size_bytes,
                "removed": not dry_run,
            }
        )

    return {
        "cache_root": str(cache_root),
        "dry_run": dry_run,
        "min_age_days": min_age_days,
        "removed_count": len(entries),
        "removed_bytes": removed_bytes,
        "entries": entries,
    }


def _search_with_library(
    query: str,
    project_root: str,
    top_k: int,
    content: list[str],
) -> dict[str, Any]:
    _, _, format_results, _, _ = _load_semble_api()

    index, cache_status = _build_index(project_root, content)
    results = index.search(query, top_k=top_k)
    payload = format_results(query, results) if results else {"query": query, "results": []}
    payload["_meta"] = {"backend": "semble", "runner": "library", "cache": cache_status}
    return payload


def _find_related_with_library(
    file_path: str,
    line: int,
    project_root: str,
    top_k: int,
    content: list[str],
) -> dict[str, Any]:
    _, _, format_results, resolve_chunk, _ = _load_semble_api()

    index, cache_status = _build_index(project_root, content)
    chunk = resolve_chunk(index.chunks, file_path, line)
    if chunk is None:
        return {
            "query": f"Chunks related to {file_path}:{line}",
            "results": [],
            "_meta": {"backend": "semble", "runner": "library", "cache": cache_status},
        }
    results = index.find_related(chunk, top_k=top_k)
    payload = format_results(f"Chunks related to {file_path}:{line}", results) if results else {
        "query": f"Chunks related to {file_path}:{line}",
        "results": [],
    }
    payload["_meta"] = {"backend": "semble", "runner": "library", "cache": cache_status}
    return payload


def search(
    query: str,
    project_root: str,
    top_k: int = 10,
    content: list[str] | None = None,
) -> dict[str, Any]:
    project_root = str(Path(project_root).expanduser().resolve())
    content_values = _normalize_content(content)
    try:
        return _search_with_library(query, project_root, top_k, content_values)
    except SembleUnavailable:
        raise
    except Exception as exc:
        raise SembleUnavailable(f"semble search failed: {exc}") from exc


def find_related(
    file_path: str,
    line: int,
    project_root: str,
    top_k: int = 10,
    content: list[str] | None = None,
) -> dict[str, Any]:
    project_root = str(Path(project_root).expanduser().resolve())
    content_values = _normalize_content(content)
    try:
        return _find_related_with_library(file_path, line, project_root, top_k, content_values)
    except SembleUnavailable:
        raise
    except Exception as exc:
        raise SembleUnavailable(f"semble find-related failed: {exc}") from exc
