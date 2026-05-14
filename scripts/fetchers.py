import json, os, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError

from utils import http_get, _strip_html, _age_ok, _is_il_location, POST_DATE_SECONDS, BROWSER_UA, load_keywords, progress_log
from filters import _extract_min_years

# ── Board slug lists ──────────────────────────────────────────────────────────

GREENHOUSE_IL_BOARDS = [
    "amwell", "apiiro", "appsflyer", "armissecurity", "atbayjobs", "augury",
    "axonius", "BigID", "bringg", "canonical", "catonetworks", "cb4",
    "celonis", "commvault", "connecteam", "cybereason", "cymulate", "datadog",
    "datarails", "doitintl", "doubleverify", "fireblocks", "forter", "gongio",
    "gusto", "honeybook", "honeycomb", "innovid", "jfrog", "lightricks",
    "lightrun", "melio", "mixmode", "mixtiles", "mongodb", "moveworks",
    "nanit", "nice", "obligo", "openweb", "optimove", "orcasecurity",
    "pagaya", "payoneer", "pendo", "playtikaltd", "riskified", "safebreach",
    "saltsecurity", "similarweb", "sisense", "stripe", "taboola", "torq",
    "transmitsecurity", "trustpilot", "truelayer", "verisign", "via",
    "vonage", "walnut", "wizinc", "yotpo", "ziprecruiter", "zoominfo",
    "zscaler",
]

LEVER_IL_BOARDS = [
    "walkme",
    "cloudinary",
]

ASHBY_IL_BOARDS = [
    "deel",
    "redis",
    "lemonade",
    "snappy",
    "diagrid",
]


# ── Jobicy ────────────────────────────────────────────────────────────────────

def fetch_jobicy(settings, max_age_s):
    if not settings.get("jobBoards", {}).get("jobicy"):
        return []
    try:
        raw = http_get("https://jobicy.com/api/v2/remote-jobs?count=100&tag=developer")
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

# ── Himalayas ─────────────────────────────────────────────────────────────────

def fetch_himalayas(settings, max_age_s):
    if not settings.get("jobBoards", {}).get("himalayas"):
        return []
    try:
        raw = http_get("https://himalayas.app/jobs/api?limit=100&q=developer")
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

# ── Greenhouse ────────────────────────────────────────────────────────────────

def _fetch_one_greenhouse(slug, max_age_s, max_years=2.5):
    try:
        raw = http_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=12)
        data = json.loads(raw)
    except HTTPError as e:
        if e.code != 404:
            print(f"    [gh:{slug}] HTTP {e.code}")
        return []
    except Exception as e:
        print(f"    [gh:{slug}] error: {e}")
        return []
    jobs = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        if not _is_il_location(loc):
            continue
        ts = None
        for date_field in ("first_published", "updated_at"):
            v = j.get(date_field)
            if v:
                try:
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
            if years is not None and years >= max_years:
                continue
            job["description"] = _strip_html(desc)
            job["description_snippet"] = _strip_html(desc)[:400]
        except Exception:
            pass
        enriched.append(job)
    return enriched


def fetch_greenhouse_il(settings, max_age_s):
    if not settings.get("jobBoards", {}).get("greenhouseIL"):
        return []
    boards = (settings.get("greenhouseBoards") or []) + GREENHOUSE_IL_BOARDS
    seen_b = set(); boards = [b for b in boards if not (b in seen_b or seen_b.add(b))]
    all_jobs = []
    max_years = settings.get("maxYears", 2.5)
    with ThreadPoolExecutor(max_workers=15) as ex:
        futs = {ex.submit(_fetch_one_greenhouse, slug, max_age_s, max_years): slug for slug in boards}
        for f in as_completed(futs):
            all_jobs.extend(f.result() or [])
    print(f"  Greenhouse (IL, {len(boards)} boards): {len(all_jobs)} listings")
    return all_jobs

# ── Lever ─────────────────────────────────────────────────────────────────────

