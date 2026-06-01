# Fast Context Skill

[中文说明](README.zh-CN.md)

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

## Hybrid pipeline

```text
User query
  -> Semble local prefetch
     -> cached index + potion-code-16M chunks
  -> fast-context prompt
     -> original query + Semble chunk hints + lexical anchors + repo map
  -> Windsurf remote search
     -> verify hints with rg/readfile/tree/ls/glob and expand related files
  -> Start here output
     -> files, line ranges, follow-up search terms, local chunk candidates

Remote failure path:
  Windsurf auth/rate-limit/timeout/resource_exhausted
    -> return local Semble chunk results instead of an empty failure
```

## Files

```text
fast-context/
├── benchmarks/
│   ├── data.py               # Semble benchmark subset loader and pinned repo checks
│   ├── metrics.py            # File-level retrieval metrics and bootstrap CIs
│   └── run_retrieval_benchmark.py   # Paced local/remote/hybrid benchmark runner
├── assets/
│   └── images/
│       └── retrieval_benchmark_speed_vs_quality.svg
├── src/
│   ├── core.py               # Protocol, search loop, repo map, local lexical anchors
│   ├── extract_key.py        # Windsurf credential extraction from state.vscdb
│   ├── local_semble.py       # Local Semble adapter
│   └── fast_context_cli.py   # CLI entrypoint for the skill
├── SKILL.md              # Skill instructions
├── pyproject.toml
└── uv.lock
```

## Requirements

- Python 3.10 through 3.13 (`>=3.10,<3.14`)
- `uv`
- A Windsurf login on the same machine, or `WINDSURF_API_KEY`
- Semble for local chunk search. `uv sync` installs it as a normal runtime dependency.
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

## Prompt snippet for code agents

Use this when your coding agent needs fast repo orientation before editing, review, or debugging:

```text
Use the installed `fast-context` skill for intent-based or open-ended codebase search when the exact path or symbol is not known yet.

Run:
python "$HOME/.agents/skills/fast-context/src/fast_context_cli.py" search \
  --query "<natural language query>" \
  --project "<repo-root>"

Notes:
- Prefer `fast-context` before `rg` for vague questions: debugging explorations, "where is X?", flow tracing, or feature-oriented repo navigation.
- If the exact filename, path, or symbol is already known, use `rg` or open the file directly instead of starting with `fast-context`.
- Treat `fast-context` as a candidate-file generator, not a proof source. After it returns results, read the relevant files and use exact search only to confirm names, events, tests, or call sites.
- Split unrelated questions into separate `fast-context` queries. Long natural-language queries are fine when they describe one workflow, but multi-topic queries can drop weaker subtopics.
- Do not treat "results found" as evidence that a feature exists. For negative or fictional queries, `fast-context` may still return approximate matches; verify existence from the code before concluding.
- Prefer queries that describe behavior and data flow, not just nouns: include user action, runtime boundary, expected effect, and any known payload fields.
- If remote Windsurf search fails, use the returned local Semble results to keep moving.
```

## CLI

### Search

```bash
uv run fast-context search \
  --query "where is the desktop browser login handoff state validated" \
  --project .
```

Useful options:

- `--backend hybrid|remote|local` (`hybrid` is default)
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

### Semble cache management

Clear the cache for one project:

```bash
uv run fast-context cache-clear --project .
```

Garbage-collect stale Semble cache entries whose indexed `root_path` no longer exists:

```bash
uv run fast-context cache-gc
```

Preview without deleting:

```bash
uv run fast-context cache-gc --dry-run
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

## Model choice

Local testing on `2026-05-31` suggests these practical defaults:

- `MODEL_SWE_1_6_FAST` is the best default for individual day-to-day coding use and one-off repo lookups.
- This repo now automatically falls back to `MODEL_SWE_1_5` when the primary model hits `resource_exhausted` or model-specific rate limiting.
- If you want a different fallback order, set `WS_FALLBACK_MODELS`, for example `WS_FALLBACK_MODELS=MODEL_SWE_1_5,MODEL_SWE_1_6`.
- `MODEL_SWE_1_7_FAST` is currently not recommended.

These results are empirical rather than guaranteed. Upstream capacity variance can affect both latency and success rate.

## Retrieval benchmark

The original 12-query smoke test is replaced by a full 40-query run over the two Semble benchmark repos already synced locally on this machine:

- `fastapi` at `c3c9dd6b1a08` (`benchmark_root=fastapi`)
- `axios` at `c7a76ddbf277` (`benchmark_root=lib`)
- 40 labeled queries total: 12 `architecture`, 17 `semantic`, 11 `symbol`

The benchmark runner is [`benchmarks/run_retrieval_benchmark.py`](benchmarks/run_retrieval_benchmark.py). By default it mirrors Semble's protocol where that still fits this repo:

- reuse Semble annotation JSON as ground truth
- enforce pinned repo revisions before the run starts
- score all backends against the same file-level relevance targets
  - file-level scoring is deliberate: `remote` returns file/range hits, while `local` returns chunks
- warm the local Semble cache once per repo, then measure query latency
- run `remote` and `hybrid` sequentially with a completion-based cooldown
  - current fair defaults are `remote_cooldown_ms=10000`, `remote_jitter_ms=2000`, `retry_base_ms=15000`, `retry_max_ms=60000`, `max_retries=4`
  - cooldown is measured after each remote attempt completes, not just between request start times
- alternate `remote` / `hybrid` order per query to reduce order bias
- compute 95% bootstrap confidence intervals from per-query metrics

Reproduce the run and regenerate the chart:

```bash
uv run python -m benchmarks.run_retrieval_benchmark \
  --clear-local-cache \
  --remote-cooldown-ms 10000 \
  --remote-jitter-ms 2000 \
  --retry-base-ms 15000 \
  --retry-max-ms 60000 \
  --max-retries 4 \
  --output benchmarks/results/retrieval-fastapi-axios-2026-06-01.json \
  --plot assets/images/retrieval_benchmark_speed_vs_quality.svg
