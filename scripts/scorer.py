import os, re, json
from urllib import request as urlreq
from utils import gha_log, progress_log

_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models/"


def _algorithmic_score(jobs, settings, keywords=None):
    if not jobs:
        return []
    kw        = keywords or {}
    min_score = settings.get("minScore", 7)
    max_r     = settings.get("maxResults", 25)
    dev_kws   = kw.get("dev_role_keywords", {})

    FULLSTACK_KW  = dev_kws.get("fullstack", ["full stack", "fullstack", "full-stack", " fs ", "fs/"])
    BACKEND_KW    = dev_kws.get("backend",   ["backend", "back end", "back-end", "server-side"])
    FRONTEND_KW   = dev_kws.get("frontend",  ["frontend", "front end", "front-end", "ui developer"])
    DEV_KW        = dev_kws.get("general",   ["developer", "engineer", "programmer"])
    TIER1         = kw.get("skill_tier1", {"react": 1.0, "typescript": 1.0, "python": 1.0, "node.js": 1.0})
    TIER2         = kw.get("skill_tier2", {"docker": 0.5, "postgresql": 0.5, "javascript": 0.5})
    JUNIOR_KW     = kw.get("junior_keywords", ["junior", "entry level", "entry-level", "intern"])
    MID_KW        = kw.get("mid_keywords",    ["mid level", "mid-level"])
    DIRECT_BOARDS = ["greenhouse", "lever", "ashby"]

    scored = []
    for job in jobs:
        title  = (job.get("role", "") or "").lower()
        desc   = (job.get("description", "") or job.get("body", "") or "").lower()
        snippet = (job.get("description_snippet", "") or "").lower()
        source = (job.get("source", "") or "").lower()
        text   = f"{title} {desc} {snippet}"
        has_desc = bool(desc.strip() or snippet.strip())
        pts, tags = 0.0, []
        if   any(k in text for k in FULLSTACK_KW): pts += 4.0; tags.append("fullstack")
        elif any(k in text for k in BACKEND_KW):   pts += 3.0; tags.append("backend")
        elif any(k in text for k in FRONTEND_KW):  pts += 2.5; tags.append("frontend")
        elif any(k in text for k in DEV_KW):       pts += 2.0; tags.append("dev")
        else:                                       pts += 1.0
        t1 = sum(v for k, v in TIER1.items() if k in text)
        t2 = sum(v for k, v in TIER2.items() if k in text)
        sp = min(4.0, t1 + t2); pts += sp
        if   sp >= 2.5: tags.append("strong-stack")
        elif sp >= 1.0: tags.append("partial-stack")
        if   any(k in text for k in JUNIOR_KW): pts += 1.5; tags.append("junior")
        elif any(k in text for k in MID_KW):    pts += 0.7; tags.append("mid")
        if any(b in source for b in DIRECT_BOARDS): pts += 0.5; tags.append("direct-board")
        # Drushim jobs sometimes have no description (detail page fetch failed).
        # They already passed card-level years filtering, so treat as borderline pass
        # rather than silently dropping. Do NOT apply to boards that always supply
        # descriptions (Greenhouse, Lever, Ashby) — missing desc there means no data.
        if not has_desc and "drushim" in source and pts < min_score and any(
            k in title for k in [*FULLSTACK_KW, *BACKEND_KW, *FRONTEND_KW, *DEV_KW]
        ):
            pts = min_score; tags.append("no-desc-fallback")
        score = max(0, min(10, round(pts)))
        if score >= min_score:
            job["match_score"] = score
            job["reason"]      = ", ".join(tags) or "dev-role"
            scored.append(job)
    scored.sort(key=lambda j: j["match_score"], reverse=True)
    return scored[:max_r]


def _build_gemini_prompt(jobs, settings):
    skills    = settings.get("skills", []) or []
    max_years = settings.get("maxYears", 2.5)
    min_score = settings.get("minScore", 7)
    locations = settings.get("locations", []) or []
    items = []
    for idx, j in enumerate(jobs):
        desc = (j.get("description") or "").strip()
        if len(desc) > 600:
            desc = desc[:600] + "…"
        items.append({
            "id": idx,
            "role": (j.get("role") or "").strip(),
            "company": (j.get("company") or "").strip(),
            "location": (j.get("location") or "").strip(),
            "source": (j.get("source") or "").strip(),
            "description": desc,
        })
    profile = {
        "target_role": "Full-stack developer, CS graduate (GPA 92), production React/TypeScript/FastAPI/Docker + AI/LLM project experience",
        "primary_stack": skills,
        "max_experience_years": max_years,
        "preferred_locations": locations,
    }
    instruction_lines = [
        "You are scoring developer job listings for a CS graduate in Israel with real production experience.",
        f"Candidate: ~{max_years} years total experience. Stack: React, TypeScript, FastAPI, Python, Docker, Tailwind CSS, PostgreSQL.",
        "Has deployed a full-stack production app and built an AI/LLM microservices project (Groq, SSE, Docker Compose).",
        "Targeting junior to mid-level full-stack, backend, and AI-adjacent developer roles.",
        "",
        "SCORING RUBRIC (use the full 0-10 range):",
        f"  9-10: Excellent — React, TypeScript, FastAPI, or Python match + junior/mid role + Israel location.",
        f"  7-8:  Good — 1-2 stack matches (JS, Node, Docker, PostgreSQL, REST, AI/LLM), no seniority red flags, no explicit years > {max_years}.",
        f"  5-6:  Partial — some tech overlap OR role is vague on seniority.",
        f"  3-4:  Weak — different stack, or description implies experienced hire without stating years.",
        f"  1-2:  Reject — requires > {max_years} years explicitly, OR senior/lead/architect title, OR"
              f" mismatched stack (PHP, .NET/C#, Ruby, SAP, COBOL, Mainframe).",
        "",
        "IMPORTANT: Score generously for React, TypeScript, Python, FastAPI, Docker, or AI/LLM mentions — these are strong matches.",
        "If seniority is vague, lean 6-7. Do NOT default to 1-2 for ambiguous descriptions.",
        "",
        'Return ONLY valid JSON: {"scores": [{"id": <int>, "score": <int>, "reason": <string max 80 chars>}, ...]}',
        "No prose, no markdown fences.",
    ]
    payload = {"candidate_profile": profile, "jobs": items}
    return "\n".join(instruction_lines) + "\n\n" + json.dumps(payload, ensure_ascii=False)