def _fetch_one_lever(slug, max_age_s, max_years=2.5):
    try:
        raw = http_get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=12)
        data = json.loads(raw)
    except HTTPError as e:
        if e.code != 404:
            print(f"    [lever:{slug}] HTTP {e.code}")
        return []
    except Exception as e:
        print(f"    [lever:{slug}] error: {e}")
        return []
    if not isinstance(data, list):
        return []
    jobs = []
    for j in data:
        loc_str = (j.get("categories") or {}).get("location", "") or ""
        if not _is_il_location(loc_str):
            continue
        ts = None
        ca = j.get("createdAt")
        if isinstance(ca, (int, float)):
            ts = ca / 1000.0 if ca > 1e12 else float(ca)
        if not _age_ok(ts, max_age_s):
            continue
        desc  = j.get("descriptionPlain") or j.get("description") or ""
        years = _extract_min_years(desc)
        if years is not None and years >= max_years:
            continue
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
    max_years = settings.get("maxYears", 2.5)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_one_lever, slug, max_age_s, max_years): slug for slug in boards}
        for f in as_completed(futs):
            all_jobs.extend(f.result() or [])
    print(f"  Lever (IL, {len(boards)} boards): {len(all_jobs)} listings")
    return all_jobs

# ── Ashby ─────────────────────────────────────────────────────────────────────

def _fetch_one_ashby(slug, max_age_s, max_years=2.5):
    try:
        raw = http_get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=25)
        data = json.loads(raw)
    except HTTPError as e:
        if e.code != 404:
            print(f"    [ashby:{slug}] HTTP {e.code}")
        return []
    except Exception as e:
        print(f"    [ashby:{slug}] error: {e}")
        return []
    jobs_out = []
    for j in data.get("jobs", []):
        if not j.get("isListed", True):
            continue
        loc = j.get("location", "") or ""
        addr = (j.get("address") or {}).get("postalAddress") or {}
        loc_combined = f"{loc} {addr.get('addressLocality','')} {addr.get('addressRegion','')} {addr.get('addressCountry','')}".strip()
        loc_lower = loc_combined.lower()
        if not _is_il_location(loc_combined) and "remote" not in loc_lower:
            continue
        ts = None
        pub = j.get("publishedAt")
        if pub:
            try:
                from datetime import datetime as _dt
                ts = _dt.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = None
        if not _age_ok(ts, max_age_s):
            continue
        desc = _strip_html(j.get("descriptionPlain") or j.get("descriptionHtml") or "")
        if desc:
            years = _extract_min_years(desc)
            if years is not None and years >= max_years:
                continue
        jobs_out.append({
            "role": j.get("title", "").strip(),
            "company": slug.title(),
            "location": loc_combined.strip(),
            "link": j.get("jobUrl") or j.get("applyUrl", ""),
            "source": f"Ashby:{slug}",
            "description": desc,
            "description_snippet": desc[:400],
        })
    return jobs_out


def fetch_ashby_il(settings, max_age_s):
    if not settings.get("jobBoards", {}).get("ashbyIL"):
        return []
    boards = settings.get("ashbyBoards") or ASHBY_IL_BOARDS
    all_jobs = []
    max_years = settings.get("maxYears", 2.5)
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_fetch_one_ashby, slug, max_age_s, max_years): slug for slug in boards}
        for f in as_completed(futs):
            all_jobs.extend(f.result() or [])
    print(f"  Ashby (IL, {len(boards)} boards): {len(all_jobs)} listings")
    return all_jobs


# ── Drushim ───────────────────────────────────────────────────────────────────

