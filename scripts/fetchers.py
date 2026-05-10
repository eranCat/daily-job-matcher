import json, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from utils import http_get, _strip_html, _age_ok, _is_il_location, POST_DATE_SECONDS, BROWSER_UA, load_keywords
from filters import _extract_min_years

# ── Module-level max-years sentinels (set at runtime per board type) ──────────
_GH_MAX_YEARS = 2.5
_LV_MAX_YEARS = 2.5
_AB_MAX_YEARS = 2.5

# ── Board slug lists ──────────────────────────────────────────────────────────

GREENHOUSE_IL_BOARDS = [
    # Removed 2026-05: 21 slugs that 404'd: acronis, binah, bluevine, clickup,
    # dynamicyield, ermetic, intelligo, itamarmedicalltd, khealth, leddartech,
    # lunasolutions, meshpayments, onedigital, pandologic, pecanai, rhinohealth,
    # singular, snyk, tremorinternational, upsolver, vimeo
    "amwell", "apiiro", "appsflyer", "armissecurity", "atbayjobs", "axonius",
    "BigID", "bringg", "canonical", "catonetworks", "cb4", "connecteam",
    "cymulate", "datadog", "datarails", "doitintl", "doubleverify",
    "fireblocks", "forter", "globalityinc", "gongio", "gusto", "honeybook",
    "innovid", "jfrog", "lightricks", "melio", "mixtiles",
    "nanit", "nice", "obligo", "optimove", "orcasecurity", "outbraininc",
    "pagaya", "payoneer", "pendo", "playtikaltd", "riskified",
    "saltsecurity", "similarweb", "sisense", "taboola",
    "techstars57", "torq", "transmitsecurity", "via",
    "vonage", "walnut", "wizinc", "yotpo", "ziprecruiter", "zoominfo", "zscaler",
]

LEVER_IL_BOARDS = [
    "walkme",
    "cloudinary",
    # NOTE: monday, wix, lemonade, fiverr, playtika, gong, salto, kaltura,
    # lightricks, coralogix, atera, silverfort, pentera, snyk all HTTP 404.
]

ASHBY_IL_BOARDS = [
    "lemonade",
    "redis",
    "deel",
]


# ── Jobicy ────────────────────────────────────────────────────────────────────

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

# ── Himalayas ─────────────────────────────────────────────────────────────────

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

# ── Greenhouse ────────────────────────────────────────────────────────────────

def _fetch_one_greenhouse(slug, max_age_s):
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
    max_years = _GH_MAX_YEARS
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
    global _GH_MAX_YEARS
    _GH_MAX_YEARS = settings.get("maxYears", 2.5)
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_fetch_one_greenhouse, slug, max_age_s): slug for slug in boards}
        for f in as_completed(futs):
            all_jobs.extend(f.result() or [])
    print(f"  Greenhouse (IL, {len(boards)} boards): {len(all_jobs)} listings")
    return all_jobs

# ── Lever ─────────────────────────────────────────────────────────────────────

def _fetch_one_lever(slug, max_age_s):
    try:
        raw = http_get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=12)
        data = json.loads(raw)
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
        if years is not None and years > _LV_MAX_YEARS:
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
    global _LV_MAX_YEARS
    _LV_MAX_YEARS = settings.get("maxYears", 2.5)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_one_lever, slug, max_age_s): slug for slug in boards}
        for f in as_completed(futs):
            all_jobs.extend(f.result() or [])
    print(f"  Lever (IL, {len(boards)} boards): {len(all_jobs)} listings")
    return all_jobs

# ── Ashby ─────────────────────────────────────────────────────────────────────

def _fetch_one_ashby(slug, max_age_s):
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
            if years is not None and years > _AB_MAX_YEARS:
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
    global _AB_MAX_YEARS
    _AB_MAX_YEARS = settings.get("maxYears", 2.5)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_one_ashby, slug, max_age_s): slug for slug in boards}
        for f in as_completed(futs):
            all_jobs.extend(f.result() or [])
    print(f"  Ashby (IL, {len(boards)} boards): {len(all_jobs)} listings")
    return all_jobs


# ── Drushim ───────────────────────────────────────────────────────────────────

