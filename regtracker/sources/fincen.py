"""United States: FinCEN Money Services Business (MSB) registrant list.

FinCEN's MSB Registrant Search offers the whole list as one tab-separated
download (the same file the search page's spreadsheet export gives), updated
weekly. One row per registration filing; a firm that renewed has several rows,
which are merged here into one record per firm.

Columns (as published): LEGAL NAME, DBA NAME, STREET ADDRESS, CITY, STATE, ZIP,
MSB ACTIVITIES (codes), STATES OF MSB ACTIVITIES, ALL STATES & TERRITORIES &
FOREIGN FLAG**, FOREIGN LOCATION, # OF BRANCHES, AUTH SIGN DATE, RECEIVED DATE.
"""
from __future__ import annotations

import hashlib
import re

from ..http import robots_allowed
from ..model import Record
from .base import Context, Source, SourceNotReady
from .national import decode_bytes, match_name, write_diagnostics

URL = "https://msb.fincen.gov/retrieve.msb.list.php"
SEARCH_URL = "https://msb.fincen.gov/"

ACTIVITIES = {
    "401": "Issuer of traveler's checks", "402": "Seller of traveler's checks",
    "403": "Redeemer of traveler's checks", "404": "Issuer of money orders",
    "405": "Seller of money orders", "406": "Redeemer of money orders",
    "407": "Currency dealer or exchanger", "408": "Check casher",
    "409": "Money transmitter", "410": "U.S. Postal Service",
    "413": "Seller of prepaid access", "414": "Provider of prepaid access",
    "415": "Dealer in foreign exchange", "499": "Other",
}
US_STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH "
             "NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY").split()
TERRITORIES = "PR GU VI AS MP".split()
SCOPE = {"A": "All states and territories", "B": "All states", "C": "All territories", "D": "Abroad"}


def us_date(s: str) -> str:
    """FinCEN dates are month-first: 11/4/2026 -> 2026-11-04."""
    s = (s or "").strip()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s


def states_from(cell: str, scope: str) -> list:
    flags = set(re.findall(r"[ABCD]", (scope or "").upper()))
    if "A" in flags or "B" in flags:
        out = list(US_STATES) + (TERRITORIES if "A" in flags else [])
    else:
        found = set(re.findall(r"\b[A-Z]{2}\b", (cell or "").upper()))
        out = [s for s in US_STATES + TERRITORIES if s in found]
        if "C" in flags:
            out += [t for t in TERRITORIES if t not in out]
    return out


def describe_states(states: list) -> str:
    if all(s in states for s in US_STATES):
        return "All states" + (" + territories" if all(t in states for t in TERRITORIES) else "")
    return ", ".join(states)


def _sheet_to_tsv(raw: bytes) -> str:
    """A downloaded spreadsheet (.xlsx, or the old .xls format) as tab-separated text."""
    if raw[:2] == b"PK":
        import io
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True).active
        rows = ws.iter_rows(values_only=True)
    else:
        import xlrd
        sh = xlrd.open_workbook(file_contents=raw).sheet_by_index(0)
        rows = (sh.row_values(r) for r in range(sh.nrows))
    def cell(v):
        if hasattr(v, "strftime"):
            return f"{v.month}/{v.day}/{v.year}"
        return "" if v is None else str(v).replace("\t", " ").replace("\n", " ")
    return "\n".join("\t".join(cell(v) for v in r) for r in rows)