_DRUSHIM_HEBREW_CITY = {
    # Tel Aviv metro
    "תל אביב": "Tel Aviv", "תל-אביב": "Tel Aviv", "תל אביב יפו": "Tel Aviv",
    "תל אביב-יפו": "Tel Aviv", "יפו": "Yafo",
    "רמת גן": "Ramat Gan", "גבעתיים": "Givatayim", "בני ברק": "Bnei Brak",
    "הרצליה": "Herzliya", "הרצליה פיתוח": "Herzliya",
    "פתח תקווה": "Petah Tikva", "פתח-תקווה": "Petah Tikva", "פתח תקוה": "Petah Tikva",
    "חולון": "Holon", "בת ים": "Bat Yam", "רמת השרון": "Ramat HaSharon",
    "כפר סבא": "Kefar Sava", "רעננה": "Ra'anana", "הוד השרון": "Hod HaSharon",
    "ראש העין": "Rosh HaAyin", "אור יהודה": "Or Yehuda", "יהוד": "Yehud",
    "יהוד-מונוסון": "Yehud",
    # Shfela / south-central
    "ראשון לציון": "Rishon LeZion", 'ראשל"צ': "Rishon LeZion",
    "רחובות": "Rehovot", "נס ציונה": "Ness Ziona", "מודיעין": "Modiin",
    "מודיעין-מכבים-רעות": "Modiin", "לוד": "Lod", "רמלה": "Ramla",
    "בית שמש": "Beit Shemesh", "אשדוד": "Ashdod", "אשקלון": "Ashkelon",
    "קריית גת": "Kiryat Gat", "קרית גת": "Kiryat Gat",
    # Sharon / north-central
    "נתניה": "Netanya", "חדרה": "Hadera", "כוכב יאיר": "Kochav Yair",
    "שוהם": "Shoham", "גני תקווה": "Ganei Tikva", "אבן יהודה": "Even Yehuda",
    # Jerusalem area
    "ירושלים": "Jerusalem", "מבשרת ציון": "Mevaseret Zion",
    # Haifa / north
    "חיפה": "Haifa", "קריית ביאליק": "Kiryat Bialik", "קרית ביאליק": "Kiryat Bialik",
    "קריית אתא": "Kiryat Ata", "קרית אתא": "Kiryat Ata",
    "קריית מוצקין": "Kiryat Motzkin", "קרית מוצקין": "Kiryat Motzkin",
    "קריית ים": "Kiryat Yam", "קרית ים": "Kiryat Yam",
    "נשר": "Nesher", "טירת כרמל": "Tirat Carmel",
    "יקנעם": "Yokneam", "יקנעם עילית": "Yokneam",
    "כרמיאל": "Karmiel", "עפולה": "Afula", "טבריה": "Tiberias",
    "נצרת": "Nazareth", "נצרת עילית": "Nof HaGalil", "נוף הגליל": "Nof HaGalil",
    "מעלות תרשיחא": "Maalot", "צפת": "Safed",
    # South
    "באר שבע": "Beer Sheva", 'באר-שבע': "Beer Sheva", 'ב"ש': "Beer Sheva",
    "אילת": "Eilat", "דימונה": "Dimona", "ערד": "Arad", "ירוחם": "Yeruham",
    "אופקים": "Ofakim", "נתיבות": "Netivot", "שדרות": "Sderot",
    # Other / generic
    "אריאל": "Ariel", "ישראל": "Israel",
    # Regional / vague
    "מרכז הארץ": "Israel", "המרכז": "Israel", "מרכז": "Israel",
    "השרון": "Israel", "השפלה": "Israel",
}


def _drushim_translate_city(s):
    if not s:
        return s
    c = s.strip().rstrip("|").strip()
    if not c:
        return s
    # Already English or contains no Hebrew letters — return as-is
    if not any('א' <= ch <= 'ת' for ch in c):
        return c
    if c in _DRUSHIM_HEBREW_CITY:
        return _DRUSHIM_HEBREW_CITY[c]
    # Prefix match (e.g. "תל אביב יפו" → matches "תל אביב")
    for heb, eng in _DRUSHIM_HEBREW_CITY.items():
        if c.startswith(heb) or heb.startswith(c):
            return eng
    # Unknown — return the original Hebrew so the user can see the real city
    # (filter will use it for IL_LOCATION_HINTS / hard_reject matching)
    return c


def _fetch_drushim_details(job_url):
    _translate = _drushim_translate_city

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
        return {params[k]: tokens[k] for k in range(min(len(params), len(tokens)))}

    try:
        _h = {
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
            "Referer": "https://www.drushim.co.il/",
        }
        try:
            html = http_get(job_url, headers=_h, timeout=12)
        except Exception:
            return None, None, None

        company = None
        description = None
        city = None

        from bs4 import BeautifulSoup as _BS
        import re as _re
        from html import unescape as _ue
        soup = _BS(html, "html.parser")

        # ── Strategy 1: Drushim CSS selectors (most reliable) ───────────────
        co_el = soup.select_one("p.display-22 span.bidi") or soup.select_one("p.display-22 a span")
        if co_el:
            company = co_el.get_text(strip=True) or None
        loc_el = soup.select_one(".display-18")
        if loc_el:
            raw_city = loc_el.get_text(strip=True).rstrip("|").strip()
            if raw_city:
                city = _translate(raw_city)

        # ── Strategy 2: JSON-LD JobPosting ──────────────────────────────────
        for s in soup.find_all("script", type="application/ld+json"):
            raw = s.string
            if not raw:
                continue
            try:
                d = json.loads(raw)
                if not company:
                    name = ((d.get("hiringOrganization") or {}).get("name") or "").strip()
                    if name:
                        company = name
                if not city:
                    loc_addr = ((d.get("jobLocation") or {}).get("address") or {})
                    locality = (loc_addr.get("addressLocality") or loc_addr.get("addressRegion") or "").strip()
                    if locality:
                        city = _translate(locality)
                if not description:
                    raw_desc = (d.get("description") or "").strip()
                    if raw_desc:
                        description = _re.sub(r'<[^>]+>', ' ', _ue(raw_desc))[:3000]
            except Exception:
                continue
            if company and city and description:
                break

        return company or None, city or None, description
    except Exception:
        return None, None, None


