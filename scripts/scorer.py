import os, re, json
from urllib import request as urlreq
from utils import gha_log, progress_log

_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models/"

_GEMINI_FALLBACK_CHAIN = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]

_OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Free models on OpenRouter, tried in order. All are :free-tagged so they
# don't consume credits. Larger/smarter models are listed first; if the
# daily free quota for one is exhausted (429) we fall through to the next.
_OPENROUTER_FALLBACK_CHAIN = [
    # Order is reliability-first, picked empirically. glm-4.5-air honors
    # response_format consistently; nemotron-nano is a small/cheap fallback;
    # gpt-oss-120b is smart but emits malformed JSON ~40% of the time so it
    # sits deeper in the chain. llama-3.3-70b and qwen3-next are heavily rate-
    # limited on the free tier and only kick in if everything above is down.
    "z-ai/glm-4.5-air:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "openai/gpt-oss-120b:free",
    "openai/gpt-oss-20b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
]


class GeminiUnavailableError(Exception):
    """Raised when a Gemini model is rate-limited, retired, or otherwise unusable."""


class OpenRouterUnavailableError(Exception):
    """Raised when an OpenRouter model is rate-limited, missing, or unusable."""


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
    SENIOR_KW     = ["senior", " sr.", " sr ", "lead ", "staff ", "principal ", "architect", "founding engineer"]
    DIRECT_BOARDS = ["greenhouse"]

    scored = []
    for job in jobs:
        title  = (job.get("role", "") or "").lower()
        desc   = (job.get("description", "") or job.get("body", "") or "").lower()
        snippet = (job.get("description_snippet", "") or "").lower()
        source = (job.get("source", "") or "").lower()
        text   = f"{title} {desc} {snippet}"
        has_desc = bool(desc.strip() or snippet.strip())
        pts, tags = 0.0, []
        if   any(k in title for k in FULLSTACK_KW): pts += 4.0; tags.append("fullstack")
        elif any(k in title for k in BACKEND_KW):   pts += 3.0; tags.append("backend")
        elif any(k in title for k in FRONTEND_KW):  pts += 2.5; tags.append("frontend")
        elif any(k in title for k in DEV_KW):       pts += 2.0; tags.append("dev")
        else:                                        pts += 1.0
        t1 = sum(v for k, v in TIER1.items() if k in text)
        t2 = sum(v for k, v in TIER2.items() if k in text)
        sp = min(4.0, t1 + t2); pts += sp
        if   sp >= 2.5: tags.append("strong-stack")
        elif sp >= 1.0: tags.append("partial-stack")
        if   any(k in title for k in SENIOR_KW): pts = min(pts, 2.0); tags.append("senior-title")
        elif any(k in text for k in JUNIOR_KW): pts += 1.5; tags.append("junior")
        elif any(k in text for k in MID_KW):    pts += 0.7; tags.append("mid")
        if any(b in source for b in DIRECT_BOARDS): pts += 0.5; tags.append("direct-board")
        if not has_desc and "drushim" in source and pts < min_score and any(
            k in title for k in [*FULLSTACK_KW, *BACKEND_KW, *FRONTEND_KW, *DEV_KW]
        ):
            pts = min_score; tags.append("no-desc-fallback")
        score = max(0, min(10, int(pts + 0.5)))
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
        "You are scoring software engineering job listings for a CS BSc graduate in Israel with real production experience.",
        f"Candidate: ~{max_years} years total experience. Primary stack: React, TypeScript, FastAPI, Python, Docker, Tailwind CSS, PostgreSQL.",
        "Has deployed a full-stack production app and built an AI/LLM microservices project (Groq, SSE, Docker Compose).",
        "TARGET: web full-stack, backend, frontend, or AI/LLM application engineering roles. Mainstream backend stacks",
        "(Java, Go, Kotlin, C++, Scala, Rust, Node.js, Python) are all acceptable — only PHP/.NET/C#/Ruby/SAP/COBOL/Mainframe are off-limits.",
        "",
        "ROLE TYPE is the most important factor — many job titles look engineering-adjacent but are NOT the target:",
        "  IN-SCOPE   (can score 7+): full-stack developer, backend developer, frontend developer, web developer,",
        "                              software engineer with a web/app description, AI engineer/LLM engineer when",
        "                              the work is building applications (not training/research-only models).",
        "  OUT-OF-SCOPE (cap at 6, regardless of seniority or location):",
        "    - data engineer / data pipeline / ETL / BI / Big Data / DBA / data analyst",
        "    - automation developer / RPA / test automation when the focus is QA infrastructure, not app dev",
        "    - hardware / firmware / embedded / FPGA / DSP / RF",
        "    - DevOps / SRE / platform / infra-as-code as the PRIMARY focus",
        "    - mobile-only Android/iOS roles (no web/backend scope)",
        "    - QA / test engineer / validation",
        "    - systems analyst / BI analyst / business analyst",
        "    - solutions/sales engineer, customer success, support, technical account manager",
        "  These can be technically interesting but are not what the candidate is applying for. Cap at 6.",
        "",
        "SCORING RUBRIC (use the full 0-10 range):",
        f"  9-10: Excellent — IN-SCOPE role + React/TypeScript/FastAPI/Python/Node.js primary stack match + junior/mid + Israel.",
        f"  7-8:  Good — IN-SCOPE role + junior/mid + acceptable stack (Java/Go/Kotlin/C++/etc.) + no seniority red flags + no explicit years > {max_years}.",
        f"  5-6:  Partial — OUT-OF-SCOPE role type (data/automation/DevOps/QA/hardware/mobile-only/analyst) even if junior, OR IN-SCOPE but ambiguous seniority/stack overlap.",
        f"  3-4:  Weak — OUT-OF-SCOPE + experienced hire wording, OR wrong stack family.",
        f"  1-2:  Reject — requires > {max_years} years explicitly, OR title contains Senior/Lead/Architect/Principal/Staff, OR off-limits stack (PHP/.NET/C#/Ruby/SAP/COBOL/Mainframe), OR clearly non-engineering (sales, support, project management).",
        "",
        "HARD RULES (override the rubric):",
        "  - Title contains 'Senior', 'Lead', 'Architect', 'Principal', or 'Staff' → score MUST be ≤2.",
        "  - Description requires more years than the candidate has (e.g. 'X+ years required', 'minimum Y years',",
        f"    'ניסיון של N שנים ומעלה' where N > {max_years}) → score MUST be ≤4. '{max_years}+ years' / 'שנתיים ומעלה' counts as requiring strictly more than {max_years}.",
        "  - Title is 'Android Developer/Engineer' or 'iOS Developer/Engineer' with no web/backend scope → score MUST be ≤4.",
        "  - Role is in the OUT-OF-SCOPE list above → score MUST be ≤6 even if everything else fits.",
        "",
        "If seniority is vague but the role is IN-SCOPE, lean 6-7. Do NOT default to 1-2 for ambiguous descriptions — that's what 5-6 is for.",
        "",
        'Return ONLY valid JSON: {"scores": [{"id": <int>, "score": <int>, "reason": <string max 80 chars>}, ...]}',
        "No prose, no markdown fences.",
    ]
    payload = {"candidate_profile": profile, "jobs": items}
    return "\n".join(instruction_lines) + "\n\n" + json.dumps(payload, ensure_ascii=False)


