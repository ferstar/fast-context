# Fast Context Skill

Python-first fast repository context search for Codex/Claude-style skills.

This repo replaces the Node/MCP packaging with a simple Python CLI and skill workflow. It still talks to Windsurf's reverse-engineered SWE-grep backend, but now keeps the local side lean:

- local Semble prefetch for cached chunk candidates
- local Windsurf credential extraction from `state.vscdb`
- local lexical anchors before the remote semantic loop
- adaptive repo-map depth to avoid oversized payloads
- skill-friendly output with candidate files, line ranges, and follow-up grep terms

## Why this shape

The high-ROI path for code search is not prompt tricks. It is a hybrid retrieval loop:

1. Run Semble locally first to get warm-cache chunk candidates.
2. Keep exact lexical evidence from the local repo: filenames, paths, literal terms.
3. Give Semble candidates, lexical evidence, and a compact repo map to the remote semantic search loop.
4. Let Windsurf verify and expand with `rg`, `readfile`, `tree`, `ls`, and `glob` calls.
5. If the remote path is unavailable, degrade to local Semble chunk retrieval.
6. Return a small set of files or chunks that are actually worth reading next.

This repo implements the local side in Python so it is easy to run in a skill. Semble is used as the high-ROI local index backend for warm, cached chunk retrieval, while Windsurf remains responsible for agentic verification and call-chain expansion.

## Files

```text
fast-context/
├── src/
│   ├── core.py               # Protocol, search loop, repo map, local lexical anchors
│   ├── extract_key.py        # Windsurf credential extraction from state.vscdb
│   ├── local_semble.py       # Local Semble adapter and uvx fallback
│   └── fast_context_cli.py   # CLI entrypoint for the skill
├── SKILL.md              # Skill instructions
├── pyproject.toml
└── uv.lock
```

## Requirements

- Python 3.10 through 3.13 (`>=3.10,<3.14`)
- `uv`
- A Windsurf login on the same machine, or `WINDSURF_API_KEY`
- Semble for local chunk search. `uv sync` installs it; direct skill usage can also fall back to `uvx --from semble`.
- `rg` is optional but recommended. Python fallback search is built in.

## Install

Install dependencies into the project environment:

```bash
uv sync
```

Run the CLI through uv:

```bash
uv run fast-context --help
```

Refresh the lockfile after dependency or Python-version changes:

```bash
uv lock --default-index https://pypi.org/simple
```

## CLI

### Search

```bash
uv run fast-context search \
  --query "where is the desktop browser login handoff state validated" \
  --project .
```

Useful options:

- `--backend hybrid|remote|local|auto` (`hybrid` is default; `auto` is a backward-compatible alias)
- `--tree-depth <1-6>`
- `--max-turns <1-5>`
- `--max-results <1-30>`
- `--timeout-ms <ms>`
- `--verbose`
- `--exclude <path-or-glob>` repeatable
- `--content code|docs|config|all` for Semble prefetch and local-only search

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

`--backend hybrid` runs Semble first, injects the top local chunks into the Windsurf search prompt, then asks Windsurf to verify and expand with restricted repo tools. If the remote path fails, including auth errors, timeouts, and upstream `resource_exhausted`, the CLI still returns the local Semble chunks so the agent can keep moving.

### Local Semble search

Run cached local chunk retrieval directly:

```bash
uv run fast-context local-search \
  --query "how semantic and lexical scores are fused" \
  --project .
```

Search documentation or config:

```bash
uv run fast-context local-search \
  --query "deployment guide" \
  --project . \
  --content docs
```

Find chunks related to a prior result:

```bash
uv run fast-context find-related \
  --file src/search.py \
  --line 77 \
  --project .
```

### Extract Windsurf credential

Local install:

```bash
uv run fast-context extract-key
```

Copied database file:

```bash
uv run fast-context extract-key --db-path /tmp/state.vscdb
```

Current Windsurf installs may store either classic API keys or session-style credentials such as `devin-session-token$...`. This repo accepts either form as long as Windsurf accepts it.

## Environment

