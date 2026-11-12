"""Publish the master list, one tab per country, the changelog and a summary
to a Google Sheet.

Needs two secrets: GOOGLE_SERVICE_ACCOUNT_JSON (the key file's contents) and
SHEET_ID (from the sheet URL). Share the sheet with the service account's
email as an Editor.

Google allows about 60 write requests a minute, so everything is sent in a
handful of batched requests however many country tabs there are.
"""
from __future__ import annotations

import re

import json
import os
from collections import Counter, defaultdict

from .diff import CHANGE_COLUMNS
from .model import CFD_COLUMNS, SHEET_COLUMNS
from .store import cfd_row, record_row, sort_records

ROWS_PER_REQUEST = 4000   # keeps each request well under Google's size limit

from .countries import COUNTRY_NAMES  # noqa: E402
FIXED_TABS = ["Summary", "Changes", "Digital Assets (all countries)", "FI (all countries)"]
FI_ALL_TAB = "FI (all countries)"
OTHER_TAB = "Other"


def enabled() -> bool:
    return bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") and os.environ.get("SHEET_ID"))


VERTICAL_NAMES = ("CFD", "iGaming")
CFD_ALL_TAB = "CFD (all countries)"


def vertical_of(r: dict) -> str:
    return (r.get("extra") or {}).get("vertical") or ""


def merge_cfd(records: list) -> list:
    """One row per firm and country when several sources list it (e.g. the ESMA register and
    CySEC's own list): licences and sources are combined, the first record's details kept."""
    from .sources.national import match_name
    out, seen = [], {}
    for r in records:
        k = ((r.get("jurisdiction") or "").upper(), match_name(r.get("name", "")) or r.get("lei") or r.get("key"))
        if k in seen:
            first = seen[k]
            for f, sep in (("licence_type", " + "), ("source", ", "), ("regulator", ", ")):
                vals = [v for v in (first.get(f) or "").split(sep) if v]
                if r.get(f) and r[f] not in vals:
                    first[f] = sep.join(vals + [r[f]])
            for f in ("website", "email", "lei", "city", "reference", "headcount", "revenue", "other_websites"):
                first[f] = first.get(f) or r.get(f, "")
            if r.get("linkedin") and (not first.get("linkedin")
                                      or (first.get("linkedin_source") or "").startswith("not found")):
                first["linkedin"], first["linkedin_source"] = r["linkedin"], r.get("linkedin_source", "")
            continue
        r = dict(r)
        seen[k] = r
        out.append(r)
    return out


def cfd_tab(code: str, vertical: str = "CFD") -> str:
    code = (code or "").strip().upper()
    return f"{COUNTRY_NAMES.get(code, code or 'Other')} ({vertical})"


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
    from .enrich import add_search_links
    add_search_links(ordered)
    tabs = {}

    summ = summary_values(records, change_rows, run_info)
    width = max(len(r) for r in summ)
    tabs["Summary"] = [r + [""] * (width - len(r)) for r in summ]
    tabs["Changes"] = [[h for _, h in CHANGE_COLUMNS]] + list(reversed(change_rows))  # newest first
    for title, rows in (extra_tabs or {}).items():
        tabs[title] = rows
    # lead-generation verticals (CFD brokers, iGaming firms) have their own tabs ("United Kingdom (CFD)",
    # "Malta (iGaming)"), not the licence tabs
    verticals = {v: merge_cfd([r for r in ordered if vertical_of(r) == v]) for v in VERTICAL_NAMES}
    ordered = [r for r in ordered if not vertical_of(r)]
    # Financial institutions and digital-asset firms are kept apart: "FI (all countries)" /
    # "Digital Assets (all countries)", and per country "United Kingdom (FI)" / "United Kingdom (Digital Assets)".
    from .model import SEGMENTS, segment_of
    by_seg = {seg: [] for seg in SEGMENTS}
    for r in ordered:
        by_seg[segment_of(r)].append(r)
    for seg in reversed(SEGMENTS):          # Digital Assets first: it is the short one
        if by_seg[seg] or seg == "FI":
            tabs[f"{seg} (all countries)"] = [header] + [record_row(r) for r in by_seg[seg]]

    if per_country:
        by_tab = defaultdict(list)
        for seg in SEGMENTS:
            for r in by_seg[seg]:
                by_tab[cfd_tab(r.get("jurisdiction", ""), seg) if country_tab(r.get("jurisdiction", "")) != OTHER_TAB
                       else f"{OTHER_TAB} ({seg})"].append(record_row(r))
        for title in sorted(by_tab, key=lambda t: (t.startswith(OTHER_TAB + " ("), t)):
            tabs[title] = [header] + by_tab[title]
    for vertical, recs in verticals.items():
        if not recs:
            continue
        vheader = [h for _, h in CFD_COLUMNS]
        tabs[f"{vertical} (all countries)"] = [vheader] + [cfd_row(r) for r in recs]
        by_tab = defaultdict(list)
        for r in recs:
            by_tab[cfd_tab(r.get("jurisdiction", ""), vertical)].append(cfd_row(r))
        for title in sorted(by_tab):
            tabs[title] = [vheader] + by_tab[title]
    return tabs


