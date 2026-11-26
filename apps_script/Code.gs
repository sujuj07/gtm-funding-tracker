/**
 * Startup Intelligence Tracker webhook.
 * Create tabs named "Funding Intelligence" and "Crypto Launch Radar" or edit the constants below.
 * Set the same new secret in this file and the GitHub WEBHOOK_TOKEN secret.
 */
const SECRET_TOKEN = 'REPLACE_WITH_A_NEW_LONG_RANDOM_SECRET';
// Bump when this file changes. Returned by the health GET so you can confirm which
// version the web app is actually SERVING -- saving the editor does not change that,
// because a deployment is pinned to a numbered version. Mismatch here is the first
// thing to check when the pipeline behaves like older code.
const SCRIPT_VERSION = 'v16';
const FUNDING_SHEET_NAME = 'Funding Intelligence';
const CRYPTO_SHEET_NAME = 'Crypto Launch Radar';
const REGISTRY_SHEET_NAME = '__ProcessedSources';

// Adds a simple in-sheet action so styling can be applied without constructing a URL.
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Tracker Tools')
    .addItem('Remove Blank Rows (Funding)', 'cleanupBlankFundingRows')
    .addSeparator()
    .addItem('Format Crypto Launch Radar', 'formatCryptoLaunchRadar')
    .addItem('Format Crypto Review Queue', 'formatCryptoReviewQueue')
    .addToUi();
}

/**
 * Deletes rows that have neither a company name nor a source URL.
 *
 * These are the residue of the insert-then-fill bug above: insertRowsBefore
 * committed the rows, the fill never completed. They are not merely cosmetic --
 * each one inflates lastRow, so every subsequent run reads more data and is more
 * likely to time out, creating more of them.
 *
 * Deletes in contiguous descending runs so earlier deletions cannot shift the
 * row numbers of rows still queued for deletion.
 */
function cleanupBlankFundingRows() {
  const stream = 'funding';
  const config = STREAMS[stream];
  const sheet = getSheetForStream(stream);
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return 0;
  const count = lastRow - 1;

  const companies = sheet.getRange(2, config.companyCol, count, 1).getValues();
  const sources = sheet.getRange(2, config.sourceCol, count, 1).getValues();

  // Descending, so index arithmetic during deletion stays valid.
  const blanks = [];
  for (let i = count - 1; i >= 0; i--) {
    const company = String(companies[i][0] || '').trim();
    const source = String(sources[i][0] || '').trim();
    if (!company && !source) blanks.push(i + 2);
  }

  let removed = 0;
  let i = 0;
  while (i < blanks.length) {
    const start = blanks[i];
    let run = 1;
    while (i + run < blanks.length && blanks[i + run] === start - run) run++;
    sheet.deleteRows(start - run + 1, run);
    removed += run;
    i += run;
  }
  SpreadsheetApp.flush();
  try {
    SpreadsheetApp.getActiveSpreadsheet()
      .toast('Removed ' + removed + ' blank row(s).', 'Tracker Tools', 6);
  } catch (ignored) {
    // Running headless (no UI bound); the return value is the result.
  }
  return removed;
}

function formatCryptoLaunchRadar() {
  const sheet = getSheetForStream('crypto');
  formatTrackerSheet(sheet, 'crypto');
  SpreadsheetApp.getActiveSpreadsheet().toast('Crypto Launch Radar formatting applied.', 'Tracker Tools', 5);
}

function formatCryptoReviewQueue() {
  const sheet = getCryptoReviewSheet();
  formatReviewQueueSheet(sheet);
  SpreadsheetApp.getActiveSpreadsheet().toast('Crypto Review Queue formatting applied.', 'Tracker Tools', 5);
}

