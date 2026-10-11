"""Lexical caller and guard expansion for the top search hits.

Low confidence: symbols are regexes, call sites are fixed-string word matches,
and guard definitions are fixed-string definition shapes. A hit range that
defines no symbols falls back to that file's exported or top-level names.
Hits are deduped by file, and only the first few distinct files are expanded.
Any failure returns an empty section instead of raising.

``rg`` is always given an explicit path. With no path, a non-interactive shell
makes ``rg`` read stdin and wait.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

SECTION_HEADING = "Callers / guard sites (low confidence, lexical):"
DEFAULT_TOP_N = 3
DEFAULT_MAX_ENTRIES = 6
GUARD_WINDOW = 8
TOTAL_TIMEOUT_S = 2.0
MAX_SYMBOLS = 6
MAX_SYMBOLS_PER_FILE = 4
FALLBACK_EXPORTS = 8
MAX_CALLS_PER_SYMBOL = 6
MAX_GUARDS_PER_CALL = 3
RG_MAX_COUNT = 4

_CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go"}
_SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    "generated",
    ".cache",
    "target",
    "coverage",
    "out",
    "vendor",
}
_SKIP_IDENTS = {
    "if",
    "elif",
    "else",
    "for",
    "while",
    "switch",
    "catch",
    "except",
    "finally",
    "return",
    "function",
    "func",
    "class",
    "await",
    "async",
    "new",
    "typeof",
    "instanceof",
    "void",
    "delete",
    "true",
    "false",
    "null",
    "undefined",
    "none",
    "and",
    "or",
    "not",
    "in",
    "is",
    "when",
    "case",
    "default",
    "try",
    "with",
    "as",
    "yield",
    "lambda",
    "def",
    "const",
    "let",
    "var",
    "import",
    "from",
    "export",
    "type",
    "interface",
    "struct",
    "enum",
    "match",
    "super",
    "this",
    "self",
    "cls",
}
_STOP_SYMBOLS = _SKIP_IDENTS | {
    "main",
    "index",
    "data",
    "value",
    "item",
    "items",
    "result",
    "results",
    "error",
    "err",
    "ctx",
    "req",
    "res",
    "next",
    "callback",
    "args",
    "kwargs",
    "props",
    "state",
    "config",
    "options",
    "name",
    "key",
    "path",
    "file",
    "files",
    "info",
    "meta",
    "test",
    "setup",
    "init",
    "run",
    "get",
    "set",
    "map",
    "filter",
    "then",
    "length",
    "string",
    "number",
    "boolean",
    "object",
    "array",
    "promise",
}

_PY_DEF = re.compile(r"^(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[.*?\])?\s*\(")
_PY_CLASS = re.compile(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_PY_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=")
_TS_FUNC = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\b[^{;\n]*\("
)
_TS_CLASS = re.compile(r"^(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)\b")
_TS_CONST = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"
)
_GO_FUNC = re.compile(r"^func\s+(?:\([^)\n]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^)\n]*\]\s*)?\(")
_GO_TYPE = re.compile(r"^type\s+([A-Za-z_][A-Za-z0-9_]*)\s+")
_GO_CONST = re.compile(r"^(?:const|var)\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_GUARD_LINE = re.compile(r"\bif\s*\(|\bif\s+|&{2}|\?(?![?.])|(?<![A-Za-z0-9_])when\b")
_CALL_RE = re.compile(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
_JS_FAMILY = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_LANG_FAMILY = {suffix: "js" for suffix in _JS_FAMILY} | {".py": "py", ".go": "go"}
_APP_ROOT_RE = re.compile(r"(?:^|/)(apps|packages|services)/([^/]+)(?:/|$)", re.IGNORECASE)
# Definition lookup only. Call sites in tests can still surface.
_DEF_SKIP_RE = re.compile(
    r"(?:^|/)(?:benchmarks?|tests?|__tests__|testdata)(?:/|$)"
    r"|(?:^|/)[^/]*[._-](?:test|spec)\.[^/]+$"
    r"|(?:^|/)test_[^/]*\.py$"
    r"|(?:^|/)[^/]*_test\.py$"
    r"|(?:^|/)[^/]*_test\.go$",
    re.IGNORECASE,
)
# Guard calls only. These are builtins and collection methods, not project predicates.
_GUARD_BUILTINS = {
    "values",
    "keys",
    "entries",
    "map",
    "flatmap",
    "filter",
    "reduce",
    "foreach",
    "some",
    "every",
    "find",
    "findindex",
    "includes",
    "has",
    "get",
    "set",
    "push",
    "pop",
    "shift",
    "unshift",
    "slice",
    "splice",
    "concat",
    "join",
    "split",
    "sort",
    "indexof",
    "replace",
    "match",
    "length",
    "trim",
    "tostring",
    "startswith",
    "endswith",
    "tolowercase",
    "touppercase",
    "isarray",
    "isnan",
    "parseint",
    "parsefloat",
    "number",
    "string",
    "boolean",
    "object",
    "array",
    "json",
    "parse",
    "stringify",
    "len",
    "isinstance",
    "issubclass",
    "str",
    "int",
    "float",
    "bool",
    "list",
    "dict",
    "tuple",
    "range",
    "enumerate",
    "zip",
    "any",
    "all",
    "min",
    "max",
    "sum",
    "abs",
    "round",
    "print",
    "open",
    "type",
    "getattr",
    "setattr",
    "hasattr",
    "sorted",
    "reversed",
    "iter",
    "next",
    "math",
    "console",
    "promise",
    "date",
    "error",
    "regexp",
    "symbol",
}


@dataclass
class GuardSite:
    symbol: str
    caller_path: str
    caller_line: int
    guard_name: str
    guard_path: str | None = None
    guard_line: int | None = None


@dataclass
class DecisionLayerReport:
    entries: list[GuardSite] = field(default_factory=list)
    guard_symbols: list[str] = field(default_factory=list)
    text: str = ""
    elapsed_ms: float = 0.0
    ran: bool = False


def decision_layer_enabled(requested: bool = True) -> bool:
    """False when the CLI flag is off or ``FAST_CONTEXT_DECISION_LAYER`` is 0."""
    if not requested:
        return False
    raw = os.environ.get("FAST_CONTEXT_DECISION_LAYER", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _rg_binary() -> str | None:
    return shutil.which("rg")


def _symbol_ok(name: str) -> bool:
    if len(name) < 3 or len(name) > 80 or name.startswith("_"):
        return False
    return name.lower() not in _STOP_SYMBOLS


def extract_defined_symbols(text: str, suffix: str) -> list[str]:
    """Names defined in ``text`` for Python, TypeScript/JavaScript, or Go."""
    found: dict[str, int] = {}

    def add(name: str, score: int) -> None:
        if _symbol_ok(name):
            found[name] = max(found.get(name, 0), score)

    family = suffix.lower()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("//", "#", "*", "/*")):
            continue
        if family == ".py":
            matched = _PY_DEF.match(line)
            if matched:
                add(matched.group(1), 30)
                continue
            matched = _PY_CLASS.match(line)
            if matched:
                add(matched.group(1), 25)
                continue
            if not raw[:1].isspace():
                matched = _PY_ASSIGN.match(line)
                if matched:
                    add(matched.group(1), 10)
            continue
        if family == ".go":
            matched = _GO_FUNC.match(line)
            if matched:
                name = matched.group(1)
                add(name, 30 + (20 if name[:1].isupper() else 0))
                continue
            matched = _GO_TYPE.match(line)
            if matched:
                name = matched.group(1)
                add(name, 25 + (20 if name[:1].isupper() else 0))
                continue
            matched = _GO_CONST.match(line)
            if matched:
                name = matched.group(1)
                add(name, 10 + (20 if name[:1].isupper() else 0))
            continue
        if family in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
            exported = 20 if line.startswith("export ") else 0
            matched = _TS_FUNC.match(line)
            if matched:
                add(matched.group(1), 30 + exported)
                continue
            matched = _TS_CLASS.match(line)
            if matched:
                add(matched.group(1), 25 + exported)
                continue
            matched = _TS_CONST.match(line)
            if matched:
                add(matched.group(1), 10 + exported)
    ordered = sorted(found.items(), key=lambda item: (-item[1], item[0]))
    return [name for name, _ in ordered[:MAX_SYMBOLS_PER_FILE]]


def _definition_name(line: str, family: str) -> str | None:
    if family == ".py":
        patterns = (_PY_DEF, _PY_CLASS, _PY_ASSIGN)
    elif family == ".go":
        patterns = (_GO_FUNC, _GO_TYPE, _GO_CONST)
    elif family in _JS_FAMILY:
        patterns = (_TS_FUNC, _TS_CLASS, _TS_CONST)
    else:
        return None
    for pattern in patterns:
        matched = pattern.match(line)
        if matched:
            return matched.group(1)
    return None


def extract_fallback_symbols(text: str, suffix: str, limit: int = FALLBACK_EXPORTS) -> list[str]:
    """Exported names in source order, else top-level names. At most ``limit``."""
    family = suffix.lower()
    exported: list[str] = []
    toplevel: list[str] = []
    seen_export: set[str] = set()
    seen_top: set[str] = set()

    def push(bucket: list[str], seen: set[str], name: str) -> None:
        if name in seen or not _symbol_ok(name):
            return
        seen.add(name)
        bucket.append(name)

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith(("//", "#", "*", "/*")):
            continue
        name = _definition_name(stripped, family)
        if not name:
            continue
        indented = bool(raw[:1].isspace())
        if family == ".py":
            if not indented:
                push(toplevel, seen_top, name)
            continue
        if family == ".go":
            if indented:
                continue
            if name[:1].isupper():
                push(exported, seen_export, name)
            else:
                push(toplevel, seen_top, name)
            continue
        if family in _JS_FAMILY:
            if stripped.startswith("export "):
                push(exported, seen_export, name)
            elif not indented:
                push(toplevel, seen_top, name)
    chosen = exported or toplevel
    return chosen[: max(0, limit)]


def _safe_rel(root: Path, raw: str) -> str | None:
    if not raw or "\x00" in raw:
        return None
    text = raw.replace("\\", "/").strip()
    candidate = Path(text)
    try:
        if candidate.is_absolute():
            return candidate.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return None
    rel = Path(os.path.normpath(text))
    if rel.is_absolute() or ".." in rel.parts:
        return None
    return rel.as_posix()


def _read_lines(root: Path, rel: str) -> list[str]:
    path = root / rel
    try:
        if not path.is_file():
            return []
        blob = path.read_bytes()
    except OSError:
        return []
    if b"\x00" in blob[:512]:
        return []
    return blob.decode("utf-8", errors="replace").splitlines()


def _slice_text(lines: list[str], start: int | None, end: int | None) -> str:
    if not lines:
        return ""
    start_no = start if isinstance(start, int) and start > 0 else 1
    end_no = end if isinstance(end, int) and end >= start_no else min(len(lines), start_no + 199)
    end_no = min(end_no, start_no + 399, len(lines))
    return "\n".join(lines[start_no - 1 : end_no])


def _hit_segments(hit: dict) -> list[tuple[str, int | None, int | None]]:
    path = ""
    ranges = None
    start = None
    end = None
    content = ""
    chunk = hit.get("chunk") if isinstance(hit.get("chunk"), dict) else None
    if chunk:
        path = str(chunk.get("file_path") or chunk.get("path") or "")
        start = chunk.get("start_line")
        end = chunk.get("end_line")
        content = str(chunk.get("content") or "")
    path = str(hit.get("path") or hit.get("file_path") or path)
    if hit.get("ranges"):
        ranges = hit.get("ranges")
    start = hit.get("start", hit.get("start_line", start))
    end = hit.get("end", hit.get("end_line", end))
    if not path and hit.get("full_path"):
        path = str(hit["full_path"])
    segments: list[tuple[str, int | None, int | None]] = []
    if isinstance(ranges, list) and ranges:
        for item in ranges[:3]:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                segments.append((path, int(item[0]), int(item[1])))
    else:
        start_no = int(start) if isinstance(start, int) else None
        end_no = int(end) if isinstance(end, int) else None
        segments.append((path, start_no, end_no))
    if content and not path:
        return []
    return [(path, seg_start, seg_end) for path, seg_start, seg_end in segments if path]


def _group_top_files(
    root: Path,
    hits: list[dict],
    top_n: int,
) -> list[tuple[str, list[tuple[int | None, int | None, str]]]]:
    """First ``top_n`` distinct code files, with every range those files contributed."""
    order: list[str] = []
    grouped: dict[str, list[tuple[int | None, int | None, str]]] = {}
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        chunk = hit.get("chunk") if isinstance(hit.get("chunk"), dict) else None
        chunk_text = str((chunk or {}).get("content") or hit.get("content") or "")
        for raw_path, start, end in _hit_segments(hit):
            rel = _safe_rel(root, raw_path)
            if not rel or Path(rel).suffix.lower() not in _CODE_SUFFIXES:
                continue
            if rel not in grouped:
                if len(order) >= top_n:
                    continue
                order.append(rel)
                grouped[rel] = []
            grouped[rel].append((start, end, chunk_text))
    return [(rel, grouped[rel]) for rel in order]


def _symbols_for_file(
    root: Path,
    rel: str,
    spans: list[tuple[int | None, int | None, str]],
) -> list[str]:
    """Symbols in the hit ranges first, then the file's exports."""
    suffix = Path(rel).suffix.lower()
    lines = _read_lines(root, rel)
    found: list[str] = []
    seen: set[str] = set()
    for start, end, chunk_text in spans:
        text = _slice_text(lines, start, end) if lines else ""
        if not text:
            text = chunk_text
        for name in extract_defined_symbols(text, suffix):
            if name not in seen:
                seen.add(name)
                found.append(name)
    if not lines:
        return found
    # Hit ranges often cover only part of a file (a doc comment, one helper),
    # so append the file's exports after the in-range symbols.
    for name in extract_fallback_symbols("\n".join(lines), suffix):
        if name not in seen:
            seen.add(name)
            found.append(name)
    return found


