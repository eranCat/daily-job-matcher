#!/usr/bin/env python3
"""Daily job matcher using Groq API (free tier, no credit card)"""

import os
import json
import sys
from pathlib import Path
from urllib import request as urlreq, error as urlerr

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# llama-3.3-70b-versatile is on Groq free tier; upgrade to 3.1-8b-instant for faster/cheaper
MODEL = "llama-3.3-70b-versatile"


def load_settings():
    """Load search settings from config/search-settings.json if present."""
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
    """Build a filter/rubric prompt from settings."""
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
    """Call Groq chat completions endpoint."""
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
        },
        method="POST",
    )
    try:
        with urlreq.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urlerr.HTTPError as e:
        err_body = e.read().decode("utf-8")
        raise RuntimeError(f"Groq API {e.code}: {err_body}")

    return data["choices"][0]["message"]["content"]


def run_job_matcher():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY not set. Get one free at https://console.groq.com/keys")

    settings = load_settings()
    prompt = build_prompt(settings)

    print(f"Running job matcher with model: {MODEL}")
    print(f"Config: {len(settings.get('skills', []))} skills, min score {settings.get('minScore', 7)}, max {settings.get('maxResults', 20)} results")

    content = call_groq(api_key, prompt)
    print("\n=== Groq response ===")
    print(content[:2000])

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        # Fallback: find JSON substring
        start = content.find("{")
        end = content.rfind("}") + 1
        result = json.loads(content[start:end])

    jobs = result.get("jobs", [])
    print(f"\n✓ Found {len(jobs)} matching jobs")
    for j in jobs[:5]:
        print(f"  [{j.get('match_score', '?')}/10] {j.get('role', '?')} @ {j.get('company', '?')} — {j.get('location', '?')}")
    if len(jobs) > 5:
        print(f"  ... +{len(jobs) - 5} more")

    # TODO: sync to Google Sheets (requires service account credentials in secrets)
    # For now, output is visible in workflow logs
    return result


if __name__ == "__main__":
    try:
        run_job_matcher()
        print("\nJob matcher completed successfully")
        sys.exit(0)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)