const CRYPTO_REVIEW_SHEET_NAME = 'Crypto Review Queue';
const REVIEW_HEADERS = [
  'First Seen UTC', 'Project', 'Signal Type', 'Verified Launch Date', 'Website',
  'What It Builds', 'Niche / Use Case', 'Chain / Ecosystem', 'Founder Names Mentioned',
  'Discovery Source URL', 'Founder Evidence URL', 'Prior Web3 Work Evidence URL',
  'Other Evidence URLs', 'Review Status', 'Missing Evidence / Gate Reason', 'Project Category',
  'Evidence Confidence', 'Last Checked UTC', 'Source Type', 'Source Title', 'Source Publisher',
  'Review Dedupe Key', 'Manual Decision', 'Reviewer Notes', 'Source Published Date', 'Source Summary Snippet'
];

function getCryptoReviewSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(CRYPTO_REVIEW_SHEET_NAME);
  if (!sheet) sheet = ss.insertSheet(CRYPTO_REVIEW_SHEET_NAME);
  if (sheet.getMaxColumns() < REVIEW_HEADERS.length) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), REVIEW_HEADERS.length - sheet.getMaxColumns());
  }
  sheet.getRange(1, 1, 1, REVIEW_HEADERS.length).setValues([REVIEW_HEADERS]);
  return sheet;
}

function formatReviewQueueSheet(sheet, rowsJustInserted) {
  const width = REVIEW_HEADERS.length;
  const lastRow = Math.max(sheet.getLastRow(), 1);
  sheet.getRange(1, 1, 1, width)
    .setBackground('#202124').setFontColor('#FFFFFF').setFontFamily('Arial')
    .setFontSize(10).setFontWeight('bold').setWrap(true)
    .setVerticalAlignment('middle').setHorizontalAlignment('center');
  sheet.setRowHeight(1, 38);
  sheet.setFrozenRows(1);
  sheet.setFrozenColumns(2);
  const widths = {
    1: 150, 2: 210, 3: 155, 4: 135, 5: 230, 6: 320, 7: 260, 8: 200,
    9: 230, 10: 300, 11: 280, 12: 280, 13: 320, 14: 260, 15: 380,
    16: 190, 17: 150, 18: 165, 19: 180, 20: 330, 21: 180, 22: 180, 23: 180, 24: 300, 25: 155, 26: 380
  };
  Object.keys(widths).forEach(function (col) { sheet.setColumnWidth(Number(col), widths[col]); });
  if (lastRow > 1) {
    const rowCount = (rowsJustInserted === undefined) ? lastRow - 1 : Math.min(Math.max(0, rowsJustInserted), lastRow - 1);
    if (rowCount > 0) {
      const body = sheet.getRange(2, 1, rowCount, width);
      body.setFontFamily('Arial').setFontSize(10).setFontColor('#202124').setVerticalAlignment('middle').setWrap(true);
      const backgrounds = [];
      const status = sheet.getRange(2, 14, rowCount, 1).getValues().flat();
      for (let i = 0; i < rowCount; i++) {
        const row = new Array(width).fill(i % 2 === 0 ? '#FFFFFF' : '#F3F4F6');
        const value = String(status[i] || '').toLowerCase();
        if (value.indexOf('qualified') >= 0 || value.indexOf('promoted') >= 0) row[13] = '#D9EAD3';
        else if (value.indexOf('retry') >= 0) row[13] = '#FCE5CD';
        else row[13] = '#FFF2CC';
        backgrounds.push(row);
      }
      body.setBackgrounds(backgrounds);
      sheet.setRowHeights(2, rowCount, 48);
      [5, 10, 11, 12, 13].forEach(function (col) {
        sheet.getRange(2, col, rowCount, 1).setFontColor('#1155CC').setFontLine('underline');
      });
    }
  }
  sheet.hideColumns(22);
  const currentFilter = sheet.getFilter();
  if (!currentFilter) sheet.getRange(1, 1, Math.max(2, lastRow), width).createFilter();
  else if (currentFilter.getRange().getLastRow() < lastRow) {
    currentFilter.remove();
    sheet.getRange(1, 1, Math.max(2, lastRow), width).createFilter();
  }
}

