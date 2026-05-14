import re
from utils import _strip_html, _is_il_location, load_keywords, gha_log, progress_log


def _extract_min_years(text, he_patterns=(), max_yrs=None, he_literals=None):
    t = _strip_html(text).lower()
    found = []

    # Hebrew word-form numbers (e.g. "שנתיים" = 2 years, dual form not captured by digit patterns)
    if he_literals:
        for word, val in he_literals.items():
            if word in t and 1 <= val <= 20:
                found.append(val)

    # For X-Y ranges: use the upper bound when it's significantly above the limit.
    # "2-4 years" targets mid-level even though the minimum is 2.
    for m in re.finditer(r'(\d+)\s*[-–]\s*(\d+)\s*(?:years?|שנים|שנות)', t):
        try:
            low, high = int(m.group(1)), int(m.group(2))
            if 1 <= low < high <= 20:
                effective = high if (max_yrs is not None and high > max_yrs) else low
                found.append(effective)
        except Exception:
            pass

    single_patterns = [
        r'(\d+)\+\s*years?\s+of\s+\w+',
        r'(\d+)\+\s*years?',
        r'at\s+least\s+(\d+)\s*years?',
        r'minimum\s+(?:of\s+)?(\d+)\s*years?',
        r'(\d+)\+?\s+or\s+more\s+years?',
        r'(\d+)\s*years?\s*(?:of\s+)?(?:experience|exp)',
        r'(\d+)\s*years?\s+of\s+\w+(?:\s+\w+){0,3}\s+(?:experience|development)',
        *he_patterns,
    ]
    for p in single_patterns:
        for m in re.finditer(p, t):
            try:
                val = int(m.group(1))
                if 1 <= val <= 20:
                    found.append(val)
            except Exception:
                pass
    return min(found) if found else None


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
    he_literals         = kw.get("experience_literals_hebrew", {})
    dev_general         = kw.get("dev_role_keywords", {}).get("general",
        ["developer", "engineer", "full stack", "fullstack", "backend", "frontend", "software"])
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
        drop_reasons.setdefault(reason, []).append(
            f"{j.get('company','?')}: {j.get('role','?')[:60]}"
        )

    for j in jobs:
        role_raw = j.get("role", "")
        role     = role_raw.lower()
        company  = (j.get("company", "")).lower()
        loc      = (j.get("location", "")).lower()
        source   = j.get("source", "")

        if any(ex == company or (ex and ex in company) for ex in excluded_companies):
            _drop("excluded_company", j); continue

        matched_kw = next((kw for kw in excluded_keywords if kw and kw in role), None)
        if matched_kw:
            _drop(f"excluded_kw:{matched_kw}", j); continue

        max_yrs = settings.get("maxYears", 2.5)

        if any(kw in role for kw in seniority_title_kws):
            _drop("over_experience:seniority_title", j); continue

        desc_text_early = j.get("description", "").lower()
        if desc_text_early and any(kw in desc_text_early for kw in seniority_desc_kws):
            _drop("over_experience:seniority_in_desc", j); continue

        title_and_desc = role + " " + (j.get("description") or j.get("description_snippet") or "")
        min_yrs = _extract_min_years(title_and_desc, he_patterns, max_yrs=max_yrs, he_literals=he_literals)
        if min_yrs is not None and min_yrs >= max_yrs:
            _drop(f"over_experience:{min_yrs}yrs_required", j); continue

        sem_m = re.search(r'\bat\s+least\s+(\d+)\s*semesters?\s+(?:left|remaining)', title_and_desc, re.I)
        if sem_m and int(sem_m.group(1)) >= 2:
            _drop(f"graduation_req:{sem_m.group(1)}_semesters", j); continue

        matched_nd = next((p for p in hard_non_dev if p in role), None)
        if not matched_nd and re.search(r'\bbi\b', role):
            matched_nd = "bi"
        if matched_nd:
            _drop(f"hard_non_dev:{matched_nd}", j); continue

        matched_st = next((st for st in excluded_stacks if st and st in role), None)
        if not matched_st and re.search(r'\bnet[\s./]', role):
            matched_st = ".net"
        if matched_st:
            _drop(f"excluded_stack:{matched_st}", j); continue

        desc_lower = (j.get("description") or j.get("description_snippet") or "").lower()
        # Skills are specific tech names (React, Python, FastAPI) — unique enough that
        # mentioning them in a description strongly implies dev work. Broadening the
        # title-only check to descriptions rescues jobs with creative titles
        # ("Tech wizard wanted!") that have real dev content in the body.
        has_skill   = any(sk in role for sk in skills) or any(sk in desc_lower for sk in skills)
        # dev_kws stays title-only — words like "developer"/"engineer" appear in
        # non-dev JDs ("you'll partner with developers") and would over-broaden.
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
                        any(w in loc for w in ["worldwide", "anywhere", "global", "europe", "emea", "international"]) or
                        loc in ("", "remote")):
                    _drop("remote_not_il_eligible", j); continue
        else:
            is_remote = any(w in loc for w in ["remote", "hybrid"])
            if any(city in loc for city in hard_reject_locs):
                _drop(f"location_not_allowed:{loc[:40]}", j); continue
            loc_ok = any(al in loc for al in allowed_locations) or _is_il_location(loc)
            if not is_remote and not loc_ok:
                _drop(f"location_not_allowed:{loc[:40]}", j); continue

        passed.append(j)

    progress_log(f"::notice title=detail::passed={len(passed)}")
    # Surface top pre-filter drop reasons to the GHA UI — biggest funnel cliff.
    # Strip variable trailing details (after ':') so reasons like "excluded_kw:senior"
    # and "excluded_kw:lead" collapse into one bucket per reason class.
    if drop_reasons:
        bucketed = {}
        for reason, items in drop_reasons.items():
            key = reason.split(":", 1)[0]
            bucketed[key] = bucketed.get(key, 0) + len(items)
        top = sorted(bucketed.items(), key=lambda x: -x[1])[:5]
        summary = ",".join(f"{r}:{n}" for r, n in top)
        gha_log(f"::notice title=detail::drops={summary}")
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