def fetch_drushim(settings, max_age_s):
    if not settings.get("jobBoards", {}).get("drushim"):
        return []

    kw = load_keywords()
    search_terms = kw.get("drushim_search_terms", [
        "מפתח", "מתכנת", "פולסטאק", "בקאנד", "פרונטאנד",
        "junior", "react", "python",
    ])
    cat_ids = kw.get("drushim_category_ids", [])

    _hdrs = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        "Referer": "https://www.drushim.co.il/",
    }

    import urllib.parse as _up
    from bs4 import BeautifulSoup as _BS

    def _scrape_page(url):
        html_text = http_get(url, headers=_hdrs, timeout=15)
        soup = _BS(html_text, "html.parser")
        cards = soup.select(".job-item")
        results = []
        for card in cards:
            title_el = card.select_one("h3 span.job-url") or card.select_one("h3")
            link_el  = card.select_one('a[href*="/job/"]')
            if not title_el or not link_el:
                continue
            title = title_el.get_text(strip=True)
            href  = link_el.get("href", "")
            link  = href if href.startswith("http") else f"https://www.drushim.co.il{href}"
            if not title or not link:
                continue
            card_text = card.get_text(separator=" ", strip=True)
            # Drushim card structure: company in p.display-22 span.bidi, city in .display-18
            co_el = card.select_one("p.display-22 span.bidi") or card.select_one("p.display-22 a span")
            card_company = co_el.get_text(strip=True) if co_el else ""
            loc_el = card.select_one(".display-18")
            card_location = loc_el.get_text(strip=True).rstrip("|").strip() if loc_el else ""
            results.append({"title": title, "link": link, "card_text": card_text,
                            "card_company": card_company, "card_location": card_location})
        return cards, results

    # Drushim page sizes are inconsistent (observed 25/12/14 across pages for the same
    # search). Break only on a truly empty page or the page cap — anything else and we
    # silently drop later pages that still have results.
    def _fetch_term(term):
        results = []
        if "." in term:
            return results
        base_url = f"https://www.drushim.co.il/jobs/search/{_up.quote(term)}"
        for page in range(1, 21):
            url = base_url if page == 1 else f"{base_url}/{page}"
            try:
                cards, page_results = _scrape_page(url)
                if not cards:
                    break
                results.extend(page_results)
            except Exception as e:
                print(f"    [drushim] '{term}' page {page}: {e}")
                break
        return results

    def _fetch_category(cat_id):
        results = []
        for page in range(1, 16):
            url = (f"https://www.drushim.co.il/jobs/cat{cat_id}/"
                   if page == 1
                   else f"https://www.drushim.co.il/jobs/cat{cat_id}/?page={page}")
            try:
                cards, page_results = _scrape_page(url)
                if not cards:
                    break
                results.extend(page_results)
            except Exception as e:
                print(f"    [drushim] cat{cat_id} page {page}: {e}")
                break
        return results

    raw_items, seen_links = [], set()

    with ThreadPoolExecutor(max_workers=20) as ex:
        term_futs = {ex.submit(_fetch_term, t): ("term", t) for t in search_terms}
        cat_futs  = {ex.submit(_fetch_category, c): ("cat",  c) for c in cat_ids}
        for fut in as_completed({**term_futs, **cat_futs}):
            kind, label = (term_futs if fut in term_futs else cat_futs)[fut]
            items = fut.result()
            new = 0
            for it in items:
                if it["link"] not in seen_links:
                    seen_links.add(it["link"])
                    raw_items.append(it)
                    new += 1
            tag = f"'{label}'" if kind == "term" else f"cat{label}"
            print(f"    [drushim] {tag}: {len(items)} cards, {new} new")

    if not raw_items:
        print("  Drushim: 0 listings")
        return []

    import re as _re
    from filters import _extract_min_years as _exy
    _kw = load_keywords()
    _he_pats = _kw.get("experience_patterns_hebrew", [])
    _max_yrs = settings.get("maxYears", 2.5)
    card_filtered = []
    for it in raw_items:
        card_yrs = _exy(it.get("card_text", ""), _he_pats, max_yrs=_max_yrs)
        if card_yrs is not None and card_yrs >= _max_yrs:
            continue
        card_filtered.append(it)
    dropped_cards = len(raw_items) - len(card_filtered)
    if dropped_cards:
        print(f"    [drushim] dropped {dropped_cards} over-experienced jobs from card metadata")
    raw_items = card_filtered

    # Skip detail fetches for cards whose title has no dev keyword or skill —
    # pre_filter would drop them anyway, so this just saves HTTP requests.
    _settings_kws = [w.lower() for w in settings.get("devRoleKeywords", [])]
    _settings_skills = [s.lower() for s in settings.get("skills", [])]
    _dev_filter = _settings_kws + _settings_skills
    if _dev_filter:
        before_dev = len(raw_items)
        raw_items = [it for it in raw_items
                     if any(w in it["title"].lower() for w in _dev_filter)]
        skipped = before_dev - len(raw_items)
        if skipped:
            print(f"    [drushim] skipped {skipped} cards with no dev keyword in title")

    all_jobs = []
    for it in raw_items:
        title = it["title"]
        # Seed company from card HTML, then try title pattern "X מגייסת..."
        company = it.get("card_company", "")
        if not company:
            cm = _re.match(r"^([֐-׿A-Za-z0-9 ()&.\-]+?)\s+מגייסת", title)
            if cm:
                company = cm.group(1).strip()
        # Seed location from card HTML, translated to English city
        raw_loc = it.get("card_location", "")
        location = _drushim_translate_city(raw_loc) if raw_loc else "Israel"
        all_jobs.append({
            "role":     title,
            "company":  company,
            "location": location,
            "link":     it["link"],
            "source":   "Drushim",
        })

    print(f"  Drushim: fetching details for {len(all_jobs)} listings...")
    resolved_co, resolved_loc, resolved_desc = 0, 0, 0
    with ThreadPoolExecutor(max_workers=20) as ex:
        detail_futs = {ex.submit(_fetch_drushim_details, j["link"]): j for j in all_jobs}
        for fut in as_completed(detail_futs):
            company, city, description = fut.result()
            job = detail_futs[fut]
            if company and not job.get("company"):
                job["company"] = company
                resolved_co += 1
            if city and job.get("location") in ("", "Israel"):
                job["location"] = city
                resolved_loc += 1
            if description:
                job["description"] = description
                job["description_snippet"] = description[:400]
                resolved_desc += 1

    no_company = sum(1 for j in all_jobs if not j.get("company"))
    no_city    = sum(1 for j in all_jobs if j.get("location") == "Israel")
    no_desc    = sum(1 for j in all_jobs if not j.get("description"))
    print(f"  Drushim: {len(all_jobs)} listings "
          f"(desc resolved: {resolved_desc}, company resolved: {resolved_co}, "
          f"location resolved: {resolved_loc}, "
          f"no-company: {no_company}, generic-location: {no_city}, no-desc: {no_desc})")
    return all_jobs

