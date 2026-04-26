#!/usr/bin/env python3
"""Daily job matcher — Real APIs + LLM scoring.

Architecture:
  1. Fetch REAL job listings from free APIs (Jobicy, RemoteOK, Himalayas)
     and scrape Israeli boards (Drushim, AllJobs) — all real URLs
  2. Filter by basic keyword/location criteria (no LLM)
  3. Batch-score shortlisted jobs with Groq (LLM scores only, never invents URLs)
  4. Verify links are live (HEAD request)
  5. Append passing jobs to Google Sheets

Run modes (RUN_MODE env var): search | test-connection | test-write
Required secrets: GROQ_API_KEY, GOOGLE_SA_KEY, GOOGLE_SHEETS_ID
"""

import os, json, sys, re, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib import request as urlreq, error as urlerr, parse as urlparse

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Constants ────────────────────────────────────────────────────────────────
GROQ_API_URL  = "https://api.groq.com/openai/v1/chat/completions"
MODEL         = "llama-3.3-70b-versatile"
BROWSER_UA    = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
SHEET_TAB     = "Saved Jobs"
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
JERUSALEM_TZ  = timezone(timedelta(hours=3))

# post-date filter → how many seconds back to accept
POST_DATE_SECONDS = {"24h": 86400, "3d": 259200, "7d": 604800,
                     "14d": 1209600, "30d": 2592000}

# ── Config ───────────────────────────────────────────────────────────────────
def load_settings():
    path = Path(__file__).resolve().parent.parent / "config" / "search-settings.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {
        "skills": ["React","TypeScript","Python","FastAPI","Node.js","Docker"],
        "maxYears": 2.5,
        "locations": ["Tel Aviv","Ramat Gan","Herzliya","Holon","Petah Tikva","Remote"],
        "remoteOk": True,
        "excludedCompanies": ["Experis","Manpower","Infinity Labs"],
        "excludedKeywords": ["senior","lead","manager","principal"],
        "excludedStacks": ["PHP",".NET","C#","Ruby"],
        "minScore": 7,
        "maxResults": 30,
        "postDateFilter": "7d",
        "verifyLinks": True,
        "jobBoards": {
            "jobicy": True, "remoteOk": True, "himalayas": True,
            "drushim": True, "alljobs": True,
        }
    }

# ── HTTP helpers ─────────────────────────────────────────────────────────────
def http_get(url, timeout=20, headers=None):
    h = {"User-Agent": BROWSER_UA, **(headers or {})}
    req = urlreq.Request(url, headers=h)
    with urlreq.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")

def verify_link(url, timeout=8):
    if not url or not url.startswith("http"):
        return False
    for method in ("HEAD", "GET"):
        try:
            req = urlreq.Request(url, method=method, headers={"User-Agent": BROWSER_UA})
            with urlreq.urlopen(req, timeout=timeout) as r:
                return r.status < 400
        except Exception:
            continue
    return False

# ── Job board fetchers ────────────────────────────────────────────────────────
def _age_ok(ts_seconds, max_age_s):
    """Return True if timestamp (unix) is within max_age_s of now."""
    if not ts_seconds:
        return True          # unknown age → include
    return (time.time() - ts_seconds) <= max_age_s

def fetch_jobicy(settings, max_age_s):
    boards = settings.get("jobBoards", {})
    if not boards.get("jobicy"):
        return []
    try:
        raw = http_get("https://jobicy.com/api/v2/remote-jobs?count=50&tag=developer")
        data = json.loads(raw)
        jobs = []
        for j in data.get("jobs", []):
            pub = j.get("pubDate", "")
            ts = None
            try:
                from email.utils import parsedate_to_datetime
                ts = parsedate_to_datetime(pub).timestamp() if pub else None
            except Exception:
                pass
            if not _age_ok(ts, max_age_s):
                continue
            jobs.append({
                "role": j.get("jobTitle", ""),
                "company": j.get("companyName", ""),
                "location": j.get("jobGeo", "Remote"),
                "link": j.get("url", ""),
                "source": "Jobicy",
            })
        print(f"  Jobicy: {len(jobs)} listings")
        return jobs
    except Exception as e:
        print(f"  Jobicy fetch error: {e}")
        return []

def fetch_remoteok(settings, max_age_s):
    boards = settings.get("jobBoards", {})
    if not boards.get("remoteOk"):
        return []
    try:
        raw = http_get("https://remoteok.com/api", headers={"Accept": "application/json"})
        data = json.loads(raw)
        jobs = []
        for j in data:
            if not isinstance(j, dict) or "slug" not in j:
                continue
            ts = j.get("epoch")
            if not _age_ok(ts, max_age_s):
                continue
            jobs.append({
                "role": j.get("position", ""),
                "company": j.get("company", ""),
                "location": "Remote",
                "link": f"https://remoteok.com/remote-jobs/{j.get('slug','')}",
                "source": "RemoteOK",
            })
        print(f"  RemoteOK: {len(jobs)} listings")
        return jobs
    except Exception as e:
        print(f"  RemoteOK fetch error: {e}")
        return []

