"""Print candidates + descriptions without scoring or writing to sheet."""
import os
from dotenv import load_dotenv
load_dotenv()

from utils import _load_il_hints, load_settings, load_keywords
from fetchers import fetch_all_jobs
from filters import pre_filter
from sheets import get_sheets_client, require_sheet_id, get_existing_links

_load_il_hints()
settings  = load_settings()
keywords  = load_keywords()
raw       = fetch_all_jobs(settings)
shortlist = pre_filter(raw, settings, keywords)

sheets, _ = get_sheets_client()
existing_links, existing_cr = get_existing_links(sheets, require_sheet_id())
candidates = [
    j for j in shortlist
    if (j.get("link") or "").strip() not in existing_links
    and (j.get("company", "").strip().lower(), j.get("role", "").strip().lower())
        not in existing_cr
]

print(f"\n{'='*70}")
print(f"  {len(candidates)} CANDIDATES (not yet in sheet)")
print(f"{'='*70}")

for i, j in enumerate(candidates, 1):
    desc = (j.get("description") or "").strip()
    snippet = (j.get("description_snippet") or "").strip()
    display = desc[:1200] if desc else snippet[:400]
    print(f"\n[{i}] {j.get('role','')} @ {j.get('company','')}")
    print(f"     Source: {j.get('source','')} | Location: {j.get('location','')}")
    print(f"     Link: {j.get('link','')}")
    if display:
        print(f"     --- Description ---")
        for line in display.splitlines():
            line = line.strip()
            if line:
                print(f"     {line}")
    else:
        print(f"     [no description available]")
    print()
