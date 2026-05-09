#!/usr/bin/env python3
"""Daily job matcher — fetches IL job listings, scores with Gemini, writes to Google Sheets.

Sources: Greenhouse / Lever / Ashby / Comeet (IL boards) + Drushim RSS.
Run modes (RUN_MODE env var): search | test-connection | test-write
Required secrets: GOOGLE_SA_KEY or GOOGLE_SA_KEY_PATH, GOOGLE_SHEETS_ID.
Optional: GEMINI_API_KEY (falls back to keyword scoring without it).
"""

import os, json, sys, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib import request as urlreq, error as urlerr

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
    sys.stderr = open(sys.stderr.fileno(), mode="w", encoding="utf-8", buffering=1)

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Constants
GEMINI_MODEL   = "gemini-2.5-flash"
GEMINI_API_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
               + GEMINI_MODEL + ":generateContent")
BROWSER_UA    = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
SHEET_TAB     = "Saved Jobs"
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def make_job(*, role, company="", location="", link, source,
             posted=None, description=None):
    return {
        "role":        role.strip(),
        "company":     company.strip() if company else "",
        "location":    location.strip() if location else "",
        "link":        link.strip(),
        "source":      source,
        "posted":      posted,
        "description": description,
        "match_score": None,
        "reason":      None,
    }

try:
    from zoneinfo import ZoneInfo
    JERUSALEM_TZ = ZoneInfo("Asia/Jerusalem")
except ImportError:
    from datetime import timezone, timedelta
    JERUSALEM_TZ = timezone(timedelta(hours=3))  # fallback

POST_DATE_SECONDS = {"24h": 86400, "3d": 259200, "7d": 604800,
                     "14d": 1209600, "30d": 2592000}

# Module-level max-years sentinels (set at runtime by fetch_greenhouse_il / fetch_lever_il)
_GH_MAX_YEARS = 2.5
_LV_MAX_YEARS = 2.5

# Loaded at runtime from config/keywords.json → il_location_hints
# (haifa / jerusalem / yokneam excluded — outside Gush Dan)
IL_LOCATION_HINTS: list[str] = []

def _load_il_hints():
    kw = load_keywords()
    hints = kw.get("il_location_hints")
    if hints:
        IL_LOCATION_HINTS[:] = hints

# Curated Israeli companies with verified public Greenhouse boards
# (Confirmed working 2026-04: returns IL job listings)
GREENHOUSE_IL_BOARDS = [
    # Removed 2026-05: 21 slugs that 404'd: acronis, binah, bluevine, clickup,
    # dynamicyield, ermetic, intelligo, itamarmedicalltd, khealth, leddartech,
    # lunasolutions, meshpayments, onedigital, pandologic, pecanai, rhinohealth,
    # singular, snyk, tremorinternational, upsolver, vimeo
    "amwell",
    "apiiro",
    "appsflyer",
    "armissecurity",
    "atbayjobs",
    "axonius",
    "BigID",
    "bringg",
    "canonical",
    "catonetworks",
    "cb4",
    "connecteam",
    "cymulate",
    "datadog",
    "datarails",
    "doitintl",
    "doubleverify",
    "fireblocks",
    "forter",
    "globalityinc",
    "gongio",
    "gusto",
    "honeybook",
    "innovid",
    "jfrog",
    "lightricks",
    "melio",
    "mixtiles",
    "nanit",
    "nice",
    "obligo",
    "optimove",
    "orcasecurity",
    "outbraininc",
    "pagaya",
    "payoneer",
    "pendo",
    "playtikaltd",
    "riskified",
    "saltsecurity",
    "similarweb",
    "sisense",
    "taboola",
    "techstars57",
    "torq",
    "transmitsecurity",
    "via",
    "vonage",
    "walnut",
    "wizinc",
    "yotpo",
    "ziprecruiter",
    "zoominfo",
    "zscaler",
]

# Curated Israeli companies with verified public Lever boards
LEVER_IL_BOARDS = [
    "walkme",
    "cloudinary",
    # NOTE: monday, wix, lemonade, fiverr, playtika, gong, salto, kaltura,
    # lightricks, coralogix, atera, silverfort, pentera, snyk all HTTP 404.
]

# Israeli companies on Ashby (https://api.ashbyhq.com/posting-api/job-board/{slug})
# Verified working 2026-04 via live API
ASHBY_IL_BOARDS = [
    "lemonade",  # 20 IL / 43 total
    "redis",     # 11 IL / 101 total
    "deel",      # 5 IL / 246 total
]

