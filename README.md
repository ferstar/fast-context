# Fast Context Skill

[中文说明](README.zh-CN.md)

Fast Context is a lightweight codebase context locator designed for coding agents (such as Codex, Claude, and similar LLM agents).

It provides a standalone Python CLI that integrates **local Semble chunk prefetching** with **remote Windsurf (SWE-grep) symbol reasoning**. This hybrid pipeline enables agents to rapidly pinpoint relevant files and line numbers in large repositories without drowning in context.

- **Fast Local Prefetch**: Retrieves cached code chunks in milliseconds using Semble.
- **Remote Symbol Expansion**: Injects local lexical anchors and query-relevant subtrees into Windsurf to verify and expand call chains.
- **Graceful Fallback**: Automatically degrades to local Semble chunks if remote requests hit rate limits, timeouts, or missing credentials.
- **Token-Efficient Repo Maps**: Ranks directories with BM25F and applies adaptive Top-K to build a focused Hotspot Repo Map, keeping payloads small while preserving exact file recall.
- **Zero-Setup Credentials**: Automatically extracts credentials from local Windsurf / Devin databases.

## Architecture

In large codebases, naive regex searches (like `rg`) miss high-level architectural semantics, while feeding full directory trees to remote models wastes context budgets and easily triggers rate limits. Fast Context bridges both worlds with a two-stage hybrid retrieval loop:

1. **Local Semantic Prefetch**: Query Semble for warm, cached code chunk candidates.
2. **Lexical Anchoring**: Extract exact candidate filenames, path tokens, and literal terms from the local repo.
3. **Hotspot Repo Mapping**: Score directories with BM25F to construct an adaptive subtree map.
4. **Remote Verification**: Pass local chunks, anchors, and the hotspot map to Windsurf for agentic verification (`rg`, `readfile`, `tree`, `ls`, `glob`) and call-chain expansion.
5. **Fallback on Failure**: If the remote service is throttled or unavailable, return the local Semble chunks so the agent never stalls.
6. **Focused Handoff**: Deliver 3-10 high-confidence candidate files with exact line ranges and suggested follow-up grep terms.

## Retrieval Pipeline

```text
User Query
  │
  ├── 1. Local Semble Prefetch ───> Cached Code Chunks
  │
  ├── 2. Local Lexical Analysis ──> Extract Anchors & Hotspot Repo Map
  │
  └── 3. Remote Verification ─────> Windsurf Validates & Expands Call Chains
            │
            ├─ (Success) ──> Return "Start Here" Files & Line Ranges
            └─ (Failure) ──> Fall Back to Local Semble Chunks
```

## Hotspot Repo Maps

Feeding a full, deep tree into an LLM context is expensive and noisy. Fast Context dynamically generates a query-shaped map:

- **Repository Map**: A compact top-level outline preserving global structural orientation.
- **Relevant File Paths**: Exact file paths surfaced by local lexical probes, prioritized when prompt budgets shrink.
- **Hotspot Subtrees**: Directories ranked by BM25F heat and expanded with adaptive `topK`, providing granular visibility into likely feature areas.

In an A/B benchmark across 16 queries on a large private repository, hotspot maps delivered significantly higher exact-file visibility compared to traditional compact trees:

| Variant | File Recall | Deep Directory Coverage | File MRR | p50 Build Latency | Avg Map Size |
|---|---:|---:|---:|---:|---:|
| `classic` | 0.0000 | 1.0000 | 0.0000 | 10 ms | 2.4 KB |
| `hotspot` | 0.4792 | 1.0000 | 0.0184 | 120 ms | 11.9 KB |

*Note: The hotspot map trades roughly ~100 ms of local preprocessing and a slightly larger prompt payload for direct candidate-file visibility before remote verification begins.*

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
│   ├── local_repo_map.py     # BM25F directory heat, adaptive topK, path spines
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

### Extract Windsurf/Devin credential

Local install:

```bash
uv run fast-context extract-key
```

Copied database or Devin CLI credentials file:

```bash
uv run fast-context extract-key --db-path /tmp/state.vscdb
uv run fast-context extract-key --db-path ~/.local/share/devin/credentials.toml
```

