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
2. Run through `uv` so the skill uses its locked dependencies and Python 3.10-3.13 environment:

```bash
uv run --project "$SKILL_DIR" fast-context search \
  --query "<natural language query>" \
  --project "<repo-root>"
```

3. If you need faster warm-cache chunk retrieval in the same repo, run:

```bash
uv run --project "$SKILL_DIR" fast-context local-search \
  --query "<natural language query>" \
  --project "<repo-root>"
```

4. If a local chunk looks promising and you want related code, run:

```bash
uv run --project "$SKILL_DIR" fast-context find-related \
  --file "<repo-relative-file>" \
  --line <line> \
  --project "<repo-root>"
```

5. Read the returned files before concluding anything.
6. Use `rg` or `ast-grep` to confirm exact symbols, routes, tests, or call sites.
7. Answer with concrete file paths, line ranges, and any uncertainty.

## Tuning

- Start with defaults.
- Default `search` uses `--backend hybrid`: Semble prefetch first, Windsurf verification second, local results as degradation if remote fails.
- Use `--backend remote` when you need to isolate Windsurf behavior without Semble hints.
- Use `--backend local` or `local-search` for Semble-only chunk results.
- Lower `--tree-depth` or add `--exclude` if payloads get large.
- Raise `--max-turns` only when the first pass is clearly incomplete.
- Keep `--max-results` small unless the user asked for broader exploration.
- Use `--content docs`, `--content config`, or `--content all` for local Semble searches beyond code.
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

- The CLI first checks `WINDSURF_API_KEY`.
- If that is unset, it reads Windsurf's local `state.vscdb`.
- That auto-discovery only works on the same machine where Windsurf is installed.
- Current Windsurf installs may store session-style credentials such as `devin-session-token$...`; this skill accepts them directly.
- If Windsurf lives on another host, copy the database locally and run:

```bash
uv run --project "$SKILL_DIR" fast-context extract-key --db-path "<copied-state.vscdb>"
```

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
- Include the returned follow-up search terms when they help the next exact search.
- Add `--verbose` only when you need anchor snippets or config diagnostics.
- Treat the output as candidate context, not proof. Verification still matters.

## Files

- `src/fast_context_cli.py`: CLI entry used by this skill
- `src/core.py`: protocol, auth, repo-map logic, local lexical anchors
- `src/local_semble.py`: local Semble search adapter
- `src/extract_key.py`: Windsurf credential extraction
