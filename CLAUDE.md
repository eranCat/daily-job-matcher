# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

```bash
# Install deps
pip install -r requirements.txt

# Run all tests (live Gemini integration tests auto-skip without GEMINI_API_KEY)
python -m pytest tests/ -v

# Run a single test
python -m pytest tests/test_scorer.py::TestAlgorithmicScore::test_fullstack_react_typescript_scores_high -v

# Skip live-API integration tests (deterministic; ~33 unit tests)
python -m pytest tests/ --deselect tests/test_scorer.py::TestGeminiIntegration

# Run the pipeline locally (requires GOOGLE_SA_KEY/GOOGLE_SA_KEY_PATH + GOOGLE_SHEETS_ID,
# typically loaded from a .env file via python-dotenv)
python scripts/job_matcher.py                        # search mode (default)
RUN_MODE=test-connection python scripts/job_matcher.py
RUN_MODE=test-write python scripts/job_matcher.py    # appends + deletes one synthetic row

# Run the local UI (serves docs/ + SSE endpoints that stream subprocess output to the browser)
python scripts/server.py                             # http://localhost:8080
```

GitHub Actions runs in two workflows: `daily-job-matcher.yml` (scheduled cron + manual dispatch with mode input) and `test-scorer.yml` (manual `pytest tests/ -v`). The schedule lives in [config/search-settings.json](config/search-settings.json) under `schedule.cron` but the active cron is hard-coded in the workflow file — changing the JSON does not change the cron unless the workflow file is also updated.

## Architecture

### Pipeline (5 stages in `run_search()`)
[scripts/job_matcher.py](scripts/job_matcher.py) orchestrates: **fetch → pre-filter → dedup vs sheet → score → verify → append**. Each stage emits `::notice` GHA annotations (capped at ~10 per workflow step, so use `progress_log()` for incremental sub-step updates instead of `gha_log()`).

The sheet's existing URLs are loaded **once at the top** and threaded into `fetch_all_jobs(settings, existing_links=...)`. This lets fetchers skip expensive per-job HTTP detail requests for jobs already in the sheet — the post-fetch dedup loop in `job_matcher.py` is now a safety net, not the primary cut.

### Two-board fetcher model
[scripts/fetchers.py](scripts/fetchers.py) has two very different sources:
- **Greenhouse**: 66 hard-coded company board slugs in `GREENHOUSE_IL_BOARDS`. One JSON list call per board (parallelized at 15 workers), then per-job detail JSON (parallelized at 8 workers per board) for description + experience extraction.
- **Drushim**: HTML scraping. ~78 search terms × up to 20 pages each, then per-job HTML detail page. Drushim is the bottleneck — the Drushim flow has three early-exit gates layered for cost reasons:
  1. Card-text experience filter (no HTTP — uses snippet from the list page)
  2. Already-in-sheet URL skip (no HTTP — uses `existing_links`)
  3. Card-text relevance pre-pass (no HTTP — checks for any skill or dev keyword in title+snippet)

  Only cards surviving all three get the per-job detail HTTP fetch. Adding more pre-detail filters here is the single highest-leverage perf change.

### Scorer chain (registry pattern)
[scripts/scorer.py](scripts/scorer.py) exposes a `SCORERS` registry (`{"gemini": ..., "algorithmic": ...}`) and `score_jobs()` walks `settings["scorers"]` in order, falling through on `ScorerUnavailable`. Default chain is `["gemini", "algorithmic"]`. The Gemini scorer handles its own model fallback (`_GEMINI_FALLBACK_CHAIN`) for 404 (retired model) and 429 (rate-limited) — only raises `ScorerUnavailable` when the entire chain is exhausted.

Gemini batches at 15 jobs per request. If a batch fails after the model chain is exhausted, those job indices are added to `unscored_indices` and routed through `algorithmic_scorer` as a partial fallback within the same `gemini_scorer` call.

### Filter logic (the most edited file)
[scripts/filters.py](scripts/filters.py) `pre_filter()` is the funnel cliff and where most settings actually take effect. Two important asymmetries:
- **`skills`** (e.g. "react", "python") match on **title OR description** — they're specific enough that mention in the body implies relevance.
- **`devRoleKeywords`** (e.g. "developer", "engineer") match on **title only** — these words appear constantly in non-dev JDs ("you'll partner with developers"), so broadening them blows up false positives.

`_extract_min_years()` for X-Y year ranges intentionally uses the *upper* bound when it exceeds `maxYears` ("2-4 years" → treats as mid-level for a `maxYears=2` candidate, even though the minimum is 2).

### Config split
- [config/search-settings.json](config/search-settings.json) — user-facing knobs (skills, locations, exclusions, `minScore`, `maxYears`, `postDateFilter`, `jobBoards`). Editable via the GitHub Pages UI in [docs/](docs/).
- [config/keywords.json](config/keywords.json) — internal vocab: Hebrew experience patterns, Drushim search terms + category IDs, IL location hints, scoring tiers (`skill_tier1`/`skill_tier2`), seniority phrase lists. Loaded once at startup via `_load_il_hints()` for the IL hints; everything else is loaded per-call.

Both files have **fallback defaults baked into [scripts/utils.py](scripts/utils.py) `load_settings()` and [scripts/scorer.py](scripts/scorer.py)** — if a key is missing from the JSON, the code keeps working with safe defaults. Don't assume settings exist; always `.get(...)` with a default.

### Sheets append (sheets.py is non-trivial)
[scripts/sheets.py](scripts/sheets.py) `append_rows()` does NOT use `sheets.values().append()` directly when the tab has a structured table. It scans column A:F for the **last row with non-whitespace data** (`_find_last_data_row`), then uses `insertDimension` + `values.update` + `updateTable` so new rows land *inside* the table, even if the user added rows manually below the table boundary. The simple `.append()` fallback is only used when no structured table is detected.

### Local UI ↔ pipeline contract
[scripts/server.py](scripts/server.py) spawns `python -u scripts/job_matcher.py` as a subprocess and streams stdout via SSE. It sets two env vars the matcher checks:
- `JM_PROGRESS=1` — enables both `gha_log()` and `progress_log()` output (the local UI parses these for its progress display)
- `LOCAL_RUN=1` — suppresses `gha_log()` for plain CLI usage when `JM_PROGRESS` is unset

When adding new progress milestones: phase transitions go through `gha_log()`, per-board/per-batch updates through `progress_log()` — GHA caps `::notice` annotations at ~10 per step.

### URL normalization is load-bearing
[scripts/utils.py](scripts/utils.py) `normalize_url()` strips tracking params (utm_*, gh_src, etc.) and trailing slashes. The sheet dedup, the fetcher-level skip, and the post-fetch dedup all rely on it producing identical strings for "the same job seen via different links" — changing it can break dedup silently.
