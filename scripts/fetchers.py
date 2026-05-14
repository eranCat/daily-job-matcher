import json, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
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
    if not any('א' <= ch <= 'ת' for ch in c):
        return c
    if c in _DRUSHIM_HEBREW_CITY:
        return _DRUSHIM_HEBREW_CITY[c]
    for heb, eng in _DRUSHIM_HEBREW_CITY.items():
        if c.startswith(heb) or heb.startswith(c):
            return eng
    return c


def _fetch_drushim_details(job_url):
    _translate = _drushim_translate_city
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

        co_el = soup.select_one("p.display-22 span.bidi") or soup.select_one("p.display-22 a span")
        if co_el:
            company = co_el.get_text(strip=True) or None
        loc_el = soup.select_one(".display-18")
        if loc_el:
            raw_city = loc_el.get_text(strip=True).rstrip("|").strip()
            if raw_city:
                city = _translate(raw_city)

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
            co_el = card.select_one("p.display-22 span.bidi") or card.select_one("p.display-22 a span")
            card_company = co_el.get_text(strip=True) if co_el else ""
            loc_el = card.select_one(".display-18")
            card_location = loc_el.get_text(strip=True).rstrip("|").strip() if loc_el else ""
            results.append({"title": title, "link": link, "card_text": card_text,
                            "card_company": card_company, "card_location": card_location})
        return cards, results

    # Drushim page sizes are inconsistent (observed 25/12/14 across pages for the same
    # search). Break only on a truly empty page or the page cap.
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
        company = it.get("card_company", "")
        if not company:
            cm = _re.match(r"^([֐-׿A-Za-z0-9 ()&.\-]+?)\s+מגייסת", title)
            if cm:
                company = cm.group(1).strip()
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


# ── Aggregator ────────────────────────────────────────────────────────────────

def fetch_all_jobs(settings):
    import time as _time
    boards    = settings.get("jobBoards", {})
    max_age_s = POST_DATE_SECONDS.get(settings.get("postDateFilter", "7d"), 604800)

    tasks = []
    if boards.get("greenhouseIL"): tasks.append(("greenhouseIL", lambda: fetch_greenhouse_il(settings, max_age_s)))
    if boards.get("drushim"):      tasks.append(("drushim",      lambda: fetch_drushim(settings, max_age_s)))

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

    with ThreadPoolExecutor(max_workers=min(len(tasks), 4)) as ex:
        futs = {ex.submit(_run_board, name, fn): name for name, fn in tasks}
        for fut in as_completed(futs):
            name, result = fut.result()
            board_stats[name] = len(result)
            all_jobs.extend(result)

    print(f"  Board summary: {board_stats}", flush=True)
    return all_jobs