def fetch_himalayas(settings, max_age_s):
    boards = settings.get("jobBoards", {})
    if not boards.get("himalayas"):
        return []
    try:
        raw = http_get("https://himalayas.app/jobs/api?limit=50&q=developer")
        data = json.loads(raw)
        jobs = []
        for j in data.get("jobs", []):
            pub = j.get("createdAt", "")
            ts = None
            try:
                ts = datetime.fromisoformat(pub.rstrip("Z")).replace(
                    tzinfo=timezone.utc).timestamp() if pub else None
            except Exception:
                pass
            if not _age_ok(ts, max_age_s):
                continue
            jobs.append({
                "role": j.get("title", ""),
                "company": j.get("company", {}).get("name", ""),
                "location": "Remote",
                "link": j.get("applicationUrl") or j.get("url", ""),
                "source": "Himalayas",
            })
        print(f"  Himalayas: {len(jobs)} listings")
        return jobs
    except Exception as e:
        print(f"  Himalayas fetch error: {e}")
        return []

def _scrape_drushim(settings, max_age_s):
    """Fetch Drushim hi-tech jobs via RSS (API requires JS session, HTML is SPA)."""
    boards = settings.get("jobBoards", {})
    if not boards.get("drushim"):
        return []
    # Tech category names as they appear in the Drushim RSS <category> tag
    TECH_CATS = {"הייטק-תוכנה", "הייטק-כללי", "הנדסה", "מחשבים"}
    try:
        raw = http_get("https://www.drushim.co.il/rss/", timeout=15)
        items_raw = re.findall(r'<item>(.*?)</item>', raw, re.DOTALL)
        jobs = []
        for item in items_raw:
            cat_m = re.search(r'<category[^>]*>(.*?)</category>', item)
            if not cat_m or not any(tc in cat_m.group(1) for tc in TECH_CATS):
                continue
            title_m   = re.search(r'<title[^>]*>(.*?)</title>', item)
            company_m = re.search(r'<company>(.*?)</company>', item)
            link_m    = re.search(r'<link>(.*?)</link>', item)
            title   = (title_m.group(1)   if title_m   else "").strip()
            company = (company_m.group(1) if company_m else "").strip()
            link    = (link_m.group(1)    if link_m    else "").strip()
            if not title or not link:
                continue
            jobs.append({
                "role": title, "company": company,
                "location": "Israel", "link": link, "source": "Drushim",
            })
        print(f"  Drushim: {len(jobs)} listings")
        return jobs
    except Exception as e:
        print(f"  Drushim fetch error: {e}")
        return []

def _scrape_alljobs(settings, max_age_s):
    """AllJobs is protected by Radware bot-detection — scraping unavailable."""
    boards = settings.get("jobBoards", {})
    if not boards.get("alljobs"):
        return []
    print("  AllJobs: 0 listings (Radware bot-protection blocks scraping)")
    return []

def fetch_all_jobs(settings):
    boards = settings.get("jobBoards", {})
    max_age_s = POST_DATE_SECONDS.get(settings.get("postDateFilter", "7d"), 604800)
    all_jobs = []
    if boards.get("jobicy"):      all_jobs += fetch_jobicy(settings, max_age_s)
    if boards.get("remoteOk"):    all_jobs += fetch_remoteok(settings, max_age_s)
    if boards.get("himalayas"):   all_jobs += fetch_himalayas(settings, max_age_s)
    if boards.get("drushim"):     all_jobs += _scrape_drushim(settings, max_age_s)
    if boards.get("alljobs"):     all_jobs += _scrape_alljobs(settings, max_age_s)
    return all_jobs