COMEET_IL_BOARDS = [
    "365scores",
    "44ventures",
    "abra-web-mobile",
    "accessibe",
    "aeronautics",
    "ai21",
    "aiola",
    "anyclip",
    "aquasec",
    "arpeely",
    "artmedical",
    "aspectiva",
    "attenti",
    "audiocodes",
    "autobrains",
    "AutoLeadStar",
    "automatit",
    "bagirasystems",
    "bigabid",
    "biocatch",
    "brix",
    "browzwear",
    "buyme",
    "caja",
    "cardinalops",
    "citadel",
    "Claroty",
    "coinmama",
    "comunix",
    "ctera",
    "Cyberbit",
    "cynet",
    "Datarails",
    "deepinstinct",
    "devalore",
    "easysend",
    "final",
    "foresightauto",
    "FrontStory",
    "fundguard",
    "global-e",
    "globalbit",
    "gotech",
    "granulate",
    "groo",
    "guardknox",
    "guesty",
    "HiredScore",
    "hunters",
    "ilyon",
    "immunai",
    "incredibuild",
    "infinidat",
    "jeeng",
    "justt",
    "jvp",
    "k2view",
    "kaltura",
    "karma",
    "klips",
    "komodor",
    "landacorp",
    "lili",
    "liveu",
    "lumenis",
    "maytronics",
    "minutemedia",
    "mitiga",
    "moonshot",
    "nanodimension",
    "navina",
    "nexar",
    "nuvoton",
    "onezerobank",
    "optibus",
    "ourcrowd",
    "p81",
    "paragon",
    "paybox",
    "pepperi",
    "pixellot",
    "prospera",
    "ptc",
    "pubplus",
    "rapyd",
    "razorlabs",
    "rekor",
    "riverside-fm",
    "roundforest",
    "safebreach",
    "salt",
    "scadafence",
    "sciplay",
    "scylladb",
    "shieldfc",
    "shopic",
    "sight",
    "silverfort",
    "skyline",
    "snc",
    "sodastream",
    "sparkion",
    "sqream",
    "syte",
    "team8",
    "Tenengroup",
    "teridion",
    "upstream",
    "vastdata",
    "verintisrael",
    "viber",
    "voyagerlabs",
    "workiz",
    "zesty",
    "zim",
    "zoominsoftware",
]

# Config
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

def load_keywords():
    path = Path(__file__).resolve().parent.parent / "config" / "keywords.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}

# HTTP helpers
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
    if not ts_seconds:
        return True  # unknown age — include
    return (time.time() - ts_seconds) <= max_age_s

def _is_il_location(loc_str):
    s = (loc_str or "").lower()
    return any(h in s for h in IL_LOCATION_HINTS)

def _strip_html(text):
    text = re.sub(r'<[^>]+>', ' ', text or '')
    text = re.sub(r'&[a-zA-Z#0-9]+;', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()

def _extract_min_years(text, he_patterns=()):
    t = (_strip_html(text)).lower()
    patterns = [
        r'(\d+)\+\s*years?\s+of\s+\w+',
        r'(\d+)\+\s*years?',
        r'(\d+)\s*[-\u2013]\s*\d+\s*years?\s*(?:of\s+)?(?:experience|exp)',
        r'at\s+least\s+(\d+)\s*years?',
        r'minimum\s+(?:of\s+)?(\d+)\s*years?',
        r'(\d+)\s+or\s+more\s+years?',
        r'(\d+)\s*years?\s*(?:of\s+)?(?:experience|exp)',
        r'(\d+)\s*years?\s+of\s+\w+(?:\s+\w+){0,3}\s+(?:experience|development)',
        *he_patterns,
    ]
    found = []
    for p in patterns:
        for m in re.finditer(p, t):
            try:
                val = int(m.group(1))
                if 1 <= val <= 20:
                    found.append(val)
            except Exception:
                pass
    return min(found) if found else None

# Job board fetchers
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
            job["description"] = _strip_html(desc)  # Full description for validation
            job["description_snippet"] = _strip_html(desc)[:400]
        except Exception:
            pass                        # can't fetch detail include anyway
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
            "description": _strip_html(desc),
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


# Ashby (public API: api.ashbyhq.com/posting-api/job-board/{slug}) 
_AB_MAX_YEARS = 2.5

def _fetch_one_ashby(slug, max_age_s):
    """Fetch one Ashby board, return Israel-located jobs."""
    try:
        raw = http_get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=12)
        data = json.loads(raw)
    except Exception as e:
        print(f"    [ashby:{slug}] error: {e}")
        return []
    jobs_out = []
    for j in data.get("jobs", []):
        if not j.get("isListed", True):
            continue
        loc = j.get("location", "") or ""
        addr = (j.get("address") or {}).get("postalAddress") or {}
        loc_combined = f"{loc} {addr.get('addressLocality','')} {addr.get('addressRegion','')} {addr.get('addressCountry','')}"
        if not _is_il_location(loc_combined):
            continue
        # publishedAt unix seconds
        ts = None
        pub = j.get("publishedAt")
        if pub:
            try:
                from datetime import datetime as _dt
                ts = _dt.fromisoformat(pub.replace("Z","+00:00")).timestamp()
            except Exception:
                ts = None
        if not _age_ok(ts, max_age_s):
            continue
        title = j.get("title", "").strip()
        # Skip senior roles at fetch time too (saves LLM calls later)
        years = None  # Ashby has no description in the listing endpoint
        jobs_out.append({
            "role": title,
            "company": slug.title(),
            "location": loc_combined.strip(),
            "link": j.get("jobUrl") or j.get("applyUrl", ""),
            "source": f"Ashby:{slug}",
            "description_snippet": "",
        })
    return jobs_out


def fetch_ashby_il(settings, max_age_s):
    if not settings.get("jobBoards", {}).get("ashbyIL"):
        return []
    boards = settings.get("ashbyBoards") or ASHBY_IL_BOARDS
    all_jobs = []
    global _AB_MAX_YEARS
    _AB_MAX_YEARS = settings.get("maxYears", 2.5)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_one_ashby, slug, max_age_s): slug for slug in boards}
        for f in as_completed(futs):
            all_jobs.extend(f.result() or [])
    print(f"  Ashby (IL, {len(boards)} boards): {len(all_jobs)} listings")
    return all_jobs


# Playwright-based Israeli board scrapers 
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



_CM_MAX_YEARS = 2.5

