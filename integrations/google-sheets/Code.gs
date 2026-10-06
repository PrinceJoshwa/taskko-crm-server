/**
 * Propzel Google Sheets lead sync.
 *
 * 1. Paste this file into Extensions > Apps Script for the lead spreadsheet.
 * 2. Replace the three values in setPropzelConfig().
 * 3. Run setPropzelConfig() once, then run syncPropzelLeads().
 */

const PROPZEL_PROPERTIES = {
  apiUrl: "PROPZEL_API_URL",
  organizationId: "PROPZEL_ORGANIZATION_ID",
  syncSecret: "PROPZEL_SYNC_SECRET",
};

function setPropzelConfig() {
  PropertiesService.getScriptProperties().setProperties({
    // The deployed backend URL. Do not add a trailing slash.
    [PROPZEL_PROPERTIES.apiUrl]: "https://taskko-crm-server.vercel.app/api",
    // Copy this organisation ID from Propzel, for example the Jagati organisation ID.
    [PROPZEL_PROPERTIES.organizationId]: "REPLACE_WITH_ORGANIZATION_ID",
    // Must exactly match the GOOGLE_SHEETS_SYNC_SECRET Vercel environment variable.
    [PROPZEL_PROPERTIES.syncSecret]: "REPLACE_WITH_SYNC_SECRET",
  });
}

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("Propzel")
    .addItem("Sync unsynced leads", "syncPropzelLeads")
    .addItem("Repair imported lead names", "repairPropzelLeadNames")
    .addItem("Install hourly sync", "installHourlySync")
    .addToUi();
}

function installHourlySync() {
  ScriptApp.getProjectTriggers()
    .filter((trigger) => trigger.getHandlerFunction() === "syncPropzelLeads")
    .forEach((trigger) => ScriptApp.deleteTrigger(trigger));
  ScriptApp.newTrigger("syncPropzelLeads").timeBased().everyHours(1).create();
}

function syncPropzelLeads() {
  syncPropzelLeadsInternal(false);
}

function repairPropzelLeadNames() {
  syncPropzelLeadsInternal(true);
}

function syncPropzelLeadsInternal(repairSyncedLeads) {
  const config = PropertiesService.getScriptProperties().getProperties();
  [PROPZEL_PROPERTIES.apiUrl, PROPZEL_PROPERTIES.organizationId, PROPZEL_PROPERTIES.syncSecret].forEach((key) => {
    if (!config[key] || config[key].indexOf("REPLACE_WITH") === 0) {
      throw new Error("Run setPropzelConfig() and enter the Propzel connection values first.");
    }
  });

  const sheet = SpreadsheetApp.getActiveSheet();
  const values = sheet.getDataRange().getDisplayValues();
  if (values.length < 2) return;

  const headerRowIndex = findHeaderRow(values);
  if (headerRowIndex === -1) {
    throw new Error("Could not find a lead header row. Include a Name/Full Name or Phone/Mobile column.");
  }
  const headers = values[headerRowIndex].map(normalizeHeader);
  const headerRowNumber = headerRowIndex + 1;
  const statusColumn = ensureColumn(sheet, headers, "Propzel Sync Status", headerRowNumber);
  const leadIdColumn = ensureColumn(sheet, headers, "Propzel Lead ID", headerRowNumber);
  const rowKeyColumn = ensureColumn(sheet, headers, "Propzel Row Key", headerRowNumber);
  const syncedAtColumn = ensureColumn(sheet, headers, "Propzel Synced At", headerRowNumber);
  const updatedValues = sheet.getDataRange().getDisplayValues();
  const updatedHeaders = updatedValues[headerRowIndex].map(normalizeHeader);

  for (let rowIndex = 0; rowIndex < updatedValues.length; rowIndex += 1) {
    const row = updatedValues[rowIndex];
    if (!row.some((cell) => String(cell).trim()) || isSourceHeaderRow(row)) continue;
    const currentStatus = row[statusColumn - 1];
    if (currentStatus === "Synced" && !repairSyncedLeads) continue;

    const lead = rowToLead(updatedHeaders, row);
    if (!lead.name) {
      sheet.getRange(rowIndex + 1, statusColumn).setValue("Error: Name is required");
      continue;
    }

    let rowKey = row[rowKeyColumn - 1];
    if (!rowKey) {
      rowKey = Utilities.getUuid();
      sheet.getRange(rowIndex + 1, rowKeyColumn).setValue(rowKey);
    }

    const response = UrlFetchApp.fetch(
      config[PROPZEL_PROPERTIES.apiUrl] + "/integrations/google-sheets/" + encodeURIComponent(config[PROPZEL_PROPERTIES.organizationId]) + "/leads",
      {
        method: "post",
        contentType: "application/json",
        headers: { "X-Propzel-Sync-Key": config[PROPZEL_PROPERTIES.syncSecret] },
        payload: JSON.stringify(Object.assign({ row_key: rowKey }, lead)),
        muteHttpExceptions: true,
      },
    );

    const responseCode = response.getResponseCode();
    const responseBody = safeJson(response.getContentText());
    if (responseCode >= 200 && responseCode < 300 && responseBody.ok) {
      sheet.getRange(rowIndex + 1, statusColumn).setValue("Synced");
      sheet.getRange(rowIndex + 1, leadIdColumn).setValue(responseBody.lead_id || "");
      sheet.getRange(rowIndex + 1, syncedAtColumn).setValue(new Date());
    } else {
      const message = responseBody.detail || response.getContentText() || "Unknown error";
      sheet.getRange(rowIndex + 1, statusColumn).setValue("Error: " + String(message).slice(0, 180));
    }
  }
}