# ── Pre-filter (no LLM) ───────────────────────────────────────────────────────
def pre_filter(jobs, settings):
    """Fast keyword-based filter before hitting the LLM."""
    excluded_companies = [c.lower() for c in settings.get("excludedCompanies", [])]
    excluded_keywords  = [k.lower() for k in settings.get("excludedKeywords", [])]
    excluded_stacks    = [s.lower() for s in settings.get("excludedStacks", [])]
    allowed_locations  = [l.lower() for l in settings.get("locations", [])]
    skills             = [s.lower() for s in settings.get("skills", [])]

    passed, dropped = [], 0
    for j in jobs:
        role_raw = j.get("role", "")
        role     = role_raw.lower()
        company  = (j.get("company", "")).lower()
        loc      = (j.get("location", "")).lower()

        # Excluded company
        if any(ex == company or ex in company for ex in excluded_companies):
            dropped += 1; continue
        # Excluded title keyword
        if any(kw in role for kw in excluded_keywords):
            dropped += 1; continue
        # Excluded stack in title
        if any(st in role for st in excluded_stacks):
            dropped += 1; continue
        # Must mention at least one skill OR be a dev role (English or Hebrew)
        has_skill = any(sk in role for sk in skills)
        is_dev_role = any(w in role for w in
            ["developer","engineer","full stack","fullstack","backend","frontend","software"]) or \
            any(w in role_raw for w in
            ["מפתח",""מהנדס","פולסטאק","פול סטאק","תוכנה","מתכנת"])
        if not has_skill and not is_dev_role:
            dropped += 1; continue
        # Location check (skip for remote boards)
        if j.get("source") not in ("Jobicy","RemoteOK","Himalayas"):
            is_remote = any(w in loc for w in ["remote","hybrid"])
            loc_ok    = any(al in loc for al in allowed_locations)
            if not is_remote and not loc_ok:
                dropped += 1; continue

        passed.append(j)

    print(f"  Pre-filter: {len(passed)} passed, {dropped} dropped")
    return passed

# ── LLM scoring ───────────────────────────────────────────────────────────────
def score_jobs_with_llm(jobs, settings, api_key):
    """Score a batch of real jobs. LLM only assigns scores — never invents URLs."""
    if not jobs:
        return []

    skills     = settings.get("skills", [])
    max_years  = settings.get("maxYears", 2.5)
    min_score  = settings.get("minScore", 7)
    max_r      = settings.get("maxResults", 30)

    # Build minimal job list for the prompt (no URLs to hallucinate from)
    job_list = "\n".join(
        f"{i+1}. [{j['source']}] {j['role']} @ {j['company']} — {j['location']}"
        for i, j in enumerate(jobs)
    )

    system = "You are a job scoring engine. Output only valid JSON, no prose, no markdown."
    prompt = f"""Score each job listing for a junior full-stack developer with these skills: {', '.join(skills)}.
Max experience required: {max_years} years.

Score 0-10 where:
10 = perfect match (uses preferred stack, junior/mid level, good location)
7-9 = strong match
4-6 = partial match
0-3 = poor match (wrong stack, too senior, unrelated)

Jobs to score:
{job_list}

Return JSON:
{{"scores": [{{"index": 1, "score": 8, "reason": "..."}}  /* one entry per job */]}}"""

    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        "max_tokens": 2048,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }).encode()

    req = urlreq.Request(GROQ_API_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "User-Agent":    BROWSER_UA,
    })
    try:
        with urlreq.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode())
    except urlerr.HTTPError as e:
        raise RuntimeError(f"Groq {e.code}: {e.read().decode()}")

    content = resp["choices"][0]["message"]["content"]
    parsed  = json.loads(content)
    scores  = {entry["index"]: entry for entry in parsed.get("scores", [])}

    scored = []
    for i, job in enumerate(jobs, start=1):
        entry = scores.get(i, {})
        score = entry.get("score", 0)
        if score >= min_score:
            job["match_score"] = score
            job["reason"]      = entry.get("reason", "")
            scored.append(job)

    scored.sort(key=lambda j: j["match_score"], reverse=True)
    return scored[:max_r]

# ── Sheets ────────────────────────────────────────────────────────────────────
def get_sheets_client():
    sa_json = os.getenv("GOOGLE_SA_KEY")
    if not sa_json:
        raise ValueError("GOOGLE_SA_KEY not set")
    creds   = service_account.Credentials.from_service_account_info(
        json.loads(sa_json), scopes=SHEETS_SCOPES)
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return service.spreadsheets(), json.loads(sa_json).get("client_email", "?")

def require_sheet_id():
    sid = os.getenv("GOOGLE_SHEETS_ID")
    if not sid:
        raise ValueError("GOOGLE_SHEETS_ID not set")
    return sid

def get_existing_links(sheets, sheet_id):
    try:
        resp = sheets.values().get(
            spreadsheetId=sheet_id, range=f"{SHEET_TAB}!E2:E").execute()
    except HttpError as e:
        if e.resp.status == 400: return set()
        raise
    return {r[0].strip() for r in resp.get("values", []) if r and r[0].strip()}