def _fetch_comeet_api(company_uid, token, slug, max_age_s):
    """Call the real Comeet careers-api JSON endpoint and return IL jobs."""
    url = (
        f"https://www.comeet.co/careers-api/2.0/company/{company_uid}"
        f"/positions?token={token}"
    )
    try:
        raw = http_get(url, timeout=15)
        positions = json.loads(raw)
    except Exception as e:
        print(f"    [comeet:{slug}] api error: {e}")
        return []

    jobs = []
    for j in positions:
        # Location can be a dict or string
        loc_raw = j.get("location") or {}
        if isinstance(loc_raw, dict):
            loc = (loc_raw.get("name") or loc_raw.get("city") or "").strip()
        else:
            loc = str(loc_raw).strip()
        if not _is_il_location(loc):
            continue

        title = (j.get("name") or j.get("title") or "").strip()
        if not title:
            continue

        link = (
            j.get("url_active_page")
            or j.get("url_comeet_hosted_page")
            or j.get("url_recruit_hosted_page")
            or j.get("url_detected_page")
            or f"https://www.comeet.com/jobs/{slug}"
        )
        desc = _strip_html(j.get("details") or j.get("description") or "")
        years = _extract_min_years(desc)
        if years is not None and years > _CM_MAX_YEARS:
            continue

        company = j.get("company_name") or slug.replace("-", " ").title()
        jobs.append({
            "role":                title,
            "company":             company,
            "location":            loc,
            "link":                link,
            "source":              f"Comeet:{slug}",
            "description":         desc,
            "description_snippet": desc[:400],
        })
    return jobs


def _fetch_one_comeet(slug, max_age_s):
    """
    Fetch one Comeet board via Playwright route-interception.
    The /jobs/{slug}/positions page is now a WordPress marketing page  it no
    longer returns JSON. The real data lives at:
      https://www.comeet.co/careers-api/2.0/company/{UID}/positions?token={TOKEN}
    We load the hosted careers page in a headless browser, intercept the
    careers-api XHR to grab UID + token, then call the JSON API directly.
    """
    try:
        from playwright.sync_api import sync_playwright, Route
    except ImportError:
        print(f"    [comeet:{slug}] playwright not installed")
        return []

    captured = {}

    try:
        with sync_playwright() as pw:
            browser, ctx = _pw_stealth_browser(pw)
            page = ctx.new_page()
            page.set_default_timeout(20000)

            def handle_route(route: "Route"):
                import re as _re
                url = route.request.url
                m = _re.search(
                    r"careers-api/[\d.]+/company/([^/]+)/positions\?token=([A-Za-z0-9]+)",
                    url,
                )
                if m and not captured:
                    captured["uid"] = m.group(1)
                    captured["token"] = m.group(2)
                route.continue_()

            page.route("**/careers-api/**", handle_route)

            try:
                page.goto(
                    f"https://www.comeet.co/jobs/{slug}/positions",
                    wait_until="domcontentloaded",
                    timeout=8000,
                )
                page.wait_for_timeout(1500)  # allow XHR to fire
            except Exception:
                pass

            browser.close()
    except Exception as e:
        print(f"    [comeet:{slug}] playwright error: {e}")
        return []

    if not captured:
        # Fallback: scrape the hosted page HTML for the token
        try:
            import re as _re
            html = http_get(f"https://www.comeet.co/jobs/{slug}/positions", timeout=12)
            m = _re.search(
                r"careers-api/[\d.]+/company/([^/]+)/positions(?:/[^?]*)?\?token=([A-Za-z0-9]+)",
                html,
            )
            if m:
                captured["uid"] = m.group(1)
                captured["token"] = m.group(2)
        except Exception:
            pass

    if not captured:
        print(f"    [comeet:{slug}] could not discover UID/token")
        return []

    return _fetch_comeet_api(captured["uid"], captured["token"], slug, max_age_s)


def fetch_comeet_il(settings, max_age_s):
    if not settings.get("jobBoards", {}).get("comeetIL"):
        return []
    global _CM_MAX_YEARS
    _CM_MAX_YEARS = settings.get("maxYears", 2.5)
    boards = (settings.get("comeetBoards") or []) + COMEET_IL_BOARDS
    seen_b = set(); boards = [b for b in boards if not (b in seen_b or seen_b.add(b))]
    all_jobs = []
    # Lower concurrency each slug now launches Playwright
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_one_comeet, slug, max_age_s): slug for slug in boards}
        for f in as_completed(futs):
            all_jobs.extend(f.result() or [])
    print(f"  Comeet (IL, {len(boards)} boards): {len(all_jobs)} listings")
    return all_jobs


