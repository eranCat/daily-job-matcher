# Daily Job Matcher

Automated job search for junior full-stack and backend developers in Israel. LLM-scored via **OpenRouter free models** (with a keyword-based algorithmic scorer as fallback) and writes directly to your Google Sheet via service account auth.

## 🎯 Configure via Web UI

**[→ Open Settings Page](https://eranCat.github.io/daily-job-matcher/)**

Edit filters, skills, locations, and schedules from your browser. Changes commit to the repo and take effect on the next workflow run. Four manual run modes available:
- **Full search run** — execute the matcher and append matches to your Sheet
- **Test sheet connection** — verify the service account can read the sheet
- **Test adding a record** — append one synthetic row to confirm write access
- **Run scorer tests** — trigger the pytest suite (33 tests) via GitHub Actions

## Overview

GitHub Actions workflow that runs on a schedule to:
- Fetch listings from **Greenhouse IL** (66 company boards) and **Drushim**
- Pre-filter by skills, experience cap, location, and excluded companies/keywords
- Score the survivors with **OpenRouter** free models (strict JSON-schema output, automatic fallback across 6 models), with a deterministic keyword-based **algorithmic** scorer as the final safety net
- Append matches directly to Google Sheets via the Sheets API

No Apps Script, no webhooks — just a service account with Editor access on your sheet.

## Setup

### 1. Free OpenRouter API key

1. Sign in at [openrouter.ai/keys](https://openrouter.ai/keys) (Google/GitHub login, no card)
2. Click **Create Key** → copy the value (`sk-or-v1-…`)

The pipeline only uses `:free`-tagged models so the key never consumes credits.

### 2. Google Cloud service account

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create or pick a project
2. Enable the Sheets API at [console.cloud.google.com/apis/library/sheets.googleapis.com](https://console.cloud.google.com/apis/library/sheets.googleapis.com)
3. Create a service account at [console.cloud.google.com/iam-admin/serviceaccounts](https://console.cloud.google.com/iam-admin/serviceaccounts)
   - Name: `job-matcher-bot`
   - Skip the role-granting step
4. Click the new service account → **Keys** tab → **Add Key** → **Create new key** → **JSON** → Download
5. Open the JSON, copy the `client_email` value
6. Share your target sheet with that email (**Editor** role, uncheck "Notify")

### 3. GitHub Secrets

Repo → **Settings → Secrets and variables → Actions**, add:

| Name | Value | Required? |
|------|-------|-----------|
| `OPENROUTER_API_KEY` | key from openrouter.ai/keys | recommended |
| `GOOGLE_SA_KEY` | full JSON from the downloaded key file | required |
| `GOOGLE_SHEETS_ID` | the long ID in your sheet URL | required |

> `OPENROUTER_API_KEY` is technically optional — without it the pipeline falls through to the built-in algorithmic scorer (deterministic keyword matching). LLM scoring is much higher quality, so set it.

### 4. Sheet tab layout

The workflow expects a tab named **`Saved Jobs`** with these columns in order:

```
DATE | ROLE | COMPANY | LOCATION | LINK | STATUS
```

Create the header row manually before the first run.

### 5. Test

From the [Settings UI](https://eranCat.github.io/daily-job-matcher/) **Test & run** section:
- **Test connection** — reads sheet metadata + header row, writes nothing
- **Test write** — appends one identifiable test row (delete after)
- **Run search** — full production run
- **Run scorer tests** — runs the pytest suite on GitHub Actions

Or via the Actions tab → pick a workflow → Run workflow.

### 6. Run locally

```bash
pip install -r requirements.txt

# Run the test suite (live LLM integration tests auto-skip without keys)
pytest tests/ -v

# Run the pipeline (loads secrets from .env)
python scripts/job_matcher.py

# Or start the local web UI on http://localhost:8080
python scripts/server.py
```

Create a `.env` file at the repo root with the same secrets you'd put in GitHub:

```
OPENROUTER_API_KEY=sk-or-v1-...
GOOGLE_SHEETS_ID=...
GOOGLE_SA_KEY_PATH=/absolute/path/to/sa-key.json
```

The local UI runs the **same code** as GitHub Actions but uses your **working tree** (uncommitted changes apply) and your **local config**. It writes to the same Google Sheet, so use `test-connection` first if you're unsure.

## Run Modes

| Workflow | Mode / trigger | What it does |
|----------|----------------|--------------|
| `daily-job-matcher.yml` | `search` | Full run: fetch → filter → score → append to sheet |
| `daily-job-matcher.yml` | `test-connection` | Fetches sheet metadata + header row to verify read access |
| `daily-job-matcher.yml` | `test-write` | Appends one synthetic row to verify write access |
| `test-scorer.yml` | *(no inputs)* | Runs `pytest tests/ -v` — 33 unit + integration tests |

## How the Search Works

A full `search` run flows through five stages, orchestrated by `run_search()` in [scripts/job_matcher.py](scripts/job_matcher.py):

### 1. Fetch
Listings are pulled in parallel from both enabled boards:

- **Greenhouse IL** ([fetchers.py:fetch_greenhouse_il](scripts/fetchers.py)): one JSON list call per board across 66 hard-coded company slugs (15 workers in parallel), then a per-job detail JSON fetch (8 workers per board) to extract the description and minimum-years requirement. Jobs older than `postDateFilter` or above `maxYears` are dropped at this stage.
- **Drushim** ([fetchers.py:fetch_drushim](scripts/fetchers.py)): HTML scraping over ~78 search terms × up to 20 pages each. The Drushim flow has **three pre-detail gates** (no HTTP, all run against the card snippet) layered to skip wasteful detail fetches:
  1. Card-text experience filter — drops anything mentioning more than `maxYears` years
  2. Already-in-sheet URL skip — uses URLs loaded once at the top of the run
  3. Card-text relevance pre-pass — requires at least one skill or dev keyword in title+snippet

Only cards surviving all three get the per-job HTML detail fetch.

### 2. Pre-filter
[filters.py:pre_filter](scripts/filters.py) applies hard exclusions: excluded companies, excluded keywords (e.g. `senior`, `qa`, `manager`), excluded stacks (e.g. `php`, `.net`), disallowed locations, and a "no skill/dev keyword" cut. Two important asymmetries:

- **Skills** (`react`, `python`, etc.) match against **title OR description** — specific enough that a mention in the body implies relevance
- **Dev role keywords** (`developer`, `engineer`, etc.) match against **title only** — these words appear constantly in non-dev JDs ("you'll partner with developers")

### 3. Dedup vs sheet
URLs already present in the sheet are normalized (tracking params stripped, trailing slashes removed) and dropped. This is a safety net — the fetchers already skip most known URLs in stage 1.

### 4. Score
Survivors run through **OpenRouter** in batches of 15, with a model-fallback chain inside each call and the algorithmic scorer as a per-batch backup. See [Scoring](#scoring) below for the model list and fallback logic. Only jobs scoring ≥ `minScore` (default 7) survive.

### 5. Verify + append
Each surviving link is HEAD-checked to make sure it still resolves (catches expired listings). The verified rows are appended to your Google Sheet via [sheets.py:append_rows](scripts/sheets.py), which scans for the last row of structured-table data and inserts new rows *inside* the table boundary rather than at the literal bottom of the sheet.

Each stage emits `::notice` annotations visible in GitHub Actions logs and counts in `logs/run.log`.

## Scoring

Candidates that survive pre-filtering are scored by **OpenRouter** in batches of 15. Each batch is sent as a single chat-completion request asking the model to score each job 0-10 against your profile (CS BSc graduate, stack from `config/search-settings.json`, `maxYears` cap, target role definition). The request uses OpenAI-compatible `response_format: {type: "json_schema", strict: true}` so providers that honor it return guaranteed-valid output.

Within a single OpenRouter call, the scorer walks a per-model fallback chain on 429/404/quota/malformed-JSON errors:

1. `z-ai/glm-4.5-air:free` — primary, most reliable JSON output observed
2. `nvidia/nemotron-nano-9b-v2:free` — small, consistent fallback
3. `openai/gpt-oss-120b:free` — high quality but ~40% malformed-JSON rate
4. `openai/gpt-oss-20b:free`
5. `meta-llama/llama-3.3-70b-instruct:free` — heavily rate-limited on free tier
6. `qwen/qwen3-next-80b-a3b-instruct:free`

The chain advances permanently once a model fails for the current run, so later batches don't re-pay the cost of trying known-bad models. If every model fails for a given batch, those jobs fall through to the **algorithmic scorer** as a per-batch backup.

The algorithmic scorer is also the final scorer in the chain (`["openrouter", "algorithmic"]` by default in `score_jobs()`). It uses keyword-weighted scoring based on `skill_tier1` / `skill_tier2` from `config/keywords.json` plus title classification (full-stack/backend/frontend/dev) and seniority penalties. Used as the last-resort scorer if `OPENROUTER_API_KEY` is missing entirely.

Only jobs scoring ≥ `minScore` (default 7) are kept for the verify+append stages.

## Settings UI

The settings page uses a GitHub Personal Access Token with `repo` scope to save config and trigger runs. Token stays in your browser's localStorage.

**Create one:** [github.com/settings/tokens](https://github.com/settings/tokens/new?scopes=repo&description=Job%20Matcher%20Settings) → `repo` scope → generate → paste into settings page.

## Troubleshooting

**`Permission denied` on sheet?** Share the sheet with the `client_email` from your SA JSON as Editor.

**`OPENROUTER_API_KEY not set`?** The chain falls through to the algorithmic scorer. Jobs are still found and saved — just with deterministic keyword matching instead of LLM scoring.

**`GOOGLE_SA_KEY is not valid JSON`?** Paste the entire file including opening/closing `{}`. No quotes wrapping it.

**`Expected tab 'Saved Jobs' not found`?** Rename your tab or update `SHEET_TAB` in `scripts/job_matcher.py`.

**Too many/few matches?** Adjust `minScore`, `maxYears`, `postDateFilter`, skills, or exclusions in the Settings UI or `config/search-settings.json`.

**Scheduled run didn't fire?** The active cron is in `.github/workflows/daily-job-matcher.yml`, not `config/search-settings.json`. Editing the JSON does not change the schedule unless the workflow file is also updated.
