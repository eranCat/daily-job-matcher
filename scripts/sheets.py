import os, json
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils import JERUSALEM_TZ

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
    try:
        resp = sheets.values().get(
            spreadsheetId=sheet_id, range=f"{SHEET_TAB}!E2:E").execute()
    except HttpError as e:
        if e.resp.status == 400:
            return set()
        raise
    return {r[0].strip() for r in resp.get("values", []) if r and r[0].strip()}


def _get_table_end_row(sheets, sheet_id):
    """Return (sheetGid, endRowIndex) for SHEET_TAB's first table, or None."""
    meta = sheets.get(spreadsheetId=sheet_id, includeGridData=False).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == SHEET_TAB:
            gid = s["properties"]["sheetId"]
            for t in s.get("tables", []):
                return gid, t["range"]["endRowIndex"]  # 0-indexed exclusive
    return None, None


def append_rows(sheets, sheet_id, rows):
    if not rows:
        return {}
    n = len(rows)
    gid, end_row = _get_table_end_row(sheets, sheet_id)
    if gid is not None and end_row is not None:
        # Insert N rows just before the table's last row so they land inside the table.
        # insertDimension within the table range causes Sheets to extend endRowIndex by N.
        insert_at = end_row - 1  # 0-indexed; last row of the table (often empty/footer)
        sheets.batchUpdate(spreadsheetId=sheet_id, body={"requests": [{
            "insertDimension": {
                "range": {
                    "sheetId": gid, "dimension": "ROWS",
                    "startIndex": insert_at, "endIndex": insert_at + n,
                },
                "inheritFromBefore": True,
            }
        }]}).execute()
        start_row_1idx = insert_at + 1  # convert 0-indexed → 1-indexed for values API
        resp = sheets.values().update(
            spreadsheetId=sheet_id,
            range=f"{SHEET_TAB}!A{start_row_1idx}:F{start_row_1idx + n - 1}",
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()
        updated = resp.get("updatedRows", n)
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