def _fetch_drushim_details(job_url):
    """
    Fetch company name AND city from a Drushim individual job page in one HTTP GET.

    Company: JSON-LD <script type="application/ld+json"> â hiringOrganization.name
             Fallback: jobLocation.address.addressLocality (translated if Hebrew)
    City:    window.__NUXT__ IIFE â CityEnglish (string literal or variable ref,
             translated via _HEBREW_CITY if the value is Hebrew)

    Returns (company_or_None, city_or_None).
    """
    _HEBREW_CITY = {
        "×ª× ××××": "Tel Aviv", "×ª×-××××": "Tel Aviv", "×ª× ×××× ××¤×": "Tel Aviv",
        "×¨××ª ××": "Ramat Gan", "××¨×¦×××": "Herzliya", "××¨×¦××× ×¤××ª××": "Herzliya",
        "×¤×ª× ×ª×§×××": "Petah Tikva", "×¤×ª×-×ª×§×××": "Petah Tikva", "×¤×ª× ×ª×§××": "Petah Tikva",
        "×××××": "Holon", "× ×¡ ×¦××× ×": "Ness Ziona", "×¨×××××ª": "Rehovot",
        "×¨××©×× ××¦×××": "Rishon LeZion", '×¨××©×"×¦': "Rishon LeZion",
        "××ª ××": "Bat Yam", "××¨××©×××": "Jerusalem", "×××¤×": "Haifa",
        "×××¨ ×©××": "Beer Sheva", "× ×ª× ××": "Netanya", "××¤×¨ ×¡××": "Kefar Sava",
        "××× ××©×¨××": "Hod HaSharon", "××××": "Yehud", "×××××××": "Modiin",
        "×§×¨×××ª ××××××§": "Kiryat Bialik", "×× × ××¨×§": "Bnei Brak", "××××ª×××": "Givatayim",
        "×¨××© ××××": "Rosh HaAyin", "×¨×× × ×": "Ra'anana", "××©×¨××": "Israel",
    }

    def _translate(s):
        if not s or not any('\u05d0' <= c <= '\u05ea' for c in s):
            return s
        c = s.strip()
        if c in _HEBREW_CITY:
            return _HEBREW_CITY[c]
        for heb, eng in _HEBREW_CITY.items():
            if c.startswith(heb) or heb.startswith(c):
                return eng
        return s

    def _parse_iife_args(blob):
        pm = re.search(r'\(function\(([^)]+)\)', blob)
        if not pm:
            return {}
        params = [p.strip() for p in pm.group(1).split(',')]
        am = re.search(r'\}\((.+)\)\);?\s*$', blob, re.DOTALL)
        if not am:
            return {}
        args_raw = am.group(1)
        tokens, i, n = [], 0, len(args_raw)
        while i < n:
            ch = args_raw[i]
            if ch == '"':
                j = i + 1
                while j < n:
                    if args_raw[j] == '\\': j += 2
                    elif args_raw[j] == '"': break
                    else: j += 1
                raw = args_raw[i+1:j]
                decoded = re.sub(r'\\u([0-9a-fA-F]{4})',
                                 lambda m: chr(int(m.group(1), 16)), raw)
                tokens.append(decoded); i = j + 1
            elif args_raw[i:i+6] == 'void 0': tokens.append(None); i += 6
            elif args_raw[i:i+4] == 'true':   tokens.append(None); i += 4
            elif args_raw[i:i+5] == 'false':  tokens.append(None); i += 5
            elif args_raw[i:i+4] == 'null':   tokens.append(None); i += 4
            elif ch in ', ':  i += 1
            elif ch == '-' or ch.isdigit():
                j = i + (1 if ch == '-' else 0)
                while j < n and (args_raw[j].isdigit() or args_raw[j] == '.'): j += 1
                tokens.append(None); i = j
            else: i += 1
        return {params[k]: tokens[k]
                for k in range(min(len(params), len(tokens)))}

    try:
        _h = {
            "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
            "Referer": "https://www.drushim.co.il/",
        }
        try:
            html = http_get(job_url, headers=_h, timeout=12)
        except Exception:
            return None, None

        # Company from JSON-LD
        company = None
        description = None
        try:
            from bs4 import BeautifulSoup as _BS
            soup = _BS(html, "html.parser")
            for s in soup.find_all("script", type="application/ld+json"):
                raw = s.string
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                    # Primary: hiringOrganization.name
                    name = ((d.get("hiringOrganization") or {}).get("name") or "").strip()
                    if name:
                        company = name
                    # Also grab description for experience filtering
                    if not description:
                        raw_desc = (d.get("description") or "").strip()
                        if raw_desc:
                            from html import unescape as _ue
                            description = _re.sub(r'<[^>]+>', ' ', _ue(raw_desc))[:3000]
                    if company:
                        break
                except Exception:
                    continue
        except Exception:
            pass

        # City from NUXT
        city = None
        ni = html.find("window.__NUXT__=(")
        ne = html.find("</script>", ni) if ni != -1 else -1
        if ni != -1 and ne != -1:
            blob = html[ni:ne]
            # 1. String literal
            lit = re.findall(r'CityEnglish:"(\\t[^"]+\\t)"', blob)
            if lit:
                clean = [c.replace("\\t", "").strip() for c in lit
                         if c.replace("\\t", "").strip()]
                if clean:
                    city = ", ".join(dict.fromkeys(_translate(c) for c in clean))
            # 2. Variable reference
            if not city:
                refs = list(dict.fromkeys(
                    re.findall(r'CityEnglish:([a-zA-Z_$][a-zA-Z0-9_$]*)', blob)
                ))
                if refs:
                    mapping = _parse_iife_args(blob)
                    cities = [_translate(mapping[ref].strip())
                              for ref in refs
                              if ref in mapping and isinstance(mapping[ref], str)
                              and mapping[ref].strip()]
                    if cities:
                        city = ", ".join(dict.fromkeys(cities))
            # 3. JSON-LD addressLocality fallback for city
            if not city:
                try:
                    from bs4 import BeautifulSoup as _BS2
                    soup2 = _BS2(html, "html.parser")
                    for s in soup2.find_all("script", type="application/ld+json"):
                        raw = s.string
                        if not raw:
                            continue
                        try:
                            d = json.loads(raw)
                            locality = ((d.get("jobLocation") or {})
                                        .get("address") or {}).get("addressLocality") or ""
                            locality = locality.strip()
                            if locality:
                                city = _translate(locality)
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

        return company, city, description
    except Exception:
        return None, None, None


