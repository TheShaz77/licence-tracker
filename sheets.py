"""Publish the master list, changelog and summary to a Google Sheet.

Needs two secrets: GOOGLE_SERVICE_ACCOUNT_JSON (the key file's contents) and
SHEET_ID (from the sheet URL). Share the sheet with the service account's
email as an Editor.
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict

from .diff import CHANGE_COLUMNS
from .model import SHEET_COLUMNS
from .store import record_row, sort_records

CHUNK = 5000


def enabled() -> bool:
    return bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") and os.environ.get("SHEET_ID"))


def _ws(sh, title, rows, cols):
    import gspread
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=max(rows, 10), cols=max(cols, 5))


def _write(ws, values):
    ws.clear()
    rows = max(len(values), 2)
    cols = max(len(values[0]) if values else 1, 1)
    ws.resize(rows=rows, cols=cols)
    for i in range(0, len(values), CHUNK):
        ws.update(range_name=f"A{i + 1}", values=values[i:i + CHUNK], value_input_option="RAW")


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


def publish(records: list, change_rows: list, run_info: dict, log):
    import gspread
    gc = gspread.service_account_from_dict(json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]))
    sh = gc.open_by_key(os.environ["SHEET_ID"])

    lic = [[h for _, h in SHEET_COLUMNS]] + [record_row(r) for r in sort_records(records)]
    ws = _ws(sh, "Licences", len(lic), len(lic[0]))
    _write(ws, lic)
    ws.freeze(rows=1)
    try:
        ws.set_basic_filter()
    except Exception:
        pass

    ch = [[h for _, h in CHANGE_COLUMNS]] + list(reversed(change_rows))  # newest first
    ws = _ws(sh, "Changes", len(ch), len(ch[0]))
    _write(ws, ch)
    ws.freeze(rows=1)

    summ = summary_values(records, change_rows, run_info)
    width = max(len(r) for r in summ)
    summ = [r + [""] * (width - len(r)) for r in summ]
    _write(_ws(sh, "Summary", len(summ), width), summ)

    # tidy the default empty tab of a brand-new sheet
    for w in sh.worksheets():
        if w.title in ("Sheet1", "Feuille 1") and len(sh.worksheets()) > 1 and not any(w.get_all_values()):
            sh.del_worksheet(w)
    log.info("Google Sheet updated: %d licences, %d change rows", len(records), len(change_rows))
