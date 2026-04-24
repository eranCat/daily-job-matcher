# Daily Job Matcher

Automated job search for junior full-stack and backend developers in Israel. Powered by **Groq** (free tier) and writes directly to your Google Sheet via service account auth.

## 🎯 Configure via Web UI

**[→ Open Settings Page](https://eranCat.github.io/daily-job-matcher/)**

Edit filters, skills, locations, and schedules from your browser. Changes commit to the repo and take effect on the next workflow run. Three manual run modes available:
- **Full search run** — execute the matcher and append matches to your Sheet
- **Test sheet connection** — verify the service account can read the sheet
- **Test adding a record** — append one synthetic row to confirm write access

## Overview

GitHub Actions workflow that runs on a schedule to:
- Score jobs against your profile using a free LLM (Groq / Llama 3.3 70B)
- Filter by skills, experience, location
- Append matches directly to Google Sheets via the Sheets API

No Apps Script, no webhooks — just a service account with Editor access on your sheet.

## Setup

### 1. Free Groq API key

1. Sign up at [console.groq.com](https://console.groq.com) (email only, no card)
2. Create a key at [console.groq.com/keys](https://console.groq.com/keys)
3. Copy the `gsk_...` value

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

| Name | Value |
|------|-------|
| `GROQ_API_KEY` | `gsk_...` |
| `GOOGLE_SA_KEY` | full JSON from the downloaded key file |
| `GOOGLE_SHEETS_ID` | the long ID in your sheet URL |

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

Or via Actions tab → Daily Job Matcher → Run workflow → pick mode.

## Run Modes

| Mode | What it does |
|------|--------------|
| `search` | Full run: LLM generates matches, appends to the sheet, dedupes by link |
| `test-connection` | Fetches sheet metadata + header row to verify read access |
| `test-write` | Appends one synthetic row to verify write access |

## Why Groq + Service Account?

**Groq:** Free tier (14,400 req/day, no card), ~500 tok/s, reliable JSON output.

**Service Account:** Direct Sheets API access — no webhook to maintain, no browser OAuth consent. Permissions scoped only to sheets you explicitly share with the SA email.

## Settings UI

The settings page uses a GitHub Personal Access Token with `repo` scope to save config and trigger runs. Token stays in your browser's localStorage.

**Create one:** [github.com/settings/tokens](https://github.com/settings/tokens/new?scopes=repo&description=Job%20Matcher%20Settings) → `repo` scope → generate → paste into settings page.

## Troubleshooting

**`Permission denied` on sheet?** Share the sheet with the `client_email` from your SA JSON as Editor.

**`GROQ_API_KEY not set`?** Check repo Secrets spells it exactly `GROQ_API_KEY`.

**`GOOGLE_SA_KEY is not valid JSON`?** Paste the entire file including opening/closing `{}`. No quotes wrapping it.

**`Expected tab 'Saved Jobs' not found`?** Rename your tab or update `SHEET_TAB` in `scripts/job_matcher.py`.

**Too many/few matches?** Adjust `minScore`, skills, or exclusions in the Settings UI.