def fetch_drushim(settings, max_age_s):
    """
    Fetch Drushim tech jobs using plain HTTP requests + BeautifulSoup.

    Drushim server-renders the full .job-item card list into the initial HTML
    response  no JavaScript or headless browser is needed. Switching from
    Playwright to requests reduces per-search-term time from ~22 s to ~0.5 s
    and allows all terms to run in parallel.

    City is resolved via a second parallel batch of HTTP requests to individual
    job pages, where CityEnglish appears as a string literal in __NUXT__ data.
    """
    if not settings.get("jobBoards", {}).get("drushim"):
        return []

    search_terms = [
        "\u05de\u05e4\u05ea\u05d7",       # developer
        "\u05de\u05ea\u05db\u05e0\u05ea",  # programmer
        "\u05e4\u05d5\u05dc\u05e1\u05d8\u05d0\u05e7",  # fullstack
        "\u05d1\u05e7\u05d0\u05e0\u05d3",  # backend
        "\u05e4\u05e8\u05d5\u05e0\u05d8\u05d0\u05e0\u05d3",  # frontend
        "junior",
        "react",
        "python",
    ]

    _hdrs = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        "Referer": "https://www.drushim.co.il/",
    }

    import urllib.parse as _up
    from bs4 import BeautifulSoup as _BS

    def _fetch_term(term):
        """Fetch all pages of results for a single search term."""
        results = []
        base_url = f"https://www.drushim.co.il/jobs/search/{_up.quote(term)}"
        for page in range(1, 11):  # up to 10 pages per term (~25 jobs each)
            url = base_url if page == 1 else f"{base_url}/{page}"
            try:
                html_text = http_get(url, headers=_hdrs, timeout=15)
                soup = _BS(html_text, "html.parser")
                cards = soup.select(".job-item")
                if not cards:
                    break  # no more results
                for card in cards:
                    title_el = card.select_one("h3 span.job-url") or card.select_one("h3")
                    link_el  = card.select_one('a[href*="/job/"]')
                    if not title_el or not link_el:
                        continue
                    title = title_el.get_text(strip=True)
                    href  = link_el.get("href", "")
                    link  = href if href.startswith("http") else f"https://www.drushim.co.il{href}"
                    if title and link:
                        results.append({"title": title, "link": link})
                if len(cards) < 20:  # fewer than full page → last page
                    break
            except Exception as e:
                print(f"    [drushim] '{term}' page {page}: {e}")
                break
        return results

    # Fetch all search terms in parallel
    raw_items, seen_links = [], set()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_term, t): t for t in search_terms}
        for fut in as_completed(futs):
            term = futs[fut]
            items = fut.result()
            new = 0
            for it in items:
                if it["link"] not in seen_links:
                    seen_links.add(it["link"])
                    raw_items.append(it)
                    new += 1
            print(f"    [drushim] '{term}': {len(items)} cards, {new} new")

    if not raw_items:
        print("  Drushim: 0 listings")
        return []

    # Build job dicts (city resolved in next step)
    import re as _re
    all_jobs = []
    for it in raw_items:
        title = it["title"]
        company = ""
        cm = _re.match(
            r"^([\u0590-\u05ffA-Za-z0-9 ()&.\-]+?)\s+\u05de\u05d2\u05d9\u05d9\u05e1\u05ea",
            title,
        )
        if cm:
            company = cm.group(1).strip()
        all_jobs.append({
            "role":     title,
            "company":  company,
            "location": "Israel",   # resolved below
            "link":     it["link"],
            "source":   "Drushim",
        })

    # Resolve company name + city in parallel via individual job pages
    print(f"  Drushim: fetching company/city for {len(all_jobs)} listings...")
    with ThreadPoolExecutor(max_workers=8) as ex:
        detail_futs = {ex.submit(_fetch_drushim_details, j["link"]): j for j in all_jobs}
        for fut in as_completed(detail_futs):
            company, city, description = fut.result()
            job = detail_futs[fut]
            if company:
                job["company"] = company
            if city:
                job["location"] = city
            if description:
                job["description"] = description

    print(f"  Drushim: {len(all_jobs)} listings")
    return all_jobs


def fetch_all_jobs(settings):
    """Fetch jobs from all enabled boards in parallel and return a normalized list."""
    import time as _time
    boards   = settings.get("jobBoards", {})
    max_age_s = POST_DATE_SECONDS.get(settings.get("postDateFilter", "7d"), 604800)

    # Build list of (board_name, callable) for enabled boards
    tasks = []
    if boards.get("comeetIL"):     tasks.append(("comeetIL",     lambda: fetch_comeet_il(settings, max_age_s)))
    if boards.get("greenhouseIL"): tasks.append(("greenhouseIL", lambda: fetch_greenhouse_il(settings, max_age_s)))
    if boards.get("leverIL"):      tasks.append(("leverIL",      lambda: fetch_lever_il(settings, max_age_s)))
    if boards.get("ashbyIL"):      tasks.append(("ashbyIL",      lambda: fetch_ashby_il(settings, max_age_s)))
    if boards.get("drushim"):      tasks.append(("drushim",      lambda: fetch_drushim(settings, max_age_s)))
    if boards.get("jobicy"):       tasks.append(("jobicy",       lambda: fetch_jobicy(settings, max_age_s)))
    if boards.get("himalayas"):    tasks.append(("himalayas",    lambda: fetch_himalayas(settings, max_age_s)))

    if not tasks:
        print("  No boards enabled.", flush=True)
        return []

    all_jobs   = []
    board_stats = {}  # name → count for summary

    def _run_board(name, fn):
        t0 = _time.time()
        try:
            result = fn()
            elapsed = _time.time() - t0
            print(f"  [{name}] done in {elapsed:.0f}s → {len(result)} listings", flush=True)
            return name, result
        except Exception as e:
            elapsed = _time.time() - t0
            print(f"  [{name}] ERROR after {elapsed:.0f}s: {e}", flush=True)
            return name, []

    with ThreadPoolExecutor(max_workers=min(len(tasks), 6)) as ex:
        futs = {ex.submit(_run_board, name, fn): name for name, fn in tasks}
        for fut in as_completed(futs):
            name, result = fut.result()
            board_stats[name] = len(result)
            all_jobs.extend(result)

    # Print per-board summary
    print(f"  Board summary: {board_stats}", flush=True)
    return all_jobs


