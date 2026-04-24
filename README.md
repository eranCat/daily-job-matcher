# Daily Job Matcher

Automated job search for junior full-stack and backend developers in Israel. Powered by **Groq** (free tier, no credit card).

## 🎯 Configure via Web UI

**[→ Open Settings Page](https://eranCat.github.io/daily-job-matcher/)**

Edit filters, skills, locations, and schedules directly from your browser. Changes commit to the repo and take effect on the next workflow run. Three manual run modes available:
- **Full search run** — execute the matcher with your current config
- **Test sheet connection** — ping the Sheets webhook to verify connectivity
- **Test adding a record** — append one synthetic row to confirm write permission

## Overview

GitHub Actions workflow runs on a schedule to:
- Score job candidates against your configured profile using a free LLM (Groq / Llama 3.3 70B)
- Filter by skills, experience, and location
- Sync suitable matches to Google Sheets via an Apps Script webhook

## Quick Start

### 1. Get a Free Groq API Key

1. Sign up at [console.groq.com](https://console.groq.com) (email only, no credit card)
2. Create a key at [console.groq.com/keys](https://console.groq.com/keys)
3. Copy it (starts with `gsk_...`)

**Free tier:** 14,400 requests/day — far more than needed for daily runs.

### 2. Set Up the Sheets Apps Script Webhook

1. Open your target Google Sheet
2. Extensions → Apps Script → paste a script that handles `doPost(e)` with two actions:
   - `ping` — return `{ok: true}`
   - `append` — push rows into the sheet from `e.postData.contents.jobs[]`
3. Deploy → New deployment → Web app → execute as yourself, access "Anyone"
4. Copy the deployment URL

### 3. Add GitHub Secrets

Repo → **Settings → Secrets and variables → Actions**:

| Name | Value |
|------|-------|
| `GROQ_API_KEY` | your `gsk_...` key |
| `SHEETS_WEBHOOK_URL` | your Apps Script web app URL |

### 4. Configure Search Settings

Use the [Settings UI](https://eranCat.github.io/daily-job-matcher/) to set:
- Schedule (cron expression)
- Skills & stack preferences
- Max years of experience
- Location filters
- Blacklisted companies, roles, and stacks
- Score threshold

Or edit `config/search-settings.json` directly.

### 5. Run & Test

From the Settings UI, use the **Test & run** section to:
- **Test connection** — verifies the sheet webhook works (safe, no data written)
- **Test write** — adds one identifiable test row you can delete
- **Run search** — full production run

Or trigger manually: Actions → Daily Job Matcher → Run workflow → pick mode.

## Project Structure

```
├── .github/workflows/
│   └── daily-job-matcher.yml     # Workflow (uses actions/checkout@v6, setup-python@v6 on Node 24)
├── config/
│   └── search-settings.json       # Active search configuration
├── docs/
│   └── index.html                 # Settings UI (GitHub Pages)
├── scripts/
│   └── job_matcher.py             # Main script (search / test-connection / test-write modes)
└── README.md
```

## Run Modes

The workflow accepts a `mode` input (default: `search`):

| Mode | What it does |
|------|--------------|
| `search` | Full run: LLM generates matches, syncs to Sheets |
| `test-connection` | POSTs `{action: "ping"}` to the Sheets webhook — verifies connectivity without writing |
| `test-write` | POSTs a single synthetic test job to the webhook — verifies write permission |

## Why Groq?

- **Free tier:** 14,400 requests/day, no credit card
- **Fast:** ~500 tok/s (Llama 3.3 70B responds in <2s)
- **Quality:** reliable structured JSON output
- **No billing surprises:** hard rate limit, not metered

## Settings UI Authentication

The settings page uses a GitHub Personal Access Token with `repo` scope to save config changes and trigger workflow runs. The token is stored only in your browser's localStorage — never sent anywhere except the GitHub API directly.

**Create one:** [github.com/settings/tokens](https://github.com/settings/tokens/new?scopes=repo&description=Job%20Matcher%20Settings) → `repo` scope → generate → paste into settings page.

## Monitoring

- **Logs:** Actions tab → workflow run → job logs (matched jobs appear here)
- **Live status:** the Settings UI shows run progress + final status after dispatch

## Troubleshooting

**Workflow fails with `Cloudflare 1010`?** Groq's Cloudflare blocks default Python-urllib User-Agent. The script already spoofs a browser UA. If you still see it, check for your runner IP being on a flagged range.

**Workflow fails with `GROQ_API_KEY not set`?** Verify the secret is added to repo Settings → Secrets → Actions with exact name `GROQ_API_KEY`.

**`SHEETS_WEBHOOK_URL secret not set`?** Add it as a repo secret — the deployment URL from your Apps Script web app.

**Test connection succeeds but write fails?** Check your Apps Script `doPost` handler logs the received `action` and `jobs` fields correctly.

**Too many/few matches?** Adjust `minScore` or skills/exclusions in the settings UI.