function upsertCryptoReviewRows(rows) {
  if (!Array.isArray(rows) || !rows.length) return { appended: 0, updated: 0 };
  const sheet = getCryptoReviewSheet();
  const width = REVIEW_HEADERS.length;
  const keyCol = 22;
  const lastRow = sheet.getLastRow();
  const rowByKey = {};
  if (lastRow >= 2) {
    sheet.getRange(2, keyCol, lastRow - 1, 1).getValues().flat().forEach(function (value, offset) {
      if (value) rowByKey[String(value)] = offset + 2;
    });
  }
  const updates = [];
  const additions = [];
  const seen = {};
  rows.forEach(function (incomingValue) {
    if (!Array.isArray(incomingValue) || incomingValue.length < keyCol) return;
    const incoming = incomingValue.slice(0, width);
    while (incoming.length < width) incoming.push('');
    const key = String(incoming[keyCol - 1] || '').trim();
    const project = String(incoming[1] || '').trim();
    if (!key || !project || seen[key]) return;
    seen[key] = true;
    const rowNum = rowByKey[key];
    if (!rowNum) {
      additions.push(incoming);
      return;
    }
    const existing = sheet.getRange(rowNum, 1, 1, width).getValues()[0];
    // Preserve First Seen, the machine dedupe key, and human review fields.
    for (let i = 1; i < width; i++) {
      if (i === 21 || i === 22 || i === 23) continue;
      if (i === 12) continue; // merge evidence URLs below
      if (incoming[i] !== '' && incoming[i] !== null && incoming[i] !== undefined) existing[i] = incoming[i];
    }
    const evidence = String(existing[12] || '').split(/\n/).map(function (x) { return x.trim(); }).filter(Boolean);
    String(incoming[12] || '').split(/\n/).map(function (x) { return x.trim(); }).filter(Boolean).forEach(function (url) {
      if (evidence.indexOf(url) < 0) evidence.push(url);
    });
    existing[12] = evidence.join('\n');
    existing[21] = key;
    updates.push({ row: rowNum, values: existing });
  });
  updates.forEach(function (item) { sheet.getRange(item.row, 1, 1, width).setValues([item.values]); });
  if (additions.length) {
    sheet.insertRowsBefore(2, additions.length);
    sheet.getRange(2, 1, additions.length, width).setValues(additions);
  }
  formatReviewQueueSheet(sheet, additions.length);
  return { appended: additions.length, updated: updates.length };
}


const STREAMS = {
  funding: { sheetName: FUNDING_SHEET_NAME, companyCol: 2, sourceCol: 16, keyCol: 19, additionalSourcesCol: 31, lastCheckedCol: 29, eventTypeCol: 26, maxCol: 31 },
  crypto: { sheetName: CRYPTO_SHEET_NAME, companyCol: 2, sourceCol: 13, keyCol: 18, additionalSourcesCol: 20, lastCheckedCol: 17, maxCol: 28 },
};

function normalizeCompany(name) {
  return String(name || '').toLowerCase()
    .replace(/\b(incorporated|inc|llc|ltd|limited|corp|corporation|co|company|labs|technologies|technology)\b/g, '')
    .replace(/[^a-z0-9]/g, '').trim();
}

function getStream(e, body) {
  const requested = (body && body.stream) || (e && e.parameter && e.parameter.stream) || 'funding';
  return STREAMS[requested] ? requested : null;
}

function getSheetForStream(stream) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const config = STREAMS[stream];
  let sheet = ss.getSheetByName(config.sheetName);
  // Backward compatibility: if the funding tab has not yet been renamed, use the first tab.
  if (!sheet && stream === 'funding') sheet = ss.getSheets()[0];
  if (!sheet) throw new Error('Missing sheet tab: ' + config.sheetName + '. Create it or update the script constant.');
  ensureHeaders(sheet, stream);
  return sheet;
}