FLAG_HEADER = "Do not contact / competitor"


def highlight_flags(sh, tabs: dict, existing: dict) -> None:
    meta = sh.fetch_sheet_metadata({"fields": "sheets(properties(sheetId,title),conditionalFormats)"})
    rules = {s["properties"]["sheetId"]: len(s.get("conditionalFormats") or []) for s in meta.get("sheets", [])}
    reqs = []
    for title, rows in tabs.items():
        if not rows or FLAG_HEADER not in rows[0]:
            continue
        sid = existing[title]
        for i in reversed(range(rules.get(sid, 0))):        # replace the rule written last run
            reqs.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": i}})
        col = rows[0].index(FLAG_HEADER)
        letter = ""
        n = col + 1
        while n:
            n, rem = divmod(n - 1, 26)
            letter = chr(65 + rem) + letter
        reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": sid, "startRowIndex": 1, "endRowIndex": max(len(rows), 2),
                        "startColumnIndex": 0, "endColumnIndex": max(len(r) for r in rows)}],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": f'=${letter}2<>""'}]},
                            "format": {"backgroundColor": {"red": 0.96, "green": 0.6, "blue": 0.6}}}}}})
    if reqs:
        sh.batch_update({"requests": reqs})


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
    # Google Sheets holds at most 10 million cells per spreadsheet. The country tabs already contain
    # every firm, so when everything won't fit, the "FI (all countries)" tab is left out.
    limit = int(os.environ.get("SHEET_CELL_LIMIT", "9800000"))
    cells = sum(max(len(rows), 2) * max((len(r) for r in rows), default=1) for rows in tabs.values())
    dropped = []
    if cells > limit and per_country and FI_ALL_TAB in tabs:
        rows = tabs.pop(FI_ALL_TAB)
        cells -= len(rows) * max((len(r) for r in rows), default=1)
        dropped.append(FI_ALL_TAB)
        log.warning("The sheet would need %d cells (Google's limit is 10 million): the 'FI (all countries)' "
                    "tab is left out; every firm is still on its country tab", cells + len(rows) * len(rows[0]))

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
    # the empty default tab of a brand-new spreadsheet, and country tabs ("Japan (JP)") with no firms
    # left, e.g. a country that has been excluded
    for title, sid in existing.items():
        if title in tabs:
            continue
        if title in ("Sheet1", "Feuille 1", "Hoja 1", "Foglio1", "Tabelle1") or title in dropped or title == "Licences" or re.search(r"\(([A-Z]{2}|CFD|iGaming|FI|Digital Assets)\)$", title) or title.endswith("(all countries)"):
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

    # 4. rows flagged "do not contact" / competitor turn red (one conditional-format rule per tab)
    try:
        highlight_flags(sh, tabs, existing)
    except Exception as e:
        log.warning("Could not colour flagged rows red: %s", e)

    n_country = len(tabs) - len(FIXED_TABS) - len(extra_tabs or {})
    log.info("Google Sheet updated: %d licences, %d country tabs, %d change rows",
             len(records), n_country, len(change_rows))
