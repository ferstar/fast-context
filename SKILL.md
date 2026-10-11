---
name: fast-context
description: Use whenever the user asks where logic lives, how a flow works, what files matter for a bug or feature, or needs fast repo orientation before editing, reviewing, or debugging. Use it for vague code-context lookups, then confirm details with rg or ast-grep.
argument-hint: "[query]"
user-invocable: true
---

# Fast Context

Use the bundled Python CLI to get a small set of high-signal candidate files. The default hybrid mode prefetches local Semble chunks, injects them into Windsurf search, then verifies details with exact local reads.

## Use this skill when

- The user asks "where is X implemented?"
- The exact symbol name is not known yet.
- You want a short list of files before editing or review.
- You want local cached chunk hints to steer remote semantic search.
- The remote Windsurf path is unavailable and local chunk retrieval is enough to keep moving.

## Do not use this skill when

- The exact filename, symbol, or path is already known. Use `rg` directly.
- The task is a structural refactor. Use `ast-grep`.
- The task needs live web research instead of local repo context.

## Workflow

1. Pick the narrowest project root that still contains the relevant code.
2. Run through `uv` so the skill uses its locked dependencies and Python `>=3.10` environment:

```bash
uv run --project "$SKILL_DIR" fast-context search \
  --query "<natural language query>" \
  --project "<repo-root>"
```

3. Prefer hybrid `search` when accuracy matters. Most of that time is the remote Windsurf call, so a warm Semble cache does not make hybrid fast.
4. Use `local-search` when a fast local candidate list is enough and you will verify the hits:

```bash
uv run --project "$SKILL_DIR" fast-context local-search \
  --query "<natural language query>" \
  --project "<repo-root>"
```

5. If a local chunk looks promising and you want related code, run:

```bash
uv run --project "$SKILL_DIR" fast-context find-related \
  --file "<repo-relative-file>" \
  --line <line> \
  --project "<repo-root>"
```

6. Read the returned files before concluding anything.
7. For "what decides whether X applies", read the `Callers / guard sites (low confidence, lexical):` section, or run `find-related` and then open the guard definition.
8. Confirm symbols with `rg` or `ast-grep`. Pass an explicit path to `rg` (`rg -n pattern path`). With no path, a non-interactive shell makes `rg` read stdin and wait.
9. Answer with concrete file paths, line ranges, and any uncertainty.

## Tuning

- Start with defaults.
- Default `search` uses `--backend hybrid`: Semble prefetch first, Windsurf verification second, local results as degradation if remote fails.
- Remote Windsurf sessions are serialized across local agents/processes with an OS-level file lock; local Semble and repo-map work remain concurrent.
- Use `--backend remote` when you need to isolate Windsurf behavior without Semble hints.
- Use `--backend local` or `local-search` for Semble-only chunk results.
- Lower `--tree-depth` or add `--exclude` if payloads get large.
- Raise `--max-turns` only when the first pass is clearly incomplete.
- Keep `--max-results` small unless the user asked for broader exploration.
- Use `--content docs`, `--content config`, or `--content all` for local Semble searches beyond code.
- `--no-decision-layer` or `FAST_CONTEXT_DECISION_LAYER=0` skips the lexical caller/guard section on `search` and `local-search`.
- `FAST_CONTEXT_JWT_CACHE=0` skips the on-disk user JWT cache. The file is `jwt-<sha256(api_key)[:16]>.json` under `$XDG_CACHE_HOME/fast-context` or `~/.cache/fast-context`, mode `0600`. A token is reused while `exp - 60s` is still in the future. A payload with no `exp` is stored as fetched-at plus 10 minutes. HTTP 401, HTTP 403, or `unauthenticated` drops the cache and refetches once. The token is never logged.
- A successful rate-limit pre-check is reused for about 30 seconds. A 429 is not cached.
- On code queries, local ranking keeps but lowers `*.sql`, `deploy/`, `migrations/`, `.env*`, `*.example`, lockfiles, `*.min.js`, `generated/`, `dist/`, `build/`, and `*.snap`. A query that names sql, migration, env, deploy, or lock keeps those scores. When the query or the top hits point at one `apps/<name>/`, `packages/<name>/`, or `services/<name>/` root, other app roots rank lower, and the same basename stays higher in the preferred app.
- If Semble cache grows after repo moves or temp-project searches, run `cache-gc`; use `cache-clear --project <repo-root>` to reset one project.

