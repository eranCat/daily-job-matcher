import os, json, re, time, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib import request as urlreq

class _Tee:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except UnicodeEncodeError:
                enc = getattr(s, 'encoding', None) or 'utf-8'
                try:
                    s.write(data.encode(enc, errors='replace').decode(enc))
                except Exception:
                    pass
            except Exception:
                pass

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

    def fileno(self):
        return self._streams[0].fileno()


def setup_file_logging(log_path: str) -> None:
    path = Path(log_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(path, "w", encoding="utf-8", buffering=1)
    sys.stdout = _Tee(sys.__stdout__, log_file)
    sys.stderr = _Tee(sys.__stderr__, log_file)


def gha_log(msg: str) -> None:
    """Emit a GitHub Actions workflow command (::notice, ::error, etc.).
    Suppressed when LOCAL_RUN is set so plain CLI output stays clean —
    unless JM_PROGRESS is also set, which the local UI server uses to
    drive its progress display."""
    if os.getenv("JM_PROGRESS") or not os.getenv("LOCAL_RUN"):
        print(msg, flush=True)


def progress_log(msg: str) -> None:
    """Sub-step progress that ONLY surfaces in the local SSE UI.
    GitHub Actions caps `::notice` annotations at 10 per step — emitting
    every per-board update there blows the budget and drops later step
    transitions. Local SSE has no such cap, so route incremental updates
    here while leaving phase milestones on `gha_log`."""
    if os.getenv("JM_PROGRESS"):
        print(msg, flush=True)


BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

try:
    from zoneinfo import ZoneInfo
    JERUSALEM_TZ = ZoneInfo("Asia/Jerusalem")
except ImportError:
    JERUSALEM_TZ = timezone(timedelta(hours=3))

POST_DATE_SECONDS = {
    "24h": 86400, "3d": 259200, "7d": 604800,
    "14d": 1209600, "30d": 2592000,
}

# Populated at startup from config/keywords.json by _load_il_hints()
IL_LOCATION_HINTS: list[str] = []


def _load_il_hints():
    kw = load_keywords()
    hints = kw.get("il_location_hints")
    if hints:
        IL_LOCATION_HINTS[:] = hints


def _is_il_location(loc_str):
    s = (loc_str or "").lower()
    return any(h in s for h in IL_LOCATION_HINTS)


def load_settings():
    path = Path(__file__).resolve().parent.parent / "config" / "search-settings.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {
        "skills": ["React", "TypeScript", "Python", "FastAPI", "Node.js", "Docker", "PostgreSQL"],
        "maxYears": 2.5,
        "locations": ["Tel Aviv", "Ramat Gan", "Herzliya", "Holon", "Petah Tikva",
                      "Ness Ziona", "Rehovot", "Rishon Lezion", "Bat Yam", "Israel", "Remote"],
        "remoteOk": True,
        "remoteIsraelOnly": True,
        "excludedCompanies": ["Experis", "Manpower", "Allstars", "Infinity Labs", "Elevation",
                              "ITC", "Naya", "Coding Academy", "ManTech"],
        "excludedKeywords": ["senior", "lead", "manager", "principal", "staff", "director",
                             "head of", "vp"],
        "excludedStacks": ["PHP", ".NET", "C#", "Ruby", "ABAP", "SAP", "Salesforce"],
        "devRoleKeywords": ["developer", "engineer", "full stack", "fullstack", "backend",
                            "frontend", "software", "מפתח", "מהנדס", "פולסטק", "מתכנת"],
        "minScore": 7,
        "maxResults": 25,
        "postDateFilter": "30d",
        "verifyLinks": True,
        "jobBoards": {
            "greenhouseIL": True, "leverIL": True, "ashbyIL": True,
            "jobicy": True, "himalayas": True, "drushim": True,
        },
    }


def load_keywords():
    path = Path(__file__).resolve().parent.parent / "config" / "keywords.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def http_get(url, timeout=20, headers=None):
    h = {"User-Agent": BROWSER_UA, **(headers or {})}
    req = urlreq.Request(url, headers=h)
    with urlreq.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        charset = r.headers.get_content_charset() or 'utf-8'
        try:
            return raw.decode(charset)
        except (UnicodeDecodeError, LookupError):
            return raw.decode('utf-8', errors='replace')


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
        return True
    return (time.time() - ts_seconds) <= max_age_s


def _strip_html(text):
    text = re.sub(r'<[^>]+>', ' ', text or '')
    # Decode HTML entities (&amp; &lt; &gt; &#NNN; &#xNNN;)
    try:
        from html import unescape as _ue
        text = _ue(text)
    except Exception:
        pass
    text = re.sub(r'<[^>]+>', ' ', text)   # strip tags exposed by entity decode
    text = re.sub(r'&[a-zA-Z#0-9]+;', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


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
