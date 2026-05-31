from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


DEFAULT_SEMBLE_PYTHON = "3.13"
DEFAULT_SEMBLE_TIMEOUT = 120


class SembleUnavailable(RuntimeError):
    """Raised when neither the semble library nor uvx fallback can run."""


def _normalize_content(content: list[str] | None) -> list[str]:
    values = content or ["code"]
    if "all" in values:
        return ["all"]
    allowed = {"code", "docs", "config"}
    normalized = [item for item in values if item in allowed]
    return normalized or ["code"]


def _search_with_library(
    query: str,
    project_root: str,
    top_k: int,
    content: list[str],
) -> dict[str, Any]:
    from semble.index import SembleIndex
    from semble.types import ContentType
    from semble.utils import format_results

    content_types = (
        [ContentType.CODE, ContentType.DOCS, ContentType.CONFIG]
        if "all" in content
        else [ContentType(item) for item in content]
    )
    index = SembleIndex.from_path(project_root, content=content_types)
    results = index.search(query, top_k=top_k)
    payload = format_results(query, results) if results else {"query": query, "results": []}
    payload["_meta"] = {"backend": "semble", "runner": "library"}
    return payload


def _find_related_with_library(
    file_path: str,
    line: int,
    project_root: str,
    top_k: int,
    content: list[str],
) -> dict[str, Any]:
    from semble.index import SembleIndex
    from semble.types import ContentType
    from semble.utils import format_results, resolve_chunk

    content_types = (
        [ContentType.CODE, ContentType.DOCS, ContentType.CONFIG]
        if "all" in content
        else [ContentType(item) for item in content]
    )
    index = SembleIndex.from_path(project_root, content=content_types)
    chunk = resolve_chunk(index.chunks, file_path, line)
    if chunk is None:
        return {"query": f"Chunks related to {file_path}:{line}", "results": []}
    results = index.find_related(chunk, top_k=top_k)
    payload = format_results(f"Chunks related to {file_path}:{line}", results) if results else {
        "query": f"Chunks related to {file_path}:{line}",
        "results": [],
    }
    payload["_meta"] = {"backend": "semble", "runner": "library"}
    return payload


def _run_semble_cli(args: list[str]) -> dict[str, Any]:
    uvx = os.environ.get("FAST_CONTEXT_SEMBLE_UVX", "uvx")
    python_version = os.environ.get("FAST_CONTEXT_SEMBLE_PYTHON", DEFAULT_SEMBLE_PYTHON)
    timeout = int(os.environ.get("FAST_CONTEXT_SEMBLE_TIMEOUT", str(DEFAULT_SEMBLE_TIMEOUT)))
    cmd = [uvx, "--python", python_version, "--from", "semble", "semble", *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SembleUnavailable("uvx not found and semble is not importable") from exc
    except subprocess.TimeoutExpired as exc:
        raise SembleUnavailable(f"semble timed out after {timeout}s") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise SembleUnavailable(detail or f"semble exited with {proc.returncode}")

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SembleUnavailable(f"semble returned non-JSON output: {proc.stdout[:200]}") from exc
    payload["_meta"] = {"backend": "semble", "runner": "uvx"}
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
    except Exception:
        args = ["search", query, project_root, "-k", str(top_k), "--content", *content_values]
        return _run_semble_cli(args)


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
    except Exception:
        args = [
            "find-related",
            file_path,
            str(line),
            project_root,
            "-k",
            str(top_k),
            "--content",
            *content_values,
        ]
        return _run_semble_cli(args)