def parse_fincen(raw: bytes) -> tuple:
    if raw[:2] == b"PK" or raw[:2] == b"\xd0\xcf":
        text = _sheet_to_tsv(raw)
    else:
        text = decode_bytes(raw)
    if "<table" in text[:5000].lower():  # some "Excel" exports are really HTML tables
        from .usstates import html_tables
        t = max(html_tables(text), key=len)
        text = "\n".join("\t".join(c for c in r) for r in t)
    lines = text.splitlines()
    head_i = next((i for i, l in enumerate(lines[:30]) if "LEGAL NAME" in l.upper()), None)
    if head_i is None:
        raise RuntimeError("FinCEN MSB file: no 'LEGAL NAME' header found")
    delim = "\t" if "\t" in lines[head_i] else ","
    import csv
    rows = list(csv.reader(lines[head_i:], delimiter=delim))
    headers = [h.strip().upper() for h in rows[0]]

    def col(*names):
        for n in names:
            for i, h in enumerate(headers):
                if h.startswith(n):
                    return i
        return None
    c = {"name": col("LEGAL NAME"), "dba": col("DBA"), "street": col("STREET"), "city": col("CITY"),
         "state": col("STATE"), "zip": col("ZIP"), "acts": col("MSB ACTIVITIES"),
         "states": col("STATES OF MSB"), "scope": col("ALL STATES"), "foreign": col("FOREIGN"),
         "branches": col("# OF BRANCHES", "NUMBER OF BRANCHES"), "signed": col("AUTH SIGN"),
         "received": col("RECEIVED")}
    info = {"headers": headers, "rows": len(rows) - 1,
            "columns": {k: (headers[v] if v is not None else None) for k, v in c.items()}}
    if c["name"] is None or c["received"] is None:
        raise RuntimeError(f"FinCEN MSB file layout changed; headers are {headers}")

    def get(row, k):
        i = c[k]
        return row[i].strip() if i is not None and i < len(row) else ""

    firms = {}
    for row in rows[1:]:
        name, received = get(row, "name"), us_date(get(row, "received"))
        if not name or not re.match(r"\d{4}-\d{2}-\d{2}", received):
            continue  # footnotes
        zip5 = re.sub(r"\D", "", get(row, "zip"))[:5]
        k = match_name(name) + ":" + (zip5 or get(row, "city").lower())
        f = firms.get(k)
        if f is None or received >= f["received"]:
            first = min(received, f["first"]) if f else received
            firms[k] = {"row": row, "received": received, "first": first, "filings": (f or {}).get("filings", 0) + 1}
        else:
            f["first"] = min(f["first"], received)
            f["filings"] += 1

    out = []
    acts_seen = {}
    for k, f in firms.items():
        row = f["row"]
        codes = re.findall(r"\d{3}", get(row, "acts"))
        for code in codes:
            acts_seen[code] = acts_seen.get(code, 0) + 1
        states = states_from(get(row, "states"), get(row, "scope"))
        foreign = get(row, "foreign")
        mt = "409" in codes
        key = "fincen:" + hashlib.sha1(k.encode()).hexdigest()[:12]
        out.append(Record(
            key=key, source="fincen", jurisdiction="US", regulator="FinCEN",
            name=get(row, "name"),
            licence_type="MSB – money transmitter" if mt else "MSB – other activities",
            active=True, status_detail=f"Registered (latest filing {f['received']})",
            status_date=f["first"], city=get(row, "city").title(),
            services="; ".join(ACTIVITIES.get(x, f"Activity {x}") for x in codes),
            register_url=SEARCH_URL,
            extra={"dba": get(row, "dba"), "street": get(row, "street"), "state": get(row, "state"),
                   "zip": get(row, "zip"), "foreign_location": foreign,
                   "branches": get(row, "branches"), "latest_filing": f["received"], "filings": f["filings"],
                   "us_states": describe_states(states), "states_list": states,
                   "scope": ", ".join(SCOPE[x] for x in sorted(set(re.findall(r"[ABCD]", get(row, "scope").upper()))))},
        ))
    info["kept"] = len(out)
    info["type_values"] = sorted(acts_seen.items(), key=lambda kv: -kv[1])
    return out, info


def to_text_table(raw: bytes) -> bytes:
    """A hand-downloaded file may be the TSV/CSV export or an Excel file."""
    if raw[:2] == b"PK" or raw[:2] == b"\xd0\xcf":
        if raw[:2] == b"PK":
            from .national import read_table
            h, rows = read_table(raw)
            every = [h] + rows
        else:
            import xlrd
            sh = xlrd.open_workbook(file_contents=raw).sheet_by_index(0)
            every = [[str(sh.cell_value(r, c)) for c in range(sh.ncols)] for r in range(sh.nrows)]
        return "\n".join("\t".join(str(c or "").replace("\t", " ") for c in r) for r in every).encode()
    return raw


class FinCENSource(Source):
    name = "fincen"
    label = "US FinCEN MSB registrant list"
    fingerprint = "fincen-v1"

    def manual_file(self, ctx):
        folder = ctx.data_dir / "manual"
        files = sorted(folder.glob("fincen*"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.exists() else []
        return files[0] if files else None

    def fetch(self, ctx: Context) -> list:
        cfg = ctx.source_config("fincen")
        url = cfg.get("url") or URL
        manual = self.manual_file(ctx)
        if manual is not None:
            # downloaded by hand from msb.fincen.gov (the site's robots.txt rules out automatic downloads)
            raw, url = to_text_table(manual.read_bytes()), f"data/manual/{manual.name}"
        elif not robots_allowed(ctx.session, url):
            raise SourceNotReady("msb.fincen.gov's robots.txt doesn't allow automatic downloads; "
                                 "download the MSB list by hand and upload it to data/manual/ (see README)")
        else:
            r = ctx.session.post(url, data={"site": "AA"}, timeout=300)
            r.raise_for_status()
            raw = r.content
        err, info, recs = None, {}, []
        try:
            recs, info = parse_fincen(raw)
        except Exception as e:
            err = e
        write_diagnostics(ctx, self.name, [(url, raw, info, err)])
        if err:
            raise err
        if len(recs) < int(cfg.get("min_records", 5000)):
            raise RuntimeError(f"FinCEN MSB list has only {len(recs)} firms; expected tens of thousands")
        return recs