def _is_definition_line(line: str, name: str) -> bool:
    escaped = re.escape(name)
    patterns = (
        rf"\bfunction\s+{escaped}\b",
        rf"\bconst\s+{escaped}\b",
        rf"\blet\s+{escaped}\b",
        rf"\bvar\s+{escaped}\b",
        rf"\bdef\s+{escaped}\b",
        rf"\bclass\s+{escaped}\b",
        rf"\bfunc\s+(?:\([^)\n]*\)\s*)?{escaped}\b",
        rf"(?<![\w.]){escaped}\s*=",
    )
    return any(re.search(pattern, line) for pattern in patterns)


def _is_call_line(line: str, name: str) -> bool:
    if not re.search(rf"\b{re.escape(name)}\s*\(", line):
        return False
    return not _is_definition_line(line, name)


def _skip_search_path(rel: str) -> bool:
    parts = [part.lower() for part in rel.split("/") if part]
    if any(part in _SKIP_DIRS for part in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    return name.endswith(".min.js")


@dataclass
class _Match:
    path: str
    line: int
    text: str


def _parse_rg_line(line: str, root: Path) -> _Match | None:
    matched = re.match(r"^(.*?):(\d+):(.*)$", line)
    if not matched:
        return None
    rel = _safe_rel(root, matched.group(1))
    if not rel:
        return None
    try:
        lineno = int(matched.group(2))
    except ValueError:
        return None
    return _Match(rel, lineno, matched.group(3))


def _run_rg(args: list[str], deadline: float) -> list[str] | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0.05:
        return []
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=max(0.1, remaining),
            env={**os.environ, "RIPGREP_CONFIG_PATH": ""},
            check=False,
        )
    except FileNotFoundError:
        return None
    except (subprocess.TimeoutExpired, OSError):
        return []
    except Exception:
        return []
    if proc.returncode not in {0, 1}:
        return []
    return proc.stdout.splitlines()


