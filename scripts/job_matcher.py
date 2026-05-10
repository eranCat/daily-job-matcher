#!/usr/bin/env python3
"""Daily job matcher — fetches IL job listings, scores with Gemini, writes to Google Sheets.

Sources: Greenhouse / Lever / Ashby / Comeet (IL boards) + Drushim.
Run modes (RUN_MODE env var): search | test-connection | test-write
Required secrets: GOOGLE_SA_KEY or GOOGLE_SA_KEY_PATH, GOOGLE_SHEETS_ID.
Optional: GEMINI_API_KEY (falls back to keyword scoring without it).
"""

import os, sys
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
    sys.stderr = open(sys.stderr.fileno(), mode="w", encoding="utf-8", buffering=1)

from googleapiclient.errors import HttpError

from utils import _load_il_hints, load_settings, load_keywords, verify_link, JERUSALEM_TZ
from fetchers import fetch_all_jobs
from filters import pre_filter
from scorer import score_jobs_with_llm
from sheets import (
    get_sheets_client, require_sheet_id, get_existing_links, append_rows,
    job_to_row, get_sheet_gid, parse_row_index, delete_row, SHEET_TAB,
)


def run_search():
    settings = load_settings()
    keywords = load_keywords()

    print(f"=== Settings: boards={[k for k,v in settings.get('jobBoards',{}).items() if v]}, "
          f"minScore={settings.get('minScore')}, maxResults={settings.get('maxResults')} ===\n")

    active_boards = [k for k, v in settings.get("jobBoards", {}).items() if v]
    print("::notice title=progress::[1/5] fetch", flush=True)
    print(f"[1/5] Fetching from {len(active_boards)} job boards...")
    raw_jobs = fetch_all_jobs(settings)
    print(f"::notice title=detail::fetched={len(raw_jobs)}", flush=True)
    print(f"  Total fetched: {len(raw_jobs)}\n")

    print("::notice title=progress::[2/5] filter", flush=True)
    print("[2/5] Pre-filtering by keyword & location...")
    shortlist = pre_filter(raw_jobs, settings, keywords)

    if not shortlist:
        print("No jobs passed pre-filter. Done.")
        return

    # Remove jobs already in the sheet before scoring
    sheets, sa_email = get_sheets_client()
    sheet_id  = require_sheet_id()
    existing_links, existing_cr = get_existing_links(sheets, sheet_id)
    before    = len(shortlist)
    shortlist = [
        j for j in shortlist
        if (j.get("link") or "").strip() not in existing_links
        and (j.get("company", "").strip().lower(), j.get("role", "").strip().lower())
            not in existing_cr
    ]
    skipped   = before - len(shortlist)
    if skipped:
        print(f"  Skipped {skipped} already-seen job(s) (duplicate URL or same company+role)\n")
    if not shortlist:
        print("All filtered jobs already in sheet. Done.")
        return

    print(f"::notice title=progress::[3/5] score {len(shortlist)}", flush=True)
    print(f"[3/5] Scoring with Gemini AI ({len(shortlist)} candidates)...")
    scored = score_jobs_with_llm(shortlist, settings, keywords)
    print(f"::notice title=detail::scored={len(scored)}", flush=True)
    print(f"  {len(scored)} jobs scored >= {settings.get('minScore', 7)}\n")

    if not scored:
        print("No jobs met the score threshold.")
        return

    verify = settings.get("verifyLinks", True)
    verified = []
    print(f"::notice title=progress::[4/5] verify {len(scored)}", flush=True)
    print(f"[4/5] Verifying job links ({len(scored)}){' — skipped' if not verify else ''}...")
    for j in scored:
        link = (j.get("link") or "").strip()
        if not verify or verify_link(link):
            verified.append(j)
            print(f"   {j['role']} @ {j['company']} [{j['source']}] score={j['match_score']}")
        else:
            print(f"   Broken link: {j['role']} @ {j['company']}  {link}")
    print(f"::notice title=detail::verified={len(verified)}", flush=True)
    print()

    if not verified:
        print("No jobs with live links. Done.")
        return

    print(f"::notice title=progress::[5/5] sync {len(verified)}", flush=True)
    print(f"[5/5] Syncing to Google Sheets ({len(verified)} jobs)...")
    today = datetime.now(JERUSALEM_TZ).strftime("%d/%m/%Y")

    rows, dupes = [], 0
    for j in verified:
        link = (j.get("link") or "").strip()
        cr   = (j.get("company", "").strip().lower(), j.get("role", "").strip().lower())
        if (link and link in existing_links) or cr in existing_cr:
            dupes += 1
            continue
        rows.append(job_to_row(j, today))

    if rows:
        resp    = append_rows(sheets, sheet_id, rows)
        updated = resp.get("updates", {}).get("updatedRows", 0)
        print(f"::notice title=detail::appended={updated}", flush=True)
        print(f"   Appended {updated} rows (skipped {dupes} duplicates)")
    else:
        print(f"::notice title=detail::appended=0", flush=True)
        print("  All jobs were duplicates, nothing appended")


def run_test_connection():
    sheets, sa_email = get_sheets_client()
    sheet_id = require_sheet_id()
    print(f"Mode: test-connection\nService account: {sa_email}")
    try:
        meta  = sheets.get(spreadsheetId=sheet_id, includeGridData=False).execute()
    except HttpError as e:
        raise RuntimeError(f"HTTP {e.resp.status}: {e.content.decode()}")
    title = meta["properties"]["title"]
    tabs  = [s["properties"]["title"] for s in meta.get("sheets", [])]
    resp  = sheets.values().get(
        spreadsheetId=sheet_id, range=f"{SHEET_TAB}!A1:F1").execute()
    header = resp.get("values", [[]])[0]
    print(f"\n Connection OK\n  Sheet: {title!r}\n  Tabs: {tabs}\n  Header: {header}")


def run_test_write():
    sheets, sa_email = get_sheets_client()
    sheet_id = require_sheet_id()
    now = datetime.now(JERUSALEM_TZ)
    test_job = {
        "role": f"TEST ROW  {now.strftime('%d/%m/%Y %H:%M')} IDT",
        "company": "daily-job-matcher", "location": "GitHub Actions",
        "link": f"https://github.com/eranCat/daily-job-matcher?ts={int(now.timestamp())}",
        "match_score": 0,
    }
    row  = job_to_row(test_job, now.strftime("%d/%m/%Y"), is_test=True)
    resp = append_rows(sheets, sheet_id, [row])
    rng  = resp.get("updates", {}).get("updatedRange", "")
    print(f"\n Test row written at {rng}")
    if rng:
        try:
            idx = parse_row_index(rng)
            gid = get_sheet_gid(sheets, sheet_id, SHEET_TAB)
            delete_row(sheets, sheet_id, gid, idx)
            print(f" Test row deleted (row {idx+1} removed)")
        except Exception as e:
            print(f"  Cleanup failed: {e}\n  Delete {rng} manually.")


MODE_HANDLERS = {
    "search":          run_search,
    "test-connection": run_test_connection,
    "test-write":      run_test_write,
}


def main():
    _load_il_hints()
    mode    = os.getenv("RUN_MODE", "search").strip()
    handler = MODE_HANDLERS.get(mode)
    if not handler:
        raise ValueError(f"Unknown RUN_MODE: {mode!r}")
    print(f"=== Daily Job Matcher (mode={mode}) ===\n")
    handler()
    print("\n=== Done ===")


if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except Exception as e:
        print(f"\n Error: {e}")
        sys.exit(1)
