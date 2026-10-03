"""Repair and salvage for malformed remote model responses.

The backend model occasionally corrupts the tool-call [ARGS] JSON while
generating it: unquoted keys, a collapsed `": ` separator (`"start": 350`
degrades to `"start_350`), or truncated output.  Ported from the upstream
JS implementation (src/response-repair.mjs) so the Python line keeps
feature parity for remote-response recovery.

Layers, in order:
  repair_json_text               fix common textual defects
  parse_json_with_repair         direct parse, then repaired parse
  salvage_restricted_exec_args   recover executable commands from wreckage
  salvage_search_evidence        recover safe file hits + rg patterns
"""

from __future__ import annotations

import json
import os
import re
from typing import Any


def repair_json_text(text: str) -> str:
    """Repair common model-produced JSON defects without evaluating code."""
    text = re.sub(r'([{,]\s*)([A-Za-z_$][\w$-]*)"\s*:', r'\1"\2":', text)
    text = re.sub(r'([{,]\s*)([A-Za-z_$][\w$-]*)\s*:', r'\1"\2":', text)
    # `"start": 350` sometimes degrades to `"start_350` (the `": ` separator
    # collapses into the underscore); only fires when no value follows. The
    # executor consumes start_line/end_line, so remap the model's aliases.
    def _remap_collapse(match: re.Match) -> str:
        prefix, key, value = match.group(1), match.group(2), match.group(3)
        field = {"start": "start_line", "end": "end_line"}.get(key, key)
        return f'{prefix}"{field}": {value}'

    text = re.sub(
        r'([{,]\s*)"([A-Za-z_$][\w$-]*)_(-?\d+)(?=\s*[,}])', _remap_collapse, text
    )
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return text


def parse_json_with_repair(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return json.loads(repair_json_text(text))
        except json.JSONDecodeError:
            return None


def _extract_balanced_object(text: str, start: int) -> str:
    depth = 0
    quote: str | None = None
    escaped = False

    for i in range(start, len(text)):
        char = text[i]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue

        if char in ('"', "'"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


_COMMAND_KEY_RE = re.compile(r"""["']?(command\d+)["']?\s*:\s*\{""")


def _collect_commands(text: str) -> list[tuple[str, dict]]:
    commands: list[tuple[str, dict]] = []
    for match in _COMMAND_KEY_RE.finditer(text):
        start = text.find("{", match.start())
        value = parse_json_with_repair(_extract_balanced_object(text, start))
        if isinstance(value, dict) and isinstance(value.get("type"), str):
            commands.append((match.group(1), value))
    return commands


_LOOSE_FILE_RE = re.compile(r"""["']?file["']?\s*:\s*["'](\/codebase(?:\/[^"'\r\n,}]+)+)["']""")
_LOOSE_START_RE = re.compile(r"""["']?start_line["']?\s*:\s*(\d+)""")
_LOOSE_END_RE = re.compile(r"""["']?end_line["']?\s*:\s*(\d+)""")


def _collect_loose_readfiles(text: str) -> list[dict]:
    commands: list[dict] = []
    for match in _LOOSE_FILE_RE.finditer(text):
        window = text[max(0, match.start() - 240) : min(len(text), match.end() + 240)]
        start_match = _LOOSE_START_RE.search(window)
        end_match = _LOOSE_END_RE.search(window)
        command: dict[str, Any] = {
            "type": "readfile",
            "file": match.group(1).replace("\\/", "/"),
        }
        if start_match:
            command["start_line"] = int(start_match.group(1))
        if end_match:
            command["end_line"] = int(end_match.group(1))
        commands.append(command)
    return commands


def salvage_restricted_exec_args(text: str) -> dict | None:
    """Recover executable restricted_exec commands from a malformed response."""
    result: dict[str, Any] = {}
    seen: set[str] = set()

    def add(key: str | None, command: dict) -> None:
        signature = json.dumps(command, sort_keys=True)
        if signature in seen:
            return
        seen.add(signature)
        if key and key not in result:
            result[key] = command
            return
        # Auto-generated keys must never overwrite a salvaged structured command.
        n = 1
        while f"command{n}" in result:
            n += 1
        result[f"command{n}"] = command

    for key, command in _collect_commands(str(text)):
        add(key, command)
    for command in _collect_loose_readfiles(str(text)):
        add(None, command)
    return result or None


_LOOSE_PATH_RE = re.compile(
    r"/codebase/[A-Za-z0-9_@+.,()\[\]{} !#$%&'=-]+"
    r"(?:/[A-Za-z0-9_@+.,()\[\]{} !#$%&'=-]+)*\.[A-Za-z0-9]{1,16}"
)
_LOOSE_PATTERN_RE = re.compile(r"""["']?pattern["']?\s*:\s*["']([^"'\r\n]+)["']""")


def salvage_search_evidence(text: str, project_root: str) -> dict:
    """Recover safe file hits and rg keywords when structured parsing fails."""
    source = str(text)
    commands = salvage_restricted_exec_args(source) or {}
    by_path: dict[str, dict] = {}
    rg_patterns: list[str] = []
    # resolve_within_root returns a realpath; relpath needs the same base or
    # symlinked temp dirs (macOS /var → /private/var) produce escaping paths.
    canonical_root = os.path.realpath(project_root)

    def add_file(virtual_path: str, ranges: list[tuple[int, int]] = []) -> None:
        # Lazy import: core.py imports this module at load time.
        from core import resolve_within_root

        full_path, error = resolve_within_root(project_root, virtual_path)
        if error or not full_path:
            return
        rel_path = os.path.relpath(full_path, canonical_root).replace(os.sep, "/")
        current = by_path.get(full_path) or {
            "path": rel_path,
            "full_path": full_path,
            "ranges": [],
        }
        for start, end in ranges:
            if [start, end] not in current["ranges"]:
                current["ranges"].append([start, end])
        by_path[full_path] = current

    for command in commands.values():
        if command.get("type") == "readfile" and isinstance(command.get("file"), str):
            start = command.get("start_line") or 0
            end = command.get("end_line") or 0
            add_file(command["file"], [(start, end)] if start and end else [])
        if command.get("type") == "rg" and isinstance(command.get("pattern"), str):
            rg_patterns.append(command["pattern"])

    for match in _LOOSE_PATH_RE.finditer(source):
        add_file(match.group(0).strip())
    for match in _LOOSE_PATTERN_RE.finditer(source):
        rg_patterns.append(match.group(1))

    return {
        "files": list(by_path.values())[:30],
        "rg_patterns": list(dict.fromkeys(rg_patterns)),
    }
