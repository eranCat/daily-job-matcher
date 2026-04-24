# Daily Job Matcher

Automated job search for junior full-stack and backend developers in Israel.

## 🎯 Configure via Web UI

**[→ Open Settings Page](https://eranCat.github.io/daily-job-matcher/)**

Edit filters, skills, locations, and schedules directly from your browser. Changes commit to the repo and take effect on the next workflow run.

## Overview

GitHub Actions workflow that runs on a schedule to:
- Search job boards (LinkedIn, Indeed, and more)
- Filter roles by skills, experience level, and location
- Score matches against your configured profile
- Automatically save suitable jobs to Google Sheets

## Quick Start

### 1. Add Your API Key

1. Go to **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret**
3. Name: `ANTHROPIC_API_KEY`
4. Value: Get from [Anthropic Console](https://console.anthropic.com/)

### 2. Configure Your Google Sheets

Update the sheet ID in `.github/workflows/daily-job-matcher.yml`:
```yaml
env:
  GOOGLE_SHEETS_ID: your-sheet-id-here
```

Your sheet should have columns:
- DATE | ROLE | COMPANY | LOCATION | LINK | STATUS

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

**Manual:** Actions → Daily Job Matcher → Run workflow

## Project Structure

```
├── .github/workflows/
│   └── daily-job-matcher.yml     # Workflow definition
├── config/
│   └── search-settings.json       # Active search configuration
├── docs/
│   └── index.html                 # Settings UI (GitHub Pages)
├── scripts/
│   └── job_matcher.py             # Main search/match script
└── README.md
```

## Settings UI Authentication

The settings page requires a GitHub Personal Access Token with `repo` scope to save changes.

**To create one:**
1. Visit [github.com/settings/tokens](https://github.com/settings/tokens/new?scopes=repo&description=Job%20Matcher%20Settings)
2. Select `repo` scope
3. Generate and copy the token
4. Paste into the settings page (stored only in your browser's localStorage)

The token is used only for GitHub API calls directly from your browser — never sent anywhere else.

## Monitoring

- **Logs:** Actions tab → workflow run → job logs
- **Success:** Matches appear in your Google Sheet
- **Failure:** Check logs for API/permissions issues

## Troubleshooting

**Settings UI shows "Not connected"?**
- Ensure your token has `repo` scope
- Try regenerating the token if it's expired

**Workflow not running?**
- Verify `ANTHROPIC_API_KEY` is set in repo Secrets
- Check the Actions tab is enabled

**Jobs not saving?**
- Confirm sheet ID is correct in the workflow file
- Check script logs for Google Sheets API errors

**Too many/few matches?**
- Adjust `minScore` in the settings UI
- Add/remove skills or excluded keywords