Auto-discovery checks Devin CLI credentials on Linux/WSL first, then local app databases under `Deviv`, `Devin`, and `Windsurf` app data paths, probing both the canonical and lowercase directory spelling because installers disagree on casing (a Windows Devin install writes `devin`). Current installs may store either classic API keys or session-style credentials such as `devin-session-token$...`. This repo accepts either form as long as Windsurf/Devin accepts it.

Inside WSL the Linux Devin CLI credentials (`~/.local/share/devin/credentials.toml`) are what gets read; a key extracted on the Windows side can return 403 from WSL, in which case run `devin login` inside WSL and retry.

When no source is usable the error lists the paths that were searched. Credentials copied from another host can either be placed at one of those local paths so auto-discovery picks them up, or read directly by pointing `WINDSURF_CREDENTIALS_DB` at the copy.

## Environment

- `WINDSURF_API_KEY`: explicit credential override
- `WINDSURF_CREDENTIALS_DB`: explicit credentials file (`state.vscdb` or `credentials.toml`). When set, only that file is read and the local app-data paths are not probed
- `WS_MODEL`: optional model override. Default is `swe-1-7`; use `swe-1-7-lightning` to opt into Lightning
- `WS_FALLBACK_MODELS`: optional comma-separated fallback chain. Default is `MODEL_SWE_1_6_FAST,MODEL_SWE_1_5`
- `WS_REMOTE_LOCK_PATH`: optional cross-process Windsurf lock file. Defaults to a per-user path under the system temp directory
- `WS_REMOTE_LOCK_TIMEOUT_MS`: maximum wait for the shared remote slot. Default is `120000`
- `WS_REMOTE_LOCK_POLL_MS`: lock retry interval. Default is `100`
- `WS_APP_VER`
- `WS_LS_VER`

## Model choice

Remote Windsurf sessions are serialized per local user with an OS-level file lock. Local repo-map construction and Semble prefetch still run concurrently; only JWT acquisition, rate-limit checks, model retries, fallback, and the remote semantic loop share the slot. The OS releases the lock if a process exits unexpectedly. If lock waiting times out, `hybrid` degrades to its local Semble results instead of adding another remote request.

Live validation on `2026-08-09` established these practical defaults:

- Current models use string `model_uid` values. The default is `swe-1-7`; guessed numeric enum names such as `MODEL_SWE_1_7_FAST` are not used.
- `swe-1-7` and `swe-1-7-lightning` each completed 3/3 candidate-only remote probes with fallback disabled and returned results. The client cannot observe the server-side model identity (`_meta.model` only echoes the requested uid), so these probes establish that the uid is accepted, not which backend model served the request.
- `swe-1-7-lightning` is an explicit opt-in through `WS_MODEL`. Its tiny-fixture p50 was effectively tied with base SWE-1.7, so Lightning is not the default.
- The default fallback order is `MODEL_SWE_1_6_FAST` then `MODEL_SWE_1_5`. Set `WS_FALLBACK_MODELS` to override it or to an empty string to disable fallback.

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

### Repo-map A/B

Use [`benchmarks/run_repo_map_ab.py`](benchmarks/run_repo_map_ab.py) when tuning repo-map behavior. It compares the classic compact tree against the hotspot map without making remote Windsurf calls, so it is deterministic and safe to run on private repos.

Prepare a task JSON outside the repo when labels or paths are sensitive:

```json
[
  {
    "category": "desktop-feature",
    "query": "where is feature setup validated and persisted",
    "relevant_paths": [
      "apps/desktop/src/lib/feature-store.ts",
      "apps/desktop/electron/ipc/feature.ts"
    ]
  }
]
```

Run it with a redacted repo label:

```bash
uv run python -m benchmarks.run_repo_map_ab \
  --repo /path/to/repo \
  --tasks /path/to/repo-map-tasks.json \
  --repo-label private-large-repo \
  --output /tmp/repo-map-ab.json
```

The main metrics are:

- `file_recall`: exact labeled files visible in the map.
- `deep_cover_recall`: labeled files covered by a directory at depth two or deeper.
- `file_mrr` / `deep_cover_mrr`: how early the first exact file or covering directory appears.
- `avg_size_bytes` and `p50_latency_ms`: prompt and local preprocessing cost.