function rowToLead(headers, row) {
  const value = (...names) => {
    for (const name of names) {
      const index = headers.indexOf(normalizeHeader(name));
      if (index !== -1 && row[index]) return String(row[index]).trim();
    }
    return "";
  };
  const leadReference = value("leadgen id", "leadgen_id", "id");
  const name = value("name", "full name", "full_name", "customer name", "lead name") || (leadReference ? "Facebook lead " + leadReference : "");
  return {
    // Facebook exports can omit contact answers for some form submissions.
    // Keep those leads visible in Propzel with a traceable lead-ID fallback.
    name,
    phone: value("phone", "phone number", "phone_number", "mobile", "mobile number", "contact number", "whatsapp number") || null,
    email: value("email", "email address", "email_address") || null,
    project_name: value("project", "project name") || null,
    budget_min: numberOrNull(value("budget min", "minimum budget", "budget")),
    budget_max: numberOrNull(value("budget max", "maximum budget")),
    configuration: value("configuration", "bhk") || null,
    location_pref: value("location", "location preference", "locality", "city") || null,
    notes: value("notes", "message", "remarks", "comment", "feedback") || null,
  };
}

function findHeaderRow(values) {
  const scanLimit = Math.min(values.length, 20);
  let bestRow = -1;
  let bestScore = 0;
  for (let rowIndex = 0; rowIndex < scanLimit; rowIndex += 1) {
    const normalized = values[rowIndex].map(normalizeHeader);
    const score = normalized.filter((value) => SOURCE_HEADER_NAMES.includes(value)).length;
    if (score > bestScore) {
      bestRow = rowIndex;
      bestScore = score;
    }
  }
  return bestScore > 0 ? bestRow : -1;
}

const SOURCE_HEADER_NAMES = [
  "id", "name", "full name", "full_name", "customer name", "phone", "phone number", "phone_number",
  "mobile", "email", "email address", "created time", "created_time", "leadgen id", "leadgen_id",
  "platform", "feedback", "lead status", "lead_status", "form name", "form_name",
].map(normalizeHeader);

function isSourceHeaderRow(row) {
  const headerCount = row
    .map(normalizeHeader)
    .filter((value) => SOURCE_HEADER_NAMES.includes(value))
    .length;
  return headerCount >= 3;
}

function ensureColumn(sheet, headers, title, headerRowNumber) {
  const normalizedTitle = normalizeHeader(title);
  const existing = headers.indexOf(normalizedTitle);
  if (existing !== -1) return existing + 1;
  const column = headers.length + 1;
  sheet.getRange(headerRowNumber, column).setValue(title);
  headers.push(normalizedTitle);
  return column;
}

function normalizeHeader(value) {
  return String(value || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

function numberOrNull(value) {
  if (!value) return null;
  const numeric = Number(String(value).replace(/[^0-9.]/g, ""));
  return Number.isFinite(numeric) ? numeric : null;
}

function safeJson(value) {
  try {
    return JSON.parse(value);
  } catch (_) {
    return {};
  }
}