def _python_lines(root: Path, deadline: float, skip_rel: str | None = None) -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    if time.monotonic() >= deadline:
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        if time.monotonic() >= deadline:
            break
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS and not name.startswith(".")]
        for filename in filenames:
            if time.monotonic() >= deadline:
                break
            if Path(filename).suffix.lower() not in _CODE_SUFFIXES:
                continue
            full = Path(dirpath) / filename
            try:
                rel = full.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            if rel == skip_rel or _skip_search_path(rel):
                continue
            for lineno, text in enumerate(_read_lines(root, rel), 1):
                found.append((rel, lineno, text))
    return found


def _search_lines(
    root: Path,
    deadline: float,
    *,
    fixed_patterns: list[str],
    word: str | None,
    skip_rel: str | None,
) -> list[_Match]:
    """Search with rg when it exists. ``None`` from a missing rg triggers the fallback."""
    rg_bin = _rg_binary()
    if rg_bin:
        command = [
            rg_bin,
            "--color=never",
            "--no-heading",
            "-n",
            "-F",
            "--max-count",
            str(RG_MAX_COUNT),
            "--max-filesize",
            "256K",
            "--glob",
            "*.{py,ts,tsx,js,jsx,mjs,cjs,go}",
            "--glob",
            "!**/{node_modules,dist,build,generated,.git}/**",
        ]
        if word:
            command.extend(["-w", word])
        else:
            for pattern in fixed_patterns:
                command.extend(["-e", pattern])
        # Explicit path: rg with no path reads stdin and hangs in this shell.
        command.append(str(root))
        lines = _run_rg(command, deadline)
        if lines is None:
            rg_bin = None
        else:
            matches: list[_Match] = []
            for line in lines:
                parsed = _parse_rg_line(line, root)
                if parsed and parsed.path != skip_rel and not _skip_search_path(parsed.path):
                    matches.append(parsed)
            return matches

    predicate: re.Pattern[str]
    if word:
        predicate = re.compile(rf"\b{re.escape(word)}\b")
    else:
        predicate = re.compile("|".join(re.escape(pattern) for pattern in fixed_patterns))
    matches = []
    per_file: dict[str, int] = {}
    for rel, lineno, text in _python_lines(root, deadline, skip_rel=skip_rel):
        if per_file.get(rel, 0) >= RG_MAX_COUNT:
            continue
        if predicate.search(text):
            per_file[rel] = per_file.get(rel, 0) + 1
            matches.append(_Match(rel, lineno, text))
            if len(matches) >= 40:
                break
    return matches


