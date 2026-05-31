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
- `--verbose`
- `--exclude <path-or-glob>` repeatable

Example output:

```text
Start here:

1. /repo/apps/desktop/src/auth/session.ts
   - L18-102: applyExternalSession() - matches: handoff, state

2. /repo/apps/desktop/src/auth/handoff.ts
   - L5-88: createAuthHandoff() - matches: handoff, desktop-launch

3. /repo/apps/desktop/test/ipc-auth-boundary.integration.test.ts
   - L40-141: rejects external-session callbacks without state - matches: state

Follow-up search terms:
applyExternalSession, createAuthHandoff, handoff.*state
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
- `WS_FALLBACK_MODELS`: optional comma-separated fallback chain. Default is `MODEL_SWE_1_5`
- `WS_APP_VER`
- `WS_LS_VER`

## Model choice

Local testing on `2026-05-31` suggests these practical defaults:

- `MODEL_SWE_1_6_FAST` is the best default for individual day-to-day coding use and one-off repo lookups.
- This repo now automatically falls back to `MODEL_SWE_1_5` when the primary model hits `resource_exhausted` or model-specific rate limiting.
- If you want a different fallback order, set `WS_FALLBACK_MODELS`, for example `WS_FALLBACK_MODELS=MODEL_SWE_1_5,MODEL_SWE_1_6`.
- `MODEL_SWE_1_7_FAST` is currently not recommended.

These results are empirical rather than guaranteed. Upstream capacity variance can affect both latency and success rate.

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
- Successful output stays concise by default. Use `--verbose` when you want anchor snippets and config diagnostics.

## License

MIT