def _call_gemini(prompt, api_key, timeout=45, model=None, max_429_retries=1):
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
    attempts = max_429_retries + 1
    for attempt in range(attempts):
        try:
            with urlreq.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            break
        except _HTTPError as e:
            if e.code == 404:
                raise GeminiUnavailableError(f"{_model} not found (retired or unknown model)") from e
            if e.code == 429 and attempt < attempts - 1:
                retry_after = e.headers.get("Retry-After") or e.headers.get("retry-after")
                try:
                    wait = int(retry_after)
                except (TypeError, ValueError):
                    wait = 15 * (2 ** attempt)
                print(f"  [scorer] Gemini 429 ({_model}) — retrying in {wait}s...", flush=True)
                _time.sleep(wait)
            elif e.code == 429:
                raise GeminiUnavailableError(f"{_model} rate-limited after {attempts} attempt(s)") from e
            else:
                raise
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise GeminiUnavailableError(f"{_model} returned non-JSON envelope ({e}): {raw[:200]!r}") from e
    candidates = data.get("candidates") or []
    if not candidates:
        raise GeminiUnavailableError(f"{_model} returned no candidates: {raw[:200]!r}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text  = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise GeminiUnavailableError(f"{_model} returned empty text")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        # Truncated or malformed JSON from the model — fall through the model
        # chain rather than aborting the batch.
        raise GeminiUnavailableError(f"{_model} returned malformed JSON ({e}): {text[:200]!r}") from e
    scores = parsed.get("scores") if isinstance(parsed, dict) else parsed
    if not isinstance(scores, list):
        raise GeminiUnavailableError(f"{_model} response missing 'scores' list: {text[:200]!r}")
    return scores


# ── Scoring methods (registry) ────────────────────────────────────────────────
#
# Each scorer is a callable: (jobs, settings, keywords) -> list[scored_jobs]
#   - returns jobs annotated with `match_score` (int 0-10) and `reason` (str)
#   - returns only jobs that meet settings["minScore"]
#   - raises ScorerUnavailable when the scorer can't run (e.g. no API key,
#     all upstream models exhausted). The pipeline then tries the next scorer.

class ScorerUnavailable(Exception):
    """Raised by a scoring method when it cannot produce scores (the pipeline
    should try the next scorer in the chain)."""


def gemini_scorer(jobs, settings, keywords=None, *, api_key=None):
    if not jobs:
        return []
    key = api_key or os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise ScorerUnavailable("GEMINI_API_KEY not set")
    min_score = settings.get("minScore", 7)
    max_r     = settings.get("maxResults", 25)
    model     = settings.get("aiModel", _DEFAULT_GEMINI_MODEL)
    if model in _GEMINI_FALLBACK_CHAIN:
        start = _GEMINI_FALLBACK_CHAIN.index(model)
        chain = _GEMINI_FALLBACK_CHAIN[start:]
    else:
        chain = [model] + _GEMINI_FALLBACK_CHAIN
    active_idx = 0

    BATCH = 15
    score_by_id, reason_by_id = {}, {}
    unscored_indices = []
    for b_start in range(0, len(jobs), BATCH):
        batch  = jobs[b_start: b_start + BATCH]
        prompt = _build_gemini_prompt(batch, settings)
        entries = None
        last_exc = None
        while active_idx < len(chain):
            current_model = chain[active_idx]
            try:
                entries = _call_gemini(prompt, key, model=current_model)
                break
            except GeminiUnavailableError as exc:
                print(f"  [scorer] {current_model} unavailable ({exc}) — falling back", flush=True)
                last_exc = exc
                active_idx += 1
                continue
            except Exception as exc:
                # Network/HTTP error etc. — try the next model rather than
                # killing the whole batch on a transient blip.
                print(f"  [scorer] {current_model} errored ({type(exc).__name__}: {exc}) — falling back", flush=True)
                last_exc = exc
                active_idx += 1
                continue
        if entries is None:
            print(f"  [scorer] Gemini batch {b_start // BATCH} failed: {last_exc}")
            unscored_indices.extend(range(b_start, b_start + len(batch)))
            continue
        if active_idx > 0:
            print(f"  [scorer] batch {b_start // BATCH} scored via {chain[active_idx]}", flush=True)
        for entry in entries:
            try:
                rel = int(entry["id"]); abs_i = b_start + rel
                if 0 <= abs_i < len(jobs):
                    score_by_id[abs_i]  = int(entry.get("score", 0))
                    reason_by_id[abs_i] = str(entry.get("reason", ""))[:120]
            except (KeyError, TypeError, ValueError):
                continue

    if not score_by_id:
        raise ScorerUnavailable("Gemini produced no scores (chain exhausted)")

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

    if unscored_indices:
        leftovers = [jobs[i] for i in unscored_indices]
        for j in algorithmic_scorer(leftovers, settings, keywords):
            scored.append(j)

    _report_rejected(rejected, settings, scored)
    scored.sort(key=lambda j: j["match_score"], reverse=True)
    return scored[:max_r]


def algorithmic_scorer(jobs, settings, keywords=None):
    return _algorithmic_score(jobs, settings, keywords)


_OPENROUTER_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id":     {"type": "integer"},
                    "score":  {"type": "integer", "minimum": 0, "maximum": 10},
                    "reason": {"type": "string", "maxLength": 120},
                },
                "required": ["id", "score", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["scores"],
    "additionalProperties": False,
}


