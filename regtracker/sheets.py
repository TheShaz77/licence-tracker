"""Publish the master list, one tab per country, the changelog and a summary
to a Google Sheet.

Needs two secrets: GOOGLE_SERVICE_ACCOUNT_JSON (the key file's contents) and
SHEET_ID (from the sheet URL). Share the sheet with the service account's
email as an Editor.

Google allows about 60 write requests a minute, so everything is sent in a
handful of batched requests however many country tabs there are.
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict

from .diff import CHANGE_COLUMNS
from .model import SHEET_COLUMNS
from .store import record_row, sort_records

ROWS_PER_REQUEST = 4000   # keeps each request well under Google's size limit

from .countries import COUNTRY_NAMES  # noqa: E402
FIXED_TABS = ["Summary", "Changes", "Licences"]
OTHER_TAB = "Other"


def enabled() -> bool:
    return bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") and os.environ.get("SHEET_ID"))


def country_tab(code: str) -> str:
    code = (code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return OTHER_TAB
    return f"{COUNTRY_NAMES.get(code, code)} ({code})"


def summary_values(records: list, change_rows: list, run_info: dict) -> list:
    counts = defaultdict(Counter)
    for r in records:
        counts[(r.get("jurisdiction", ""), r.get("licence_type", ""))]["active" if r.get("active") else "inactive"] += 1
    out = [["Last run (UTC)", run_info.get("finished", "")],
           ["Sources", run_info.get("sources_status", "")],
           ["Active licences tracked", sum(1 for r in records if r.get("active"))],
           [],
           ["Jurisdiction", "Licence type", "Active", "Inactive / former"]]
    for (j, t), c in sorted(counts.items()):
        out.append([j, t, c["active"], c["inactive"]])
    out += [[], ["Changes logged (all time)", "Event", "Count"]]
    ev = Counter(row[1] for row in change_rows if len(row) > 1)
    for e, n in ev.most_common():
        out.append(["", e, n])
    return out


MICA_HEADER = ["Firm name", "Trading name", "Home country", "Regulator", "MiCA status", "Status",
               "Authorised / notified on", "Crypto-asset services", "Passported to", "LEI", "Website", "Email",
               "Phone", "Swiss SRO / notes", "Register link", "First seen", "Last changed"]


def mica_tab(records: list) -> list:
    """Every crypto-asset service provider on ESMA's MiCA register, one row each:
    CASPs authorised under MiCA and financial firms (banks, investment firms,
    EMIs) that notified crypto services under Article 60. E-money token issuers
    are not CASPs and stay on the country tabs only."""
    rows = []
    for r in records:
        if r.get("source") != "mica" or "token issuer" in (r.get("licence_type") or ""):
            continue
        ex = r.get("extra") or {}
        lt = r.get("licence_type") or ""
        rows.append([r.get("name", ""), ex.get("trading_name", ""), country_tab(r.get("jurisdiction", "")),
                     r.get("regulator", ""),
                     "Notified financial firm (Art. 60)" if "Art. 60" in lt else "Authorised CASP (Art. 63)",
                     "Active" if r.get("active") else "Inactive", r.get("status_date", ""), r.get("services", ""),
                     r.get("passports", ""), r.get("lei", ""), r.get("website", ""),
                     r.get("email") or ex.get("email", ""), r.get("phone") or ex.get("phone", ""),
                     ex.get("comments", ""), r.get("register_url", ""), r.get("first_seen", ""),
                     r.get("last_changed", "")])
    rows.sort(key=lambda x: (x[5] != "Active", x[2], x[4], x[0].lower()))
    return [MICA_HEADER] + rows


def build_tabs(records: list, change_rows: list, run_info: dict, per_country: bool = True,
               extra_tabs: dict | None = None) -> dict:
    """Return {tab title: rows (list of lists, header first)} in display order."""
    header = [h for _, h in SHEET_COLUMNS]
    ordered = sort_records(records)
    tabs = {}

    summ = summary_values(records, change_rows, run_info)
    width = max(len(r) for r in summ)
    tabs["Summary"] = [r + [""] * (width - len(r)) for r in summ]
    tabs["Changes"] = [[h for _, h in CHANGE_COLUMNS]] + list(reversed(change_rows))  # newest first
    for title, rows in (extra_tabs or {}).items():
        tabs[title] = rows
    tabs["Licences"] = [header] + [record_row(r) for r in ordered]

    if per_country:
        by_tab = defaultdict(list)
        for r in ordered:
            by_tab[country_tab(r.get("jurisdiction", ""))].append(record_row(r))
        for title in sorted(by_tab, key=lambda t: (t == OTHER_TAB, t)):
            tabs[title] = [header] + by_tab[title]
    return tabs


def _a1(title: str, row: int) -> str:
    return "'" + title.replace("'", "''") + f"'!A{row}"


def publish(records: list, change_rows: list, run_info: dict, log, per_country: bool = True, sh=None,
            extra_tabs: dict | None = None):
    if sh is None:
        import gspread
        gc = gspread.service_account_from_dict(json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]))
        sh = gc.open_by_key(os.environ["SHEET_ID"].strip())

    tabs = build_tabs(records, change_rows, run_info, per_country, extra_tabs)
    existing = {ws.title: ws.id for ws in sh.worksheets()}

    # 1. create any missing tabs (one request)
    missing = [t for t in tabs if t not in existing]
    if missing:
        reply = sh.batch_update({"requests": [
            {"addSheet": {"properties": {"title": t}}} for t in missing]})
        for t, rep in zip(missing, reply.get("replies", [])):
            existing[t] = rep["addSheet"]["properties"]["sheetId"]

    # 2. size every tab to its data, freeze headers, switch on filters, order tabs (one request)
    reqs = []
    for title, rows in tabs.items():
        if len(rows) < 2:  # blank out a leftover second row
            rows.append([""] * max((len(r) for r in rows), default=1))
    for index, (title, rows) in enumerate(tabs.items()):
        sid = existing[title]
        n_rows = max(len(rows), 2)
        n_cols = max(max((len(r) for r in rows), default=1), 1)
        reqs.append({"updateSheetProperties": {
            "properties": {"sheetId": sid, "index": index,
                           "gridProperties": {"rowCount": n_rows, "columnCount": n_cols,
                                              "frozenRowCount": 0 if title in ("Summary", "Source check") else 1}},
            "fields": "index,gridProperties(rowCount,columnCount,frozenRowCount)"}})
        if title not in ("Summary", "Source check"):
            reqs.append({"setBasicFilter": {"filter": {"range": {
                "sheetId": sid, "startRowIndex": 0, "endRowIndex": n_rows,
                "startColumnIndex": 0, "endColumnIndex": n_cols}}}})
    # the empty default tab of a brand-new spreadsheet
    for title, sid in existing.items():
        if title in ("Sheet1", "Feuille 1", "Hoja 1", "Foglio1", "Tabelle1") and title not in tabs:
            reqs.append({"deleteSheet": {"sheetId": sid}})
    sh.batch_update({"requests": reqs})

    # 3. write the values, a few thousand rows per request
    data, size = [], 0
    for title, rows in tabs.items():
        for i in range(0, max(len(rows), 1), ROWS_PER_REQUEST):
            part = rows[i:i + ROWS_PER_REQUEST]
            data.append({"range": _a1(title, i + 1), "values": part})
            size += len(part)
            if size >= ROWS_PER_REQUEST:
                sh.values_batch_update(body={"valueInputOption": "RAW", "data": data})
                data, size = [], 0
    if data:
        sh.values_batch_update(body={"valueInputOption": "RAW", "data": data})

    n_country = len(tabs) - len(FIXED_TABS) - len(extra_tabs or {})
    log.info("Google Sheet updated: %d licences, %d country tabs, %d change rows",
             len(records), n_country, len(change_rows))
