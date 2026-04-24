# Daily Job Matcher

Automated job search for junior full-stack and backend developers in Israel.

🔧 **[Settings UI →](https://erancat.github.io/daily-job-matcher/)** — configure filters, schedule, and job boards visually, then copy the generated config into the workflow.

## Overview

GitHub Actions workflow that runs on a schedule to:
- Search multiple job boards (Indeed, AllJobs, Drushim, HireMeTech, SecretHunter)
- Filter roles by skills, experience level, and location
- Score matches against your profile
- Automatically save suitable jobs to Google Sheets

## Quick Start

### 1. Add Your Secrets

Go to **Settings** → **Secrets and variables** → **Actions** and add:

| Secret | Value |
|--------|-------|
| `ANTHROPIC_API_KEY` | Get from [console.anthropic.com](https://console.anthropic.com/) |
| `GOOGLE_SHEETS_ID` | Your Google Sheet ID from the URL |

### 2. Configure Settings

Open the **[Settings UI](https://erancat.github.io/daily-job-matcher/)** to visually configure:
- Skills to match and minimum score
- Allowed locations
- Company blacklist and role exclusions
- Job boards to search
- Schedule (cron)

Click **Generate Config** and paste the output into `.github/workflows/daily-job-matcher.yml`.

### 3. Run Workflow

**Automatic:** 9 AM Israel time every weekday (customizable via Settings UI)

**Manual:** Go to **Actions** → **Daily Job Matcher** → **Run workflow**

## Schedule Reference

| Preset | Cron |
|--------|------|
| Weekdays 9 AM (IL) | `0 7 * * 1-5` |
| Daily 9 AM (IL) | `0 7 * * *` |
| Every 6 hours | `0 */6 * * *` |
| Weekdays 9 AM & 4 PM (IL) | `0 7,14 * * 1-5` |

## Default Filters

**Skills:** React, TypeScript, Python, FastAPI, Node.js, Docker, PostgreSQL, Firebase

**Locations:** Tel Aviv, Ramat Gan, Herzliya, Holon, Bat Yam, Rishon LeZion, Ness Ziona, Rehovot, Petah Tikva, Or Yehuda + Remote-Israel

**Excluded companies:** Experis, Manpower, Allstars, Infinity Labs, Elevation, ITC, Naya, Coding Academy

**Excluded roles:** Senior, Lead, Manager, Staff, Principal, Data Science, ML

**Excluded stacks:** PHP, .NET, C#, Ruby

## Monitoring

- **Logs:** Actions tab → workflow run
- **Results:** Jobs with score ≥ 7 saved to your Google Sheet with `Status = Saved`
- **Failures:** Check logs for API/permissions errors

## Troubleshooting

**Workflow not running?** Verify secrets are set, check the Actions tab is enabled.

**Jobs not saving?** Confirm `GOOGLE_SHEETS_ID` is correct and the sheet has columns: `DATE | ROLE | COMPANY | LOCATION | LINK | STATUS`.

**Too many/few matches?** Use the [Settings UI](https://erancat.github.io/daily-job-matcher/) to adjust filters and regenerate the config.
