"""
Windsurf Fast Context — core protocol implementation.

Reverse-engineered Windsurf SWE-grep Connect-RPC/Protobuf protocol
for standalone AI-driven semantic code search.

Flow:
  query + tree → Windsurf Devstral API
  → Devstral returns tool_calls (rg/readfile/tree/ls/glob, up to 8 parallel)
  → execute locally → send results back → repeat for N rounds
  → ANSWER: file paths + line ranges + suggested rg patterns
"""

from __future__ import annotations

import gzip
import json
import multiprocessing
import os
import platform
import re
import sqlite3
import struct
import subprocess
import ssl
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from local_semble import SembleUnavailable, find_related as semble_find_related, search as semble_search


# ─── SSL ────────────────────────────────────────────────────

def _ssl_ctx() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    try:
        ctx = ssl.create_default_context()
        ctx.load_default_certs()
        return ctx
    except Exception:
        pass
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

_SSL_CTX = _ssl_ctx()


# ─── Protobuf Encoder ──────────────────────────────────────

class ProtobufEncoder:
    """手动 protobuf 编码器，完全匹配 Windsurf 的请求格式。"""

    def __init__(self) -> None:
        self.buf = bytearray()

    def _varint(self, value: int) -> bytes:
        parts: list[int] = []
        while value > 0x7F:
            parts.append((value & 0x7F) | 0x80)
            value >>= 7
        parts.append(value & 0x7F)
        return bytes(parts)

    def _tag(self, field: int, wire: int) -> bytes:
        return self._varint((field << 3) | wire)

    def write_varint(self, field: int, value: int) -> ProtobufEncoder:
        self.buf.extend(self._tag(field, 0))
        self.buf.extend(self._varint(value))
        return self

    def write_string(self, field: int, value: str) -> ProtobufEncoder:
        data = value.encode("utf-8")
        self.buf.extend(self._tag(field, 2))
        self.buf.extend(self._varint(len(data)))
        self.buf.extend(data)
        return self

    def write_bytes(self, field: int, value: bytes) -> ProtobufEncoder:
        self.buf.extend(self._tag(field, 2))
        self.buf.extend(self._varint(len(value)))
        self.buf.extend(value)
        return self

    def write_message(self, field: int, sub: ProtobufEncoder) -> ProtobufEncoder:
        data = bytes(sub.buf)
        self.buf.extend(self._tag(field, 2))
        self.buf.extend(self._varint(len(data)))
        self.buf.extend(data)
        return self

    def to_bytes(self) -> bytes:
        return bytes(self.buf)


# ─── Connect-RPC 帧编解码 ──────────────────────────────────

def connect_frame_encode(proto_bytes: bytes) -> bytes:
    compressed = gzip.compress(proto_bytes)
    return struct.pack("B", 1) + struct.pack(">I", len(compressed)) + compressed


def connect_frames_decode(data: bytes) -> List[bytes]:
    frames: list[bytes] = []
    i = 0
    while i + 5 <= len(data):
        flags = data[i]
        length = struct.unpack(">I", data[i + 1 : i + 5])[0]
        i += 5
        payload = data[i : i + length]
        i += length
        if flags in (1, 3):
            try:
                payload = gzip.decompress(payload)
            except Exception:
                pass
        frames.append(payload)
    return frames


# ─── Protobuf 解码 ─────────────────────────────────────────

def proto_extract_strings(data: bytes) -> List[str]:
    strings: list[str] = []
    i = 0
    while i < len(data):
        tag = 0
        shift = 0
        while i < len(data):
            b = data[i]; i += 1
            tag |= (b & 0x7F) << shift; shift += 7
            if not (b & 0x80):
                break
        wire = tag & 0x7
        if wire == 0:
            while i < len(data):
                b = data[i]; i += 1
                if not (b & 0x80):
                    break
        elif wire == 1:
            i += 8
        elif wire == 2:
            length = 0; shift = 0
            while i < len(data):
                b = data[i]; i += 1
                length |= (b & 0x7F) << shift; shift += 7
                if not (b & 0x80):
                    break
            if i + length <= len(data):
                raw = data[i : i + length]
                try:
                    text = raw.decode("utf-8")
                    if len(text) > 5:
                        strings.append(text)
                except UnicodeDecodeError:
                    pass
            i += length
        elif wire == 5:
            i += 4
        else:
            break
    return strings


# ─── 本地工具执行器 ────────────────────────────────────────

RESULT_MAX_LINES = 50
LINE_MAX_CHARS = 250
MAX_TREE_BYTES = 12 * 1024

DEFAULT_EXCLUDE_PATTERNS = [
    "node_modules",
    ".git",
    "dist",
    "build",
    "coverage",
    ".venv",
    "venv",
    "target",
    "out",
    ".cache",
    "__pycache__",
    "vendor",
    "deps",
    "third_party",
    "logs",
    "data",
]

QUERY_STOPWORDS = {
    "the", "and", "with", "from", "into", "where", "what", "when", "which",
    "that", "this", "these", "those", "about", "logic", "handled", "handle",
    "used", "using", "there", "their", "your", "just", "need", "show", "give",
    "look", "files", "file", "code", "repo", "repository", "project", "flow",
    "implementation", "implement", "inside", "over", "under", "through",
    "real", "true", "search", "find", "does", "have", "been", "into", "then",
    "also", "like", "help", "quick", "quickly", "before", "after", "state",
}

GENERIC_GREP_PATTERNS = QUERY_STOPWORDS | {
    "api",
    "app",
    "args",
    "block",
    "class",
    "command",
    "commands",
    "config",
    "content",
    "core",
    "data",
    "error",
    "exec",
    "exec_command",
    "function",
    "functions",
    "import",
    "imports",
    "key",
    "keys",
    "line",
    "lines",
    "main",
    "meta",
    "module",
    "output",
    "parse",
    "path",
    "paths",
    "range",
    "ranges",
    "response",
    "result",
    "results",
    "round",
    "search",
    "symbol",
    "symbols",
    "text",
    "tool",
    "tools",
}