function ensureHeaders(sheet, stream) {
  const headers = stream === 'funding' ? [
    'Date', 'Company', 'Stage', 'Amount USD', 'Sector', 'HQ', 'One-line Summary', 'Customer Type',
    'GTM Fit Score', 'GTM Fit Reason', 'Hiring Signal', 'Website', 'Careers Page', 'Founders',
    'Lead Investor', 'Source URL', 'Status', 'Notes', 'Dedupe Key', 'Broad Offerings', 'Niche Presence',
    'Product Type', 'Target Customers', 'Other Investors', 'Valuation USD', 'Event Type',
    'Source Publisher', 'Evidence Confidence', 'Last Checked UTC', 'Source Published Date', 'Additional Evidence URLs'
  ] : [
    'First Seen / Announced', 'Project', 'Signal', 'Launch Date', 'X Account Created',
    '@X Handle', 'X Profile', 'Website', 'What It Builds', 'Niche / Use Case', 'Chain / Ecosystem',
    'Founders / Team', 'Launch Source', 'Launch Post', 'Qualification Status', 'Evidence Confidence',
    'Last Checked (UTC)', 'Dedupe Key', 'X Account Age', 'Other Evidence URLs',
    'Public Founder Verified', 'Prior Web3 Build Verified', 'Previous Web3 Projects / Roles',
    'Founder Evidence', 'Track Record Evidence', 'Project Category', 'Why It Passed', 'Social / Docs Links Found on Website'
  ];
  if (sheet.getMaxColumns() < headers.length) sheet.insertColumnsAfter(sheet.getMaxColumns(), headers.length - sheet.getMaxColumns());

  // Preserve custom funding-sheet headers. For the crypto sheet, use concise, consistent labels
  // while retaining the same column positions expected by the Python pipeline.
  if (stream === 'crypto') {
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
  } else {
    const existing = sheet.getRange(1, 1, 1, headers.length).getValues()[0];
    let changed = false;
    for (let i = 0; i < headers.length; i++) {
      if (!existing[i]) { existing[i] = headers[i]; changed = true; }
    }
    if (changed) sheet.getRange(1, 1, 1, headers.length).setValues([existing]);
  }
}

/**
 * Gives Crypto Launch Radar the same clean, filterable treatment as Funding Intelligence.
 * Called on GET/POST so the existing blank tab gets styled and newly inserted rows stay readable.
 */