# ── AllJobs (keyword-based, Israel's largest general job board) ───────────────

def _fetch_alljobs_details(url, opener):
    try:
        html = _alljobs_get(opener, url, timeout=15)
        from bs4 import BeautifulSoup as _BS
        soup = _BS(html, "html.parser")
        for sel in [".job-description", "#job-description", ".jobdescription",
                    "[class*='description']", ".job-content", ".content"]:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                return _strip_html(el.get_text(separator=" ", strip=True))[:3000]
        return None
    except Exception:
        return None


def _alljobs_opener():
    import urllib.request as _ur
    import http.cookiejar as _cj
    jar = _cj.CookieJar()
    opener = _ur.build_opener(_ur.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent", BROWSER_UA),
        ("Accept", "text/html,application/xhtml+xml,*/*;q=0.8"),
        ("Accept-Language", "he-IL,he;q=0.9,en;q=0.8"),
        ("Accept-Encoding", "gzip, deflate"),
        ("Connection", "keep-alive"),
    ]
    try:
        opener.open("https://www.alljobs.co.il/", timeout=10)
    except Exception:
        pass
    return opener


def _alljobs_get(opener, url, timeout=20):
    import gzip as _gz
    resp = opener.open(url, timeout=timeout)
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = _gz.decompress(raw)
    charset = resp.headers.get_content_charset() or "windows-1255"
    try:
        return raw.decode(charset)
    except (UnicodeDecodeError, LookupError):
        return raw.decode("utf-8", errors="replace")