```

The benchmark script looks for a sibling `../semble/benchmarks` checkout by default. Override with `SEMBLE_BENCHMARK_ROOT=/path/to/semble/benchmarks` when needed.

Artifacts from the run:

- JSON summary and per-query traces: [`benchmarks/results/retrieval-fastapi-axios-2026-06-01.json`](benchmarks/results/retrieval-fastapi-axios-2026-06-01.json)
- Speed-vs-quality chart: [`assets/images/retrieval_benchmark_speed_vs_quality.svg`](assets/images/retrieval_benchmark_speed_vs_quality.svg)

![Retrieval benchmark speed vs quality](assets/images/retrieval_benchmark_speed_vs_quality.svg)

### Important note on fairness

The `2026-06-01` numbers below were collected before the runner switched to completion-based cooldown. That older runner only enforced a start-gap, which meant any `~5s` remote call effectively launched the next one immediately after completion and could over-stress Windsurf during long batches. Treat the published `remote` / `hybrid` rows as an operational stress run, not the final apples-to-apples backend comparison.

The current runner defaults are intentionally slower and fairer. Use them for any future published benchmark refresh.

### Quality summary

| Backend | NDCG@10 | 95% CI | Recall@10 | 95% CI | Top-1 | MRR |
|---|---:|---:|---:|---:|---:|---:|
| `local` | 0.854 | 0.774-0.926 | 0.946 | 0.875-1.000 | 0.775 | 0.850 |
| `remote` | 0.453 | 0.309-0.604 | 0.467 | 0.312-0.617 | 0.450 | 0.475 |
| `hybrid` | 0.890 | 0.835-0.939 | 0.979 | 0.946-1.000 | 0.825 | 0.896 |

### Operational summary

| Backend | Batch p50 latency | Batch p90 latency | Final non-empty output | Remote success | `resource_exhausted` / degraded | Total retries |
|---|---:|---:|---:|---:|---:|---:|
| `local` | 30 ms | 39 ms | 100% | n/a | 0 | 0 |
| `remote` | 24.4 s | 37.5 s | 50% | 52.5% | 19 | 43 |
| `hybrid` | 28.3 s | 40.0 s | 100% | 50.0% | 20 degraded | 44 |

Warm local cache build cost, measured separately before timing queries:

- `fastapi`: 422 ms
- `axios`: 65 ms

### By category

NDCG@10 by query category:

| Category | `local` | `remote` | `hybrid` |
|---|---:|---:|---:|
| `architecture` | 0.718 | 0.506 | 0.819 |
| `semantic` | 0.855 | 0.473 | 0.869 |
| `symbol` | 1.000 | 0.364 | 1.000 |

### Interpretation

- `local` is the throughput baseline: warm-cache p50 is `30 ms`, quality is already strong (`0.854` NDCG@10 / `0.946` recall@10), and the run had zero failures.
- `local` is the stable throughput baseline and remains the safest choice for bulk evals, CI, and low-latency repo search.
- The old `remote` / `hybrid` rows surfaced a runner bug more than a backend-quality truth: a start-gap alone was not enough to prevent long-batch upstream throttling.
- The fair runner now uses completion-based cooldown plus capped retry windows, so the next published `remote` / `hybrid` numbers should be regenerated with the current defaults instead of compared directly against the stress-run table above.
- In day-to-day usage, `hybrid` is still the right interactive default when you want Windsurf verification on top of local Semble hints. Just do not treat the older degraded batch result as its steady-state quality ceiling.

## Skill usage

The intended usage is through `SKILL.md`, but the CLI is also fine for direct local runs and quick repo checks.

Typical flow:

1. Run Fast Context with a natural-language query. Default `--backend hybrid` prefetches local Semble chunks, then uses Windsurf to verify and expand.
2. Read the returned files.
3. Use `--backend local` for bulk runs, CI, and low-latency repo search without any Windsurf dependency.
4. Use `--backend remote` only when you want to isolate Windsurf behavior without local chunk hints.
5. Use `find-related` to follow a promising local chunk to similar code.
6. Confirm exact call sites or symbols with `rg` or `ast-grep`.

## Notes

- Local lexical anchors are generic. They bias toward exact filenames, path segments, and literal content hits from the query.
- Repo maps shrink automatically when the tree gets too large.
- If the remote call times out or the payload is too large, the search loop trims old context and retries once.
- Fast Context calls the Semble Python library directly, saves fresh local indexes into Semble's cache, and lets Semble invalidate that cache when indexed files change.
- Semble chunk hits are candidate evidence, not proof. Hybrid mode asks Windsurf to verify them before producing the main `Start here` output.
- Successful output stays concise by default. Use `--verbose` when you want anchor snippets and config diagnostics.

## License

MIT
