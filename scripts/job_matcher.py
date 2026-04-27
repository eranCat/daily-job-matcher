#!/usr/bin/env python3
"""Daily job matcher — Real APIs + LLM scoring.

Architecture:
  1. Fetch REAL job listings from free public APIs:
       - Jobicy / RemoteOK / Himalayas         (remote, JSON APIs)
       - Greenhouse public boards              (Israeli companies, JSON)
       - Lever public boards                   (Israeli companies, JSON)
     Drushim RSS and AllJobs scraping have been removed: the Drushim RSS
     no longer exposes a software-developer category (it returns sales /
     admin / general roles only) and AllJobs is protected by Radware
     bot-detection. Greenhouse/Lever boards of well-known Israeli tech
     companies replace them — they're stable, dated, and dev-rich.
  2. Filter by basic keyword/location criteria (no LLM)
  3. Batch-score shortlisted jobs with Groq (LLM scores only, never invents URLs)
  4. Verify links are live (HEAD request)
  5. Append passing jobs to Google Sheets

Run modes (RUN_MODE env var): search | test-connection | test-write
Required secrets: GROQ_API_KEY, GOOGLE_SA_KEY, GOOGLE_SHEETS_ID
"""

import os, json, sys, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib import request as urlreq, error as urlerr

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

# Module-level max-years sentinels (set at runtime by fetch_greenhouse_il / fetch_lever_il)
_GH_MAX_YEARS = 2.5
_LV_MAX_YEARS = 2.5

# Locations that count as "Israel" on Greenhouse/Lever boards (case-insensitive substring match)
IL_LOCATION_HINTS = [
    "israel", "tel aviv", "tel-aviv", "tlv", "herzliya", "ramat gan",
    "petah tikva", "petach tikva", "holon", "rehovot", "ness ziona",
    "rishon", "bat yam", "haifa", "jerusalem", "yafo", "yokneam",
]

# Curated Israeli companies with verified public Greenhouse boards
# (Confirmed working 2026-04: returns IL job listings)
GREENHOUSE_IL_BOARDS = [
    # Verified 2026-04 — all return Israeli dev job listings
    "jfrog", "similarweb", "yotpo", "forter", "fireblocks",
    "melio", "riskified", "optimove", "via", "nice",
    "payoneer", "appsflyer", "taboola", "axonius", "lightricks", "nanit",
    "sisense",
    # Additional Israeli tech companies on Greenhouse
    "monday", "wix", "outbrain", "ironSource", "check-point", "radware",
    "varonis", "cyberark", "salepoint", "gusto", "allot", "amdocs",
    "imperva", "gilat", "akamai-technologies", "gett", "fundbox",
    "rapyd", "tipalti", "papaya-global", "jit", "orca-security",
    "snyk", "delinea", "aquasec", "palo-alto-networks", "armis",
]