def _call_openrouter(prompt, api_key, timeout=60, model=None, max_429_retries=1):
    import time as _time
    from urllib.error import HTTPError as _HTTPError
    _model = model or _OPENROUTER_FALLBACK_CHAIN[0]
    body = {
        "model": _model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 4096,
        # Force the model into structured output. Providers that honor it (e.g.
        # OpenAI, Mistral, Google) return guaranteed-valid JSON matching the
        # schema. Providers that don't will still return content in some form
        # and the chain falls through if parsing fails.
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "job_scores",
                "strict": True,
                "schema": _OPENROUTER_RESPONSE_SCHEMA,
            },
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        # OpenRouter asks for these but they're optional; harmless to send.
        "HTTP-Referer": "https://github.com/eranCat/daily-job-matcher",
        "X-Title": "daily-job-matcher",
    }
    req = urlreq.Request(
        _OPENROUTER_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    attempts = max_429_retries + 1
    raw = None
    for attempt in range(attempts):
        try:
            with urlreq.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            break
        except _HTTPError as e:
            if e.code == 404:
                raise OpenRouterUnavailableError(f"{_model} not found") from e
            if e.code in (402, 403):
                # Quota exhausted / model not free for this account.
                raise OpenRouterUnavailableError(f"{_model} payment/quota error ({e.code})") from e
            if e.code == 429 and attempt < attempts - 1:
                retry_after = e.headers.get("Retry-After") or e.headers.get("retry-after")
                try:
                    wait = int(retry_after)
                except (TypeError, ValueError):
                    wait = 10 * (2 ** attempt)
                print(f"  [scorer] OpenRouter 429 ({_model}) — retrying in {wait}s...", flush=True)
                _time.sleep(wait)
            elif e.code == 429:
                raise OpenRouterUnavailableError(f"{_model} rate-limited after {attempts} attempt(s)") from e
            else:
                raise
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise OpenRouterUnavailableError(f"{_model} returned non-JSON envelope ({e}): {raw[:200]!r}") from e
    choices = data.get("choices") or []
    if not choices:
        # OpenRouter returns {"error": {...}} for upstream provider failures even on HTTP 200.
        err = data.get("error") or {}
        if err:
            raise OpenRouterUnavailableError(f"{_model} returned error: {str(err)[:200]}")
        raise OpenRouterUnavailableError(f"{_model} returned no choices: {raw[:200]!r}")
    msg  = choices[0].get("message") or {}
    text = (msg.get("content") or "").strip()
    if not text:
        raise OpenRouterUnavailableError(f"{_model} returned empty content")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        # Most likely a model that didn't honor response_format. Fall through
        # the model chain instead of aborting the batch.
        raise OpenRouterUnavailableError(f"{_model} returned malformed JSON ({e}): {text[:200]!r}") from e
    scores = parsed.get("scores") if isinstance(parsed, dict) else parsed
    if not isinstance(scores, list):
        raise OpenRouterUnavailableError(f"{_model} response missing 'scores' list: {text[:200]!r}")
    return scores


def openrouter_scorer(jobs, settings, keywords=None, *, api_key=None):
    """LLM scorer backed by OpenRouter's free model tier.
    Reuses the Gemini prompt (same rubric) and the same batch/fallback shape:
    each batch tries the configured model chain in order; batches that fail
    every model fall through to the algorithmic scorer as a partial fallback."""
    if not jobs:
        return []
    key = api_key or os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise ScorerUnavailable("OPENROUTER_API_KEY not set")
    min_score = settings.get("minScore", 7)
    max_r     = settings.get("maxResults", 25)
    model     = settings.get("openRouterModel") or _OPENROUTER_FALLBACK_CHAIN[0]
    if model in _OPENROUTER_FALLBACK_CHAIN:
        start = _OPENROUTER_FALLBACK_CHAIN.index(model)
        chain = _OPENROUTER_FALLBACK_CHAIN[start:]
    else:
        chain = [model] + _OPENROUTER_FALLBACK_CHAIN
    active_idx = 0

    BATCH = 15
    score_by_id, reason_by_id = {}, {}
    unscored_indices = []
    for b_start in range(0, len(jobs), BATCH):
        batch  = jobs[b_start: b_start + BATCH]
        prompt = _build_gemini_prompt(batch, settings)
        entries = None
        last_exc = None
        while active_idx < len(chain):
            current_model = chain[active_idx]
            try:
                entries = _call_openrouter(prompt, key, model=current_model)
                break
            except OpenRouterUnavailableError as exc:
                print(f"  [scorer] {current_model} unavailable ({exc}) — falling back", flush=True)
                last_exc = exc
                active_idx += 1
                continue
            except Exception as exc:
                # Network/HTTP error etc. — try the next model rather than
                # killing the whole batch on a transient blip.
                print(f"  [scorer] {current_model} errored ({type(exc).__name__}: {exc}) — falling back", flush=True)
                last_exc = exc
                active_idx += 1
                continue
        if entries is None:
            print(f"  [scorer] OpenRouter batch {b_start // BATCH} failed: {last_exc}")
            unscored_indices.extend(range(b_start, b_start + len(batch)))
            continue
        if active_idx > 0:
            print(f"  [scorer] batch {b_start // BATCH} scored via {chain[active_idx]}", flush=True)
        for entry in entries:
            try:
                rel = int(entry["id"]); abs_i = b_start + rel
                if 0 <= abs_i < len(jobs):
                    score_by_id[abs_i]  = int(entry.get("score", 0))
                    reason_by_id[abs_i] = str(entry.get("reason", ""))[:120]
            except (KeyError, TypeError, ValueError):
                continue

    if not score_by_id:
        raise ScorerUnavailable("OpenRouter produced no scores (chain exhausted)")

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

    if unscored_indices:
        leftovers = [jobs[i] for i in unscored_indices]
        for j in algorithmic_scorer(leftovers, settings, keywords):
            scored.append(j)

    _report_rejected(rejected, settings, scored)
    scored.sort(key=lambda j: j["match_score"], reverse=True)
    return scored[:max_r]


SCORERS = {
    "openrouter": openrouter_scorer,
    "gemini":     gemini_scorer,
    "algorithmic": algorithmic_scorer,
}


def _report_rejected(rejected, settings, scored):
    if not rejected:
        return
    min_score = settings.get("minScore", 7)
    rejected_sorted = sorted(rejected, reverse=True)
    top_score, top_role, top_company, _ = rejected_sorted[0]
    msg = f"::notice title=detail::rejected_top=score{top_score}:{top_company}:{top_role[:40]}"
    (gha_log if not scored else progress_log)(msg)
    print(f"  [scorer] {len(rejected)} jobs below threshold (minScore={min_score}):")
    for s, role, company, reason in rejected_sorted:
        print(f"    score={s}  {company}: {role}  — {reason}")


def score_jobs(jobs, settings, keywords=None):
    """Run the configured scorer chain. settings["scorers"] is a list of names
    from SCORERS, tried in order. Each scorer can raise ScorerUnavailable to
    pass control to the next. Defaults to ["gemini", "algorithmic"]."""
    if not jobs:
        return []
    chain = settings.get("scorers") or ["openrouter", "gemini", "algorithmic"]
    last_exc = None
    for name in chain:
        fn = SCORERS.get(name)
        if fn is None:
            print(f"  [scorer] Unknown scorer '{name}' — skipping")
            continue
        try:
            result = fn(jobs, settings, keywords)
            print(f"  [scorer] Used '{name}' scorer")
            return result
        except ScorerUnavailable as exc:
            print(f"  [scorer] '{name}' unavailable: {exc} — trying next")
            last_exc = exc
            continue
    if last_exc:
        print(f"  [scorer] All scorers unavailable; last error: {last_exc}")
    return []


def score_jobs_with_llm(jobs, settings, keywords=None, api_key=None):
    """Backward-compat entry point. Delegates to the scorer registry, but if
    api_key is explicitly passed (including ""), use the legacy gemini-first
    behavior so existing tests can force the algorithmic path."""
    if not jobs:
        return []
    if api_key is not None:
        try:
            return gemini_scorer(jobs, settings, keywords, api_key=api_key)
        except ScorerUnavailable:
            return algorithmic_scorer(jobs, settings, keywords)
    return score_jobs(jobs, settings, keywords)
