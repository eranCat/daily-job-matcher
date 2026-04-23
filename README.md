# Daily Job Matcher

Automated job search for junior full-stack and backend developers in Israel.

## Overview

GitHub Actions workflow that runs on a schedule to:
- Search multiple job boards (LinkedIn, Indeed, etc.)
- Filter roles by skills, experience level, and location
- Evaluate matches against your profile
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

### 3. Run Workflow

**Automatic:** 9 AM every weekday (customizable)

**Manual:** Go to **Actions** → **Daily Job Matcher** → **Run workflow**

## Configuration

Edit `.github/workflows/daily-job-matcher.yml` to change:

| Setting | Default | Notes |
|---------|---------|-------|
| Schedule | `0 7 * * 1-5` | 9 AM UTC, Mon-Fri (7 AM = 9 AM Jerusalem) |
| Daily | `0 7 * * *` | Every day at 9 AM UTC |
| Every 6h | `0 */6 * * *` | 4x per day |

## Filters

Automatically excludes:
- **Experience:** 3+ years required
- **Companies:** Staffing firms (Experis, Manpower), bootcamps
- **Roles:** Senior, management, data science, ML
- **Stacks:** PHP, .NET, C#, Ruby
- **Locations:** Remote outside Israel

Matches based on:
- React, TypeScript, Python, FastAPI, Node.js, Docker, PostgreSQL, Firebase
- Central Israel: Tel Aviv, Ramat Gan, Herzliya, Holon, Ness Ziona, Rehovot

## Monitoring

- **Logs:** Actions tab → workflow run → job logs
- **Success:** Jobs saved to your sheet with score ≥ 7/10
- **Failure:** Check logs for API/permissions issues

## Advanced

Modify `scripts/job_matcher.py` to:
- Add custom filters
- Change scoring logic
- Add job boards
- Post to Slack/email on matches

## Troubleshooting

**Workflow not running?**
- Verify API key is set in Secrets
- Check workflow status in Actions tab
- Ensure `.github/workflows/daily-job-matcher.yml` exists

**Jobs not saving?**
- Confirm sheet ID is correct in workflow
- Check script logs for Google Sheets API errors
- Verify sheet has correct column names

**Too many/few matches?**
- Adjust filters in `scripts/job_matcher.py`
- Modify cron schedule to run less frequently
- Add/remove skills from the matching rubric
