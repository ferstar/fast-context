---
name: fast-context
description: Use whenever the user asks where logic lives, how a flow works, what files matter for a bug or feature, or needs fast repo orientation before editing, reviewing, or debugging. Use it for vague code-context lookups, then confirm details with rg or ast-grep.
argument-hint: "[query]"
user-invocable: true
---

# Fast Context

Use the bundled Python CLI to get a small set of high-signal candidate files from Windsurf's semantic search backend, then verify details with exact local reads.

## Use this skill when

- The user asks "where is X implemented?"
- The exact symbol name is not known yet.
- You want a short list of files before editing or review.

## Do not use this skill when

- The exact filename, symbol, or path is already known. Use `rg` directly.
- The task is a structural refactor. Use `ast-grep`.
- The task needs live web research instead of local repo context.

## Workflow

1. Pick the narrowest project root that still contains the relevant code.
2. Run:

```bash
python "$SKILL_DIR/src/fast_context_cli.py" search \
  --query "<natural language query>" \
  --project "<repo-root>"
```

3. Read the returned files before concluding anything.
4. Use `rg` or `ast-grep` to confirm exact symbols, routes, tests, or call sites.
5. Answer with concrete file paths, line ranges, and any uncertainty.

## Tuning

- Start with defaults.
- Lower `--tree-depth` or add `--exclude` if payloads get large.
- Raise `--max-turns` only when the first pass is clearly incomplete.
- Keep `--max-results` small unless the user asked for broader exploration.

Example:

```bash
python "$SKILL_DIR/src/fast_context_cli.py" search \
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
python "$SKILL_DIR/src/fast_context_cli.py" extract-key --db-path "<copied-state.vscdb>"
```

## Output expectations

- Prefer 3-10 candidate files, not a dump.
- Include the returned grep keywords when they help the next exact search.
- Treat the output as candidate context, not proof. Verification still matters.

## Files

- `src/fast_context_cli.py`: CLI entry used by this skill
- `src/core.py`: protocol, auth, repo-map logic, local lexical anchors
- `src/extract_key.py`: Windsurf credential extraction