Example:

```bash
uv run --project "$SKILL_DIR" fast-context search \
  --query "where is the browser login handoff state validated" \
  --project "/workspace/repo" \
  --tree-depth 4 \
  --max-turns 4 \
  --max-results 8
```

## Authentication

- The CLI first checks `WINDSURF_API_KEY`, then `WINDSURF_CREDENTIALS_DB`.
- `WINDSURF_CREDENTIALS_DB` points at one explicit credentials file (a `state.vscdb` or `credentials.toml`). When it is set only that file is read, so a bad path fails loudly instead of silently falling back to another account.
- Otherwise it reads Devin CLI credentials on Linux/WSL, then local `Deviv`, `Devin`, and `Windsurf` `state.vscdb` files, probing both the canonical and lowercase directory spelling.
- That auto-discovery only works on the same machine where Windsurf/Devin is installed or where `devin login` has been run.
- Current installs may store session-style credentials such as `devin-session-token$...`; this skill accepts them directly.
- If Windsurf/Devin lives on another host, copy the database or credentials file locally, then either point `WINDSURF_CREDENTIALS_DB` at the copy or place it in one of the local paths above so auto-discovery picks it up, and run:

```bash
uv run --project "$SKILL_DIR" fast-context extract-key --db-path "<copied-state.vscdb>"
uv run --project "$SKILL_DIR" fast-context extract-key --db-path "<copied-credentials.toml>"
```

A copied `state.vscdb` also works without any environment variable once it sits at one of the local paths the CLI probes, for example `~/Library/Application Support/Devin/User/globalStorage/state.vscdb` on macOS. Refresh that copy when the stored session token expires.

## Cache maintenance

```bash
uv run --project "$SKILL_DIR" fast-context cache-gc
uv run --project "$SKILL_DIR" fast-context cache-clear --project "<repo-root>"
```

## Output expectations

- Prefer 3-10 candidate files, not a dump.
- Prefer file paths plus a few labeled semantic blocks over raw range dumps.
- Hybrid output can include both remote `Start here` files and local Semble chunk candidates. Treat local chunks as hints unless exact reads confirm them.
- Local-only Semble results are chunk-level. Use them as precise starting snippets, then open the file when broader context is needed.
- A `Salvaged from malformed remote response (low confidence)` section lists file hits recovered textually from a corrupted remote reply. Weaker than `Start here` — verify with a read before acting on them.
- A `Callers / guard sites (low confidence, lexical):` section lists caller `file:line` and the guard definition `file:line`, at most about six entries. Treat it as a lexical hint. Follow-up search terms may include those guard names.
- Follow-up search terms are model-generated hypotheses: deduplicate them, drop the ones you already know, and expect an occasional wrong or sibling-level hit. The ranked candidate files and their line ranges are the reliable part of the output and the better input to `rg`.
- Add `--verbose` when you need anchor snippets, config diagnostics, or the `[timing]` breakdown (repo map, anchors, local prefetch, rerank, JWT, rate limit, remote, decision layer).
- Treat the output as candidate context, not proof. Verification still matters.

## Files

- `src/fast_context_cli.py`: CLI entry used by this skill
- `src/core.py`: protocol, auth, repo-map logic, local lexical anchors
- `src/local_repo_map.py`: BM25F directory heat, adaptive topK, hotspot subtrees, path spines
- `src/local_semble.py`: local Semble search adapter
- `src/decision_layer.py`: lexical caller and guard expansion
- `src/local_rank.py`: local noise demotion and sibling-app ranking
- `src/extract_key.py`: Windsurf credential extraction
