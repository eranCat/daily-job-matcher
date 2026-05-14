import os, json
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils import JERUSALEM_TZ, normalize_url

SHEET_TAB     = "Saved Jobs"
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_sheets_client():
    sa_json = os.getenv("GOOGLE_SA_KEY")
    if not sa_json:
        key_path = os.getenv("GOOGLE_SA_KEY_PATH")
        if not key_path:
            raise ValueError("GOOGLE_SA_KEY or GOOGLE_SA_KEY_PATH not set")
        sa_json = Path(key_path).read_text(encoding="utf-8")
    creds   = service_account.Credentials.from_service_account_info(
        json.loads(sa_json), scopes=SHEETS_SCOPES)
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return service.spreadsheets(), json.loads(sa_json).get("client_email", "?")


def require_sheet_id():
    sid = os.getenv("GOOGLE_SHEETS_ID")
    if not sid:
        raise ValueError("GOOGLE_SHEETS_ID not set")
    return sid


def get_existing_links(sheets, sheet_id):
    """Return (links_set, company_role_set) from the sheet.

    Reads B:E in one call so we can dedup both by URL and by (company, role)
    — the same job reposted on Drushim gets a new URL but identical title/company.
    """
    try:
        resp = sheets.values().get(
            spreadsheetId=sheet_id, range=f"{SHEET_TAB}!B2:E").execute()
    except HttpError as e:
        if e.resp.status == 400:
            return set(), set()
        raise
    links, company_roles = set(), set()
    for r in resp.get("values", []):
        # columns order: B=role, C=company, D=location, E=link
        if len(r) >= 4 and r[3].strip():
            links.add(normalize_url(r[3].strip()))
        if len(r) >= 2:
            role_    = (r[0] or "").strip().lower()
            company_ = (r[1] or "").strip().lower()
            if role_ and company_:
                company_roles.add((company_, role_))
    return links, company_roles


def _find_last_data_row(sheets, sheet_id):
    """Return 1-indexed row number of the last row with non-whitespace data in A:F.

    Used to truly append below all user data — including rows the user added
    manually outside the structured-table boundary. Returns 1 (header row only)
    if the sheet is empty of data.

    Note: scans ALL rows and ignores whitespace-only cells (e.g. a stray ' ' in
    a column), otherwise a single dirty cell creates a gap on the next append.
    """
    resp = sheets.values().get(
        spreadsheetId=sheet_id, range=f"{SHEET_TAB}!A:F").execute()
    last = 1
    for i, r in enumerate(resp.get("values", []), start=1):
        if any((c or "").strip() for c in r):
            last = i
    return last


def _get_table_meta(sheets, sheet_id):
    """Return (sheetGid, tableId, table_range_dict) for SHEET_TAB's first table, or (None, None, None)."""
    meta = sheets.get(spreadsheetId=sheet_id, includeGridData=False).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == SHEET_TAB:
            gid = s["properties"]["sheetId"]
            for t in s.get("tables", []):
                return gid, t.get("tableId"), t["range"]
    return None, None, None


def append_rows(sheets, sheet_id, rows):
    if not rows:
        return {}
    n = len(rows)
    gid, table_id, table_range = _get_table_meta(sheets, sheet_id)
    last_data_row = _find_last_data_row(sheets, sheet_id)  # 1-indexed

    if gid is not None and table_range is not None:
        # Insert below the last row that actually has data — covers both rows
        # inside the table AND rows the user added manually below it.
        insert_at = last_data_row  # 0-indexed start for insertDimension == 1-indexed row right after last data
        sheets.batchUpdate(spreadsheetId=sheet_id, body={"requests": [{
            "insertDimension": {
                "range": {
                    "sheetId": gid, "dimension": "ROWS",
                    "startIndex": insert_at, "endIndex": insert_at + n,
                },
                "inheritFromBefore": True,
            }
        }]}).execute()
        start_row_1idx = insert_at + 1
        resp = sheets.values().update(
            spreadsheetId=sheet_id,
            range=f"{SHEET_TAB}!A{start_row_1idx}:F{start_row_1idx + n - 1}",
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()

        # Extend the table's endRowIndex so the new rows live inside the table.
        # updateTable is a newer API method — fall back gracefully if unavailable.
        new_end = max(table_range["endRowIndex"], insert_at + n)
        if table_id and new_end > table_range["endRowIndex"]:
            try:
                sheets.batchUpdate(spreadsheetId=sheet_id, body={"requests": [{
                    "updateTable": {
                        "table": {
                            "tableId": table_id,
                            "range": {**table_range, "endRowIndex": new_end},
                        },
                        "fields": "range",
                    }
                }]}).execute()
            except HttpError as e:
                print(f"  [sheets] could not extend table range: HTTP {e.resp.status}")

        updated = resp.get("updatedRows", len(rows))
        return {"updates": {"updatedRows": updated}}
    # Fallback for sheets without a structured table
    return sheets.values().append(
        spreadsheetId=sheet_id, range=f"{SHEET_TAB}!A:F",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": rows}).execute()


def job_to_row(job, today, is_test=False):
    return [
        today,
        (job.get("role") or job.get("title") or "").strip(),
        (job.get("company") or job.get("employer") or "").strip(),
        (job.get("location") or job.get("region") or "Remote").strip(),
        (job.get("link") or job.get("url") or "").strip(),
        "TEST" if is_test else "Saved",
    ]


def get_sheet_gid(sheets, sheet_id, tab_name):
    meta = sheets.get(spreadsheetId=sheet_id, includeGridData=False).execute()
    for s in meta.get("sheets", []):
        p = s["properties"]
        if p["title"] == tab_name:
            return p["sheetId"]
    raise RuntimeError(f"Tab {tab_name!r} not found")


def parse_row_index(updated_range):
    cell = updated_range.split("!")[-1].split(":")[0]
    return int("".join(c for c in cell if c.isdigit())) - 1


def delete_row(sheets, sheet_id, gid, row_idx):
    sheets.batchUpdate(spreadsheetId=sheet_id, body={"requests": [{
        "deleteDimension": {"range": {
            "sheetId": gid, "dimension": "ROWS",
            "startIndex": row_idx, "endIndex": row_idx + 1,
        }}
    }]}).execute()