def append_rows(sheets, sheet_id, rows):
    return sheets.values().append(
        spreadsheetId=sheet_id, range=f"{SHEET_TAB}!A:F",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": rows}).execute()

def job_to_row(job, today, is_test=False):
    return [
        today,
        job.get("role", ""),
        job.get("company", ""),
        job.get("location", ""),
        (job.get("link") or "").strip(),
        "TEST" if is_test else "Saved",
    ]

def get_sheet_gid(sheets, sheet_id, tab_name):
    meta = sheets.get(spreadsheetId=sheet_id, includeGridData=False).execute()
    for s in meta.get("sheets", []):
        p = s["properties"]
        if p["title"] == tab_name:
            return p["sheetId"]
    raise RuntimeError(f"Tab {tab_name!r} not found")

def parse_row_index(updated_range):
    cell = updated_range.split("!")[-1].split(":")[0]
    return int("".join(c for c in cell if c.isdigit())) - 1

def delete_row(sheets, sheet_id, gid, row_idx):
    sheets.batchUpdate(spreadsheetId=sheet_id, body={"requests": [{
        "deleteDimension": {"range": {
            "sheetId": gid, "dimension": "ROWS",
            "startIndex": row_idx, "endIndex": row_idx + 1
        }}
    }]}).execute()

# ── Run modes ─────────────────────────────────────────────────────────────────
def run_search():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY not set")

    settings = load_settings()
    print(f"=== Settings: boards={[k for k,v in settings.get('jobBoards',{}).items() if v]}, "
          f"minScore={settings.get('minScore')}, maxResults={settings.get('maxResults')} ===\n")

    # 1. Fetch real listings
    print("[1/5] Fetching listings from job boards...")
    raw_jobs = fetch_all_jobs(settings)
    print(f"  Total fetched: {len(raw_jobs)}\n")

    # 2. Pre-filter (no LLM)
    print("[2/5] Pre-filtering...")
    shortlist = pre_filter(raw_jobs, settings)
    print()

    if not shortlist:
        print("No jobs passed pre-filter. Done.")
        return

    # 3. Score with LLM (real jobs, no URL fabrication)
    print(f"[3/5] Scoring {len(shortlist)} jobs with LLM...")
    scored = score_jobs_with_llm(shortlist, settings, api_key)
    print(f"  {len(scored)} jobs scored >= {settings.get('minScore', 7)}\n")

    if not scored:
        print("No jobs met the score threshold.")
        return

    # 4. Verify links
    verify = settings.get("verifyLinks", True)
    verified = []
    print(f"[4/5] Verifying {len(scored)} links{' (skipped)' if not verify else ''}...")
    for j in scored:
        link = (j.get("link") or "").strip()
        if not verify or verify_link(link):
            verified.append(j)
            print(f"  ✓ {j['role']} @ {j['company']} [{j['source']}] score={j['match_score']}")
        else:
            print(f"  ✗ Broken link: {j['role']} @ {j['company']} — {link}")
    print()

    if not verified:
        print("No jobs with live links. Done.")
        return

    # 5. Sync to Sheets
    print(f"[5/5] Syncing {len(verified)} jobs to Google Sheets...")
    sheets, sa_email = get_sheets_client()
    sheet_id  = require_sheet_id()
    existing  = get_existing_links(sheets, sheet_id)
    today     = datetime.now(JERUSALEM_TZ).strftime("%d/%m/%Y")

    rows, dupes = [], 0
    for j in verified:
        link = (j.get("link") or "").strip()
        if link and link in existing:
            dupes += 1
            continue
        rows.append(job_to_row(j, today))

    if rows:
        resp    = append_rows(sheets, sheet_id, rows)
        updated = resp.get("updates", {}).get("updatedRows", 0)
        print(f"  ✓ Appended {updated} rows (skipped {dupes} duplicates)")
    else:
        print(f"  All jobs were duplicates, nothing appended")

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
    print(f"\n✓ Connection OK\n  Sheet: {title!r}\n  Tabs: {tabs}\n  Header: {header}")

def run_test_write():
    sheets, sa_email = get_sheets_client()
    sheet_id = require_sheet_id()
    now = datetime.now(JERUSALEM_TZ)
    test_job = {
        "role": f"TEST ROW — {now.strftime('%d/%m/%Y %H:%M')} IDT",
        "company": "daily-job-matcher", "location": "GitHub Actions",
        "link": f"https://github.com/eranCat/daily-job-matcher?ts={int(now.timestamp())}",
        "match_score": 0,
    }
    row  = job_to_row(test_job, now.strftime("%d/%m/%Y"), is_test=True)
    resp = append_rows(sheets, sheet_id, [row])
    rng  = resp.get("updates", {}).get("updatedRange", "")
    print(f"\n✓ Test row written at {rng}")
    if rng:
        try:
            idx = parse_row_index(rng)
            gid = get_sheet_gid(sheets, sheet_id, SHEET_TAB)
            delete_row(sheets, sheet_id, gid, idx)
            print(f"✓ Test row deleted (row {idx+1} removed)")
        except Exception as e:
            print(f"⚠ Cleanup failed: {e}\n  Delete {rng} manually.")

MODE_HANDLERS = {
    "search": run_search,
    "test-connection": run_test_connection,
    "test-write": run_test_write,
}

def main():
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
        print(f"\n✗ Error: {e}")
        sys.exit(1)
