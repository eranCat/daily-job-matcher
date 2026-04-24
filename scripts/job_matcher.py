#!/usr/bin/env python3
"""Daily job matcher using Groq API (free tier).

Run modes (via RUN_MODE env var):
  search           - normal run: query LLM for jobs, sync matches to sheet
  test-connection  - ping sheet webhook to verify connectivity
  test-write       - append a synthetic test row to verify write permission
"""

import os
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urlreq, error as urlerr

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "llama-3.3-70b-versatile"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


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


def build_prompt(settings):
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

TASK: Based on your knowledge of the Israeli junior dev job market, list up to {settings.get("maxResults", 20)} currently-likely open roles that match this profile. For each, provide a match_score 0-10 against the criteria. Only include jobs with score >= {settings.get("minScore", 7)}.

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


def call_webhook(webhook_url, payload, timeout=30):
    """POST JSON to the Sheets Apps Script webhook. Returns parsed response."""
    body = json.dumps(payload).encode("utf-8")
    req = urlreq.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": BROWSER_UA},
        method="POST",
    )
    with urlreq.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        try:
            return json.loads(raw), resp.status
        except json.JSONDecodeError:
            return {"raw": raw}, resp.status


def require_webhook():
    url = os.getenv("SHEETS_WEBHOOK_URL")
    if not url:
        raise ValueError("SHEETS_WEBHOOK_URL secret not set. Add your Apps Script web app URL in repo Settings → Secrets.")
    return url


# ---- RUN MODES ----

def run_search():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY not set. Get one free at https://console.groq.com/keys")

    settings = load_settings()
    prompt = build_prompt(settings)

    print(f"Mode: search | Model: {MODEL}")
    print(f"Config: {len(settings.get('skills', []))} skills, min score {settings.get('minScore', 7)}, max {settings.get('maxResults', 20)} results")

    content = call_groq(api_key, prompt)
    print("\n=== Groq response ===")
    print(content[:2000])

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}") + 1
        result = json.loads(content[start:end])

    jobs = result.get("jobs", [])
    print(f"\n✓ Found {len(jobs)} matching jobs")
    for j in jobs[:5]:
        print(f"  [{j.get('match_score', '?')}/10] {j.get('role', '?')} @ {j.get('company', '?')} — {j.get('location', '?')}")
    if len(jobs) > 5:
        print(f"  ... +{len(jobs) - 5} more")

    # Sync to Sheets if webhook is configured
    webhook_url = os.getenv("SHEETS_WEBHOOK_URL")
    if webhook_url and jobs:
        print("\nSyncing to Google Sheets...")
        resp, status = call_webhook(webhook_url, {"action": "append", "jobs": jobs})
        print(f"  Webhook response [{status}]: {resp}")
    elif not webhook_url:
        print("\n(No SHEETS_WEBHOOK_URL configured — skipping sheet sync)")

    return result


def run_test_connection():
    """Ping the Apps Script webhook to verify the URL is reachable and the sheet is accessible."""
    url = require_webhook()
    print(f"Mode: test-connection")
    print(f"Webhook: {url[:60]}...")

    try:
        resp, status = call_webhook(url, {"action": "ping"})
        print(f"\n✓ Connection OK [HTTP {status}]")
        print(f"  Response: {resp}")
        return resp
    except urlerr.HTTPError as e:
        raise RuntimeError(f"✗ Webhook HTTP {e.code}: {e.read().decode('utf-8')}")
    except Exception as e:
        raise RuntimeError(f"✗ Connection failed: {e}")


def run_test_write():
    """Append a synthetic test row to verify write permission."""
    url = require_webhook()
    print(f"Mode: test-write")

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    test_job = {
        "role": "TEST ROW — Workflow Connectivity Check",
        "company": "daily-job-matcher",
        "location": "GitHub Actions",
        "link": "https://github.com/eranCat/daily-job-matcher",
        "match_score": 0,
        "_test": True,
        "_timestamp": timestamp,
    }

    resp, status = call_webhook(url, {"action": "append", "jobs": [test_job], "test": True})
    print(f"\n✓ Test row written [HTTP {status}]")
    print(f"  Response: {resp}")
    print(f"\n  You can safely delete this row from the sheet after verifying.")
    return resp


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