def _language_family(path: str) -> str | None:
    return _LANG_FAMILY.get(Path(path).suffix.lower())


def _app_root_key(path: str) -> tuple[str, str] | None:
    matched = _APP_ROOT_RE.search(path.replace("\\", "/"))
    if not matched:
        return None
    return (matched.group(1).lower(), matched.group(2).lower())


def _excluded_definition_path(rel: str) -> bool:
    return _DEF_SKIP_RE.search(rel.replace("\\", "/")) is not None


def _invoked_as_method(line: str, start: int) -> bool:
    return line[:start].rstrip(" \t").endswith(".")


def _guard_names(lines: list[str], call_line: int, symbol: str) -> list[str]:
    if call_line < 1:
        return []
    start = max(0, call_line - 1 - GUARD_WINDOW)
    end = min(len(lines), call_line + GUARD_WINDOW)
    found: list[str] = []
    for line in lines[start:end]:
        if not _GUARD_LINE.search(line):
            continue
        for match in _CALL_RE.finditer(line):
            name = match.group(1)
            if _invoked_as_method(line, match.start(1)):
                continue
            lowered = name.lower()
            if name == symbol or lowered in _SKIP_IDENTS or lowered in _GUARD_BUILTINS or name in found:
                continue
            if not _symbol_ok(name):
                continue
            found.append(name)
            if len(found) >= MAX_GUARDS_PER_CALL:
                return found
    return found


