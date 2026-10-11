"""Post-process local chunk hits: demote non-source noise and sibling-app dupes.

The function is pure. Callers fetch more candidates than they display, then
truncate to ``max_results`` after scores change.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

NOISE_SCORE_FACTOR = 0.2
OTHER_APP_SCORE_FACTOR = 0.45
_TOP_VOTE_WINDOW = 8

_LOCKFILES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "uv.lock",
    "cargo.lock",
    "poetry.lock",
}
_GENERATED_DIRS = {"generated", "dist", "build"}
_APP_ROOT_RE = re.compile(
    r"(?:^|/)(apps|packages|services)/([^/]+)(?:/|$)",
    re.IGNORECASE,
)
_EXEMPTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "sql": re.compile(r"(?<![A-Za-z0-9_])sql(?![A-Za-z0-9_])", re.IGNORECASE),
    "migration": re.compile(r"(?<![A-Za-z0-9_])migrations?(?![A-Za-z0-9_])", re.IGNORECASE),
    "env": re.compile(r"(?<![A-Za-z0-9_])env(?![A-Za-z0-9_])", re.IGNORECASE),
    "deploy": re.compile(r"(?<![A-Za-z0-9_])deploy(?:ments?|s)?(?![A-Za-z0-9_])", re.IGNORECASE),
    "lock": re.compile(r"(?<![A-Za-z0-9_])locks?(?:files?)?(?![A-Za-z0-9_])", re.IGNORECASE),
}


def is_code_query(content_types: list[str] | None) -> bool:
    """True when the search is aimed at source code rather than docs or config."""
    if not content_types:
        return True
    values = {item.lower() for item in content_types if item}
    if not values or "all" in values or "code" not in values:
        return False
    return True


def result_path(item: dict[str, Any]) -> str:
    chunk = item.get("chunk")
    if isinstance(chunk, dict):
        path = chunk.get("file_path") or chunk.get("path") or ""
        if path:
            return str(path)
    for key in ("file_path", "path"):
        value = item.get(key)
        if value:
            return str(value)
    return ""


def noise_kinds(path: str) -> set[str]:
    """Return noise categories that apply to ``path``. Empty means keep the score."""
    norm = path.replace("\\", "/").lower()
    parts = [part for part in norm.split("/") if part]
    if not parts:
        return set()
    name = parts[-1]
    dirs = parts[:-1]
    kinds: set[str] = set()
    if name.endswith(".sql"):
        kinds.add("sql")
    if "migrations" in dirs or "migration" in dirs:
        kinds.add("migration")
    if name.startswith(".env"):
        kinds.add("env")
    if "deploy" in dirs:
        kinds.add("deploy")
    if name in _LOCKFILES:
        kinds.add("lock")
    if name.endswith(".example"):
        kinds.add("example")
    if name.endswith(".min.js"):
        kinds.add("minjs")
    if any(part in _GENERATED_DIRS for part in dirs):
        kinds.add("generated")
    if name.endswith(".snap"):
        kinds.add("snap")
    return kinds


def _exempt_kinds(query: str) -> set[str]:
    return {kind for kind, pattern in _EXEMPTION_PATTERNS.items() if pattern.search(query or "")}


def app_root_key(path: str) -> tuple[str, str] | None:
    """Return ``(kind, name)`` for ``apps/<name>``, ``packages/<name>``, or ``services/<name>``."""
    match = _APP_ROOT_RE.search(path.replace("\\", "/"))
    if not match:
        return None
    return match.group(1).lower(), match.group(2).lower()


def _base_score(item: dict[str, Any], index: int) -> float:
    score = item.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return 1.0 / (index + 1)
    return float(score)


def _majority_app(paths: list[str]) -> tuple[str, str] | None:
    if not paths:
        return None
    counts: Counter[tuple[str, str]] = Counter()
    for path in paths:
        key = app_root_key(path)
        if key:
            counts[key] += 1
    if not counts:
        return None
    best, votes = counts.most_common(1)[0]
    if votes * 2 > len(paths):
        return best
    return None


def _query_app(query: str, keys: set[tuple[str, str]], paths: list[str]) -> tuple[str, str] | None:
    by_name: dict[str, list[tuple[str, str]]] = {}
    for key in keys:
        by_name.setdefault(key[1], []).append(key)
    ranked: list[tuple[int, int, str]] = []
    for name in by_name:
        mentions = len(
            re.findall(
                rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                query or "",
                flags=re.IGNORECASE,
            )
        )
        if mentions:
            ranked.append((mentions, len(name), name))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    group = by_name[ranked[0][2]]
    if len(group) == 1:
        return group[0]
    support = Counter(key for key in (app_root_key(path) for path in paths) if key in group)
    return max(group, key=lambda key: (support[key], key[0]))


def preferred_app_root(
    query: str,
    results: list[dict[str, Any]],
    anchor_paths: list[str] | None = None,
) -> tuple[str, str] | None:
    """Pick one app root from the query, hybrid anchors, or the current top hits."""
    anchors = [path for path in (anchor_paths or []) if path]
    indexed = [
        (index, result_path(item), _base_score(item, index))
        for index, item in enumerate(results)
        if isinstance(item, dict) and result_path(item)
    ]
    indexed.sort(key=lambda item: (-item[2], item[0]))
    top_paths = [path for _, path, _ in indexed[:_TOP_VOTE_WINDOW]]
    keys = {
        key
        for key in (app_root_key(path) for path in [*top_paths, *anchors, *(path for _, path, _ in indexed)])
        if key
    }
    queried = _query_app(query, keys, [*top_paths, *anchors])
    if queried:
        return queried
    anchored = _majority_app(anchors[:_TOP_VOTE_WINDOW])
    if anchored:
        return anchored
    return _majority_app(top_paths)


def _basename(path: str) -> str:
    norm = path.replace("\\", "/")
    return norm.rsplit("/", 1)[-1].lower()


def rerank_local_results(
    results: list[dict[str, Any]],
    query: str,
    *,
    max_results: int | None = None,
    anchor_paths: list[str] | None = None,
    content_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return a new ranked list. Noise and off-app hits stay, with a lower score.

    ``results`` is treated as the retrieval order. ``anchor_paths`` are hybrid
    hints (remote files or lexical anchors) that can select the preferred app.
    """
    if not results:
        return []

    code_query = is_code_query(content_types)
    exempt = _exempt_kinds(query) if code_query else set()
    preferred = preferred_app_root(query, results, anchor_paths)
    adjusted: list[tuple[float, int, dict[str, Any], str]] = []

    for index, item in enumerate(results):
        if not isinstance(item, dict):
            continue
        path = result_path(item)
        score = _base_score(item, index)
        if code_query and path:
            kinds = noise_kinds(path)
            if kinds and kinds.isdisjoint(exempt):
                score *= NOISE_SCORE_FACTOR
        if preferred and path:
            app = app_root_key(path)
            if app and app != preferred:
                score *= OTHER_APP_SCORE_FACTOR
        adjusted.append((score, index, item, path))

    if preferred:
        groups: dict[str, list[int]] = {}
        for slot, (_, _, _, path) in enumerate(adjusted):
            if path:
                groups.setdefault(_basename(path), []).append(slot)
        for slots in groups.values():
            pref_slots = [slot for slot in slots if app_root_key(adjusted[slot][3]) == preferred]
            other_slots = [
                slot
                for slot in slots
                if app_root_key(adjusted[slot][3]) not in (None, preferred)
            ]
            if not pref_slots or not other_slots:
                continue
            pref_score = max(adjusted[slot][0] for slot in pref_slots)
            ceiling = pref_score * 0.5 if pref_score > 0 else pref_score - 1.0
            for slot in other_slots:
                score, index, item, path = adjusted[slot]
                if score >= pref_score:
                    adjusted[slot] = (min(score, ceiling), index, item, path)

    adjusted.sort(key=lambda row: (-row[0], row[1]))
    ranked: list[dict[str, Any]] = []
    for score, _, item, _ in adjusted:
        copied = dict(item)
        copied["score"] = score
        ranked.append(copied)
    if max_results is not None and max_results >= 0:
        return ranked[:max_results]
    return ranked