# Curated Israeli companies with verified public Lever boards
LEVER_IL_BOARDS = [
    "walkme",       # Verified: 7+ IL dev jobs
    "cloudinary",   # Verified: IL dev jobs
    "monday",       # Monday.com (also has Lever)
    "wix",          # Wix Engineering
    "lemonade",     # Lemonade Insurance
    "fiverr",       # Fiverr IL dev jobs
    "playtika",     # Playtika Gaming
    "gong",         # Gong.io
    "salto",        # Salto Networks
    "kaltura",      # Kaltura Video
]

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
        "postDateFilter": "30d",
        "verifyLinks": True,
        "jobBoards": {
            "jobicy": True, "remoteOk": True, "himalayas": True,
            "greenhouseIL": True, "leverIL": True,
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

def _age_ok(ts_seconds, max_age_s):
    """Return True if timestamp (unix) is within max_age_s of now."""
    if not ts_seconds:
        return True          # unknown age → include
    return (time.time() - ts_seconds) <= max_age_s

def _is_il_location(loc_str):
    s = (loc_str or "").lower()
    return any(h in s for h in IL_LOCATION_HINTS)

def _strip_html(text):
    """Strip HTML tags and decode common entities."""
    text = re.sub(r'<[^>]+>', ' ', text or '')
    text = re.sub(r'&[a-zA-Z#0-9]+;', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()

def _extract_min_years(text):
    """
    Return the minimum years of experience explicitly mentioned in a job description.
    Returns None when no requirement is found (i.e. treat as unknown / junior-ok).
    """
    t = (_strip_html(text)).lower()
    patterns = [
        r'(\d+)\+\s*years?\s+of\s+\w+(?:\s+\w+){0,3}\s+experience',
        r'(\d+)\+\s*years?\s*(?:of\s+)?(?:experience|exp)',
        r'(\d+)\s*[-\u2013]\s*\d+\s*years?\s*(?:of\s+)?(?:experience|exp)',
        r'at\s+least\s+(\d+)\s*years?\s*(?:of\s+)?(?:experience|exp)',
        r'minimum\s+(?:of\s+)?(\d+)\s*years?\s*(?:of\s+)?(?:experience|exp)',
        r'(\d+)\s+or\s+more\s+years?',
        r'(\d+)\s*years?\s*(?:of\s+)?(?:relevant\s+)?(?:hands.on\s+)?(?:professional\s+)?'
        r'(?:fullstack\s+)?(?:backend\s+)?(?:frontend\s+)?(?:software\s+)?(?:web\s+)?'
        r'(?:development\s+)?experience',
    ]
    found = []
    for p in patterns:
        for m in re.finditer(p, t):
            try:
                found.append(int(m.group(1)))
            except Exception:
                pass
    return min(found) if found else None

# ── Job board fetchers ────────────────────────────────────────────────────────
def fetch_jobicy(settings, max_age_s):
    if not settings.get("jobBoards", {}).get("jobicy"):
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
    if not settings.get("jobBoards", {}).get("remoteOk"):
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
    if not settings.get("jobBoards", {}).get("himalayas"):
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

def _fetch_one_greenhouse(slug, max_age_s):
    """Fetch one Greenhouse board, return Israel-located jobs."""
    try:
        raw = http_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=12)
        data = json.loads(raw)
    except Exception as e:
        print(f"    [gh:{slug}] error: {e}")
        return []
    jobs = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        if not _is_il_location(loc):
            continue
        # Use first_published or updated_at for age check
        ts = None
        for date_field in ("first_published", "updated_at"):
            v = j.get(date_field)
            if v:
                try:
                    # ISO 8601 with offset like "2026-02-23T07:28:03-05:00"
                    ts = datetime.fromisoformat(v).timestamp()
                    break
                except Exception:
                    pass
        if not _age_ok(ts, max_age_s):
            continue
        jobs.append({
            "role": (j.get("title") or "").strip(),
            "company": j.get("company_name") or slug.title(),
            "location": loc,
            "link": j.get("absolute_url", ""),
            "source": f"Greenhouse:{slug}",
            "_job_id": j.get("id"),
            "_board":  slug,
        })
    # Enrich with description + experience filter
    max_years = _GH_MAX_YEARS  # injected at call time
    enriched = []
    for job in jobs:
        job_id = job.pop("_job_id", None)
        board  = job.pop("_board", slug)
        try:
            detail = json.loads(http_get(
                f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=false",
                timeout=12))
            desc  = detail.get("content", "")
            years = _extract_min_years(desc)
            if years is not None and years > max_years:
                continue                # over-experienced: skip
            job["description_snippet"] = _strip_html(desc)[:400]
        except Exception:
            pass                        # can't fetch detail → include anyway
        enriched.append(job)
    return enriched

def fetch_greenhouse_il(settings, max_age_s):
    if not settings.get("jobBoards", {}).get("greenhouseIL"):
        return []
    # Use config-specified boards, or fall back to the hardcoded list
    # Extra boards can be added via config key "greenhouseBoards"
    boards = (settings.get("greenhouseBoards") or []) + GREENHOUSE_IL_BOARDS
    # Dedupe while preserving order
    seen_b = set(); boards = [b for b in boards if not (b in seen_b or seen_b.add(b))]
    all_jobs = []
    global _GH_MAX_YEARS
    _GH_MAX_YEARS = settings.get("maxYears", 2.5)
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_fetch_one_greenhouse, slug, max_age_s): slug for slug in boards}
        for f in as_completed(futs):
            all_jobs.extend(f.result() or [])
    print(f"  Greenhouse (IL, {len(boards)} boards): {len(all_jobs)} listings")
    return all_jobs

