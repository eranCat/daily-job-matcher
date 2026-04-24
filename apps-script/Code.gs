/**
 * Daily Job Matcher — Google Apps Script webhook
 *
 * Handles POST requests from the GitHub Actions workflow:
 *   - action: "ping"    → health check, no data written
 *   - action: "append"  → append jobs[] as rows to the "Saved Jobs" sheet
 *
 * SETUP:
 * 1. Open your Google Sheet (the one tracked in your job search)
 * 2. Extensions → Apps Script
 * 3. Paste this entire file into Code.gs (replace the default content)
 * 4. Update SHEET_NAME below if your tab is named differently
 * 5. Deploy → New deployment → Web app
 *    - Description: "Job Matcher Webhook"
 *    - Execute as: Me
 *    - Who has access: Anyone
 * 6. Copy the Web App URL
 * 7. Add it to the GitHub repo as a secret named SHEETS_WEBHOOK_URL
 *
 * Sheet column layout: DATE | ROLE | COMPANY | LOCATION | LINK | STATUS
 */

const SHEET_NAME = 'Saved Jobs';
const TIMEZONE = 'Asia/Jerusalem';

/**
 * Entry point for POST requests.
 */
function doPost(e) {
  try {
    const payload = JSON.parse(e.postData.contents);
    const action = payload.action || 'unknown';

    switch (action) {
      case 'ping':
        return handlePing(payload);
      case 'append':
        return handleAppend(payload);
      default:
        return jsonResponse({ ok: false, error: `Unknown action: ${action}` }, 400);
    }
  } catch (err) {
    return jsonResponse({ ok: false, error: err.toString() }, 500);
  }
}

/**
 * Also handle GET for simple browser-based connectivity checks.
 */
function doGet(e) {
  return jsonResponse({
    ok: true,
    service: 'daily-job-matcher webhook',
    message: 'POST requests only — use action: ping | append'
  });
}

/**
 * Ping: verifies the sheet is accessible.
 */
function handlePing(payload) {
  const sheet = getSheet();
  if (!sheet) {
    return jsonResponse({
      ok: false,
      error: `Sheet tab "${SHEET_NAME}" not found. Update SHEET_NAME in the Apps Script.`
    }, 404);
  }

  return jsonResponse({
    ok: true,
    action: 'ping',
    sheet_name: sheet.getName(),
    sheet_id: sheet.getParent().getId(),
    row_count: sheet.getLastRow(),
    column_count: sheet.getLastColumn(),
    timestamp: new Date().toISOString()
  });
}

/**
 * Append: adds one row per job to the sheet.
 * Expected payload: { action: "append", jobs: [{role, company, location, link, match_score}, ...], test: bool }
 */
function handleAppend(payload) {
  const sheet = getSheet();
  if (!sheet) {
    return jsonResponse({ ok: false, error: `Sheet "${SHEET_NAME}" not found` }, 404);
  }

  const jobs = payload.jobs || [];
  if (!jobs.length) {
    return jsonResponse({ ok: false, error: 'No jobs provided' }, 400);
  }

  const isTest = !!payload.test;
  const today = Utilities.formatDate(new Date(), TIMEZONE, 'yyyy-MM-dd');
  const existingLinks = getExistingLinks(sheet);

  const rowsToAppend = [];
  const skipped = [];

  jobs.forEach(job => {
    const link = (job.link || '').trim();

    // Deduplicate by link (unless this is a test row)
    if (!isTest && link && existingLinks.has(link)) {
      skipped.push({ role: job.role, reason: 'duplicate link' });
      return;
    }

    // Column layout: DATE | ROLE | COMPANY | LOCATION | LINK | STATUS
    const statusCell = isTest
      ? `TEST (score: ${job.match_score ?? '?'})`
      : `NEW (score: ${job.match_score ?? '?'})`;

    rowsToAppend.push([
      today,
      job.role || '',
      job.company || '',
      job.location || '',
      link,
      statusCell
    ]);
  });

  if (rowsToAppend.length > 0) {
    const startRow = sheet.getLastRow() + 1;
    sheet.getRange(startRow, 1, rowsToAppend.length, 6).setValues(rowsToAppend);
  }

  return jsonResponse({
    ok: true,
    action: 'append',
    appended: rowsToAppend.length,
    skipped: skipped.length,
    skipped_details: skipped,
    test_mode: isTest,
    timestamp: new Date().toISOString()
  });
}

/**
 * Get the target sheet by name.
 */
function getSheet() {
  return SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
}

/**
 * Build a set of existing links for deduplication.
 * LINK column is column 5 (1-indexed).
 */
function getExistingLinks(sheet) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return new Set();

  const linkColumn = 5;
  const range = sheet.getRange(2, linkColumn, lastRow - 1, 1);
  const values = range.getValues();
  const links = new Set();
  values.forEach(row => {
    const link = (row[0] || '').toString().trim();
    if (link) links.add(link);
  });
  return links;
}

/**
 * Helper: return a JSON response.
 */
function jsonResponse(data, status) {
  const output = ContentService.createTextOutput(JSON.stringify(data));
  output.setMimeType(ContentService.MimeType.JSON);
  return output;
}