function formatTrackerSheet(sheet, stream, rowsJustInserted) {
  if (stream !== 'crypto') return; // preserve the user's established funding-sheet format

  const width = STREAMS.crypto.maxCol;
  const lastRow = Math.max(sheet.getLastRow(), 1);
  const header = sheet.getRange(1, 1, 1, width);

  // Match Funding Intelligence: dark header, white bold text, filter controls, frozen headings.
  header
    .setBackground('#202124')
    .setFontColor('#FFFFFF')
    .setFontFamily('Arial')
    .setFontSize(10)
    .setFontWeight('bold')
    .setWrap(true)
    .setVerticalAlignment('middle')
    .setHorizontalAlignment('center');
  sheet.setRowHeight(1, 36);
  sheet.setFrozenRows(1);
  sheet.setFrozenColumns(2);

  // Compact the key discovery columns; reserve wide cells for actual research context.
  const widths = {
    1: 145, 2: 210, 3: 155, 4: 115, 5: 145, 6: 130, 7: 205, 8: 205,
    9: 320, 10: 265, 11: 205, 12: 245, 13: 300, 14: 260, 15: 300, 16: 135,
    17: 165, 18: 220, 19: 165, 20: 300, 21: 155, 22: 175, 23: 300, 24: 250,
    25: 250, 26: 185, 27: 310, 28: 310
  };
  Object.keys(widths).forEach(function (col) { sheet.setColumnWidth(Number(col), widths[col]); });

  if (lastRow > 1) {
    // Full format on an explicit GET health/format call; on POST, style only newly inserted rows.
    const rowCount = (rowsJustInserted === undefined) ? (lastRow - 1) : Math.min(Math.max(0, rowsJustInserted), lastRow - 1);
    if (rowCount > 0) {
    const body = sheet.getRange(2, 1, rowCount, width);
    body.setFontFamily('Arial').setFontSize(10).setFontColor('#202124').setVerticalAlignment('middle');
    body.setWrap(false);

    // Alternating soft fills; confidence is visibly highlighted without scoring projects.
    const backgrounds = [];
    const confidenceValues = sheet.getRange(2, 16, rowCount, 1).getValues().flat();
    for (let i = 0; i < rowCount; i++) {
      const fill = (i % 2 === 0) ? '#FFFFFF' : '#F3F4F6';
      const rowBackgrounds = new Array(width).fill(fill);
      const confidence = String(confidenceValues[i] || '').toLowerCase();
      if (confidence.indexOf('high') >= 0) rowBackgrounds[15] = '#D9EAD3';
      else if (confidence.indexOf('medium') >= 0) rowBackgrounds[15] = '#FFF2CC';
      else if (confidence.indexOf('low') >= 0) rowBackgrounds[15] = '#F4CCCC';
      backgrounds.push(rowBackgrounds);
    }
    body.setBackgrounds(backgrounds);
    sheet.setRowHeights(2, rowCount, 42);

    // Keep the high-value descriptive fields readable; leave dates and IDs compact.
    [9, 10, 11, 12, 13, 15, 20, 23, 24, 25, 27, 28].forEach(function (col) {
      sheet.getRange(2, col, rowCount, 1).setWrap(true);
    });
    [1, 3, 4, 5, 15, 16, 17, 21, 22, 26].forEach(function (col) {
      sheet.getRange(2, col, rowCount, 1).setHorizontalAlignment('center');
    });

    // Blue underlined treatment for source/profile/site URL columns.
    [7, 8, 13, 14, 20, 24, 25, 28].forEach(function (col) {
      sheet.getRange(2, col, rowCount, 1).setFontColor('#1155CC').setFontLine('underline');
    });
    }
  }

  // Hide the machine-only key. Founder track-record and source evidence remain visible/auditable.
  sheet.hideColumns(18);

  // Ensure the filter covers all populated rows; keep it if it already covers the table.
  const filterLastRow = Math.max(2, lastRow);
  const currentFilter = sheet.getFilter();
  if (!currentFilter) {
    sheet.getRange(1, 1, filterLastRow, width).createFilter();
  } else if (currentFilter.getRange().getLastRow() < filterLastRow) {
    currentFilter.remove();
    sheet.getRange(1, 1, filterLastRow, width).createFilter();
  }
}


function getRegistrySheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(REGISTRY_SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(REGISTRY_SHEET_NAME);
    sheet.getRange(1, 1, 1, 3).setValues([['Stream', 'Source URL', 'Processed At UTC']]);
    sheet.hideSheet();
  }
  return sheet;
}

function getProcessedSourceSet(stream) {
  const sheet = getRegistrySheet();
  const lastRow = sheet.getLastRow();
  const result = {};
  if (lastRow < 2) return result;
  const values = sheet.getRange(2, 1, lastRow - 1, 2).getValues();
  values.forEach(function (row) {
    if (row[0] === stream && row[1]) result[String(row[1])] = true;
  });
  return result;
}

function recordProcessedSources(stream, urls) {
  if (!Array.isArray(urls) || !urls.length) return;
  const sheet = getRegistrySheet();
  const existing = getProcessedSourceSet(stream);
  const now = new Date().toISOString();
  const additions = [];
  urls.forEach(function (url) {
    const value = String(url || '').trim();
    if (value && !existing[value]) {
      existing[value] = true;
      additions.push([stream, value, now]);
    }
  });
  if (additions.length) sheet.getRange(sheet.getLastRow() + 1, 1, additions.length, 3).setValues(additions);
}

function getExistingSources(stream, sheet) {
  const config = STREAMS[stream];
  const set = getProcessedSourceSet(stream);
  const lastRow = sheet.getLastRow();
  if (lastRow >= 2) {
    sheet.getRange(2, config.sourceCol, lastRow - 1, 1).getValues().flat().forEach(function (value) {
      if (value) set[String(value)] = true;
    });
  }
  return Object.keys(set);
}

function getExistingKeys(stream, sheet) {
  const config = STREAMS[stream];
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return [];
  return sheet.getRange(2, config.keyCol, lastRow - 1, 1).getValues().flat().filter(Boolean).map(String);
}