def _fetch_one_lever(slug, max_age_s):
    """Fetch one Lever board, return Israel-located jobs."""
    try:
        raw = http_get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=12)
        data = json.loads(raw)
    except Exception as e:
        print(f"    [lever:{slug}] error: {e}")
        return []
    jobs = []
    if not isinstance(data, list):
        return []
    for j in data:
        loc_str = (j.get("categories") or {}).get("location", "") or ""
        if not _is_il_location(loc_str):
            continue
        # createdAt is unix milliseconds
        ts = None
        ca = j.get("createdAt")
        if isinstance(ca, (int, float)):
            ts = ca / 1000.0 if ca > 1e12 else float(ca)
        if not _age_ok(ts, max_age_s):
            continue
        desc  = j.get("descriptionPlain") or j.get("description") or ""
        years = _extract_min_years(desc)
        if years is not None and years > _LV_MAX_YEARS:
            continue                    # over-experienced: skip
        jobs.append({
            "role": (j.get("text") or "").strip(),
            "company": slug.title(),
            "location": loc_str,
            "link": j.get("hostedUrl") or j.get("applyUrl", ""),
            "source": f"Lever:{slug}",
            "description_snippet": _strip_html(desc)[:400],
        })
    return jobs

def fetch_lever_il(settings, max_age_s):
    if not settings.get("jobBoards", {}).get("leverIL"):
        return []
    boards = settings.get("leverBoards") or LEVER_IL_BOARDS
    all_jobs = []
    global _LV_MAX_YEARS
    _LV_MAX_YEARS = settings.get("maxYears", 2.5)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_one_lever, slug, max_age_s): slug for slug in boards}
        for f in as_completed(futs):
            all_jobs.extend(f.result() or [])
    print(f"  Lever (IL, {len(boards)} boards): {len(all_jobs)} listings")
    return all_jobs


# ── Playwright-based Israeli board scrapers ───────────────────────────────────
def _pw_stealth_browser(playwright_instance):
    """Launch a stealth Chromium browser that avoids common bot-detection checks."""
    browser = playwright_instance.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-extensions",
            "--window-size=1280,900",
        ],
    )
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
        locale="he-IL",
        timezone_id="Asia/Jerusalem",
        extra_http_headers={"Accept-Language": "he-IL,he;q=0.9,en;q=0.8"},
    )
    # Patch navigator.webdriver to undefined
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    )
    return browser, ctx


def fetch_drushim_playwright(settings, max_age_s):
    """
    Fetch Drushim tech jobs by intercepting the XHR the page makes to
    /api/jobs/search — this gives us the real JSON result without waiting
    for DOM rendering, and works even when Radware delays the page.
    """
    if not settings.get("jobBoards", {}).get("drushim"):
        return []
    try:
        from playwright.sync_api import sync_playwright, Route
    except ImportError:
        print("  Drushim: playwright not installed — skipping")
        return []

    search_terms = [
        "fullstack developer", "full stack developer",
        "backend developer", "python developer",
        "react developer", "frontend developer",
        "node developer", "software developer",
    ]

    all_jobs, seen_links = [], set()

    try:
        with sync_playwright() as pw:
            browser, ctx = _pw_stealth_browser(pw)
            page = ctx.new_page()
            page.set_default_timeout(15000)

            for term in search_terms:
                captured = []

                def handle_route(route: Route):
                    resp = route.fetch()
                    try:
                        body = resp.json()
                        if body.get("ResultList"):
                            captured.append(body["ResultList"])
                    except Exception:
                        pass
                    route.fulfill(response=resp)

                page.route("**/api/jobs/search**", handle_route)

                try:
                    import urllib.parse as _up
                    url = f"https://www.drushim.co.il/jobs/search/?q={_up.quote(term)}"
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(5000)   # allow XHR to fire
                except Exception as e:
                    print(f"    [drushim] '{term}': {e}")

                page.unroute("**/api/jobs/search**")

                for result_list in captured:
                    for j in result_list:
                        lnk = (j.get("ApplyUrl") or j.get("JobUrl") or
                               f"https://www.drushim.co.il/job/{j.get('JobId','')}/").strip()
                        title = (j.get("Title") or j.get("JobTitle") or "").strip()
                        if lnk and lnk not in seen_links and title:
                            seen_links.add(lnk)
                            all_jobs.append({
                                "role":     title,
                                "company":  (j.get("CompanyName") or "").strip(),
                                "location": (j.get("CityName") or j.get("Area") or "Israel").strip(),
                                "link":     lnk,
                                "source":   "Drushim",
                            })

            browser.close()

    except Exception as e:
        print(f"  Drushim playwright error: {e}")

    print(f"  Drushim (XHR intercept): {len(all_jobs)} listings")
    return all_jobs