SYMBOL_PATTERNS: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]] = [
    (re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("), lambda m: f"{m.group(1)}()"),
    (re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b"), lambda m: m.group(1)),
    (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\("), lambda m: f"{m.group(1)}()"),
    (re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"), lambda m: m.group(1)),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*="), lambda m: m.group(1)),
    (re.compile(r"^\s*(?:pub\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("), lambda m: f"{m.group(1)}()"),
    (re.compile(r"^\s*(?:pub\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)\b"), lambda m: m.group(1)),
    (re.compile(r"^\s*(?:pub\s+)?enum\s+([A-Za-z_][A-Za-z0-9_]*)\b"), lambda m: m.group(1)),
    (re.compile(r"^\s*impl\s+([A-Za-z_][A-Za-z0-9_]*)\b"), lambda m: f"impl {m.group(1)}"),
]

COMMENT_PREFIXES = ("#", "//", "///", "//!", "/*", "*")


class FastContextError(RuntimeError):
    """Structured error for HTTP/network failures."""

    def __init__(self, message: str, code: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class ToolExecutor:
    """在本地项目目录执行 SWE-grep 的受限工具命令。"""

    def __init__(self, project_root: str) -> None:
        self.root = os.path.abspath(project_root)
        self.collected_rg_patterns: List[str] = []

    def _real(self, virtual: str) -> str:
        if virtual.startswith("/codebase"):
            rel = virtual[len("/codebase") :].lstrip("/")
            return os.path.join(self.root, rel)
        return virtual

    @staticmethod
    def _truncate(text: str) -> str:
        lines = text.split("\n")
        # Match original Windsurf behavior: 50 line limit + ~250 char per-line silent truncation
        truncated_lines = []
        for line in lines[:RESULT_MAX_LINES]:
            if len(line) > LINE_MAX_CHARS:
                truncated_lines.append(line[:LINE_MAX_CHARS])
            else:
                truncated_lines.append(line)
        text = "\n".join(truncated_lines)
        if len(lines) > RESULT_MAX_LINES:
            text += "\n... (lines truncated) ..."
        return text

    def _remap(self, text: str) -> str:
        return text.replace(self.root, "/codebase")

    _rg_bin: str | None = None
    _rg_checked: bool = False

    @classmethod
    def _find_rg(cls) -> str | None:
        """Find rg binary. Returns path or None. Result is cached."""
        if cls._rg_checked:
            return cls._rg_bin
        cls._rg_checked = True
        for candidate in ["rg", "/opt/homebrew/bin/rg", "/usr/local/bin/rg"]:
            try:
                subprocess.run([candidate, "--version"], capture_output=True, timeout=5)
                cls._rg_bin = candidate
                return candidate
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        cls._rg_bin = None
        return None

    @staticmethod
    def _glob_match(rel_path: str, filename: str, patterns: list[str]) -> bool:
        """Check if a file matches any glob pattern."""
        for pat in patterns:
            normalized = pat.replace("\\", "/")
            # **/*.ext → match by filename
            if normalized.startswith("**/"):
                sub = normalized[3:]
                if "/**" in sub:
                    # **/dirname/** → handled by skip_dirs
                    continue
                if fnmatch(filename, sub):
                    return True
            elif fnmatch(rel_path, normalized):
                return True
            elif fnmatch(filename, normalized):
                return True
        return False

    def _rg_python(self, pattern: str, real_path: str, virtual_path: str,
                   include: list[str] | None, exclude: list[str] | None) -> str:
        """Pure Python ripgrep replacement — zero external dependencies."""
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))

        results: list[str] = []
        skip_dirs = {
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            "dist", "build", ".cache", "target", ".tox", ".eggs",
            ".mypy_cache", "coverage", "out",
        }
        # Add directory-based exclude patterns
        if exclude:
            for pat in exclude:
                normalized = pat.lstrip("!").replace("\\", "/")
                if normalized.startswith("**/") and normalized.endswith("/**"):
                    skip_dirs.add(normalized[3:-3])

        if os.path.isfile(real_path):
            matches = self._rg_search_file(regex, real_path, RESULT_MAX_LINES)
            for lineno, line in matches:
                results.append(f"{virtual_path}:{lineno}:{line}")
        else:
            for root, dirs, files in os.walk(real_path):
                dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
                for fname in sorted(files):
                    fpath = os.path.join(root, fname)
                    rel = os.path.relpath(fpath, real_path)
                    if include and not self._glob_match(rel, fname, include):
                        continue
                    if exclude and self._glob_match(
                        rel, fname, [p.lstrip("!") for p in exclude]
                    ):
                        continue
                    matches = self._rg_search_file(regex, fpath, RESULT_MAX_LINES)
                    vpath = virtual_path.rstrip("/") + "/" + rel.replace(os.sep, "/")
                    for lineno, line in matches:
                        results.append(f"{vpath}:{lineno}:{line}")
                    if len(results) >= RESULT_MAX_LINES:
                        break
                if len(results) >= RESULT_MAX_LINES:
                    break

        output = "\n".join(results) if results else "(no matches)"
        return self._truncate(self._remap(output))

    @staticmethod
    def _rg_search_file(regex, filepath: str, max_matches: int) -> list[tuple[int, str]]:
        """Search a single file for regex matches."""
        try:
            with open(filepath, "rb") as f:
                head = f.read(512)
                if b"\x00" in head:
                    return []
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                matches = []
                for lineno, line in enumerate(f, 1):
                    if regex.search(line):
                        matches.append((lineno, line.rstrip("\n\r")))
                        if len(matches) >= max_matches:
                            break
                return matches
        except (OSError, PermissionError):
            return []

    def rg(self, pattern: str, path: str,
           include: list[str] | None = None,
           exclude: list[str] | None = None) -> str:
        self.collected_rg_patterns.append(pattern)
        rp = self._real(path)
        if not os.path.exists(rp):
            return f"Error: path does not exist: {path}"
        rg_bin = self._find_rg()
        if rg_bin is None:
            return self._rg_python(pattern, rp, path, include, exclude)
        cmd = [rg_bin, "--no-heading", "-n", "--max-count", "50", pattern, rp]
        if include:
            for g in include:
                cmd += ["--glob", g]
        if exclude:
            for g in exclude:
                cmd += ["--glob", f"!{g}"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30,
                               env={**os.environ, "RIPGREP_CONFIG_PATH": ""})
            out = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
            err = r.stderr.decode("utf-8", errors="replace") if r.stderr else ""
            return self._truncate(self._remap(out or err or "(no matches)"))
        except FileNotFoundError:
            return self._rg_python(pattern, rp, path, include, exclude)
        except subprocess.TimeoutExpired:
            return "Error: timed out"

    def readfile(self, file: str,
                 start_line: int | None = None,
                 end_line: int | None = None) -> str:
        rp = self._real(file)
        if not os.path.isfile(rp):
            return f"Error: file not found: {file}"
        try:
            with open(rp, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
        except Exception as e:
            return f"Error: {e}"
        s = (start_line or 1) - 1
        e = end_line or len(all_lines)
        selected = all_lines[s:e]
        out = "".join(f"{i}:{line}" for i, line in enumerate(selected, start=s + 1))
        return self._truncate(out)

    def tree(self, path: str, levels: int | None = None) -> str:
        rp = self._real(path)
        if not os.path.isdir(rp):
            return f"Error: dir not found: {path}"
        cmd = ["tree", rp]
        if levels:
            cmd += ["-L", str(levels)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return self._truncate(self._remap(r.stdout or r.stderr))
        except FileNotFoundError:
            return self._tree_py(rp, levels or 3, path)
        except subprocess.TimeoutExpired:
            return "Error: timed out"

    def _tree_py(self, real: str, levels: int, virt: str) -> str:
        lines = [virt]
        def walk(p: str, pfx: str, d: int) -> None:
            if d >= levels:
                return
            try:
                entries = sorted(os.listdir(p))
            except PermissionError:
                return
            for e in entries:
                fp = os.path.join(p, e)
                lines.append(f"{pfx}├── {e}")
                if os.path.isdir(fp) and not e.startswith("."):
                    walk(fp, pfx + "│   ", d + 1)
        walk(real, "", 0)
        return "\n".join(lines[:300])

    def ls(self, path: str, long_format: bool = False, all_files: bool = False) -> str:
        rp = self._real(path)
        cmd = ["ls"]
        if long_format:
            cmd.append("-l")
        if all_files:
            cmd.append("-a")
        cmd.append(rp)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return self._truncate(self._remap(r.stdout or r.stderr))
        except Exception as e:
            return f"Error: {e}"

    def glob_cmd(self, pattern: str, path: str, type_filter: str = "all") -> str:
        import glob as gmod
        rp = self._real(path)
        matches = gmod.glob(os.path.join(rp, pattern), recursive=True)
        if type_filter == "file":
            matches = [m for m in matches if os.path.isfile(m)]
        elif type_filter == "directory":
            matches = [m for m in matches if os.path.isdir(m)]
        out = "\n".join(self._remap(m) for m in sorted(matches)[:100])
        return out or "(no matches)"

    def exec_command(self, cmd: Dict[str, Any]) -> str:
        t = cmd.get("type", "")
        if t == "rg":
            return self.rg(cmd["pattern"], cmd["path"], cmd.get("include"), cmd.get("exclude"))
        if t == "readfile":
            return self.readfile(cmd["file"], cmd.get("start_line"), cmd.get("end_line"))
        if t == "tree":
            return self.tree(cmd["path"], cmd.get("levels"))
        if t == "ls":
            return self.ls(cmd["path"], cmd.get("long_format", False), cmd.get("all", False))
        if t == "glob":
            return self.glob_cmd(cmd["pattern"], cmd["path"], cmd.get("type_filter", "all"))
        return f"Error: unknown command type '{t}'"

    def exec_tool_call(self, args: Dict[str, Any]) -> str:
        parts: list[str] = []
        for key in sorted(args.keys()):
            if key.startswith("command") and isinstance(args[key], dict):
                output = self.exec_command(args[key])
                parts.append(f"<{key}_result>\n{output}\n</{key}_result>")
        return "".join(parts)

    def exec_tool_call_async(self, args: Dict[str, Any]) -> str:
        keys = [
            key for key in sorted(args.keys())
            if key.startswith("command") and isinstance(args[key], dict)
        ]
        if not keys:
            return ""

        max_workers = min(len(keys), 8)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                key: pool.submit(self.exec_command, args[key])
                for key in keys
            }
            parts: list[str] = []
            for key in keys:
                output = futures[key].result()
                parts.append(f"<{key}_result>\n{output}\n</{key}_result>")
        return "".join(parts)


# ─── 协议常量 ──────────────────────────────────────────────

API_BASE = "https://server.self-serve.windsurf.com/exa.api_server_pb.ApiServerService"
AUTH_BASE = "https://server.self-serve.windsurf.com/exa.auth_pb.AuthService"
WS_APP = "windsurf"
DEFAULT_WS_MODEL = "MODEL_SWE_1_6_FAST"
DEFAULT_WS_FALLBACK_MODELS = ("MODEL_SWE_1_5",)
WS_APP_VER = os.environ.get("WS_APP_VER", "1.48.2")
WS_LS_VER = os.environ.get("WS_LS_VER", "1.9544.35")

# 系统提示模板（{max_turns} 和 {max_commands} 由调用者填入）
SYSTEM_PROMPT_TEMPLATE = (
    "You are an expert software engineer, responsible for providing context "
    "to another engineer to solve a code issue in the current codebase. "
    "The user will present you with a description of the issue, and it is "
    "your job to provide a series of file paths with associated line ranges "
    "that contain ALL the information relevant to understand and correctly "
    "address the issue.\n\n"
    "# IMPORTANT:\n"
    "- A relevant file does not mean only the files that must be modified to "
    "solve the task. It means any file that contains information relevant to "
    "planning and implementing the fix, such as the definitions of classes "
    "and functions that are relevant to the pieces of code that will have to "
    "be modified.\n"
    "- If the query describes a cross-boundary flow or mentions multiple "
    "stages, cover distinct layers of that flow instead of returning only one "
    "cluster of files. Prefer one strong file per layer over near-duplicates.\n"
    "- You should include enough context around the relevant lines to allow "
    "the engineer to understand the task correctly. You must include ENTIRE "
    "semantic blocks (functions, classes, definitions, etc). For example:\n"
    "If addressing the issue requires modifying a method within a class, then "
    "you should include the entire class definition, not just the lines around "
    "the method we want to modify.\n"
    "- NEVER truncate these blocks unless they are very large (hundreds of "
    "lines or more, in which case providing only a relevant portion of the "
    "block is acceptable).\n"
    "- Your job is to essentially alleviate the job of the other engineer by "
    "giving them a clean starting context from which to start working. More "
    "precisely, you should minimize the number of files the engineer has to "
    "read to understand and solve the task correctly (while not providing "
    "irrelevant code snippets).\n\n"
    "# ENVIRONMENT\n"
    "- Working directory: /codebase. Make sure to run commands in this "
    "directory, not `.`.\n"
    "- Tool access: use the restricted_exec tool ONLY\n"
    "- Allowed sub-commands (schema-enforced):\n"
    "  - rg: Search for patterns in files using ripgrep\n"
    "    - Required: pattern (string), path (string)\n"
    "    - Optional: include (array of globs), exclude (array of globs)\n"
    "  - readfile: Read contents of a file with optional line range\n"
    "    - Required: file (string)\n"
    "    - Optional: start_line (int), end_line (int) — 1-indexed, inclusive\n"
    "  - tree: Display directory structure as a tree\n"
    "    - Required: path (string)\n"
    "    - Optional: levels (int)\n"
    "  - ls: List files in a directory\n"
    "    - Required: path (string)\n"
    "    - Optional: long_format (bool), all (bool)\n"
    "  - glob: Find files matching a glob pattern\n"
    "    - Required: pattern (string), path (string)\n"
    "    - Optional: type_filter (string: file/directory/all)\n\n"
    "# THINKING RULES\n"
    "- Think step-by-step. Plan, reason, and reflect before each tool call.\n"
    "- Use tool calls liberally and purposefully to ground every conclusion "
    "in real code, not assumptions.\n"
    "- If a command fails, rethink and try something different; do not "
    "complain to the user.\n\n"
    "# FAST-SEARCH DEFAULTS (optimize rg/tree on large repos)\n"
    "- Start NARROW, then widen only if needed. Prefer searching likely code "
    "roots first (e.g., `src/`, `lib/`, `app/`, `packages/`, `services/`) "
    "instead of `/codebase`.\n"
    "- Prefer fixed-string search for literals: escape patterns or keep regex "
    "simple. Use smart case; avoid case-insensitive unless necessary.\n"
    "- Prefer file-type filters and globs (in include) over full-repo scans.\n"
    "- Default EXCLUDES for speed (apply via the exclude array): "
    "node_modules, .git, dist, build, coverage, .venv, venv, target, out, "
    ".cache, __pycache__, vendor, deps, third_party, logs, data, *.min.*\n"
    "- Skip huge files where possible; when opening files, prefer reading "
    "only relevant ranges with readfile.\n"
    "- Limit directory traversal with tree levels to quickly orient before "
    "deeper inspection.\n\n"
    "# SOME EXAMPLES OF WORKFLOWS\n"
    "- MAP – Use `tree` with small levels; `rg` on likely roots to grasp "
    "structure and hotspots.\n"
    "- ANCHOR – `rg` for problem keywords and anchor symbols; restrict by "
    "language globs via include.\n"
    "- TRACE – Follow imports with targeted `rg` in narrowed roots; open "
    "files with `readfile` scoped to entire semantic blocks.\n"
    "- VERIFY – Confirm each candidate path exists by reading or additional "
    "searches; drop false positives (tests, vendored, generated) unless they "
    "must change.\n\n"
    "# TOOL USE GUIDELINES\n"
    "- You must use a SINGLE restricted_exec call in your answer, that lets "
    "you execute at most {max_commands} commands in a single turn. Each command must be "
    "an object with a `type` field of `rg`, `readfile`, `tree`, `ls`, or "
    "`glob` and the appropriate fields for that type.\n"
    "- Example restricted_exec usage:\n"
    '[TOOL_CALLS]restricted_exec[ARGS]{{\n'
    '  "command1": {{\n'
    '    "type": "rg",\n'
    '    "pattern": "Controller",\n'
    '    "path": "/codebase/slime",\n'
    '    "include": ["**/*.py"],\n'
    '    "exclude": ["**/node_modules/**", "**/.git/**", "**/dist/**", '
    '"**/build/**", "**/.venv/**", "**/__pycache__/**"]\n'
    "  }},\n"
    '  "command2": {{\n'
    '    "type": "readfile",\n'
    '    "file": "/codebase/slime/train.py",\n'
    '    "start_line": 1,\n'
    '    "end_line": 200\n'
    "  }},\n"
    '  "command3": {{\n'
    '    "type": "tree",\n'
    '    "path": "/codebase/slime/",\n'
    '    "levels": 2\n'
    "  }}\n"
    "}}\n"
    "- You have at most {max_turns} turns to interact with the environment by calling "
    "tools, so issuing multiple commands at once is necessary and encouraged "
    "to speed up your research.\n"
    "- Each command result may be truncated to 50 lines; prefer multiple "
    "targeted reads/searches to build complete context.\n"
    "- DO NOT EVER USE MORE THAN {max_commands} commands in a single turn, or you will "
    "be penalized.\n\n"
    "# ANSWER FORMAT (strict format, including tags)\n"
    '- You will output an XML structure with a root element "ANSWER" '
    'containing "file" elements. Each "file" element will have a "path" '
    'attribute and contain "range" elements.\n'
    "- You will output this as your final response.\n"
    "- The line ranges must be inclusive.\n\n"
    'Output example inside the "answer" tool argument:\n'
    "<ANSWER>\n"
    '  <file path="/codebase/info_theory/formulas/entropy.py">\n'
    "    <range>10-60</range>\n"
    "    <range>150-210</range>\n"
    "  </file>\n"
    '  <file path="/codebase/info_theory/data_structures/bits.py">\n'
    "    <range>1-40</range>\n"
    "    <range>110-170</range>\n"
    "  </file>\n"
    "</ANSWER>\n\n\n"
    "Remember: Prefer narrow, fixed-string, and type-filtered searches with "
    "aggressive excludes and size/depth limits. Widen scope only as needed. "
    "Use the restricted tools available to you, and output your answer in "
    "exactly the specified format.\n\n"
    "# NO RESULTS POLICY\n"
    "If after thorough searching you are confident that no relevant files "
    "exist for the query, return an empty answer: <ANSWER></ANSWER>. Do not "
    "pad the result with low-signal files.\n\n"
    "# RESULT COUNT\n"
    "Aim to return at most {max_results} files in your answer. Focus on the "
    "most relevant files first.\n"
)

FINAL_FORCE_ANSWER = (
    "You have no turns left. Now you MUST provide your final ANSWER, even if it's not complete."
)


def build_system_prompt(
    max_turns: int = 3,
    max_commands: int = 8,
    max_results: int = 10,
) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        max_turns=max_turns,
        max_commands=max_commands,
        max_results=max_results,
    )


def _build_command_schema(n: int) -> dict:
    return {
        "type": "object",
        "description": f"Command {n} to execute. Must be one of: rg, readfile, or tree.",
        "oneOf": [
            {
                "properties": {
                    "type": {"type": "string", "const": "rg",
                             "description": "Search for patterns in files using ripgrep."},
                    "pattern": {"type": "string", "description": "The regex pattern to search for."},
                    "path": {"type": "string", "description": "The path to search in."},
                    "include": {"type": "array", "items": {"type": "string"},
                                "description": "File patterns to include."},
                    "exclude": {"type": "array", "items": {"type": "string"},
                                "description": "File patterns to exclude."},
                },
                "required": ["type", "pattern", "path"],
            },
            {
                "properties": {
                    "type": {"type": "string", "const": "readfile",
                             "description": "Read contents of a file with optional line range."},
                    "file": {"type": "string", "description": "Path to the file to read."},
                    "start_line": {"type": "integer", "description": "Starting line number (1-indexed)."},
                    "end_line": {"type": "integer", "description": "Ending line number (1-indexed)."},
                },
                "required": ["type", "file"],
            },
            {
                "properties": {
                    "type": {"type": "string", "const": "tree",
                             "description": "Display directory structure as a tree."},
                    "path": {"type": "string", "description": "Path to the directory."},
                    "levels": {"type": "integer", "description": "Number of directory levels."},
                },
                "required": ["type", "path"],
            },
            {
                "properties": {
                    "type": {"type": "string", "const": "ls",
                             "description": "List files in a directory."},
                    "path": {"type": "string", "description": "Path to the directory."},
                    "long_format": {"type": "boolean"},
                    "all": {"type": "boolean"},
                },
                "required": ["type", "path"],
            },
            {
                "properties": {
                    "type": {"type": "string", "const": "glob",
                             "description": "Find files matching a glob pattern."},
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "type_filter": {"type": "string", "enum": ["file", "directory", "all"]},
                },
                "required": ["type", "pattern", "path"],
            },
        ],
    }


def get_tool_definitions(max_commands: int = 8) -> str:
    props = {f"command{i}": _build_command_schema(i) for i in range(1, max_commands + 1)}
    tools = [
        {
            "type": "function",
            "function": {
                "name": "restricted_exec",
                "description": "Execute restricted commands (rg, readfile, tree, ls, glob) in parallel.",
                "parameters": {"type": "object", "properties": props, "required": ["command1"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "answer",
                "description": "Final answer with relevant files and line ranges.",
                "parameters": {
                    "type": "object",
                    "properties": {"answer": {"type": "string", "description": "The final answer in XML format."}},
                    "required": ["answer"],
                },
            },
        },
    ]
    return json.dumps(tools, ensure_ascii=False)


def _classify_error(exc: Exception) -> FastContextError:
    if isinstance(exc, FastContextError):
        return exc
    if isinstance(exc, HTTPError):
        if exc.code == 413:
            return FastContextError("HTTP 413 payload too large", "PAYLOAD_TOO_LARGE", status=exc.code)
        if exc.code == 429:
            return FastContextError("HTTP 429 rate limited", "RATE_LIMITED", status=exc.code)
        if exc.code in {401, 403}:
            return FastContextError(f"HTTP {exc.code} auth error", "AUTH_ERROR", status=exc.code)
        return FastContextError(f"HTTP {exc.code}", "SERVER_ERROR", status=exc.code)
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return FastContextError(str(exc) or "timed out", "TIMEOUT")
    if isinstance(exc, URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError):
            return FastContextError("timed out", "TIMEOUT")
        return FastContextError(str(exc), "NETWORK_ERROR")
    message = str(exc)
    if "timed out" in message.lower():
        return FastContextError(message, "TIMEOUT")
    return FastContextError(message or exc.__class__.__name__, "NETWORK_ERROR")


def _trim_messages(messages: list[dict]) -> bool:
    if len(messages) <= 4:
        return False
    head = messages[:2]
    tail = messages[-2:]
    messages.clear()
    messages.extend(head)
    messages.append({
        "role": 1,
        "content": "[Prior search rounds omitted to reduce payload. Provide your best answer based on the remaining context.]",
    })
    messages.extend(tail)
    return True


def _normalize_rel_path(path: str) -> str:
    return path.replace(os.sep, "/").lstrip("./")


def _is_excluded_path(rel_path: str, exclude_paths: list[str]) -> bool:
    normalized = _normalize_rel_path(rel_path)
    base = Path(normalized).name
    for pattern in exclude_paths:
        token = pattern.replace("\\", "/").lstrip("./")
        token = token.lstrip("!")
        if not token:
            continue
        token = token.rstrip("/")
        if any(ch in token for ch in "*?[]"):
            if fnmatch(normalized, token) or fnmatch(base, token):
                return True
            continue
        if normalized == token or base == token:
            return True
        if normalized.startswith(f"{token}/"):
            return True
        if f"/{token}/" in f"/{normalized}":
            return True
    return False


def _effective_excludes(exclude_paths: list[str] | None = None) -> list[str]:
    patterns = list(DEFAULT_EXCLUDE_PATTERNS)
    if exclude_paths:
        patterns.extend(exclude_paths)
    return patterns


def _extract_query_terms(query: str, max_terms: int = 8) -> list[str]:
    quoted = [
        phrase.strip()
        for phrase in re.findall(r'"([^"]{3,80})"|\'([^\']{3,80})\'', query)
        for phrase in phrase if phrase.strip()
    ]
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_./:-]{2,}", query)
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()

    for phrase in quoted:
        lowered = phrase.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ranked.append((100, phrase))

    for token in tokens:
        cleaned = token.strip(".,:;()[]{}<>")
        lowered = cleaned.lower()
        if lowered in seen or lowered in QUERY_STOPWORDS or len(cleaned) < 3:
            continue
        seen.add(lowered)
        score = 1
        if any(ch in cleaned for ch in "_-./:"):
            score += 5
        if re.search(r"[a-z][A-Z]|[A-Z]{2,}", cleaned):
            score += 4
        if len(cleaned) >= 8:
            score += 2
        ranked.append((score, cleaned))

    ranked.sort(key=lambda item: (-item[0], item[1].lower()))
    return [term for _, term in ranked[:max_terms]]


def _list_repo_files(project_root: str, exclude_paths: list[str], limit: int = 50000) -> list[str]:
    rg_bin = ToolExecutor._find_rg()
    if rg_bin:
        try:
            result = subprocess.run(
                [rg_bin, "--files", "."],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=15,
                env={**os.environ, "RIPGREP_CONFIG_PATH": ""},
            )
            if result.returncode in {0, 1}:
                files = [
                    _normalize_rel_path(line)
                    for line in result.stdout.splitlines()
                    if line.strip()
                ]
                return [
                    path for path in files[:limit]
                    if not _is_excluded_path(path, exclude_paths)
                ]
        except Exception:
            pass

    files: list[str] = []
    for root, dirs, filenames in os.walk(project_root):
        rel_root = os.path.relpath(root, project_root)
        rel_root = "" if rel_root == "." else _normalize_rel_path(rel_root)
        dirs[:] = [
            dirname for dirname in dirs
            if not _is_excluded_path(
                f"{rel_root}/{dirname}" if rel_root else dirname,
                exclude_paths,
            )
        ]
        for filename in filenames:
            rel_path = f"{rel_root}/{filename}" if rel_root else filename
            rel_path = _normalize_rel_path(rel_path)
            if _is_excluded_path(rel_path, exclude_paths):
                continue
            files.append(rel_path)
            if len(files) >= limit:
                return files
    return files


def _collect_content_hits(
    project_root: str,
    terms: list[str],
    exclude_paths: list[str],
    limit_per_term: int = 20,
) -> dict[str, set[str]]:
    rg_bin = ToolExecutor._find_rg()
    if not rg_bin or not terms:
        return {}

    hits: dict[str, set[str]] = {}
    for term in terms[:6]:
        try:
            cmd = [rg_bin, "-l", "--max-count", "1", "--fixed-strings", term, "."]
            result = subprocess.run(
                cmd,
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "RIPGREP_CONFIG_PATH": ""},
            )
        except Exception:
            continue
        if result.returncode not in {0, 1}:
            continue
        for line in result.stdout.splitlines()[:limit_per_term]:
            rel_path = _normalize_rel_path(line)
            if _is_excluded_path(rel_path, exclude_paths):
                continue
            hits.setdefault(rel_path, set()).add(term)
    return hits


def build_local_anchor_brief(
    query: str,
    project_root: str,
    exclude_paths: list[str],
    max_files: int = 6,
) -> str:
    terms = _extract_query_terms(query)
    if not terms:
        return ""

    files = _list_repo_files(project_root, exclude_paths)
    if not files:
        return ""

    content_hits = _collect_content_hits(project_root, terms, exclude_paths)
    scored: list[tuple[int, str, list[str]]] = []

    for rel_path in files:
        normalized = rel_path.lower()
        base = Path(rel_path).name.lower()
        stem = Path(rel_path).stem.lower()
        score = 0
        reasons: list[str] = []
        for term in terms:
            lowered = term.lower()
            if lowered == stem or lowered == base:
                score += 7
                reasons.append(f"filename:{term}")
            elif lowered in base:
                score += 5
                reasons.append(f"filename:{term}")
            if lowered in normalized:
                score += 3
                reasons.append(f"path:{term}")

        matched_terms = sorted(content_hits.get(rel_path, set()))
        if matched_terms:
            score += 2 * len(matched_terms)
            reasons.extend(f"content:{term}" for term in matched_terms)

        if score > 0:
            deduped_reasons = list(dict.fromkeys(reasons))
            scored.append((score, rel_path, deduped_reasons))

    if not scored:
        return ""

    scored.sort(key=lambda item: (-item[0], item[1]))
    lines = ["Local lexical anchors:"]
    for _, rel_path, reasons in scored[:max_files]:
        preview = ", ".join(reasons[:3])
        lines.append(f"- /codebase/{rel_path} [{preview}]")
    lines.append(f"Exact terms worth preserving: {', '.join(terms[:6])}")
    return "\n".join(lines)


# ─── 凭证 ──────────────────────────────────────────────────

def auto_discover_api_key() -> Optional[str]:
    try:
        from extract_key import extract_key

        result = extract_key()
        api_key = (result.get("api_key") or "").strip()
        if api_key:
            return api_key
    except Exception:
        pass
    return None


def get_api_key() -> str:
    """获取 API key：环境变量 > 自动发现。"""
    key = os.environ.get("WINDSURF_API_KEY")
    if key:
        return key
    key = auto_discover_api_key()
    if key:
        return key
    raise RuntimeError(
        "未找到 Windsurf API Key。请设置环境变量 WINDSURF_API_KEY "
        "或确保 Windsurf 已登录。运行 src/extract_key.py 查看提取方法。"
    )


def _parse_model_env(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _resolve_model_candidates(primary_model: str | None = None) -> list[str]:
    primary = (primary_model or os.environ.get("WS_MODEL", DEFAULT_WS_MODEL)).strip()
    fallback_raw = os.environ.get("WS_FALLBACK_MODELS")
    fallback_models = (
        list(DEFAULT_WS_FALLBACK_MODELS)
        if fallback_raw is None
        else _parse_model_env(fallback_raw)
    )

    ordered: list[str] = []
    for model in [primary, *fallback_models]:
        if model and model not in ordered:
            ordered.append(model)
    return ordered or [DEFAULT_WS_MODEL]


def _extract_inline_error_code(text: str) -> str | None:
    match = re.match(r"^\[Error\]\s+([A-Za-z_]+)\s*:", text.strip())
    if not match:
        return None
    return match.group(1).lower()


def _should_try_next_model(error_code: str | None) -> bool:
    if not error_code:
        return False
    return error_code.lower() in {"rate_limited", "resource_exhausted", "unavailable"}


def _attach_model_metadata(
    result: dict[str, Any],
    model: str,
    attempted_models: list[str],
) -> dict[str, Any]:
    meta = result.setdefault("_meta", {})
    meta["model"] = model
    meta["model_attempts"] = attempted_models[:]
    meta["fallback_used"] = len(attempted_models) > 1
    return result


# ─── 网络层 ────────────────────────────────────────────────

def _unary_request(url: str, proto_bytes: bytes, compress: bool = True) -> bytes:
    headers = {
        "Content-Type": "application/proto",
        "Connect-Protocol-Version": "1",
        "User-Agent": f"connect-go/1.18.1 (go1.25.5)",
        "Accept-Encoding": "gzip",
    }
    if compress:
        body = gzip.compress(proto_bytes)
        headers["Content-Encoding"] = "gzip"
    else:
        body = proto_bytes
    req = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=30, context=_SSL_CTX) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data
    except Exception as exc:
        raise _classify_error(exc) from exc


def _streaming_request(
    proto_bytes: bytes,
    timeout_ms: int = 30000,
    max_retries: int = 2,
) -> bytes:
    frame = connect_frame_encode(proto_bytes)
    url = f"{API_BASE}/GetDevstralStream"
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    headers = {
        "Content-Type": "application/connect+proto",
        "Connect-Protocol-Version": "1",
        "Connect-Accept-Encoding": "gzip",
        "Connect-Content-Encoding": "gzip",
        "Connect-Timeout-Ms": str(timeout_ms),
        "User-Agent": f"connect-go/1.18.1 (go1.25.5)",
        "Accept-Encoding": "identity",
        "Baggage": (
            f"sentry-release=language-server-windsurf@{WS_LS_VER},"
            f"sentry-environment=stable,sentry-sampled=false,"
            f"sentry-trace_id={trace_id},"
            "sentry-public_key=b813f73488da69eedec534dba1029111"
        ),
        "Sentry-Trace": f"{trace_id}-{span_id}-0",
    }
    req = Request(url, data=frame, headers=headers, method="POST")

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            with urlopen(req, timeout=(timeout_ms / 1000) + 5, context=_SSL_CTX) as resp:
                return resp.read()
        except Exception as exc:
            classified = _classify_error(exc)
            last_error = classified
            if classified.code in {"AUTH_ERROR", "PAYLOAD_TOO_LARGE"}:
                raise classified from exc
            if attempt >= max_retries:
                raise classified from exc
            time.sleep(attempt + 1)

    raise _classify_error(last_error or RuntimeError("unknown streaming error"))


def fetch_jwt(api_key: str) -> str:
    meta = ProtobufEncoder()
    meta.write_string(1, WS_APP)
    meta.write_string(2, WS_APP_VER)
    meta.write_string(3, api_key)
    meta.write_string(4, "zh-cn")
    meta.write_string(7, WS_LS_VER)
    meta.write_string(12, WS_APP)
    meta.write_bytes(30, b"\x00\x01")
    outer = ProtobufEncoder()
    outer.write_message(1, meta)
    resp = _unary_request(f"{AUTH_BASE}/GetUserJwt", outer.to_bytes(), compress=False)
    for s in proto_extract_strings(resp):
        if s.startswith("eyJ") and "." in s:
            return s
    raise RuntimeError("无法从 GetUserJwt 响应中提取 JWT")


def check_rate_limit(api_key: str, jwt: str, model: str) -> bool:
    req = ProtobufEncoder()
    req.write_message(1, _build_metadata(api_key, jwt))
    req.write_string(3, model)
    try:
        _unary_request(f"{API_BASE}/CheckUserMessageRateLimit", req.to_bytes(), compress=True)
        return True
    except HTTPError as e:
        if e.code == 429:
            return False
        raise
    except Exception:
        return True  # 网络问题时不阻塞


# ─── 请求构建 ──────────────────────────────────────────────

def _build_metadata(api_key: str, jwt: str) -> ProtobufEncoder:
    meta = ProtobufEncoder()
    meta.write_string(1, WS_APP)
    meta.write_string(2, WS_APP_VER)
    meta.write_string(3, api_key)
    meta.write_string(4, "zh-cn")
    sys_info = {
        "Os": platform.system().lower(),
        "Arch": platform.machine(),
        "Release": platform.release(),
        "Version": platform.version(),
        "Machine": platform.machine(),
        "Nodename": platform.node(),
        "Sysname": platform.system(),
        "ProductVersion": platform.mac_ver()[0] if sys.platform == "darwin" else "",
    }
    meta.write_string(5, json.dumps(sys_info))
    meta.write_string(7, WS_LS_VER)
    try:
        ncpu = multiprocessing.cpu_count()
    except Exception:
        ncpu = 4
    try:
        mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, AttributeError, OSError):
        mem = 0
    cpu_info = {
        "NumSockets": 1, "NumCores": ncpu, "NumThreads": ncpu,
        "VendorID": "", "Family": "0", "Model": "0",
        "ModelName": platform.processor() or "Unknown", "Memory": mem,
    }
    meta.write_string(8, json.dumps(cpu_info))
    meta.write_string(12, WS_APP)
    meta.write_string(21, jwt)
    meta.write_bytes(30, b"\x00\x01")
    return meta


def _build_chat_message(role: int, content: str, *,
                        tool_call_id: str | None = None,
                        tool_name: str | None = None,
                        tool_args_json: str | None = None,
                        ref_call_id: str | None = None) -> ProtobufEncoder:
    msg = ProtobufEncoder()
    msg.write_varint(2, role)
    msg.write_string(3, content)
    if tool_call_id and tool_name and tool_args_json:
        tc = ProtobufEncoder()
        tc.write_string(1, tool_call_id)
        tc.write_string(2, tool_name)
        tc.write_string(3, tool_args_json)
        msg.write_message(6, tc)
    if ref_call_id:
        msg.write_string(7, ref_call_id)
    return msg


def _build_request(
    api_key: str,
    jwt: str,
    messages: list[dict],
    tool_defs: str,
    model: str,
) -> bytes:
    req = ProtobufEncoder()
    req.write_message(1, _build_metadata(api_key, jwt))
    for m in messages:
        msg_enc = _build_chat_message(
            role=m["role"], content=m["content"],
            tool_call_id=m.get("tool_call_id"),
            tool_name=m.get("tool_name"),
            tool_args_json=m.get("tool_args_json"),
            ref_call_id=m.get("ref_call_id"),
        )
        req.write_message(2, msg_enc)
    req.write_string(3, model)
    req.write_string(4, tool_defs)
    return req.to_bytes()


# ─── 响应解析 ──────────────────────────────────────────────

def _parse_tool_call(text: str) -> Optional[Tuple[str, str, Dict]]:
    text = text.replace("</s>", "")
    m = re.search(r"\[TOOL_CALLS\](\w+)\[ARGS\](\{.+)", text, re.DOTALL)
    if not m:
        return None
    name = m.group(1)
    raw = m.group(2).strip()
    depth = 0
    end = 0
    for i, ch in enumerate(raw):
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == 0:
        end = len(raw)
    try:
        args = json.loads(raw[:end])
    except json.JSONDecodeError:
        return None
    thinking = text[: m.start()].strip()
    return thinking, name, args


def _parse_response(data: bytes) -> Tuple[str, Optional[Tuple[str, Dict]]]:
    frames = connect_frames_decode(data)
    all_text = ""
    for frame_data in frames:
        try:
            text_candidate = frame_data.decode("utf-8")
            if text_candidate.startswith("{"):
                err_obj = json.loads(text_candidate)
                if "error" in err_obj:
                    code = err_obj["error"].get("code", "unknown")
                    msg = err_obj["error"].get("message", "")
                    return f"[Error] {code}: {msg}", None
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            pass
        # 直接从帧数据提取文本（绕过 protobuf 嵌套问题）
        raw_text = frame_data.decode("utf-8", errors="ignore")
        if "[TOOL_CALLS]" in raw_text:
            all_text = raw_text
            break
        for s in proto_extract_strings(frame_data):
            if len(s) > 10:
                all_text += s

    parsed = _parse_tool_call(all_text)
    if parsed:
        thinking, name, args = parsed
        return thinking, (name, args)
    return all_text, None


# ─── 核心搜索 ──────────────────────────────────────────────

def _render_repo_tree(project_root: str, depth: int, exclude_paths: list[str]) -> str:
    lines = ["/codebase"]

    def walk(real_dir: str, rel_dir: str, level: int, prefix: str) -> None:
        if level >= depth:
            return
        try:
            entries = sorted(os.listdir(real_dir))
        except (OSError, PermissionError):
            return

        visible: list[tuple[str, str]] = []
        for name in entries:
            rel_path = f"{rel_dir}/{name}" if rel_dir else name
            rel_path = _normalize_rel_path(rel_path)
            if _is_excluded_path(rel_path, exclude_paths):
                continue
            visible.append((name, rel_path))

        for name, rel_path in visible[:200]:
            lines.append(f"{prefix}├── {name}")
            full_path = os.path.join(project_root, rel_path)
            if os.path.isdir(full_path) and not name.startswith("."):
                walk(full_path, rel_path, level + 1, prefix + "│   ")

    walk(project_root, "", 0, "")
    return "\n".join(lines)


def get_repo_map(
    project_root: str,
    target_depth: int = 3,
    exclude_paths: list[str] | None = None,
) -> tuple[str, int, int, bool]:
    excludes = _effective_excludes(exclude_paths)
    safe_depth = max(1, min(target_depth, 6))

    for depth in range(safe_depth, 0, -1):
        tree = _render_repo_tree(project_root, depth, excludes)
        size_bytes = len(tree.encode("utf-8"))
        if size_bytes <= MAX_TREE_BYTES:
            return tree, depth, size_bytes, depth < safe_depth

    try:
        entries = sorted(
            entry for entry in os.listdir(project_root)
            if not _is_excluded_path(entry, excludes)
        )
    except (OSError, PermissionError):
        return "/codebase\n(empty or inaccessible)", 0, 28, True

    tree = "\n".join(["/codebase"] + [f"├── {entry}" for entry in entries[:200]])
    return tree, 0, len(tree.encode("utf-8")), True


def _search_once(
    query: str,
    project_root: str,
    api_key: str,
    jwt: str,
    model: str,
    system_prompt: str,
    tool_defs: str,
    user_content: str,
    max_turns: int,
    timeout_ms: int,
    actual_depth: int,
    tree_size_bytes: int,
    fell_back: bool,
    on_progress: Callable[[str], None] | None = None,
) -> Dict[str, Any]:
    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    def build_meta(**extra: Any) -> dict[str, Any]:
        meta = {
            "tree_depth": actual_depth,
            "tree_size_kb": round(tree_size_bytes / 1024, 1),
            "fell_back": fell_back,
            "project_root": project_root,
            "model": model,
        }
        meta.update(extra)
        return meta

    executor = ToolExecutor(project_root)
    messages: list[dict] = [
        {"role": 5, "content": system_prompt},
        {"role": 1, "content": user_content},
    ]

    total_api_calls = max_turns + 1

    for turn in range(total_api_calls):
        log(f"{model}: 轮次 {turn + 1}/{total_api_calls}")

        proto = _build_request(api_key, jwt, messages, tool_defs, model)
        try:
            resp_data = _streaming_request(proto, timeout_ms=timeout_ms)
        except Exception as exc:
            err = _classify_error(exc)
            if err.code in {"PAYLOAD_TOO_LARGE", "TIMEOUT"} and len(messages) > 4:
                log(f"{model}: {err.code}，裁剪上下文后重试")
                _trim_messages(messages)
                retry_proto = _build_request(api_key, jwt, messages, tool_defs, model)
                try:
                    resp_data = _streaming_request(retry_proto, timeout_ms=timeout_ms)
                except Exception as retry_exc:
                    retry_err = _classify_error(retry_exc)
                    return {
                        "files": [],
                        "error": f"{retry_err.code}: {retry_err}",
                        "_meta": build_meta(
                            context_trimmed=True,
                            error_code=retry_err.code,
                        ),
                    }
            else:
                return {
                    "files": [],
                    "error": f"{err.code}: {err}",
                    "_meta": build_meta(error_code=err.code),
                }

        thinking, tool_info = _parse_response(resp_data)

        if tool_info is None:
            if thinking.startswith("[Error]"):
                return {
                    "files": [],
                    "error": thinking,
                    "_meta": build_meta(error_code=_extract_inline_error_code(thinking)),
                }
            return {
                "files": [],
                "raw_response": thinking,
                "_meta": build_meta(),
            }

        tool_name, tool_args = tool_info

        if tool_name == "answer":
            answer_xml = tool_args.get("answer", "")
            log(f"{model}: 收到最终答案")
            result = _parse_answer(answer_xml, project_root)
            result["rg_patterns"] = list(dict.fromkeys(executor.collected_rg_patterns))
            result["_meta"] = build_meta()
            return result

        if tool_name == "restricted_exec":
            call_id = str(uuid.uuid4())
            args_json = json.dumps(tool_args, ensure_ascii=False)

            cmds = [k for k in tool_args if k.startswith("command")]
            log(f"{model}: 执行 {len(cmds)} 个本地命令")

            results = executor.exec_tool_call_async(tool_args)

            messages.append({
                "role": 2, "content": thinking,
                "tool_call_id": call_id, "tool_name": "restricted_exec",
                "tool_args_json": args_json,
            })
            messages.append({"role": 4, "content": results, "ref_call_id": call_id})

            if turn >= max_turns - 1:
                messages.append({"role": 1, "content": FINAL_FORCE_ANSWER})
                log(f"{model}: 注入强制回答提示")

    return {
        "files": [],
        "error": "达到最大轮次仍未获得答案",
        "rg_patterns": list(dict.fromkeys(executor.collected_rg_patterns)),
        "_meta": build_meta(),
    }


def search(
    query: str,
    project_root: str,
    api_key: str | None = None,
    jwt: str | None = None,
    max_turns: int = 3,
    max_commands: int = 8,
    max_results: int = 10,
    tree_depth: int = 3,
    timeout_ms: int = 30000,
    exclude_paths: list[str] | None = None,
    local_context: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> Dict[str, Any]:
    """
    执行 Fast Context 搜索。

    Args:
        query: 自然语言搜索查询
        project_root: 项目根目录
        api_key: Windsurf API key（不传则自动获取）
        jwt: JWT token（不传则自动获取）
        max_turns: 搜索轮数（默认 3，与 Windsurf 原版一致）
        max_commands: 每轮最大命令数（默认 8）
        max_results: 最多返回的文件数量
        tree_depth: repo map 目标深度（1-6）
        timeout_ms: 流式请求超时
        exclude_paths: 额外排除路径
        local_context: 本地检索候选 chunk，注入远端搜索提示
        on_progress: 进度回调

    Returns:
        {"files": [...], "error": "..."} 或 {"files": [...]}
    """
    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    project_root = os.path.abspath(project_root)
    exclude_paths = exclude_paths or []

    if not api_key:
        api_key = get_api_key()
    if not jwt:
        log("获取 JWT...")
        jwt = fetch_jwt(api_key)

    tool_defs = get_tool_definitions(max_commands)
    system_prompt = build_system_prompt(max_turns, max_commands, max_results)

    repo_map, actual_depth, tree_size_bytes, fell_back = get_repo_map(
        project_root,
        tree_depth,
        exclude_paths,
    )
    log(
        f"Repo map: tree -L {actual_depth} "
        f"({tree_size_bytes / 1024:.1f}KB)"
        f"{' [fell back]' if fell_back else ''}"
    )
    local_anchor_brief = build_local_anchor_brief(
        query,
        project_root,
        _effective_excludes(exclude_paths),
    )
    user_content = "\n\n".join([
        f"Problem Statement: {query}",
        local_context or "",
        local_anchor_brief,
        f"Repo Map (tree -L {actual_depth} /codebase):\n```text\n{repo_map}\n```",
    ]).strip()

    model_candidates = _resolve_model_candidates()
    last_result: Dict[str, Any] | None = None

    for index, model in enumerate(model_candidates, 1):
        log(f"检查模型 {model} ({index}/{len(model_candidates)})")
        if not check_rate_limit(api_key, jwt, model):
            limited_result = {
                "files": [],
                "error": "触发限流，请稍后再试",
                "_meta": {
                    "tree_depth": actual_depth,
                    "tree_size_kb": round(tree_size_bytes / 1024, 1),
                    "fell_back": fell_back,
                    "project_root": project_root,
                    "error_code": "RATE_LIMITED",
                },
            }
            last_result = _attach_model_metadata(
                limited_result,
                model=model,
                attempted_models=model_candidates[:index],
            )
            if index < len(model_candidates):
                log(f"{model} 被限流，切换到备用模型")
                continue
            return last_result

        result = _search_once(
            query=query,
            project_root=project_root,
            api_key=api_key,
            jwt=jwt,
            model=model,
            system_prompt=system_prompt,
            tool_defs=tool_defs,
            user_content=user_content,
            max_turns=max_turns,
            timeout_ms=timeout_ms,
            actual_depth=actual_depth,
            tree_size_bytes=tree_size_bytes,
            fell_back=fell_back,
            on_progress=on_progress,
        )
        result = _attach_model_metadata(
            result,
            model=model,
            attempted_models=model_candidates[:index],
        )
        last_result = result

        error_code = None
        if result.get("error"):
            meta = result.get("_meta") or {}
            error_code = meta.get("error_code") or _extract_inline_error_code(result["error"])

        if result.get("error") and _should_try_next_model(error_code) and index < len(model_candidates):
            log(f"{model} 返回 {error_code}，切换到备用模型")
            continue

        return result

    return last_result or {
        "files": [],
        "error": "没有可用的模型候选",
        "_meta": {
            "tree_depth": actual_depth,
            "tree_size_kb": round(tree_size_bytes / 1024, 1),
            "fell_back": fell_back,
            "project_root": project_root,
        },
    }


def _parse_answer(xml_text: str, project_root: str) -> Dict[str, Any]:
    files = []
    resolved_root = os.path.abspath(project_root)
    for fm in re.finditer(r'<file\s+path="([^"]+)">(.*?)</file>', xml_text, re.DOTALL):
        vpath = fm.group(1)
        rel = vpath.replace("/codebase/", "").replace("/codebase", "").lstrip("/\\")
        full = os.path.abspath(os.path.join(project_root, rel))
        try:
            common = os.path.commonpath([resolved_root, full])
        except ValueError:
            continue
        if common != resolved_root:
            continue
        ranges = [(int(s), int(e)) for s, e in re.findall(r"<range>(\d+)-(\d+)</range>", fm.group(2))]
        files.append({"path": rel, "full_path": full, "ranges": ranges})
    return {"files": files}


def _truncate_display(text: str, limit: int = 96) -> str:
    compact = re.sub(r"\s+", " ", text.strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _clean_comment_text(text: str) -> str:
    stripped = text.strip()
    for prefix in COMMENT_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :].strip()
    stripped = stripped.removeprefix('"""').removeprefix("'''").strip()
    stripped = stripped.removesuffix('"""').removesuffix("'''").strip()
    return _truncate_display(stripped)


def _coalesce_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 2:
            merged[-1] = (prev_start, max(prev_end, end))
            continue
        merged.append((start, end))
    return merged


def _find_symbol_label(lines: list[str], start: int, end: int) -> tuple[str | None, int | None]:
    lower = max(0, start)
    upper = min(len(lines), end + 1)
    for idx in range(lower, upper):
        line = lines[idx]
        for pattern, formatter in SYMBOL_PATTERNS:
            match = pattern.match(line)
            if match:
                return formatter(match), idx
    return None, None


def _extract_doc_or_comment(
    lines: list[str],
    start: int,
    end: int,
    symbol_idx: int | None,
) -> str | None:
    if symbol_idx is None:
        return None

    search_end = min(len(lines), end + 1)
    for idx in range(symbol_idx + 1, min(search_end, symbol_idx + 6)):
        stripped = lines[idx].strip()
        if not stripped:
            continue
        if stripped.startswith(('"""', "'''")):
            if len(stripped) > 6 and stripped.endswith(('"""', "'''")):
                return _clean_comment_text(stripped)
            block = [_clean_comment_text(stripped)]
            for follow in range(idx + 1, min(search_end, idx + 5)):
                next_stripped = lines[follow].strip()
                if not next_stripped:
                    continue
                block.append(_clean_comment_text(next_stripped))
                if next_stripped.endswith(('"""', "'''")):
                    break
            block = [item for item in block if item]
            return _truncate_display(" ".join(block)) if block else None
        if any(stripped.startswith(prefix) for prefix in COMMENT_PREFIXES):
            return _clean_comment_text(stripped)
        break

    comment_lines: list[str] = []
    for idx in range(symbol_idx - 1, max(start - 1, symbol_idx - 4), -1):
        stripped = lines[idx].strip()
        if not stripped:
            if comment_lines:
                break
            continue
        if any(stripped.startswith(prefix) for prefix in COMMENT_PREFIXES):
            comment_lines.append(_clean_comment_text(stripped))
            continue
        break
    if comment_lines:
        comment_lines.reverse()
        return _truncate_display(" ".join(part for part in comment_lines if part))
    return None


def _match_query_terms(text: str, query_terms: list[str], limit: int = 3) -> list[str]:
    lowered = text.lower()
    matches: list[str] = []
    for term in query_terms:
        normalized = term.lower()
        if len(normalized) < 4 or normalized in GENERIC_GREP_PATTERNS:
            continue
        if normalized in lowered:
            matches.append(term)
        if len(matches) >= limit:
            break
    return matches


def _find_anchor_text(lines: list[str], start: int, end: int, symbol_idx: int | None) -> str | None:
    if symbol_idx is not None and 0 <= symbol_idx < len(lines):
        return _truncate_display(lines[symbol_idx])
    for idx in range(max(0, start), min(len(lines), end + 1)):
        stripped = lines[idx].strip()
        if stripped:
            return _truncate_display(stripped)
    return None


def _summarize_range(
    lines: list[str],
    start: int,
    end: int,
    query_terms: list[str],
    verbose: bool,
) -> tuple[str, str | None]:
    start_idx = max(0, start - 1)
    end_idx = max(start_idx, min(len(lines) - 1, end - 1)) if lines else start_idx
    symbol_label, symbol_idx = _find_symbol_label(lines, start_idx, end_idx)
    anchor = _find_anchor_text(lines, start_idx, end_idx, symbol_idx)
    reason = _extract_doc_or_comment(lines, start_idx, end_idx, symbol_idx)
    if not reason:
        block_text = "".join(lines[start_idx : end_idx + 1])
        matches = _match_query_terms(block_text, query_terms)
        if matches:
            reason = f"matches: {', '.join(matches)}"

    title = symbol_label or anchor or "selected semantic block"
    line = f"L{start}-{end}: {title}"
    if reason:
        line += f" - {reason}"

    if verbose and anchor:
        return line, anchor
    return line, None


def _filter_signal_patterns(patterns: list[str], query_terms: list[str]) -> list[str]:
    query_term_set = {term.lower() for term in query_terms}
    kept: list[str] = []
    for pattern in patterns:
        text = pattern.strip()
        lowered = text.lower()
        if len(text) < 3 or lowered in GENERIC_GREP_PATTERNS:
            continue
        has_structure = (
            bool(re.search(r"[A-Z_]", text))
            or any(ch in text for ch in "./:-")
            or bool(re.search(r"[\[\](){}*+?|\\]", text))
            or any(ch.isdigit() for ch in text)
        )
        if has_structure or (lowered in query_term_set and len(lowered) >= 6):
            kept.append(text)
    return list(dict.fromkeys(kept))


def _format_success_output(
    files: list[dict[str, Any]],
    query: str,
    rg_patterns: list[str],
    meta: dict[str, Any],
    raw_response: str,
    max_turns: int,
    max_results: int,
    max_commands: int,
    timeout_ms: int,
    exclude_paths: list[str] | None,
    verbose: bool,
) -> str:
    query_terms = _extract_query_terms(query, max_terms=12)
    signal_patterns = _filter_signal_patterns(rg_patterns, query_terms)
    parts: list[str] = []

    if files:
        parts.append("Start here:")
        parts.append("")
        for index, entry in enumerate(files, 1):
            parts.append(f"{index}. {entry['full_path']}")
            try:
                with open(entry["full_path"], "r", encoding="utf-8", errors="replace") as handle:
                    lines = handle.readlines()
            except OSError:
                lines = []
            for start, end in _coalesce_ranges(entry["ranges"]):
                summary, anchor = _summarize_range(lines, start, end, query_terms, verbose)
                parts.append(f"   - {summary}")
                if anchor:
                    parts.append(f"     anchor: {anchor}")
            parts.append("")
    elif signal_patterns:
        parts.append("No direct file matches found.")
        parts.append("")
    else:
        return f"No relevant files found.\n\nRaw response:\n{raw_response}" if raw_response else "No relevant files found."

    if signal_patterns:
        parts.append("Follow-up search terms:")
        parts.append(", ".join(signal_patterns))
        parts.append("")

    if verbose and meta:
        config_line = (
            f"[config] tree_depth={meta.get('tree_depth')}, "
            f"tree_size={meta.get('tree_size_kb')}KB, "
            f"max_turns={max_turns}, max_results={max_results}, "
            f"max_commands={max_commands}, timeout_ms={timeout_ms}"
        )
        if meta.get("model"):
            config_line += f", model={meta['model']}"
        attempts = meta.get("model_attempts") or []
        if len(attempts) > 1:
            config_line += f", model_attempts={' -> '.join(attempts)}"
        if meta.get("fell_back"):
            config_line += " (fell back from requested depth)"
        if exclude_paths:
            config_line += f", exclude_paths=[{', '.join(exclude_paths)}]"
        parts.append(config_line)

    while parts and not parts[-1]:
        parts.pop()
    return "\n".join(parts)


def _chunk_title_and_snippet(content: str) -> tuple[str, str | None]:
    lines = content.splitlines()
    symbol_label, symbol_idx = _find_symbol_label([line + "\n" for line in lines], 0, len(lines) - 1)
    if symbol_label:
        start = max(0, symbol_idx or 0)
        snippet_lines = [line.strip() for line in lines[start : start + 3] if line.strip()]
        return symbol_label, _truncate_display(" ".join(snippet_lines), 160) if snippet_lines else None
    for line in lines:
        stripped = line.strip()
        if stripped:
            return _truncate_display(stripped), _truncate_display(stripped, 160)
    return "selected local chunk", None


def _format_semble_output(
    payload: dict[str, Any],
    project_root: str,
    verbose: bool = False,
    heading: str = "Local Semble results:",
) -> str:
    results = payload.get("results") or []
    if not results:
        return "No local Semble chunks found."

    root = Path(project_root).expanduser().resolve()
    parts = [heading, ""]
    for index, item in enumerate(results, 1):
        chunk = item.get("chunk") or {}
        rel_path = chunk.get("file_path") or ""
        full_path = root / rel_path if rel_path else root
        start = chunk.get("start_line")
        end = chunk.get("end_line")
        score = item.get("score")
        title, snippet = _chunk_title_and_snippet(chunk.get("content") or "")
        score_text = f", score={float(score):.4f}" if isinstance(score, (int, float)) else ""
        parts.append(f"{index}. {full_path}")
        parts.append(f"   - L{start}-{end}: {title}{score_text}")
        if snippet:
            parts.append(f"     snippet: {snippet}")
        parts.append("")

    meta = payload.get("_meta") or {}
    if verbose and meta:
        runner = meta.get("runner", "unknown")
        cache_status = meta.get("cache")
        detail = f"[local] backend=semble, runner={runner}"
        if cache_status:
            detail += f", cache={cache_status}"
        parts.append(detail)

    while parts and not parts[-1]:
        parts.pop()
    return "\n".join(parts)


def _limit_semble_payload(payload: dict[str, Any], limit: int) -> dict[str, Any]:
    limited = dict(payload)
    limited["results"] = list(payload.get("results") or [])[:limit]
    return limited


def _format_semble_prompt_context(
    payload: dict[str, Any],
    max_chunks: int = 5,
    max_chars: int = 5000,
) -> str:
    results = payload.get("results") or []
    if not results:
        return ""

    lines = [
        "Local Semble chunk candidates:",
        "Use these local chunk hits as retrieval hints. Verify them with restricted tools before finalizing.",
    ]
    used = sum(len(line) + 1 for line in lines)
    for item in results[:max_chunks]:
        chunk = item.get("chunk") or {}
        path = chunk.get("file_path") or ""
        start = chunk.get("start_line")
        end = chunk.get("end_line")
        score = item.get("score")
        score_text = f" score={float(score):.4f}" if isinstance(score, (int, float)) else ""
        content = _truncate_display(chunk.get("content") or "", 900)
        block = [
            f"- /codebase/{path}:L{start}-{end}{score_text}",
            f"  {content}",
        ]
        block_size = sum(len(line) + 1 for line in block)
        if used + block_size > max_chars:
            break
        lines.extend(block)
        used += block_size
    return "\n".join(lines)


def local_search_with_content(
    query: str,
    project_root: str,
    max_results: int = 10,
    content_types: list[str] | None = None,
    verbose: bool = False,
) -> str:
    try:
        payload = semble_search(
            query=query,
            project_root=project_root,
            top_k=max_results,
            content=content_types,
        )
    except SembleUnavailable as exc:
        return f"Error: local Semble unavailable: {exc}"
    return _format_semble_output(payload, project_root, verbose=verbose)


def find_related_with_content(
    file_path: str,
    line: int,
    project_root: str,
    max_results: int = 10,
    content_types: list[str] | None = None,
    verbose: bool = False,
) -> str:
    try:
        payload = semble_find_related(
            file_path=file_path,
            line=line,
            project_root=project_root,
            top_k=max_results,
            content=content_types,
        )
    except SembleUnavailable as exc:
        return f"Error: local Semble unavailable: {exc}"
    return _format_semble_output(
        payload,
        project_root,
        verbose=verbose,
        heading=f"Related local Semble chunks for {file_path}:{line}:",
    )


def _format_error_result(
    result: dict[str, Any],
    max_turns: int,
    max_results: int,
    max_commands: int,
    timeout_ms: int,
) -> str:
    message = f"Error: {result['error']}"
    meta = result.get("_meta") or {}
    if meta:
        message += (
            f"\n\n[diagnostic] tree_depth={meta.get('tree_depth')}, "
            f"tree_size={meta.get('tree_size_kb')}KB"
        )
        if meta.get("model"):
            message += f", model={meta['model']}"
        attempts = meta.get("model_attempts") or []
        if len(attempts) > 1:
            message += f", model_attempts={' -> '.join(attempts)}"
        if meta.get("fell_back"):
            message += " (auto fell back)"
        if meta.get("context_trimmed"):
            message += ", context_trimmed=true"
        if meta.get("error_code"):
            message += f", error_type={meta['error_code']}"
        message += (
            f"\n[config] max_turns={max_turns}, max_results={max_results}, "
            f"max_commands={max_commands}, timeout_ms={timeout_ms}"
        )
    if "AUTH_ERROR" in result["error"]:
        message += "\n[hint] Windsurf 凭证可能已过期，重新提取后设置 WINDSURF_API_KEY 再试。"
    elif "PAYLOAD_TOO_LARGE" in result["error"] or "TIMEOUT" in result["error"]:
        message += "\n[hint] 尝试降低 tree_depth、缩小 project_root，或增加 exclude_paths。"
    elif "resource_exhausted" in result["error"].lower():
        message += "\n[hint] 已自动尝试备用模型；如果仍失败，可稍后重试或设置 WS_FALLBACK_MODELS 扩展候选链。"
    return message


def search_with_content(
    query: str,
    project_root: str,
    api_key: str | None = None,
    max_turns: int = 3,
    max_commands: int = 8,
    max_results: int = 10,
    tree_depth: int = 3,
    timeout_ms: int = 30000,
    exclude_paths: list[str] | None = None,
    verbose: bool = False,
    backend: str = "hybrid",
    content_types: list[str] | None = None,
) -> str:
    """搜索并返回适合 CLI / skill 的格式化结果。"""
    if backend not in {"hybrid", "remote", "local"}:
        raise ValueError(f"unknown backend: {backend}")

    if backend == "local":
        return local_search_with_content(
            query=query,
            project_root=project_root,
            max_results=max_results,
            content_types=content_types,
            verbose=verbose,
        )

    local_payload: dict[str, Any] | None = None
    local_context = ""
    local_error = ""
    if backend == "hybrid":
        try:
            local_payload = semble_search(
                query=query,
                project_root=project_root,
                top_k=max_results,
                content=content_types,
            )
            local_context = _format_semble_prompt_context(
                local_payload,
                max_chunks=min(max_results, 5),
            )
        except SembleUnavailable as exc:
            local_error = str(exc)

    try:
        result = search(
            query=query, project_root=project_root,
            api_key=api_key,
            max_turns=max_turns,
            max_commands=max_commands,
            max_results=max_results,
            tree_depth=tree_depth,
            timeout_ms=timeout_ms,
            exclude_paths=exclude_paths,
            local_context=local_context,
        )
    except Exception as exc:
        if backend == "hybrid" and local_payload:
            local_output = _format_semble_output(local_payload, project_root, verbose=verbose)
            return f"Remote search unavailable: {exc}\nUsing local Semble results.\n\n{local_output}"
        if backend == "hybrid" and local_error:
            return f"Error: {exc}\n[local Semble unavailable] {local_error}"
        return f"Error: {exc}"

    if result.get("error"):
        remote_error = _format_error_result(
            result,
            max_turns=max_turns,
            max_results=max_results,
            max_commands=max_commands,
            timeout_ms=timeout_ms,
        )
        if backend == "hybrid" and local_payload:
            local_output = _format_semble_output(local_payload, project_root, verbose=verbose)
            return f"{remote_error}\n\nUsing local Semble results.\n\n{local_output}"
        if backend == "hybrid" and local_error:
            return f"{remote_error}\n[local Semble unavailable] {local_error}"
        return remote_error

    files = result.get("files", [])
    rg_patterns = result.get("rg_patterns", [])
    meta = result.get("_meta") or {}
    remote_output = _format_success_output(
        files=files,
        query=query,
        rg_patterns=rg_patterns,
        meta=meta,
        raw_response=result.get("raw_response", ""),
        max_turns=max_turns,
        max_results=max_results,
        max_commands=max_commands,
        timeout_ms=timeout_ms,
        exclude_paths=exclude_paths,
        verbose=verbose,
    )
    if backend == "hybrid" and local_payload and (local_payload.get("results") or []):
        local_output = _format_semble_output(
            _limit_semble_payload(local_payload, min(max_results, 5)),
            project_root,
            verbose=verbose,
            heading="Local Semble chunk candidates:",
        )
        return f"{remote_output}\n\n{local_output}"
    if backend == "hybrid" and local_error and verbose:
        return f"{remote_output}\n\n[local Semble unavailable] {local_error}"
    return remote_output


def extract_key_info() -> dict:
    """提取 Windsurf API Key 信息（供本地 CLI / skill 使用）。"""
    from extract_key import extract_key
    return extract_key()