def _definition_patterns(name: str) -> list[str]:
    return [
        f"function {name}",
        f"const {name}",
        f"def {name}",
        f"{name} =",
        f"func {name}",
        f"class {name}",
        f"var {name}",
        f"let {name}",
    ]


def _definition_candidates(matches: list[_Match], name: str) -> list[_Match]:
    return [item for item in matches if _is_definition_line(item.text, name)]


def _pick_definition(candidates: list[_Match], caller_path: str, caller_line: int) -> _Match | None:
    """Same language family only. Same app root, then any packages/* root."""
    caller_family = _language_family(caller_path)
    caller_app = _app_root_key(caller_path)
    pool: list[_Match] = []
    for item in candidates:
        if item.path == caller_path and item.line == caller_line:
            continue
        if caller_family is None or _language_family(item.path) != caller_family:
            continue
        if _excluded_definition_path(item.path):
            continue
        pool.append(item)
    if not pool:
        return None

    def sort_key(item: _Match) -> tuple:
        item_app = _app_root_key(item.path)
        same_app = caller_app is not None and item_app == caller_app
        shared_package = item_app is not None and item_app[0] == "packages" and not same_app
        different_file = item.path != caller_path
        return (
            0 if same_app else 1,
            0 if shared_package else 1,
            0 if different_file else 1,
            item.path,
            item.line,
        )

    pool.sort(key=sort_key)
    return pool[0]


def _resolve_definition(
    root: Path,
    name: str,
    caller_path: str,
    caller_line: int,
    deadline: float,
    cache: dict[str, list[_Match]],
) -> _Match | None:
    if name not in cache:
        matches = _search_lines(
            root,
            deadline,
            fixed_patterns=_definition_patterns(name),
            word=None,
            skip_rel=None,
        )
        cache[name] = _definition_candidates(matches, name)
    return _pick_definition(cache[name], caller_path, caller_line)


