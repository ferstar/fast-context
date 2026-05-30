#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core import search_with_content
from extract_key import extract_key


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
        "--exclude",
        action="append",
        default=[],
        help="Extra exclude path or glob. Repeatable.",
    )

    extract_parser = subparsers.add_parser("extract-key", help="Extract Windsurf credential")
    extract_parser.add_argument("--db-path", help="Path to a copied state.vscdb")

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


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "search":
        return run_search(args)
    if args.command == "extract-key":
        return run_extract_key(args)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