### Important note on fairness
### Notes on Evaluation Fairness

The `2026-06-01` baseline numbers below were recorded using an earlier test harness that paced requests solely by start-time intervals. Because each remote interaction took ~5 seconds, back-to-back runs quickly saturated upstream Windsurf rate limits. Consequently, older `remote` and `hybrid` results reflect heavy upstream throttling rather than pure model capability.

The current runner enforces **completion-based cooldowns** and bounded exponential backoff retries. Future benchmark refreshes will be published under these fairer pacing constraints.

### Quality Summary

| Backend | NDCG@10 | 95% CI | Recall@10 | 95% CI | Top-1 | MRR |
|---|---:|---:|---:|---:|---:|---:|
| `local` (Semble only) | 0.854 | 0.774-0.926 | 0.946 | 0.875-1.000 | 0.775 | 0.850 |
| `remote` (Windsurf only) | 0.453 | 0.309-0.604 | 0.467 | 0.312-0.617 | 0.450 | 0.475 |
| `hybrid` (Two-stage) | 0.890 | 0.835-0.939 | 0.979 | 0.946-1.000 | 0.825 | 0.896 |

### Performance Summary

| Backend | Batch p50 Latency | Batch p90 Latency | Valid Output Rate | Remote Success | `resource_exhausted` / Fallbacks | Total Retries |
|---|---:|---:|---:|---:|---:|---:|
| `local` | 30 ms | 39 ms | 100% | N/A | 0 | 0 |
| `remote` | 24.4 s | 37.5 s | 50% | 52.5% | 19 | 43 |
| `hybrid` | 28.3 s | 40.0 s | 100% | 50.0% | 20 degraded | 44 |

Local warm-cache index build time (measured prior to query evaluation):

- `fastapi`: 422 ms
- `axios`: 65 ms

### Category Breakdown (NDCG@10)

| Query Category | `local` | `remote` | `hybrid` |
|---|---:|---:|---:|
| `architecture` | 0.718 | 0.506 | 0.819 |
| `semantic` | 0.855 | 0.473 | 0.869 |
| `symbol` | 1.000 | 0.364 | 1.000 |

### Key Takeaways

- **`local` is a reliable, high-throughput baseline**: With a warm p50 of `30 ms` and strong retrieval metrics (`0.854` NDCG@10 / `0.946` Recall@10 with zero failures), it is ideal for CI, bulk evals, and latency-critical offline pipelines.
- **`hybrid` is the recommended interactive default**: Combining Semble prefetch hints with Windsurf verification achieves top retrieval quality (`0.890` NDCG@10 / `0.979` Recall@10) while ensuring the agent never stalls due to remote limits.
- **`remote` serves as an ablation baseline**: Useful for isolating raw Windsurf reasoning performance without prior local chunk hints.

## Recommended Workflow

Fast Context works best when configured as an agent skill via `SKILL.md`, but the CLI is equally suited for direct interactive exploration:

1. **Start with Hybrid Search**: Submit natural-language queries using default `--backend hybrid`.
2. **Inspect Candidate Files**: Focus on the returned candidate files and suggested line ranges.
3. **Offline or Low-Latency Needs**: Switch to `--backend local` for instant, dependency-free chunk retrieval.
4. **Follow Call Chains**: For promising code locations, run `find-related` to discover adjacent logic.
5. **Pinpoint Changes**: Transition to exact-match tools (`rg` or `ast-grep`) once candidates are identified.

## Implementation Details

- **Lexical Anchors**: Heuristically extracts exact filenames, path segments, and verbatim literals from the user query.
- **Dynamic Tree Pruning**: Repo maps prioritize top-level trees and exact lexical hits. When token budgets shrink, hotspot subtrees compress dynamically before falling back to classic compact trees.
- **Cache Invalidation**: Leverages Semble's native cache tracking, invalidating indexes incrementally only when files change.
- **Verification Rule**: Search results are candidate pointers rather than definitive proofs; agents should inspect targeted source files before committing edits.

## License

MIT