def fetch_alljobs(settings, max_age_s):
    if not settings.get("jobBoards", {}).get("alljobs"):
        return []

    import urllib.parse as _up
    from bs4 import BeautifulSoup as _BS

    search_terms = load_keywords().get("alljobs_search_terms", [
        "developer", "fullstack", "backend", "frontend",
        "מפתח", "מתכנת", "פולסטאק", "בקאנד",
    ])

    opener = _alljobs_opener()

    # Probe with one request before spawning threads — bail if bot-blocked
    _probe_url = (f"https://www.alljobs.co.il/SearchResultsPage.aspx"
                  f"?query=developer&from=1&numOfResults=20")
    try:
        _probe = _alljobs_get(opener, _probe_url, timeout=15)
        if len(_probe) < 10_000 and 'stormcaster' in _probe:
            print("  AllJobs: bot-block detected — skipping board (temporary IP block)")
            return []
    except Exception as e:
        print(f"  AllJobs: probe failed ({e}) — skipping board")
        return []

    all_jobs, seen_links = [], set()

    def _scrape_term(term):
        results = []
        _per_page = 50
        page = 1
        while True:
            url = (
                f"https://www.alljobs.co.il/SearchResultsPage.aspx"
                f"?query={_up.quote(term)}&from={(page - 1) * _per_page + 1}&numOfResults={_per_page}"
            )
            try:
                html = _alljobs_get(opener, url, timeout=20)
            except Exception as e:
                print(f"    [alljobs] '{term}' page {page}: {e}")
                break
            if len(html) < 10_000 and 'stormcaster' in html:
                break
            soup = _BS(html, "html.parser")
            cards = (
                soup.select("li.job-item") or
                soup.select("div.job-item") or
                soup.select("article") or
                soup.select("[class*='job'][class*='item']")
            )
            if not cards:
                break
            for card in cards:
                link_el = card.select_one("a[href*='/job/']") or card.select_one("a[href]")
                if not link_el:
                    continue
                href = link_el.get("href", "")
                link = href if href.startswith("http") else f"https://www.alljobs.co.il{href}"
                if not link or link in seen_links:
                    continue

                title = link_el.get_text(strip=True)
                if not title:
                    t = card.select_one("h2, h3, .job-title, [class*='title']")
                    title = t.get_text(strip=True) if t else ""
                if not title:
                    continue

                c = card.select_one(".company, .company-name, [class*='company']")
                company = c.get_text(strip=True) if c else ""
                l = card.select_one(".location, .city, [class*='location'], [class*='city']")
                location = l.get_text(strip=True) if l else "Israel"

                results.append({
                    "role": title, "company": company, "location": location,
                    "link": link, "source": "AllJobs",
                })
            if len(cards) < _per_page:
                break
            page += 1
        return results

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_scrape_term, t): t for t in search_terms}
        for fut in as_completed(futs):
            term = futs[fut]
            items = fut.result()
            new = 0
            for it in items:
                if it["link"] not in seen_links:
                    seen_links.add(it["link"])
                    all_jobs.append(it)
                    new += 1
            if items:
                print(f"    [alljobs] '{term}': {len(items)} cards, {new} new")

    # Fetch detail pages to get descriptions and apply years filter
    if all_jobs:
        desc_count = 0
        with ThreadPoolExecutor(max_workers=10) as ex:
            detail_futs = {ex.submit(_fetch_alljobs_details, j["link"], opener): j for j in all_jobs}
            for fut in as_completed(detail_futs):
                desc = fut.result()
                job = detail_futs[fut]
                if desc:
                    job["description"] = desc
                    job["description_snippet"] = desc[:400]
                    desc_count += 1
        if desc_count:
            print(f"    [alljobs] fetched descriptions for {desc_count}/{len(all_jobs)} jobs")

        _he_pats = load_keywords().get("experience_patterns_hebrew", [])
        _max_yrs = settings.get("maxYears", 2.5)
        before_yrs = len(all_jobs)
        filtered = []
        for j in all_jobs:
            if j.get("description"):
                yrs = _extract_min_years(j["description"], _he_pats, max_yrs=_max_yrs)
                if yrs is not None and yrs >= _max_yrs:
                    continue
            filtered.append(j)
        dropped_yrs = before_yrs - len(filtered)
        if dropped_yrs:
            print(f"    [alljobs] dropped {dropped_yrs} over-experienced from descriptions")
        all_jobs = filtered

    print(f"  AllJobs: {len(all_jobs)} listings")
    return all_jobs


