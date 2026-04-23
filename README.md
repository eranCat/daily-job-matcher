# Daily Job Matcher

GitHub Actions workflow that runs daily to search job boards and save suitable matches to Google Sheets.

## Setup

1. Add GitHub Secret: `ANTHROPIC_API_KEY` with your Anthropic API key
2. Workflow runs at **9 AM Israel time, weekdays** (customizable in `.github/workflows/daily-job-matcher.yml`)
3. Jobs are saved to: `1zk3vAgTwKmcn4xgwrue54KyUqRYSlvrkr9azOWeCKno`

## Run manually

Go to Actions → "Daily Job Matcher" → "Run workflow"

## Filters

- Skills: React, TypeScript, Python, FastAPI, Node.js, Docker
- Experience: Max 2.5 years
- Location: Central Israel or remote-Israel
- Excludes: Experis, Infinity Labs, bootcamps, senior roles