def _select_entries(entries: list[GuardSite], max_entries: int) -> list[GuardSite]:
    """Keep resolved guards when any exist, and show cross-file definitions first."""
    resolved = [entry for entry in entries if entry.guard_path and entry.guard_line]
    pool = resolved or list(entries)
    ranked = sorted(
        pool,
        key=lambda entry: 0 if entry.guard_path and entry.guard_path != entry.caller_path else 1,
    )
    return ranked[:max_entries]


def _display_path(root: Path, rel: str) -> str:
    return str((root / rel).resolve())


def format_decision_section(root: Path, entries: list[GuardSite]) -> str:
    if not entries:
        return ""
    lines = [SECTION_HEADING, ""]
    for entry in entries[:DEFAULT_MAX_ENTRIES]:
        caller = f"{_display_path(root, entry.caller_path)}:{entry.caller_line}"
        if entry.guard_path and entry.guard_line:
            defined = (
                f"{entry.guard_name} defined at "
                f"{_display_path(root, entry.guard_path)}:{entry.guard_line}"
            )
        else:
            defined = f"{entry.guard_name} (definition not resolved)"
        lines.append(f"- {caller} calls {entry.symbol}; guard {defined}")
    return "\n".join(lines)


def _expand(root: Path, hits: list[dict], top_n: int, max_entries: int, deadline: float) -> DecisionLayerReport:
    report = DecisionLayerReport(ran=True)
    symbols_seen: list[tuple[str, str]] = []
    symbol_cap = max(MAX_SYMBOLS, FALLBACK_EXPORTS)
    for rel, spans in _group_top_files(root, hits, top_n):
        for name in _symbols_for_file(root, rel, spans):
            pair = (rel, name)
            if pair not in symbols_seen:
                symbols_seen.append(pair)
            if len(symbols_seen) >= symbol_cap:
                break
        if len(symbols_seen) >= symbol_cap:
            break

    definitions: dict[str, list[_Match]] = {}
    for source_rel, symbol in symbols_seen:
        if time.monotonic() >= deadline:
            break
        calls = [
            item
            for item in _search_lines(
                root,
                deadline,
                fixed_patterns=[],
                word=symbol,
                skip_rel=source_rel,
            )
            if _is_call_line(item.text, symbol)
        ]
        for call in calls[:MAX_CALLS_PER_SYMBOL]:
            if time.monotonic() >= deadline:
                break
            caller_lines = _read_lines(root, call.path)
            for guard in _guard_names(caller_lines, call.line, symbol):
                if time.monotonic() >= deadline:
                    break
                defined = _resolve_definition(root, guard, call.path, call.line, deadline, definitions)
                report.entries.append(
                    GuardSite(
                        symbol=symbol,
                        caller_path=call.path,
                        caller_line=call.line,
                        guard_name=guard,
                        guard_path=defined.path if defined else None,
                        guard_line=defined.line if defined else None,
                    )
                )
    selected = _select_entries(report.entries, max_entries)
    report.entries = selected
    report.guard_symbols = []
    for entry in selected:
        if entry.guard_name not in report.guard_symbols:
            report.guard_symbols.append(entry.guard_name)
    report.text = format_decision_section(root, selected)
    return report


def build_decision_layer(
    project_root: str,
    hits: list[dict],
    *,
    top_n: int = DEFAULT_TOP_N,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    enabled: bool = True,
    timeout_s: float = TOTAL_TIMEOUT_S,
) -> DecisionLayerReport:
    """Expand the top hits into caller and guard sites. Never raises."""
    started = time.perf_counter()
    try:
        if not decision_layer_enabled(enabled) or not hits or top_n <= 0 or max_entries <= 0:
            return DecisionLayerReport(elapsed_ms=0.0, ran=False)
        root = Path(project_root).expanduser().resolve()
        if not root.is_dir():
            return DecisionLayerReport(elapsed_ms=(time.perf_counter() - started) * 1000, ran=True)
        deadline = time.monotonic() + max(0.0, timeout_s)
        report = _expand(root, hits, top_n, max_entries, deadline)
    except Exception:
        report = DecisionLayerReport(ran=True)
    report.elapsed_ms = (time.perf_counter() - started) * 1000
    return report
