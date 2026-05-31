#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core import find_related_with_content, local_search_with_content, search_with_content
from extract_key import extract_key
from local_semble import clear_project_cache, gc_stale_caches


def _format_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f}{unit}" if unit != "B" else f"{int(amount)}B"
        amount /= 1024
    return f"{amount:.1f}GB"


def _add_local_content_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--content",
        nargs="+",
        default=["code"],
        choices=["code", "docs", "config", "all"],
        help="Content types for local Semble search.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fast-context",
        description="Fast repository context search powered by Windsurf.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="Run semantic repo search")
    search_parser.add_argument("--query", required=True, help="Natural language query")
    search_parser.add_argument("--project", default=".", help="Project root directory")
    search_parser.add_argument("--tree-depth", type=int, default=3, help="Repo tree depth (1-6)")
    search_parser.add_argument("--max-turns", type=int, default=3, help="Search rounds")
    search_parser.add_argument("--max-results", type=int, default=10, help="Max files to return")
    search_parser.add_argument("--timeout-ms", type=int, default=30000, help="Streaming timeout in ms")
    search_parser.add_argument(
        "--backend",
        choices=["hybrid", "remote", "local"],
        default="hybrid",
        help=(
            "Search backend. hybrid prefetches local Semble chunks, injects them "
            "into Windsurf search, and uses local results if remote search fails."
        ),
    )
    _add_local_content_args(search_parser)
    search_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show anchor snippets and diagnostic config in successful output.",
    )
    search_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Extra exclude path or glob. Repeatable.",
    )

    local_parser = subparsers.add_parser("local-search", help="Run local Semble chunk search")
    local_parser.add_argument("--query", required=True, help="Natural language query")
    local_parser.add_argument("--project", default=".", help="Project root directory")
    local_parser.add_argument("--max-results", type=int, default=10, help="Max chunks to return")
    local_parser.add_argument("--verbose", action="store_true", help="Show local backend diagnostics.")
    _add_local_content_args(local_parser)

    related_parser = subparsers.add_parser("find-related", help="Find local chunks related to a file and line")
    related_parser.add_argument("--file", required=True, help="Repo-relative file path from a search result")
    related_parser.add_argument("--line", required=True, type=int, help="Line number inside the source chunk")
    related_parser.add_argument("--project", default=".", help="Project root directory")
    related_parser.add_argument("--max-results", type=int, default=10, help="Max chunks to return")
    related_parser.add_argument("--verbose", action="store_true", help="Show local backend diagnostics.")
    _add_local_content_args(related_parser)

    extract_parser = subparsers.add_parser("extract-key", help="Extract Windsurf credential")
    extract_parser.add_argument("--db-path", help="Path to a copied state.vscdb")

    cache_clear_parser = subparsers.add_parser("cache-clear", help="Clear Semble cache for a project")
    cache_clear_parser.add_argument("--project", default=".", help="Project root directory")
    cache_clear_parser.add_argument("--dry-run", action="store_true", help="Show what would be removed")

    cache_gc_parser = subparsers.add_parser("cache-gc", help="Remove stale Semble cache entries")
    cache_gc_parser.add_argument("--dry-run", action="store_true", help="Show what would be removed")
    cache_gc_parser.add_argument(
        "--min-age-days",
        type=float,
        default=0.0,
        help="Only remove stale entries at least this old. Default: 0",
    )

    return parser


def run_search(args: argparse.Namespace) -> int:
    project_root = str(Path(args.project).expanduser().resolve())
    result = search_with_content(
        query=args.query,
        project_root=project_root,
        max_turns=args.max_turns,
        max_results=args.max_results,
        tree_depth=args.tree_depth,
        timeout_ms=args.timeout_ms,
        exclude_paths=args.exclude,
        verbose=args.verbose,
        backend=args.backend,
        content_types=args.content,
    )
    print(result)
    return 0


def run_local_search(args: argparse.Namespace) -> int:
    project_root = str(Path(args.project).expanduser().resolve())
    result = local_search_with_content(
        query=args.query,
        project_root=project_root,
        max_results=args.max_results,
        content_types=args.content,
        verbose=args.verbose,
    )
    print(result)
    return 0


def run_find_related(args: argparse.Namespace) -> int:
    project_root = str(Path(args.project).expanduser().resolve())
    result = find_related_with_content(
        file_path=args.file,
        line=args.line,
        project_root=project_root,
        max_results=args.max_results,
        content_types=args.content,
        verbose=args.verbose,
    )
    print(result)
    return 0


def run_extract_key(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path).expanduser().resolve() if args.db_path else None
    result = extract_key(db_path)
    if "error" in result:
        print(f"Error: {result['error']}", file=sys.stderr)
        if result.get("hint"):
            print(result["hint"], file=sys.stderr)
        if result.get("db_path"):
            print(f"DB path: {result['db_path']}", file=sys.stderr)
        return 1

    api_key = result["api_key"]
    fmt = api_key.split("$", 1)[0] if "$" in api_key else "api-key"
    print("Windsurf credential extracted successfully")
    print()
    print(f"Format: {fmt}")
    print(f"Key: {api_key}")
    print(f"Length: {len(api_key)}")
    print(f"Source: {result['db_path']}")
    print()
    print(f'Usage: export WINDSURF_API_KEY="{api_key}"')
    return 0


def run_cache_clear(args: argparse.Namespace) -> int:
    result = clear_project_cache(args.project, dry_run=args.dry_run)
    action = "Would remove" if args.dry_run else "Removed"
    if not result["existed"]:
        print(f"No Semble cache found for {result['project_root']}")
        print(f"Cache path: {result['cache_path']}")
        return 0

    print(f"{action} Semble cache for {result['project_root']}")
    print(f"Cache path: {result['cache_path']}")
    print(f"Size: {_format_bytes(result['bytes'])}")
    return 0


def run_cache_gc(args: argparse.Namespace) -> int:
    result = gc_stale_caches(dry_run=args.dry_run, min_age_days=args.min_age_days)
    action = "Would remove" if args.dry_run else "Removed"
    print(
        f"{action} {result['removed_count']} stale Semble cache entr"
        f"{'y' if result['removed_count'] == 1 else 'ies'} "
        f"({_format_bytes(result['removed_bytes'])})"
    )
    print(f"Cache root: {result['cache_root']}")
    for entry in result["entries"]:
        root = entry["root_path"] or "<unknown>"
        print(f"- {entry['reason']}: {entry['cache_path']} ({_format_bytes(entry['bytes'])})")
        print(f"  root: {root}")
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "search":
        return run_search(args)
    if args.command == "local-search":
        return run_local_search(args)
    if args.command == "find-related":
        return run_find_related(args)
    if args.command == "extract-key":
        return run_extract_key(args)
    if args.command == "cache-clear":
        return run_cache_clear(args)
    if args.command == "cache-gc":
        return run_cache_gc(args)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