# Pre-filter (no LLM)
def pre_filter(jobs, settings, keywords=None):
    kw = keywords or {}
    excluded_companies  = [c.lower() for c in settings.get("excludedCompanies", [])]
    excluded_keywords   = [k.lower() for k in settings.get("excludedKeywords", [])]
    excluded_stacks     = [s.lower() for s in settings.get("excludedStacks", [])]
    excluded_stacks    += [s.lower() for s in kw.get("always_excluded_stacks", [])]
    allowed_locations   = [l.lower() for l in settings.get("locations", [])]
    skills              = [s.lower() for s in settings.get("skills", [])]
    seniority_title_kws = kw.get("seniority_title", [
        "senior", " sr.", " sr ", "lead ", "staff ", "principal ",
        "architect", " vp ", "director", "head of",
        "mid-level", "mid level", "medior",
        "founding engineer", "founding developer",
    ])
    seniority_desc_kws  = kw.get("seniority_desc", [])
    hard_non_dev        = kw.get("hard_non_dev_roles", [
        "customer success", "sales engineer", "pre-sales", "presales",
        "business intelligence", "data analyst", "data scientist",
        "machine learning", "scrum master", "product manager", "product owner",
    ])
    hard_reject_locs    = kw.get("hard_reject_locations", [])
    he_patterns         = kw.get("experience_patterns_hebrew", [])
    dev_general         = kw.get("dev_role_keywords", {}).get("general",
        ["developer","engineer","full stack","fullstack","backend","frontend","software"])
    dev_kws_raw      = settings.get("devRoleKeywords", dev_general)
    dev_kws_lower    = [w.lower() for w in dev_kws_raw]
    dev_kws_nonascii = [w for w in dev_kws_raw if not w.isascii()]

    remote_sources = {"Jobicy", "RemoteOK", "Himalayas"}
    remote_ok      = settings.get("remoteOk", True)
    remote_il_only = settings.get("remoteIsraelOnly", False)
    passed, dropped = [], 0
    drop_reasons = {}
    def _drop(reason, j):
        nonlocal dropped
        dropped += 1
        drop_reasons.setdefault(reason, []).append(f"{j.get('company','?')}: {j.get('role','?')[:60]}")

    for j in jobs:
        role_raw = j.get("role", "")
        role     = role_raw.lower()
        company  = (j.get("company", "")).lower()
        loc      = (j.get("location", "")).lower()
        source   = j.get("source", "")

        # Excluded company
        if any(ex == company or (ex and ex in company) for ex in excluded_companies):
            _drop("excluded_company", j); continue
        # Excluded title keyword
        matched_kw = next((kw for kw in excluded_keywords if kw and kw in role), None)
        if matched_kw:
            _drop(f"excluded_kw:{matched_kw}", j); continue
        max_yrs = settings.get("maxYears", 2.5)

        if any(kw in role for kw in seniority_title_kws):
            _drop("over_experience:seniority_title", j); continue

        desc_text_early = j.get("description", "").lower()
        if desc_text_early and any(kw in desc_text_early for kw in seniority_desc_kws):
            _drop("over_experience:seniority_in_desc", j); continue

        title_and_desc = role + " " + j.get("description", "")
        min_yrs = _extract_min_years(title_and_desc, he_patterns)
        if min_yrs is not None and min_yrs > max_yrs:
            _drop(f"over_experience:{min_yrs}yrs_required", j); continue

        matched_nd = next((p for p in hard_non_dev if p in role), None)
        if matched_nd:
            _drop(f"hard_non_dev:{matched_nd}", j); continue

        matched_st = next((st for st in excluded_stacks if st and st in role), None)
        if not matched_st and re.search(r'\bnet[\s./]', role):
            matched_st = ".net"
        if matched_st:
            _drop(f"excluded_stack:{matched_st}", j); continue

        has_skill   = any(sk in role for sk in skills)
        is_dev_role = any(w in role for w in dev_kws_lower) or \
                      any(w in role_raw for w in dev_kws_nonascii)
        if not has_skill and not is_dev_role:
            _drop("no_skill_no_dev_kw", j); continue

        is_remote_source = source in remote_sources or any(s in source for s in remote_sources)
        if is_remote_source:
            if not remote_ok:
                _drop("remote_disabled", j); continue
            if remote_il_only:
                if not (_is_il_location(loc) or
                        any(w in loc for w in ["worldwide","anywhere","global","europe","emea","international"]) or
                        loc in ("", "remote")):
                    _drop("remote_not_il_eligible", j); continue
        else:
            is_remote = any(w in loc for w in ["remote","hybrid"])
            if any(city in loc for city in hard_reject_locs):
                _drop(f"location_not_allowed:{loc[:40]}", j); continue
            loc_ok = any(al in loc for al in allowed_locations) or _is_il_location(loc)
            if not is_remote and not loc_ok:
                _drop(f"location_not_allowed:{loc[:40]}", j); continue

        passed.append(j)

    print(f"::notice title=detail::passed={len(passed)}", flush=True)
    print(f"  Pre-filter: {len(passed)} passed, {dropped} dropped")
    if drop_reasons:
        print("  Drop reasons:")
        for reason, items in sorted(drop_reasons.items(), key=lambda x: -len(x[1])):
            print(f"    [{len(items)}] {reason}")
            for it in items[:3]:
                print(f"        · {it}")
            if len(items) > 3:
                print(f"        · ...and {len(items)-3} more")
    return passed