# ── GotFriends ────────────────────────────────────────────────────────────────

_GF_LOC = {
    'ת"א': 'Tel Aviv', "ת'א": 'Tel Aviv', 'תל אביב': 'Tel Aviv', 'תל-אביב': 'Tel Aviv',
    'רמת גן': 'Ramat Gan', 'הרצליה': 'Herzliya', 'הרצליה פיתוח': 'Herzliya',
    'פתח תקווה': 'Petah Tikva', 'פתח-תקווה': 'Petah Tikva',
    'חולון': 'Holon', 'ראשון לציון': 'Rishon LeZion', 'ראשל"צ': 'Rishon LeZion',
    'רחובות': 'Rehovot', 'נס ציונה': 'Ness Ziona', 'בת ים': 'Bat Yam',
    'ירושלים': 'Jerusalem', 'חיפה': 'Haifa', 'באר שבע': 'Beer Sheva',
    'נתניה': 'Netanya', 'כפר סבא': 'Kefar Sava', 'רעננה': "Ra'anana",
    'גבעתיים': 'Givatayim', 'יפו': 'Yafo', 'ישראל': 'Israel',
    'מרכז הארץ': 'Israel', 'המרכז': 'Israel', 'מרכז': 'Israel',
}


def _gf_translate_loc(raw):
    for heb, eng in _GF_LOC.items():
        if heb in raw:
            return eng
    return 'Israel'


def _fetch_gotfriends_detail(url, hdrs):
    try:
        html = http_get(url, headers=hdrs, timeout=12)
    except Exception:
        return '', 'Israel'
    from bs4 import BeautifulSoup as _BS
    soup = _BS(html, 'html.parser')
    desc = ''
    for sel in ('.item_content', '.inner', '.position-details', '.job-description'):
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator='\n', strip=True)
            if 'מיקום' in text or 'תיאור' in text or 'דרישות' in text:
                desc = text
                break
    if not desc:
        candidates = []
        for el in soup.find_all(['div', 'section']):
            t = el.get_text()
            if 'מיקום' in t and 'תיאור' in t and len(t) > 150:
                candidates.append(el)
        if candidates:
            candidates.sort(key=lambda e: len(e.get_text()))
            desc = candidates[0].get_text(separator='\n', strip=True)
    loc = 'Israel'
    m = re.search(r'מיקום\s*[:\-]?\s*([^\n]{2,60})', desc)
    if m:
        loc = _gf_translate_loc(m.group(1).strip())
    return desc[:3000], loc


