#!/usr/bin/env python3
"""Daily job matcher using Groq API + Google Sheets API (service account auth).

Run modes (via RUN_MODE env var):
  search           - normal run: query LLM for jobs, append matches to the sheet
  test-connection  - verify sheet is accessible via service account
  test-write       - append a synthetic test row

Required secrets:
  GROQ_API_KEY      - free key from https://console.groq.com/keys
  GOOGLE_SA_KEY     - full JSON contents of a service account key file
  GOOGLE_SHEETS_ID  - the spreadsheet ID from the sheet URL
"""

import os
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib import request as urlreq, error as urlerr

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "llama-3.3-70b-versatile"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

SHEET_TAB = "Saved Jobs"
# Column layout: DATE | ROLE | COMPANY | LOCATION | LINK | STATUS
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Asia/Jerusalem UTC+3 (IDT) most of the year; Python stdlib doesn't ship tz data, so fix offset
JERUSALEM_TZ = timezone(timedelta(hours=3))


# ---- Config ----

def load_settings():
    path = Path(__file__).resolve().parent.parent / "config" / "search-settings.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    print("No config found, using defaults")
    return {
        "skills": ["React", "TypeScript", "Python", "FastAPI", "Node.js", "Docker"],
        "maxYears": 2.5,
        "locations": ["Tel Aviv", "Ramat Gan", "Herzliya", "Holon"],
        "remoteOk": True,
        "remoteIsraelOnly": True,
        "excludedCompanies": ["Experis", "Manpower", "Infinity Labs"],
        "excludedKeywords": ["senior", "lead", "manager"],
        "excludedStacks": ["PHP", ".NET", "C#", "Ruby"],
        "minScore": 7,
        "maxResults": 20,
    }


# ---- Groq ----

def build_prompt(settings):
    # Build active boards list
    boards = settings.get("jobBoards", {})
    active_boards = [name for name, enabled in boards.items() if enabled]
    boards_line = ", ".join(active_boards) if active_boards else "LinkedIn, Indeed, Drushim, AllJobs (defaults)"

    return f"""You are a job search automation assistant for a junior full-stack/backend developer in Israel.

SEARCH CRITERIA:
- Preferred skills: {", ".join(settings.get("skills", []))}
- Max years of experience required: {settings.get("maxYears", 2.5)}
- Target locations: {", ".join(settings.get("locations", []))}
- Allow remote: {settings.get("remoteOk", True)}
- Remote-Israel only: {settings.get("remoteIsraelOnly", True)}

EXCLUSIONS (skip these):
- Companies: {", ".join(settings.get("excludedCompanies", []))}
- Role-title keywords: {", ".join(settings.get("excludedKeywords", []))}
- Tech stacks: {", ".join(settings.get("excludedStacks", []))}

JOB BOARDS TO FOCUS ON:
{boards_line}

TASK: Search the above job boards for up to {settings.get("maxResults", 20)} currently-likely open roles that match this profile. For each, provide a match_score 0-10 against the criteria. Only include jobs with score >= {settings.get("minScore", 7)}. Prioritize roles from the specified boards.

Return STRICTLY valid JSON (no markdown, no prose):
{{
  "jobs_found": N,
  "jobs": [
    {{"role": "...", "company": "...", "location": "...", "link": "...", "match_score": N}}
  ]
}}"""


def call_groq(api_key, prompt):
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You output only valid JSON as instructed. No markdown fences, no prose."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 4096,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")

    req = urlreq.Request(
        GROQ_API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": BROWSER_UA,
        },
        method="POST",
    )
    try:
        with urlreq.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urlerr.HTTPError as e:
        raise RuntimeError(f"Groq API {e.code}: {e.read().decode('utf-8')}")

    return data["choices"][0]["message"]["content"]


# ---- Sheets ----

def get_sheets_client():
    """Build an authenticated Sheets API client from the service account JSON."""
    sa_json = os.getenv("GOOGLE_SA_KEY")
    if not sa_json:
        raise ValueError(
            "GOOGLE_SA_KEY secret not set. Paste the full service account JSON "
            "into repo Settings → Secrets → Actions as GOOGLE_SA_KEY."
        )
    try:
        sa_info = json.loads(sa_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"GOOGLE_SA_KEY is not valid JSON: {e}")

    credentials = service_account.Credentials.from_service_account_info(
        sa_info, scopes=SHEETS_SCOPES
    )
    service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    return service.spreadsheets(), sa_info.get("client_email", "unknown")


def require_sheet_id():
    sheet_id = os.getenv("GOOGLE_SHEETS_ID")
    if not sheet_id:
        raise ValueError(
            "GOOGLE_SHEETS_ID secret not set. Add your spreadsheet ID to repo Secrets."
        )
    return sheet_id


def get_existing_links(sheets, sheet_id):
    """Read the LINK column (E) to dedupe future appends."""
    try:
        resp = sheets.values().get(
            spreadsheetId=sheet_id,
            range=f"{SHEET_TAB}!E2:E",
        ).execute()
    except HttpError as e:
        if e.resp.status == 400:
            return set()  # sheet may be empty
        raise
    values = resp.get("values", [])
    return {row[0].strip() for row in values if row and row[0].strip()}


