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
  const config = PropertiesService.getScriptProperties().getProperties();
  [PROPZEL_PROPERTIES.apiUrl, PROPZEL_PROPERTIES.organizationId, PROPZEL_PROPERTIES.syncSecret].forEach((key) => {
    if (!config[key] || config[key].indexOf("REPLACE_WITH") === 0) {
      throw new Error("Run setPropzelConfig() and enter the Propzel connection values first.");
    }
  });

  const sheet = SpreadsheetApp.getActiveSheet();
  const values = sheet.getDataRange().getDisplayValues();
  if (values.length < 2) return;

  const headers = values[0].map(normalizeHeader);
  const statusColumn = ensureColumn(sheet, headers, "Propzel Sync Status");
  const leadIdColumn = ensureColumn(sheet, headers, "Propzel Lead ID");
  const rowKeyColumn = ensureColumn(sheet, headers, "Propzel Row Key");
  const syncedAtColumn = ensureColumn(sheet, headers, "Propzel Synced At");
  const updatedValues = sheet.getDataRange().getDisplayValues();
  const updatedHeaders = updatedValues[0].map(normalizeHeader);

  for (let rowIndex = 1; rowIndex < updatedValues.length; rowIndex += 1) {
    const row = updatedValues[rowIndex];
    const currentStatus = row[statusColumn - 1];
    if (currentStatus === "Synced") continue;

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
  return {
    name: value("name", "full name", "customer name"),
    phone: value("phone", "mobile", "mobile number", "contact number") || null,
    email: value("email", "email address") || null,
    project_name: value("project", "project name") || null,
    budget_min: numberOrNull(value("budget min", "minimum budget", "budget")),
    budget_max: numberOrNull(value("budget max", "maximum budget")),
    configuration: value("configuration", "bhk") || null,
    location_pref: value("location", "location preference", "locality", "city") || null,
    notes: value("notes", "message", "remarks", "comment") || null,
  };
}

function ensureColumn(sheet, headers, title) {
  const normalizedTitle = normalizeHeader(title);
  const existing = headers.indexOf(normalizedTitle);
  if (existing !== -1) return existing + 1;
  const column = headers.length + 1;
  sheet.getRange(1, column).setValue(title);
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
