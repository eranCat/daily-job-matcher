# Daily Job Matcher

Automated job search for junior full-stack and backend developers in Israel. Powered by **Groq** (free tier, no credit card).

## 🎯 Configure via Web UI

**[→ Open Settings Page](https://eranCat.github.io/daily-job-matcher/)**

Edit filters, skills, locations, and schedules directly from your browser. Changes commit to the repo and take effect on the next workflow run.

## Overview

GitHub Actions workflow that runs on a schedule to:
- Score job candidates against your configured profile using a free LLM (Groq/Llama 3.3 70B)
- Filter by skills, experience level, and location
- Output suitable matches to workflow logs (and optionally Google Sheets)

## Quick Start

### 1. Get a Free Groq API Key

1. Sign up at [console.groq.com](https://console.groq.com) (email only, no credit card)
2. Create a key at [console.groq.com/keys](https://console.groq.com/keys)
3. Copy the key (starts with `gsk_...`)

**Free tier limits:** 14,400 requests/day — more than enough for daily runs.

### 2. Add Your API Key to GitHub

1. Go to **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret**
3. Name: `GROQ_API_KEY`
4. Value: your `gsk_...` key

### 3. Configure Search Settings

Use the [Settings UI](https://eranCat.github.io/daily-job-matcher/) to set:
- Schedule (cron expression)
- Skills & stack preferences
- Max years of experience
- Location filters
- Blacklisted companies, roles, and stacks
- Score threshold

Or edit `config/search-settings.json` directly.

### 4. Run Workflow

**Automatic:** runs on the schedule you configure (default 9 AM weekdays)

**Manual:** use the "Run workflow now" button in the [Settings UI](https://eranCat.github.io/daily-job-matcher/), or go to Actions → Daily Job Matcher → Run workflow

## Project Structure

```
├── .github/workflows/
│   └── daily-job-matcher.yml     # Workflow definition
├── config/
│   └── search-settings.json       # Active search configuration
├── docs/
│   └── index.html                 # Settings UI (GitHub Pages)
├── scripts/
│   └── job_matcher.py             # Main search/match script (Groq)
└── README.md
```

## Why Groq?

- **Free tier:** 14,400 requests/day, no credit card required
- **Fast:** ~500 tok/s inference (Llama 3.3 70B responds in <2s)
- **Quality:** Llama 3.3 70B handles structured JSON output reliably
- **Zero billing surprises:** hard rate limit, not metered

Switching back to Claude or OpenAI is a one-line change in `scripts/job_matcher.py`.

## Settings UI Authentication

The settings page requires a GitHub Personal Access Token with `repo` scope to save changes and trigger workflow runs.

**To create one:**
1. Visit [github.com/settings/tokens](https://github.com/settings/tokens/new?scopes=repo&description=Job%20Matcher%20Settings)
2. Select `repo` scope
3. Generate and copy the token
4. Paste into the settings page (stored only in your browser's localStorage)

## Monitoring

- **Logs:** Actions tab → workflow run → job logs (matched jobs appear here)
- **Live status:** the Settings UI shows run progress after you click "Run workflow now"

## Troubleshooting

**Settings UI shows "Not connected"?**
- Ensure your token has `repo` scope
- Try regenerating the token if it's expired

**Workflow fails with "GROQ_API_KEY not set"?**
- Verify the secret is added to repo Settings → Secrets → Actions
- Secret name must be exactly `GROQ_API_KEY`

**Workflow fails with "Rate limit exceeded"?**
- Groq free tier allows 14,400 requests/day — you'd need to hit the API very heavily
- Check [console.groq.com/settings/limits](https://console.groq.com/settings/limits)

**Too many/few matches?**
- Adjust `minScore` in the settings UI
- Add/remove skills or excluded keywords