def fetch_alljobs_playwright(settings, max_age_s):
    """Fetch AllJobs tech listings using a headless browser to bypass Radware."""
    if not settings.get("jobBoards", {}).get("alljobs"):
        return []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  AllJobs: playwright not installed — skipping")
        return []

    # AllJobs category 57 = Software/Internet; 4 = Tech
    search_urls = [
        "https://www.alljobs.co.il/SearchResults.aspx?page=1&type=1&lang=0&q=fullstack+developer",
        "https://www.alljobs.co.il/SearchResults.aspx?page=1&type=1&lang=0&q=backend+developer",
        "https://www.alljobs.co.il/SearchResults.aspx?page=1&type=1&lang=0&q=python+developer",
        "https://www.alljobs.co.il/SearchResults.aspx?page=1&type=1&lang=0&q=react+developer",
        "https://www.alljobs.co.il/SearchResults.aspx?page=1&type=1&lang=0&q=frontend+developer",
    ]

    all_jobs, seen_links = [], set()

    try:
        with sync_playwright() as pw:
            browser, ctx = _pw_stealth_browser(pw)
            page = ctx.new_page()
            page.set_default_timeout(20000)

            for url in search_urls:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)

                    # Wait for job cards — AllJobs uses .single-job-item or .job-item
                    try:
                        page.wait_for_selector(
                            ".single-job-item, .job-item, [class*='job-item'], .search-result",
                            timeout=8000
                        )
                    except Exception:
                        pass

                    # Extract job cards from rendered DOM
                    results = page.evaluate(r"""
                        () => {
                            const cards = document.querySelectorAll(
                                '.single-job-item, .job-item, [class*="job-item"]'
                            );
                            const jobs = [];
                            cards.forEach(c => {
                                const titleEl  = c.querySelector('h2, h3, .job-title, [class*="title"]');
                                const compEl   = c.querySelector('.company, [class*="company"], [class*="employer"]');
                                const cityEl   = c.querySelector('.city, [class*="city"], [class*="location"]');
                                const linkEl   = c.querySelector('a[href]');
                                const title   = (titleEl?.innerText || '').trim();
                                const company = (compEl?.innerText  || '').trim();
                                const city    = (cityEl?.innerText   || '').trim();
                                let link      = linkEl?.href || '';
                                if (link && !link.startsWith('http')) {
                                    link = 'https://www.alljobs.co.il' + link;
                                }
                                if (title && link) jobs.push({title, company, city, link});
                            });
                            return jobs;
                        }
                    """)

                    for j in results:
                        lnk = (j.get("link") or "").strip()
                        if lnk and lnk not in seen_links and j.get("title"):
                            seen_links.add(lnk)
                            all_jobs.append({
                                "role":     j["title"],
                                "company":  j.get("company", ""),
                                "location": j.get("city") or "Israel",
                                "link":     lnk,
                                "source":   "AllJobs",
                            })
                except Exception as e:
                    print(f"    [alljobs] {url[-50:]}: {e}")

            browser.close()

    except Exception as e:
        print(f"  AllJobs playwright error: {e}")

    print(f"  AllJobs (playwright): {len(all_jobs)} listings")
    return all_jobs


def fetch_all_jobs(settings):
    boards = settings.get("jobBoards", {})
    max_age_s = POST_DATE_SECONDS.get(settings.get("postDateFilter", "7d"), 604800)
    all_jobs = []
    # Israeli company public API boards (Greenhouse / Lever)
    if boards.get("greenhouseIL"): all_jobs += fetch_greenhouse_il(settings, max_age_s)
    if boards.get("leverIL"):      all_jobs += fetch_lever_il(settings, max_age_s)
    # Israeli job boards — scraped via Playwright headless browser
    if boards.get("drushim"):      all_jobs += fetch_drushim_playwright(settings, max_age_s)
    if boards.get("alljobs"):      all_jobs += fetch_alljobs_playwright(settings, max_age_s)
    # Remote boards (off by default, user preference)
    if boards.get("jobicy"):       all_jobs += fetch_jobicy(settings, max_age_s)
    if boards.get("remoteOk"):     all_jobs += fetch_remoteok(settings, max_age_s)
    if boards.get("himalayas"):    all_jobs += fetch_himalayas(settings, max_age_s)
    return all_jobs