- `WINDSURF_API_KEY`: explicit credential override
- `WS_MODEL`: optional model override. Default is `MODEL_SWE_1_6_FAST`
- `WS_FALLBACK_MODELS`: optional comma-separated fallback chain. Default is `MODEL_SWE_1_5`
- `WS_APP_VER`
- `WS_LS_VER`
- `FAST_CONTEXT_SEMBLE_PYTHON`: Python version used by the `uvx --from semble` fallback. Default is `3.13`
- `FAST_CONTEXT_SEMBLE_TIMEOUT`: timeout in seconds for the `uvx --from semble` fallback. Default is `120`
- `FAST_CONTEXT_SEMBLE_UVX`: uvx executable path. Default is `uvx`

## Model choice

Local testing on `2026-05-31` suggests these practical defaults:

- `MODEL_SWE_1_6_FAST` is the best default for individual day-to-day coding use and one-off repo lookups.
- This repo now automatically falls back to `MODEL_SWE_1_5` when the primary model hits `resource_exhausted` or model-specific rate limiting.
- If you want a different fallback order, set `WS_FALLBACK_MODELS`, for example `WS_FALLBACK_MODELS=MODEL_SWE_1_5,MODEL_SWE_1_6`.
- `MODEL_SWE_1_7_FAST` is currently not recommended.

These results are empirical rather than guaranteed. Upstream capacity variance can affect both latency and success rate.

## Retrieval benchmark

Local testing on `2026-05-31` reused Semble's benchmark protocol: pinned repos, annotation JSON as ground truth, and NDCG/recall metrics from `benchmarks/metrics.py`. To keep remote Windsurf usage practical, the run sampled 12 queries from two synced Semble benchmark repos (`fastapi` and `axios`), with 2 queries per category (`architecture`, `semantic`, `symbol`) per repo.

Command settings:

- `max_results=10`
- `max_turns=2`
- `timeout_ms=30000`
- backends compared: `local`, `remote`, `hybrid`

| Backend | NDCG@10 | Recall@10 | Top-1 | MRR | p50 latency | Remote/degradation errors |
|---|---:|---:|---:|---:|---:|---:|
| `local` | 0.865 | 1.000 | 0.833 | 0.903 | 197 ms | 0 |
| `remote` | 0.630 | 0.667 | 0.667 | 0.667 | 4.87 s | 4 |
| `hybrid` | 0.895 | 1.000 | 0.833 | 0.917 | 4.05 s | 8 |

Interpretation:

- `local` is the fastest path and already has strong recall once the Semble cache is warm.
- `remote` is strong when upstream succeeds, but its end-to-end result is sensitive to auth, rate limit, and transient backend failures.
- `hybrid` was the best default in this run: Semble chunk prefetch preserved recall, while Windsurf verification/expansion improved ranking quality. When remote degraded, local chunks still kept the output usable.

This is a small operational benchmark, not a statistically complete replacement for Semble's full 1,251-query benchmark suite. It is intended to validate the fast-context integration shape and default backend choice.

## Skill usage

The intended usage is through `SKILL.md`, but the CLI is also fine for direct local runs and quick repo checks.

Typical flow:

1. Run Fast Context with a natural-language query. Default `--backend hybrid` prefetches local Semble chunks, then uses Windsurf to verify and expand.
2. Read the returned files.
3. Use `--backend remote` only when you want to isolate Windsurf behavior without local chunk hints.
4. Use `find-related` to follow a promising local chunk to similar code.
5. Confirm exact call sites or symbols with `rg` or `ast-grep`.

## Notes

- Local lexical anchors are generic. They bias toward exact filenames, path segments, and literal content hits from the query.
- Repo maps shrink automatically when the tree gets too large.
- If the remote call times out or the payload is too large, the search loop trims old context and retries once.
- Semble caches local indexes and invalidates them when files change.
- Semble chunk hits are candidate evidence, not proof. Hybrid mode asks Windsurf to verify them before producing the main `Start here` output.
- Successful output stays concise by default. Use `--verbose` when you want anchor snippets and config diagnostics.

## License

MIT