function getKeyRowMap(stream, sheet) {
  const config = STREAMS[stream];
  const lastRow = sheet.getLastRow();
  const map = {};
  if (lastRow < 2) return map;
  sheet.getRange(2, config.keyCol, lastRow - 1, 1).getValues().flat().forEach(function (value, offset) {
    if (value) map[String(value)] = offset + 2;
  });
  return map;
}

function dateKey(value) {
  if (value instanceof Date) return Utilities.formatDate(value, 'UTC', 'yyyy-MM-dd');
  return String(value || '').trim().slice(0, 10);
}

// Month granularity, deliberately. One raise gets reported across several days by
// several outlets; keying on the exact date, stage and amount made each of those a
// separate "event", which is why one company could occupy six rows. Trade-off: a
// company that genuinely raises twice inside one calendar month merges into one row.
function monthKey(value) {
  return dateKey(value).slice(0, 7);
}

function fundingNaturalKey(company, eventType, dateValue) {
  const norm = normalizeCompany(company);
  if (!norm) return '';
  const type = String(eventType || 'Funding round').toLowerCase().trim();
  return [norm, type, monthKey(dateValue)].join('|');
}

function naturalEventKey(stream, row) {
  const config = STREAMS[stream];
  const company = normalizeCompany(row[config.companyCol - 1]);
  if (!company) return '';
  if (stream === 'funding') {
    return fundingNaturalKey(row[config.companyCol - 1], row[config.eventTypeCol - 1], row[0]);
  }
  const date = dateKey(row[3] || row[0]);
  const signal = String(row[2] || '').toLowerCase().trim();
  return [company, date, signal].join('|');
}

// Reads ONLY the columns the key needs. The previous version read the whole sheet at
// full width on every POST -- at ~1000 rows x 31 columns that is ~31,000 cells per
// request, which is what exhausted the Apps Script execution budget and left the
// half-written rows behind. Three single-column reads are roughly a tenth of the data.
function getNaturalEventRowMap(stream, sheet) {
  const config = STREAMS[stream];
  const lastRow = sheet.getLastRow();
  const map = {};
  if (lastRow < 2) return map;
  const count = lastRow - 1;

  if (stream === 'funding') {
    const dates = sheet.getRange(2, 1, count, 1).getValues();
    const companies = sheet.getRange(2, config.companyCol, count, 1).getValues();
    const types = sheet.getRange(2, config.eventTypeCol, count, 1).getValues();
    for (let i = 0; i < count; i++) {
      const key = fundingNaturalKey(companies[i][0], types[i][0], dates[i][0]);
      if (key && !map[key]) map[key] = i + 2;
    }
    return map;
  }

  const width = config.maxCol || Math.max(config.keyCol, config.sourceCol, config.additionalSourcesCol);
  sheet.getRange(2, 1, count, width).getValues().forEach(function (row, offset) {
    const natural = naturalEventKey(stream, row);
    if (natural && !map[natural]) map[natural] = offset + 2;
  });
  return map;
}

function mergeEventRow(sheet, stream, targetRow, incoming) {
  const config = STREAMS[stream];
  const width = config.maxCol || config.additionalSourcesCol;
  const oldRow = sheet.getRange(targetRow, 1, 1, width).getValues()[0];
  while (incoming.length < width) incoming.push('');
  const incomingSource = String(incoming[config.sourceCol - 1] || '').trim();
  const oldSource = String(oldRow[config.sourceCol - 1] || '').trim();
  const additional = String(oldRow[config.additionalSourcesCol - 1] || '').split(/\n/).map(function (x) { return x.trim(); }).filter(Boolean);
  for (let i = 0; i < width; i++) {
    if (i === config.sourceCol - 1 || i === config.additionalSourcesCol - 1) continue;
    if (incoming[i] !== '' && incoming[i] !== null && incoming[i] !== undefined) oldRow[i] = incoming[i];
  }
  if (incomingSource && incomingSource !== oldSource && additional.indexOf(incomingSource) < 0) additional.push(incomingSource);
  oldRow[config.additionalSourcesCol - 1] = additional.join('\n');
  sheet.getRange(targetRow, 1, 1, width).setValues([oldRow]);
}