def fetch_gotfriends(settings, max_age_s):
    if not settings.get("jobBoards", {}).get("gotfriends"):
        return []
    from bs4 import BeautifulSoup as _BS

    _hdrs = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        "Referer": "https://www.gotfriends.co.il/",
    }
    base = "https://www.gotfriends.co.il/jobslobby/software/"
    raw_cards, seen_links = [], set()
    page = 1
    total = None

    MAX_PAGES = 30  # ~300 jobs; sorted by recency so covers ~4 weeks
    while page <= MAX_PAGES:
        url = base if page == 1 else f"{base}?page={page}&total={total or 1134}"
        html = None
        for attempt in range(2):
            try:
                html = http_get(url, headers=_hdrs, timeout=25)
                break
            except Exception as e:
                print(f"    [gotfriends] page {page} attempt {attempt+1}: {e}")
        if html is None:
            if page == 1:
                page += 1
                continue
            break
        try:
            soup = _BS(html, "html.parser")
            cards = soup.select(".position")
            if not cards:
                break
            if page == 1 and total is None:
                for a in soup.select(".pagination li a"):
                    m = re.search(r"total=(\d+)", a.get("href", ""))
                    if m:
                        total = int(m.group(1))
                        break
            for card in cards:
                href = card.get("href", "")
                link = href if href.startswith("http") else f"https://www.gotfriends.co.il{href}"
                if not link or link in seen_links:
                    continue
                title_el = card.select_one("h2.title, h2, .title")
                title = title_el.get_text(strip=True) if title_el else ""
                if not title:
                    continue
                seen_links.add(link)
                raw_cards.append({"title": title, "link": link})
            if len(cards) < 10:
                break
            page += 1
        except Exception as e:
            print(f"    [gotfriends] page {page} parse: {e}")
            break

    if not raw_cards:
        print("  GotFriends: 0 listings")
        return []

    print(f"  GotFriends: fetching details for {len(raw_cards)} listings...")
    all_jobs = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_fetch_gotfriends_detail, c["link"], _hdrs): c for c in raw_cards}
        for fut in as_completed(futs):
            card = futs[fut]
            desc, loc = fut.result()
            all_jobs.append({
                "role":                card["title"],
                "company":             "GotFriends (recruiter)",
                "location":            loc,
                "link":                card["link"],
                "source":              "GotFriends",
                "description":         desc,
                "description_snippet": desc[:400],
            })

    _he_pats = load_keywords().get("experience_patterns_hebrew", [])
    _max_yrs = settings.get("maxYears", 2.5)
    filtered = []
    for j in all_jobs:
        if j.get("description"):
            yrs = _extract_min_years(j["description"], _he_pats, max_yrs=_max_yrs)
            if yrs is not None and yrs >= _max_yrs:
                continue
        filtered.append(j)
    dropped = len(all_jobs) - len(filtered)
    if dropped:
        print(f"    [gotfriends] dropped {dropped} over-experienced")
    print(f"  GotFriends: {len(filtered)} listings")
    return filtered


# ── Aggregator ────────────────────────────────────────────────────────────────

def fetch_all_jobs(settings):
    import time as _time
    boards    = settings.get("jobBoards", {})
    max_age_s = POST_DATE_SECONDS.get(settings.get("postDateFilter", "7d"), 604800)

    tasks = []
    if boards.get("greenhouseIL"): tasks.append(("greenhouseIL", lambda: fetch_greenhouse_il(settings, max_age_s)))
    if boards.get("leverIL"):      tasks.append(("leverIL",      lambda: fetch_lever_il(settings, max_age_s)))
    if boards.get("ashbyIL"):      tasks.append(("ashbyIL",      lambda: fetch_ashby_il(settings, max_age_s)))
    if boards.get("drushim"):      tasks.append(("drushim",      lambda: fetch_drushim(settings, max_age_s)))
    if boards.get("gotfriends"):   tasks.append(("gotfriends",   lambda: fetch_gotfriends(settings, max_age_s)))
    if boards.get("jobicy"):       tasks.append(("jobicy",       lambda: fetch_jobicy(settings, max_age_s)))
    if boards.get("himalayas"):    tasks.append(("himalayas",    lambda: fetch_himalayas(settings, max_age_s)))

    if not tasks:
        print("  No boards enabled.", flush=True)
        return []

    all_jobs    = []
    board_stats = {}

    def _run_board(name, fn):
        t0 = _time.time()
        try:
            result  = fn()
            elapsed = _time.time() - t0
            print(f"  [{name}] done in {elapsed:.0f}s -> {len(result)} listings", flush=True)
            progress_log(f"::notice title=board_done::{name}={len(result)}")
            return name, result
        except Exception as e:
            elapsed = _time.time() - t0
            print(f"  [{name}] ERROR after {elapsed:.0f}s: {e}", flush=True)
            progress_log(f"::notice title=board_done::{name}=error")
            return name, []

    with ThreadPoolExecutor(max_workers=min(len(tasks), 6)) as ex:
        futs = {ex.submit(_run_board, name, fn): name for name, fn in tasks}
        for fut in as_completed(futs):
            name, result = fut.result()
            board_stats[name] = len(result)
            all_jobs.extend(result)

    print(f"  Board summary: {board_stats}", flush=True)
    return all_jobs