def append_rows(sheets, sheet_id, rows):
    """Append rows to the Saved Jobs tab."""
    return sheets.values().append(
        spreadsheetId=sheet_id,
        range=f"{SHEET_TAB}!A:F",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def job_to_row(job, today, is_test=False):
    score = job.get("match_score", "?")
    status = f"{'TEST' if is_test else 'NEW'} (score: {score})"
    return [
        today,
        job.get("role", ""),
        job.get("company", ""),
        job.get("location", ""),
        (job.get("link") or "").strip(),
        status,
    ]


# ---- Run modes ----

def run_search():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY not set. Get one at https://console.groq.com/keys")

    settings = load_settings()
    prompt = build_prompt(settings)

    print(f"Mode: search | Model: {MODEL}")
    print(f"Config: {len(settings.get('skills', []))} skills, min score {settings.get('minScore', 7)}, max {settings.get('maxResults', 20)} results")

    content = call_groq(api_key, prompt)
    print("\n=== Groq response (preview) ===")
    print(content[:1500])

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}") + 1
        result = json.loads(content[start:end])

    jobs = result.get("jobs", [])
    print(f"\n✓ Matched {len(jobs)} jobs")

    if not jobs:
        return result

    # Sync to Sheets
    sheets, sa_email = get_sheets_client()
    sheet_id = require_sheet_id()

    existing = get_existing_links(sheets, sheet_id)
    today = datetime.now(JERUSALEM_TZ).strftime("%Y-%m-%d")
    rows_to_append = []
    skipped = 0
    for job in jobs:
        link = (job.get("link") or "").strip()
        if link and link in existing:
            skipped += 1
            continue
        rows_to_append.append(job_to_row(job, today))

    if rows_to_append:
        resp = append_rows(sheets, sheet_id, rows_to_append)
        updated = resp.get("updates", {}).get("updatedRows", 0)
        print(f"✓ Appended {updated} rows (skipped {skipped} duplicates)")
    else:
        print(f"  All {len(jobs)} matches were duplicates, nothing appended")

    return result


def run_test_connection():
    sheets, sa_email = get_sheets_client()
    sheet_id = require_sheet_id()

    print(f"Mode: test-connection")
    print(f"Service account: {sa_email}")
    print(f"Target sheet:    {sheet_id}")

    try:
        meta = sheets.get(spreadsheetId=sheet_id, includeGridData=False).execute()
    except HttpError as e:
        if e.resp.status == 403:
            raise RuntimeError(
                f"✗ Permission denied. Share the sheet with {sa_email} as Editor."
            )
        if e.resp.status == 404:
            raise RuntimeError(
                f"✗ Sheet {sheet_id!r} not found. Check the ID."
            )
        raise

    title = meta.get("properties", {}).get("title", "?")
    tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
    print(f"\n✓ Connection OK")
    print(f"  Spreadsheet: {title!r}")
    print(f"  Tabs: {tabs}")
    if SHEET_TAB not in tabs:
        print(f"  ⚠  Expected tab {SHEET_TAB!r} not found. Writes will fail until you create it.")
    else:
        # Try a small read to confirm read access
        resp = sheets.values().get(
            spreadsheetId=sheet_id,
            range=f"{SHEET_TAB}!A1:F1",
        ).execute()
        header = resp.get("values", [[]])[0]
        print(f"  Header row: {header}")

    return {"ok": True, "sheet": title, "tabs": tabs}


def run_test_write():
    sheets, sa_email = get_sheets_client()
    sheet_id = require_sheet_id()

    print(f"Mode: test-write")
    print(f"Service account: {sa_email}")

    now = datetime.now(JERUSALEM_TZ)
    test_job = {
        "role": f"TEST ROW — {now.strftime('%Y-%m-%d %H:%M:%S')} IDT",
        "company": "daily-job-matcher",
        "location": "GitHub Actions",
        "link": f"https://github.com/eranCat/daily-job-matcher/actions?ts={int(now.timestamp())}",
        "match_score": 0,
    }
    row = job_to_row(test_job, now.strftime("%Y-%m-%d"), is_test=True)

    resp = append_rows(sheets, sheet_id, [row])
    updated = resp.get("updates", {}).get("updatedRange", "?")
    print(f"\n✓ Test row written at {updated}")
    print(f"  Row: {row}")
    print(f"\n  You can safely delete this row from the sheet after verifying.")
    return {"ok": True, "range": updated}


MODE_HANDLERS = {
    "search": run_search,
    "test-connection": run_test_connection,
    "test-write": run_test_write,
}


def main():
    mode = os.getenv("RUN_MODE", "search").strip()
    handler = MODE_HANDLERS.get(mode)
    if not handler:
        raise ValueError(f"Unknown RUN_MODE: {mode!r}. Valid: {', '.join(MODE_HANDLERS)}")
    print(f"=== Daily Job Matcher (mode={mode}) ===\n")
    handler()
    print("\n=== Done ===")


if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except Exception as e:
        print(f"\n✗ Error: {e}")
        sys.exit(1)
