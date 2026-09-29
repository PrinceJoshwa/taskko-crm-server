# Google Sheets Lead Sync

This integration creates a Propzel lead for each unsynced spreadsheet row.

## One-time backend configuration

Add a long random value as `GOOGLE_SHEETS_SYNC_SECRET` in the **Production** environment variables for `taskko-crm-server`, then redeploy. Keep this value private.

## One-time spreadsheet setup

1. Open the lead spreadsheet and select **Extensions > Apps Script**.
2. Paste the contents of `Code.gs`, save, and replace the organisation ID and sync secret in `setPropzelConfig()`.
3. Run `setPropzelConfig()` once and approve Google permissions.
4. Return to the spreadsheet, reload it, then choose **Propzel > Sync unsynced leads**.
5. Optionally choose **Propzel > Install hourly sync** for automatic hourly imports.

The script accepts column names such as `Name`, `Phone`/`Mobile`, `Email`, `Project`, `Budget Min`, `Budget Max`, `Configuration`/`BHK`, `Location`, and `Notes`. It adds `Propzel Sync Status`, `Propzel Lead ID`, `Propzel Row Key`, and `Propzel Synced At` columns automatically.

`Propzel Row Key` is kept with the row so retries and future runs cannot create a duplicate lead.