function jsonOut(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// POST body: { token, stream: "funding"|"crypto", rows: [[...]], processedSources: [url, ...] }
function doPost(e) {
  const lock = LockService.getScriptLock();
  try {
    const body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    if (body.token !== SECRET_TOKEN) return jsonOut({ ok: false, error: 'unauthorized' });
    const stream = getStream(null, body);
    if (!stream) return jsonOut({ ok: false, error: 'invalid stream; use funding or crypto' });
    if (!lock.tryLock(20000)) return jsonOut({ ok: false, error: 'busy; retry later' });

    const config = STREAMS[stream];
    const sheet = getSheetForStream(stream);
    const existingSources = {};
    getExistingSources(stream, sheet).forEach(function (url) { existingSources[url] = true; });
    // Allow a one-time, authenticated crypto backfill recheck to re-evaluate earlier sources.
    // Existing event keys/natural keys still route matching rows to merge/update instead of duplicating them.
    const allowReprocessedSources = stream === 'crypto' && body.allowReprocessedSources === true;
    const keyRows = getKeyRowMap(stream, sheet);
    const naturalRows = getNaturalEventRowMap(stream, sheet);
    const rows = Array.isArray(body.rows) ? body.rows : [];
    const reviewRows = Array.isArray(body.reviewRows) ? body.reviewRows : [];
    if (reviewRows.length && stream !== 'crypto') throw new Error('reviewRows are only valid for stream=crypto');
    const batchSources = {};
    const batchKeys = {};
    const toAdd = [];
    const toUpdate = [];
    let skipped = 0;

    rows.forEach(function (row) {
      if (!Array.isArray(row) || row.length < config.keyCol) { skipped++; return; }
      const company = String(row[config.companyCol - 1] || '').trim();
      const source = String(row[config.sourceCol - 1] || '').trim();
      const key = String(row[config.keyCol - 1] || '').trim();
      if (!company || !source || !key || (existingSources[source] && !allowReprocessedSources) || batchSources[source] || batchKeys[key]) {
        skipped++;
        return;
      }
      batchSources[source] = true;
      batchKeys[key] = true;
      const natural = naturalEventKey(stream, row);
      const existingRowNumber = keyRows[key] || (natural ? naturalRows[natural] : null);
      if (existingRowNumber) {
        toUpdate.push({ rowNumber: existingRowNumber, row: row });
      } else {
        toAdd.push(row);
      }
    });

    // Enrich existing funding/launch events rather than dropping every subsequent source.
    toUpdate.forEach(function (entry) {
      mergeEventRow(sheet, stream, entry.rowNumber, entry.row);
    });

    if (toAdd.length) {
      const width = config.maxCol || toAdd[0].length;
      // Normalise BEFORE touching the sheet. A row that fails validation must not be
      // able to abort the job after blank rows have already been inserted.
      const normalized = toAdd.map(function (row) {
        if (row.length > width) throw new Error('A row exceeds the configured column count.');
        while (row.length < width) row.push('');
        return row;
      });

      // insertRowsBefore + setValues is two operations. If the script dies between
      // them -- the execution-time limit being the usual cause -- the blank rows are
      // already committed and stay in the sheet forever. 187 such rows had
      // accumulated, and because each one inflates lastRow, every later run read more
      // data and was more likely to time out: the failure fed itself.
      // Roll the insert back on any failure so a bad run leaves the sheet unchanged.
      sheet.insertRowsBefore(2, normalized.length);
      try {
        sheet.getRange(2, 1, normalized.length, width).setValues(normalized);
        SpreadsheetApp.flush();
      } catch (writeErr) {
        try {
          sheet.deleteRows(2, normalized.length);
          SpreadsheetApp.flush();
        } catch (rollbackErr) {
          // Rollback itself failed; say so explicitly rather than reporting a plain
          // write error, because the sheet now needs cleanupBlankRows().
          throw new Error('write failed AND rollback failed (' + rollbackErr +
            '); run Tracker Tools > Remove Blank Rows. Original error: ' + writeErr);
        }
        throw writeErr;
      }
    }

    // Write the review queue before marking sources processed. If the queue write fails,
    // the request returns ok:false and the Python backfill keeps the entire batch retryable.
    const reviewResult = reviewRows.length ? upsertCryptoReviewRows(reviewRows) : { appended: 0, updated: 0 };

    // Apply the crypto presentation after inserts/updates so newly added rows match the table style.
    formatTrackerSheet(sheet, stream, toAdd.length);

    // Mark all inspected source URLs, including rejected stories and queued candidates, only
    // after all sheet writes have completed successfully.
    const processed = (Array.isArray(body.processedSources) ? body.processedSources : []).concat(
      rows.map(function (row) { return Array.isArray(row) ? row[config.sourceCol - 1] : ''; })
    );
    recordProcessedSources(stream, processed);
    return jsonOut({ ok: true, stream: stream, appended: toAdd.length, updated: toUpdate.length,
      reviewAppended: reviewResult.appended, reviewUpdated: reviewResult.updated,
      skipped: skipped, processed: processed.length });
  } catch (err) {
    return jsonOut({ ok: false, error: String(err) });
  } finally {
    try { lock.releaseLock(); } catch (ignored) {}
  }
}

function doGet(e) {
  const params = (e && e.parameter) || {};
  if (params.token !== SECRET_TOKEN) return jsonOut({ ok: false, error: 'unauthorized' });
  const stream = getStream(e, null);
  if (!stream) return jsonOut({ ok: false, error: 'invalid stream; use funding or crypto' });
  try {
    const action = params.action || 'health';
    const sheet = getSheetForStream(stream);
    if (action === 'health' || action === 'format') formatTrackerSheet(sheet, stream);
    if (action === 'existing_sources') return jsonOut({ ok: true, stream: stream, sources: getExistingSources(stream, sheet) });
    if (action === 'existing_keys') return jsonOut({ ok: true, stream: stream, keys: getExistingKeys(stream, sheet) });
    if (action === 'existing_companies') {
      const cfg = STREAMS[stream];
      const lastRow = sheet.getLastRow();
      const names = lastRow < 2 ? [] : sheet.getRange(2, cfg.companyCol, lastRow - 1, 1).getValues().flat();
      const set = {};
      names.forEach(function (name) { const norm = normalizeCompany(name); if (norm) set[norm] = true; });
      return jsonOut({ ok: true, stream: stream, companies: Object.keys(set) });
    }
    if (action === 'review_queue') {
      if (stream !== 'crypto') return jsonOut({ ok: false, error: 'review_queue requires stream=crypto' });
      const reviewSheet = getCryptoReviewSheet();
      const reviewLast = reviewSheet.getLastRow();
      const reviewRows = reviewLast < 2 ? [] : reviewSheet.getRange(2, 1, reviewLast - 1, REVIEW_HEADERS.length).getValues();
      // Return only unresolved candidates; promoted/excluded rows stay visible in Sheets for audit.
      const unresolved = reviewRows.filter(function (row) {
        const status = String(row[13] || '').toLowerCase();
        const decision = String(row[22] || '').toLowerCase();
        return status.indexOf('qualified') !== 0 && status.indexOf('excluded') !== 0 &&
          decision !== 'reject' && decision !== 'rejected' && decision !== 'ignore';
      });
      return jsonOut({ ok: true, stream: stream, rows: unresolved });
    }
    return jsonOut({ ok: true, scriptVersion: SCRIPT_VERSION,
      message: action === 'format' ? 'sheet formatting applied' : 'webhook live',
      stream: stream, sheet: sheet.getName(),
      knownActions: ['health', 'format', 'existing_sources', 'existing_keys',
                     'existing_companies', 'review_queue'] });
  } catch (err) {
    return jsonOut({ ok: false, error: String(err) });
  }
}