# ── Pre-filter (no LLM) ───────────────────────────────────────────────────────
def pre_filter(jobs, settings):
    """Fast keyword-based filter before hitting the LLM."""
    excluded_companies = [c.lower() for c in settings.get("excludedCompanies", [])]
    excluded_keywords  = [k.lower() for k in settings.get("excludedKeywords", [])]
    excluded_stacks    = [s.lower() for s in settings.get("excludedStacks", [])]
    allowed_locations  = [l.lower() for l in settings.get("locations", [])]
    skills             = [s.lower() for s in settings.get("skills", [])]
    dev_kws_raw = settings.get("devRoleKeywords",
        ["developer","engineer","full stack","fullstack","backend","frontend","software"])
    dev_kws_lower = [w.lower() for w in dev_kws_raw]
    # Hebrew (and other non-ASCII) keywords need substring match on the original case
    dev_kws_nonascii = [w for w in dev_kws_raw if not w.isascii()]

    # Sources that are inherently remote — skip the strict location check for these
    remote_sources = {"Jobicy", "RemoteOK", "Himalayas"}
    remote_ok        = settings.get("remoteOk", True)
    remote_il_only   = settings.get("remoteIsraelOnly", False)

    passed, dropped = [], 0
    for j in jobs:
        role_raw = j.get("role", "")
        role     = role_raw.lower()
        company  = (j.get("company", "")).lower()
        loc      = (j.get("location", "")).lower()
        source   = j.get("source", "")

        # Excluded company
        if any(ex == company or (ex and ex in company) for ex in excluded_companies):
            dropped += 1; continue
        # Excluded title keyword
        if any(kw and kw in role for kw in excluded_keywords):
            dropped += 1; continue
        # Exclude roles that don't match a fullstack/backend software developer profile
        # Hard exclude: non-dev roles with no ambiguity
        hard_non_dev = [
            "customer success", "sales engineer", "pre-sales", "presales",
            "technical account manager", "support engineer",
            "business intelligence", "data analyst", "data scientist",
            "machine learning", "scrum master", "product manager", "product owner",
        ]
        if any(p in role for p in hard_non_dev):
            dropped += 1; continue
        # Excluded stack in title
        if any(st and st in role for st in excluded_stacks):
            dropped += 1; continue
        # Must mention at least one skill OR be a dev role (English or Hebrew)
        has_skill   = any(sk in role for sk in skills)
        is_dev_role = any(w in role for w in dev_kws_lower) or \
                      any(w in role_raw for w in dev_kws_nonascii)
        if not has_skill and not is_dev_role:
            dropped += 1; continue
        # Location check
        is_remote_source = source in remote_sources or any(s in source for s in remote_sources)
        if is_remote_source:
            # Global remote board (Jobicy/RemoteOK/Himalayas)
            if not remote_ok:
                dropped += 1; continue
            # If user wants only remote roles open to Israel, drop region-locked listings.
            # The location text usually says "USA", "Europe", "EU", "Americas only", "EST", "Public Trust", etc.
            if remote_il_only:
                if not (_is_il_location(loc) or
                        any(w in loc for w in ["worldwide","anywhere","global","europe","emea","international"]) or
                        loc in ("", "remote")):
                    dropped += 1; continue
        else:
            is_remote = any(w in loc for w in ["remote","hybrid"])
            loc_ok    = any(al in loc for al in allowed_locations) or _is_il_location(loc)
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
    prompt = f"""You are scoring job listings for a specific candidate. Score each listing 0-10.

CANDIDATE PROFILE:
- Role target: Junior / Mid Full-Stack Developer or Backend Developer
- Production experience: ~1 year (React + TypeScript + FastAPI + Python + Docker, deployed to production)
- Stack: {', '.join(skills)}
- Education: B.Sc. Computer Science, GPA 92
- Location: Tel Aviv, Israel — prefers on-site or hybrid in Israel
- Maximum years of experience required: {max_years}

WHAT HE DOES: builds web applications — React frontends, Python/FastAPI or Node.js backends, PostgreSQL/Firebase databases, Docker deployments.

WHAT HE DOES NOT DO (score 0-2 for these):
- Business Intelligence / BI / analytics dashboards
- Data Science / Machine Learning / AI research
- DevOps / SRE / Infrastructure / Cloud Ops
- Android / iOS / Mobile development
- Embedded / Firmware / Hardware
- Network engineering / Security research / Pentesting
- QA Automation as primary role
- Any senior/lead/manager/staff roles

SCORING RUBRIC:
  9-10: IL-based Junior/Mid Fullstack or Backend role, uses React+TS or Python/FastAPI/Node, ≤{max_years}yr req
  7-8:  IL or remote-IL dev role, good stack overlap, reasonable seniority
  5-6:  Dev role but partial overlap or seniority unclear
  2-4:  Wrong domain (BI, DevOps, mobile) or too senior
  0-1:  Completely wrong role type or requires citizenship/clearance

Jobs to score:
{job_list}

Return JSON:
{{"scores": [{{"index": 1, "score": 8, "reason": "one sentence"}}]}}"""

    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        "max_tokens": 4096,
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
    # Sheet columns: Date added | Role | Company | Location | Job Link | Status
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