def _call_gemini(prompt, api_key, timeout=45, model=None):
    import time as _time
    from urllib.error import HTTPError as _HTTPError
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
        },
    }
    _model = model or _DEFAULT_GEMINI_MODEL
    url = _GEMINI_API_BASE + _model + ":generateContent?key=" + api_key
    req = urlreq.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(4):
        try:
            with urlreq.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            break
        except _HTTPError as e:
            if e.code == 429 and attempt < 3:
                # Respect Retry-After header if present, else exponential backoff
                retry_after = e.headers.get("Retry-After") or e.headers.get("retry-after")
                try:
                    wait = int(retry_after)
                except (TypeError, ValueError):
                    wait = 30 * (2 ** attempt)   # 30s, 60s, 120s
                print(f"  [scorer] Gemini 429 — retrying in {wait}s...", flush=True)
                _time.sleep(wait)
            else:
                raise
    data       = json.loads(raw)
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {raw[:300]}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text  = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise RuntimeError("Gemini returned empty text")
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
    min_score = settings.get("minScore", 7)
    max_r     = settings.get("maxResults", 25)
    model     = settings.get("aiModel", _DEFAULT_GEMINI_MODEL)
    BATCH = 15
    score_by_id, reason_by_id = {}, {}
    algo_fallback_indices: list[int] = []
    for b_start in range(0, len(jobs), BATCH):
        batch  = jobs[b_start: b_start + BATCH]
        prompt = _build_gemini_prompt(batch, settings)
        try:
            entries = _call_gemini(prompt, key, model=model)
        except Exception as exc:
            print(f"  [scorer] Gemini batch {b_start // BATCH} failed: {exc} — using algorithmic for those jobs")
            algo_fallback_indices.extend(range(b_start, b_start + len(batch)))
            continue
        for entry in entries:
            try:
                rel = int(entry["id"]); abs_i = b_start + rel
                if 0 <= abs_i < len(jobs):
                    score_by_id[abs_i]  = int(entry.get("score", 0))
                    reason_by_id[abs_i] = str(entry.get("reason", ""))[:120]
            except (KeyError, TypeError, ValueError):
                continue
    if not score_by_id and not algo_fallback_indices:
        print("  [scorer] No usable Gemini scores — using algorithmic fallback")
        return _algorithmic_score(jobs, settings, keywords)
    if algo_fallback_indices:
        fallback_jobs = [jobs[i] for i in algo_fallback_indices]
        for i, job in enumerate(_algorithmic_score(fallback_jobs, settings, keywords)):
            orig_idx = algo_fallback_indices[fallback_jobs.index(job)]
            score_by_id[orig_idx]  = job.get("match_score", 0)
            reason_by_id[orig_idx] = job.get("reason", "algo")
    scored = []
    rejected = []
    for idx, job in enumerate(jobs):
        if idx not in score_by_id:
            continue
        s = max(0, min(10, score_by_id[idx]))
        reason = reason_by_id.get(idx) or "llm-match"
        if s >= min_score:
            job["match_score"] = s
            job["reason"]      = reason
            scored.append(job)
        else:
            rejected.append((s, job.get("role", "?")[:50], job.get("company", "?"), reason))

    if rejected:
        rejected_sorted = sorted(rejected, reverse=True)
        top_score, top_role, top_company, top_reason = rejected_sorted[0]
        progress_log(f"::notice title=detail::rejected_top=score{top_score}:{top_company}:{top_role[:40]}")
        print(f"  [scorer] {len(rejected)} jobs below threshold (minScore={min_score}):")
        for s, role, company, reason in rejected_sorted:
            print(f"    score={s}  {company}: {role}  — {reason}")

    scored.sort(key=lambda j: j["match_score"], reverse=True)
    return scored[:max_r]
