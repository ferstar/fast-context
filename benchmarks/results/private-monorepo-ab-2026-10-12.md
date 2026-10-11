# Private monorepo A/B (2026-10-12)

Before: `main` at 19f20e4. After: branch `perf/accuracy-improvements`.

## Setup

- Repo: private TypeScript monorepo, about 4.4k tracked files, about two dozen app roots
  (an API gateway, an Electron desktop app, several web apps, shared packages, locale files).
  Read-only, local Semble cache warm.
- 5 natural-language "where is X" questions with known answers, asked with `--max-results 8`:
  1. Where does the gateway set a default max output tokens value when the request has none,
     and what decides whether it applies? (cross-module: the guard lives in a quota service
     called from the route)
  2. Which component lays out the user profile fields on the desktop account settings page?
  3. Where are usage-page feature keys mapped to display labels?
  4. Where does the desktop app render the inline memory confirmation card?
  5. Where is the chat composer input cleared after sending?
- Hybrid `search` ran twice per question, `local-search` twice per question. Latency is wall
  clock including `uv run` startup.
- Hit = an expected file in the main result list. Q4 accepts either of the two
  confirmation-card components. "Files to sift" = number of files in the main list (top 8 at most).
- Cross-module = the guard definition file (`apps/<gateway>/src/features/<quota>/service.ts`)
  shows up in the output.

## Results

| Mode | Version | Median latency | Top3 | Top8 | Files to sift (median) | Cross-module guard found |
|---|---|---|---|---|---|---|
| hybrid `search` | before | 7.3 s | 5/5 | 5/5 | 4 | 0/2 runs |
| hybrid `search` | after | 6.7 s | 5/5 | 5/5 | 3 | 2/2 runs |
| `local-search` | before | 2.3 s | 4/5 | 5/5 | 8 | 0/2 runs |
| `local-search` | after | 2.5 s | 5/5 | 5/5 | 8 | 2/2 runs |

Noise in `local-search` top 8 (`*.sql`, `.env*`, `deploy/`, `migrations/`), first run of every question:
4 before, 0 after. The case where a same-named component in a sibling app ranked #1 over the desktop
target now ranks the desktop file first.

Cross-module question, after:

```text
Callers / guard sites (low confidence, lexical):

- apps/<gateway>/src/features/proxy/<routes>.ts:<line> calls resolveOutputTokenLimit;
  guard isMemberQuotaRequired defined at apps/<gateway>/src/features/<quota>/service.ts:<line>
```

## Latency breakdown (hybrid, one run, `--verbose`)

| Stage | JWT and rate-limit cache off | Cache on (warm) |
|---|---|---|
| jwt | 318 ms | 0 ms |
| rate_limit pre-check | 848 ms | 355 ms |
| remote search | 3756 ms | 3691 ms |
| decision layer | 183 ms | 221 ms |

## Honest notes

- The remote model call is still most of the time. Caching saves roughly 0.5 to 1 s per call; the
  rest of the median gain is run-to-run variance.
- `local-search` got about 0.2 s slower: it fetches 2x candidates for reranking and runs the
  decision layer (about 0.1 to 0.2 s). Use `--no-decision-layer` to skip that part.
- The guard section is lexical and low confidence. On questions without a guard it sometimes lists
  unrelated helpers. It is labeled as such and never replaces the main results.