def _fetch_drushim_details(job_url):
    _HEBREW_CITY = {
        "תל אביב": "Tel Aviv", "תל-אביב": "Tel Aviv", "תל אביב יפו": "Tel Aviv",
        "רמת גן": "Ramat Gan", "הרצליה": "Herzliya", "הרצליה פיתוח": "Herzliya",
        "פתח תקווה": "Petah Tikva", "פתח-תקווה": "Petah Tikva", "פתח תקוה": "Petah Tikva",
        "חולון": "Holon", "נס ציונה": "Ness Ziona", "רחובות": "Rehovot",
        "ראשון לציון": "Rishon LeZion", 'ראשל"צ': "Rishon LeZion",
        "בת ים": "Bat Yam", "ירושלים": "Jerusalem", "חיפה": "Haifa",
        "באר שבע": "Beer Sheva", "נתניה": "Netanya", "כפר סבא": "Kefar Sava",
        "הוד השרון": "Hod HaSharon", "יהוד": "Yehud", "מודיעין": "Modiin",
        "קריית ביאליק": "Kiryat Bialik", "בני ברק": "Bnei Brak",
        "גבעתיים": "Givatayim", "ראש העין": "Rosh HaAyin",
        "רעננה": "Ra'anana", "ישראל": "Israel", "יפו": "Yafo",
    }

    def _translate(s):
        if not s or not any('א' <= c <= 'ת' for c in s):
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
        try:
            from bs4 import BeautifulSoup as _BS
            import re as _re
            soup = _BS(html, "html.parser")
            for s in soup.find_all("script", type="application/ld+json"):
                raw = s.string
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                    name = ((d.get("hiringOrganization") or {}).get("name") or "").strip()
                    if name:
                        company = name
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

        city = None
        ni = html.find("window.__NUXT__=(")
        ne = html.find("</script>", ni) if ni != -1 else -1
        if ni != -1 and ne != -1:
            blob = html[ni:ne]
            lit = re.findall(r'CityEnglish:"(\\t[^"]+\\t)"', blob)
            if lit:
                clean = [c.replace("\\t", "").strip() for c in lit if c.replace("\\t", "").strip()]
                if clean:
                    city = ", ".join(dict.fromkeys(_translate(c) for c in clean))
            if not city:
                refs = list(dict.fromkeys(re.findall(r'CityEnglish:([a-zA-Z_$][a-zA-Z0-9_$]*)', blob)))
                if refs:
                    mapping = _parse_iife_args(blob)
                    cities = [_translate(mapping[ref].strip())
                              for ref in refs
                              if ref in mapping and isinstance(mapping[ref], str)
                              and mapping[ref].strip()]
                    if cities:
                        city = ", ".join(dict.fromkeys(cities))
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
    if not settings.get("jobBoards", {}).get("drushim"):
        return []

    search_terms = load_keywords().get("drushim_search_terms", [
        "מפתח", "מתכנת", "פולסטאק", "בקאנד", "פרונטאנד",
        "junior", "react", "python",
    ])

    _hdrs = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        "Referer": "https://www.drushim.co.il/",
    }

    import urllib.parse as _up
    from bs4 import BeautifulSoup as _BS

    def _fetch_term(term):
        results = []
        base_url = f"https://www.drushim.co.il/jobs/search/{_up.quote(term)}"
        for page in range(1, 11):
            url = base_url if page == 1 else f"{base_url}/{page}"
            try:
                html_text = http_get(url, headers=_hdrs, timeout=15)
                soup = _BS(html_text, "html.parser")
                cards = soup.select(".job-item")
                if not cards:
                    break
                for card in cards:
                    title_el = card.select_one("h3 span.job-url") or card.select_one("h3")
                    link_el  = card.select_one('a[href*="/job/"]')
                    if not title_el or not link_el:
                        continue
                    title = title_el.get_text(strip=True)
                    href  = link_el.get("href", "")
                    link  = href if href.startswith("http") else f"https://www.drushim.co.il{href}"
                    if title and link:
                        card_text = card.get_text(separator=" ", strip=True)
                        results.append({"title": title, "link": link, "card_text": card_text})
                if len(cards) < 20:
                    break
            except Exception as e:
                print(f"    [drushim] '{term}' page {page}: {e}")
                break
        return results

    raw_items, seen_links = [], set()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_term, t): t for t in search_terms}
        for fut in as_completed(futs):
            term  = futs[fut]
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

    import re as _re
    from filters import _extract_min_years as _exy
    _kw = load_keywords()
    _he_pats = _kw.get("experience_patterns_hebrew", [])
    _max_yrs = settings.get("maxYears", 2.5)
    card_filtered = []
    for it in raw_items:
        card_yrs = _exy(it.get("card_text", ""), _he_pats, max_yrs=_max_yrs)
        if card_yrs is not None and card_yrs > _max_yrs:
            continue
        card_filtered.append(it)
    dropped_cards = len(raw_items) - len(card_filtered)
    if dropped_cards:
        print(f"    [drushim] dropped {dropped_cards} over-experienced jobs from card metadata")
    raw_items = card_filtered

    all_jobs = []
    for it in raw_items:
        title = it["title"]
        company = ""
        cm = _re.match(
            r"^([֐-׿A-Za-z0-9 ()&.\-]+?)\s+מגייסת",
            title,
        )
        if cm:
            company = cm.group(1).strip()
        all_jobs.append({
            "role":     title,
            "company":  company,
            "location": "Israel",
            "link":     it["link"],
            "source":   "Drushim",
        })

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

# ── AllJobs (keyword-based, Israel's largest general job board) ───────────────

def fetch_alljobs(settings, max_age_s):
    if not settings.get("jobBoards", {}).get("alljobs"):
        return []

    import urllib.parse as _up
    from bs4 import BeautifulSoup as _BS

    search_terms = load_keywords().get("alljobs_search_terms", [
        "developer", "fullstack", "backend", "frontend",
        "מפתח", "מתכנת", "פולסטאק", "בקאנד",
    ])

    _hdrs = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        "Referer": "https://www.alljobs.co.il/",
    }

    all_jobs, seen_links = [], set()

    def _scrape_term(term):
        results = []
        for page in range(1, 6):
            url = (
                f"https://www.alljobs.co.il/SearchResultsPage.aspx"
                f"?query={_up.quote(term)}&from={(page - 1) * 20 + 1}&numOfResults=20"
            )
            try:
                html = http_get(url, headers=_hdrs, timeout=20)
            except Exception as e:
                print(f"    [alljobs] '{term}' page {page}: {e}")
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
            if len(cards) < 20:
                break
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

    print(f"  AllJobs: {len(all_jobs)} listings")
    return all_jobs


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
    if boards.get("jobicy"):       tasks.append(("jobicy",       lambda: fetch_jobicy(settings, max_age_s)))
    if boards.get("himalayas"):    tasks.append(("himalayas",    lambda: fetch_himalayas(settings, max_age_s)))
    if boards.get("alljobs"):      tasks.append(("alljobs",      lambda: fetch_alljobs(settings, max_age_s)))

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

    print(f"  Board summary: {board_stats}", flush=True)
    return all_jobs