# LLM scoring
def _algorithmic_score(jobs, settings, keywords=None):
    if not jobs:
        return []
    kw        = keywords or {}
    min_score = settings.get("minScore", 6)
    max_r     = settings.get("maxResults", 30)
    dev_kws   = kw.get("dev_role_keywords", {})

    FULLSTACK_KW  = dev_kws.get("fullstack", ["full stack","fullstack","full-stack"," fs ","fs/"])
    BACKEND_KW    = dev_kws.get("backend",   ["backend","back end","back-end","server-side"])
    FRONTEND_KW   = dev_kws.get("frontend",  ["frontend","front end","front-end","ui developer"])
    DEV_KW        = dev_kws.get("general",   ["developer","engineer","programmer"])
    TIER1         = kw.get("skill_tier1", {"react":1.0,"typescript":1.0,"python":1.0,"node.js":1.0})
    TIER2         = kw.get("skill_tier2", {"docker":0.5,"postgresql":0.5,"javascript":0.5})
    JUNIOR_KW     = kw.get("junior_keywords", ["junior","entry level","entry-level","intern"])
    MID_KW        = kw.get("mid_keywords",    ["mid level","mid-level"])
    DIRECT_BOARDS = ["greenhouse","lever","ashby"]

    scored = []
    for job in jobs:
        title  = (job.get("role","") or "").lower()
        desc   = (job.get("description","") or job.get("body","") or "").lower()
        source = (job.get("source","") or "").lower()
        text   = f"{title} {desc}"
        pts, tags = 0.0, []
        if   any(k in text for k in FULLSTACK_KW): pts += 4.0; tags.append("fullstack")
        elif any(k in text for k in BACKEND_KW):   pts += 3.0; tags.append("backend")
        elif any(k in text for k in FRONTEND_KW):  pts += 2.5; tags.append("frontend")
        elif any(k in text for k in DEV_KW):        pts += 2.0; tags.append("dev")
        else:                                       pts += 1.0
        t1 = sum(v for k,v in TIER1.items() if k in text)
        t2 = sum(v for k,v in TIER2.items() if k in text)
        sp = min(4.0, t1 + t2); pts += sp
        if   sp >= 2.5: tags.append("strong-stack")
        elif sp >= 1.0: tags.append("partial-stack")
        if   any(k in text for k in JUNIOR_KW): pts += 1.5; tags.append("junior")
        elif any(k in text for k in MID_KW):    pts += 0.7; tags.append("mid")
        if any(b in source for b in DIRECT_BOARDS): pts += 0.5; tags.append("direct-board")
        score = max(0, min(10, round(pts)))
        if score >= min_score:
            job["match_score"] = score
            job["reason"]      = ", ".join(tags) or "dev-role"
            scored.append(job)
    scored.sort(key=lambda j: j["match_score"], reverse=True)
    return scored[:max_r]


def _build_gemini_prompt(jobs, settings):
    """Build a batched scoring prompt for Gemini."""
    skills    = settings.get("skills", []) or []
    max_years = settings.get("maxYears", 2.5)
    min_score = settings.get("minScore", 6)
    locations = settings.get("locations", []) or []
    items = []
    for idx, j in enumerate(jobs):
        desc = (j.get("description") or "").strip()
        if len(desc) > 600:
            desc = desc[:600] + "\u2026"
        items.append({"id": idx, "role": (j.get("role") or "").strip(),
                      "company": (j.get("company") or "").strip(),
                      "location": (j.get("location") or "").strip(),
                      "source": (j.get("source") or "").strip(),
                      "description": desc})
    profile = {"target_role": "Junior to mid full-stack/backend developer in Israel",
               "primary_stack": skills, "max_experience_years": max_years,
               "preferred_locations": locations, "min_score": min_score}
    instruction_lines = [
        "You are scoring developer job listings for a candidate.",
        "For each listing return a score 0-10 reflecting fit with the candidate stack, seniority cap, and location preferences.",
        "Be strict: score >=8 only when role, stack, and seniority all clearly match.",
        "Score <=4 for senior/lead/manager, mismatched stack (PHP .NET C# Ruby), or wrong geography.",
        "",
        'Return ONLY valid JSON: {"scores": [{"id": <int>, "score": <int>, "reason": <string max 100 chars>}, ...]}',
        "No prose, no markdown fences.",
    ]
    payload = {"candidate_profile": profile, "jobs": items}
    return "\n".join(instruction_lines) + "\n\n" + json.dumps(payload, ensure_ascii=False)


