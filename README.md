# Fast Context Skill

Python-first fast repository context search for Codex/Claude-style skills.

This repo replaces the Node/MCP packaging with a simple Python CLI and skill workflow. It still talks to Windsurf's reverse-engineered SWE-grep backend, but now keeps the local side lean:

- zero runtime dependencies beyond Python itself
- local Windsurf credential extraction from `state.vscdb`
- local lexical anchors before the remote semantic loop
- adaptive repo-map depth to avoid oversized payloads
- skill-friendly output with candidate files, line ranges, and follow-up grep terms

## Why this shape

The high-ROI path for code search is not prompt tricks. It is a hybrid retrieval loop:

1. Keep exact lexical evidence from the local repo: filenames, paths, literal terms.
2. Give that evidence plus a compact repo map to the remote semantic search loop.
3. Let Windsurf drive `rg`, `readfile`, `tree`, `ls`, and `glob` calls.
4. Return a small set of files that are actually worth reading next.

This repo implements the local side in Python so it is easy to run in a skill without dragging in Node dependencies.

## Files

```text
fast-context/
├── src/
│   ├── core.py               # Protocol, search loop, repo map, local lexical anchors
│   ├── extract_key.py        # Windsurf credential extraction from state.vscdb
│   └── fast_context_cli.py   # CLI entrypoint for the skill
├── SKILL.md              # Skill instructions
├── pyproject.toml
└── evals/evals.json
```

## Requirements

- Python 3.10+
- A Windsurf login on the same machine, or `WINDSURF_API_KEY`
- `rg` is optional but recommended. Python fallback search is built in.

## Install

Editable install:

```bash
pip install -e .
```

Direct use without install also works:

```bash
python src/fast_context_cli.py --help
```

## CLI

### Search

```bash
python src/fast_context_cli.py search \
  --query "where is the desktop browser login handoff state validated" \
  --project .
```

Useful options:

- `--tree-depth <1-6>`
- `--max-turns <1-5>`
- `--max-results <1-30>`
- `--timeout-ms <ms>`
- `--exclude <path-or-glob>` repeatable

Example output:

```text
Found 3 relevant files.

  [1/3] /repo/apps/desktop/src/auth/session.ts (L18-102)
  [2/3] /repo/apps/desktop/src/auth/handoff.ts (L5-88)
  [3/3] /repo/apps/desktop/test/ipc-auth-boundary.integration.test.ts (L40-141)

grep keywords: handoff, applyExternalSession, state

[config] tree_depth=3, tree_size=11.8KB, max_turns=3, max_results=10, timeout_ms=30000
```

### Extract Windsurf credential

Local install:

```bash
python src/fast_context_cli.py extract-key
```

Copied database file:

```bash
python src/fast_context_cli.py extract-key --db-path /tmp/state.vscdb
```

Current Windsurf installs may store either classic API keys or session-style credentials such as `devin-session-token$...`. This repo accepts either form as long as Windsurf accepts it.

## Environment

- `WINDSURF_API_KEY`: explicit credential override
- `WS_MODEL`: optional model override. Default is `MODEL_SWE_1_6_FAST`
- `WS_APP_VER`
- `WS_LS_VER`

## Skill usage

The intended usage is through `SKILL.md`, but the CLI is also fine for direct local runs and quick repo checks.

Typical flow:

1. Run Fast Context with a natural-language query.
2. Read the returned files.
3. Confirm exact call sites or symbols with `rg` or `ast-grep`.

## Notes

- Local lexical anchors are generic. They bias toward exact filenames, path segments, and literal content hits from the query.
- Repo maps shrink automatically when the tree gets too large.
- If the remote call times out or the payload is too large, the search loop trims old context and retries once.

## License

MIT