def _call_gemini(prompt, api_key, timeout=45):
    """POST prompt to Gemini generateContent and return the scores list."""
    from urllib import request as urlreq
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096,
                             "responseMimeType": "application/json"},
    }
    url = GEMINI_API_URL + "?key=" + api_key
    req = urlreq.Request(url, data=json.dumps(body).encode("utf-8"),
                         headers={"Content-Type": "application/json"}, method="POST")
    with urlreq.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data       = json.loads(raw)
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {raw[:300]}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text  = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("Gemini returned empty text")
    # Strip accidental markdown fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    parsed = json.loads(text)
    scores = parsed.get("scores") if isinstance(parsed, dict) else parsed
    if not isinstance(scores, list):
        raise RuntimeError(f"Gemini response missing 'scores' list: {text[:300]}")
    return scores


def score_jobs_with_llm(jobs, settings, keywords=None, api_key=None):
    if not jobs:
        return []
    key = api_key or os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        print("  [scorer] GEMINI_API_KEY not set — using algorithmic fallback")
        return _algorithmic_score(jobs, settings, keywords)
    min_score = settings.get("minScore", 6)
    max_r     = settings.get("maxResults", 30)
    BATCH = 25
    score_by_id, reason_by_id, failed = {}, {}, 0
    for b_start in range(0, len(jobs), BATCH):
        batch  = jobs[b_start: b_start + BATCH]
        prompt = _build_gemini_prompt(batch, settings)
        try:
            entries = _call_gemini(prompt, key)
        except Exception as exc:
            failed += 1
            print(f"  [scorer] Gemini batch {b_start // BATCH} failed: {exc}")
            continue
        for entry in entries:
            try:
                rel = int(entry["id"]); abs_i = b_start + rel
                if 0 <= abs_i < len(jobs):
                    score_by_id[abs_i]  = int(entry.get("score", 0))
                    reason_by_id[abs_i] = str(entry.get("reason", ""))[:120]
            except (KeyError, TypeError, ValueError):
                continue
    if not score_by_id:
        print("  [scorer] No usable Gemini scores — using algorithmic fallback")
        return _algorithmic_score(jobs, settings, keywords)
    if failed:
        print(f"  [scorer] {failed} Gemini batch(es) failed — those jobs skipped")
    scored = []
    for idx, job in enumerate(jobs):
        if idx not in score_by_id:
            continue
        s = max(0, min(10, score_by_id[idx]))
        if s >= min_score:
            job["match_score"] = s
            job["reason"]      = reason_by_id.get(idx) or "llm-match"
            scored.append(job)
    scored.sort(key=lambda j: j["match_score"], reverse=True)
    return scored[:max_r]


def get_sheets_client():
    sa_json = os.getenv("GOOGLE_SA_KEY")
    if not sa_json:
        key_path = os.getenv("GOOGLE_SA_KEY_PATH")
        if not key_path:
            raise ValueError("GOOGLE_SA_KEY or GOOGLE_SA_KEY_PATH not set")
        sa_json = Path(key_path).read_text(encoding="utf-8")
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
        (job.get("role") or job.get("title") or "").strip(),
        (job.get("company") or job.get("employer") or "").strip(),
        (job.get("location") or job.get("region") or "Remote").strip(),
        (job.get("link") or job.get("url") or "").strip(),
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

# Run modes
def run_search():
    settings = load_settings()
    keywords = load_keywords()
    print(f"=== Settings: boards={[k for k,v in settings.get('jobBoards',{}).items() if v]}, "
          f"minScore={settings.get('minScore')}, maxResults={settings.get('maxResults')} ===\n")

    print("::notice title=progress::[1/5] fetch", flush=True)
    print("[1/5] Fetching listings from job boards...")
    raw_jobs = fetch_all_jobs(settings)
    print(f"::notice title=detail::fetched={len(raw_jobs)}", flush=True)
    print(f"  Total fetched: {len(raw_jobs)}\n")

    print("::notice title=progress::[2/5] filter", flush=True)
    print("[2/5] Pre-filtering...")
    shortlist = pre_filter(raw_jobs, settings, keywords)
    print()

    if not shortlist:
        print("No jobs passed pre-filter. Done.")
        return

    print(f"::notice title=progress::[3/5] score {len(shortlist)}", flush=True)
    print(f"[3/5] Scoring {len(shortlist)} jobs...")
    scored = score_jobs_with_llm(shortlist, settings, keywords)
    print(f"::notice title=detail::scored={len(scored)}", flush=True)
    print(f"  {len(scored)} jobs scored >= {settings.get('minScore', 7)}\n")

    if not scored:
        print("No jobs met the score threshold.")
        return

    # 4. Verify links
    verify = settings.get("verifyLinks", True)
    verified = []
    print(f"::notice title=progress::[4/5] verify {len(scored)}", flush=True)
    print(f"[4/5] Verifying {len(scored)} links{' (skipped)' if not verify else ''}...")
    for j in scored:
        link = (j.get("link") or "").strip()
        if not verify or verify_link(link):
            verified.append(j)
            print(f"   {j['role']} @ {j['company']} [{j['source']}] score={j['match_score']}")
        else:
            print(f"   Broken link: {j['role']} @ {j['company']}  {link}")
    print()

    if not verified:
        print("No jobs with live links. Done.")
        return

    # 5. Sync to Sheets
    print(f"::notice title=progress::[5/5] sync {len(verified)}", flush=True)
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
        print(f"   Appended {updated} rows (skipped {dupes} duplicates)")
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
            print(f"  Cleanup failed: {e}\n  Delete {rng} manually.")

MODE_HANDLERS = {
    "search": run_search,
    "test-connection": run_test_connection,
    "test-write": run_test_write,
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
