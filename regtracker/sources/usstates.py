"""United States: money transmitter licensee lists published by state regulators.

Only states that publish their own list are used (NMLS Consumer Access is not:
its terms forbid automated extraction and use for solicitation). Each state is
its own source (us_fl, us_ca, ...), so one state's site being down never
affects the others, and a new or lost licence in that state raises an alert.

  FL  OFR monthly Excel download (zip), Part II = money transmitters
  CA  DFPI "Directory of Money Transmitters" web page
  MA  Division of Banks Excel list (quarterly)
  CT  Department of Banking Excel list
  LA  OFI licensee table (web page)
  IN  DFI licensee listing (web page)
  DE  State Bank Commissioner PDF (check sellers + money transmitters)
  AK  Division of Banking & Securities PDF roster
"""
from __future__ import annotations

import html as htmlmod
import io
import re
import zipfile
from html.parser import HTMLParser
from urllib.parse import urljoin

from ..http import robots_allowed
from ..model import Record
from .base import Context, Source, SourceNotReady
from .national import match_name, read_table, write_diagnostics

STATES = {
    "fl": {"state": "FL", "regulator": "FL OFR", "format": "fl_zip",
           # the download page is read each run for the current link (the file moved once already)
           "page": "https://flofr.gov/education/public-information/registration-data-download",
           "page_link": r"(?:Money[ _%20-]*Transmitter|/MT/)[^\"']*\.(?:zip|xlsx?|csv)",
           "url": "https://real.flofr.gov/Public/MT/Money_Transmitters_monthly.zip",
           "register_url": "https://flofr.gov/education/public-information/registration-data-download"},
    "ca": {"state": "CA", "regulator": "CA DFPI", "format": "ca_html",
           "url": "https://dfpi.ca.gov/regulated-industries/money-transmitters/directory-of-money-transmitters/"},
    "ma": {"state": "MA", "regulator": "MA DOB", "format": "table", "manual_ext": "xlsx",
           "url": "https://www.mass.gov/doc/money-transmitter-licensee-list/download",
           "register_url": "https://www.mass.gov/lists/download-a-list-of-approved-licensees"},
    "ct": {"state": "CT", "regulator": "CT DOB", "format": "table", "find_link": r"money_transmitters?[^\"']*\.xlsx",
           "url": "https://portal.ct.gov/dob/consumer-credit-licenses/consumer-credit-licenses/money-transmitters-licensed-in-connecticut"},
    "la": {"state": "LA", "regulator": "LA OFI", "format": "html_table",
           "url": "https://ofi.la.gov/non-depository/money-orders-transmitters/licensees/"},
    "in": {"state": "IN", "regulator": "IN DFI", "format": "html_table",
           "url": "https://extranet.dfi.in.gov/ConsumerCredit/CompanyListing/5?handler=CompaniesInLicenseType"},
    "de": {"state": "DE", "regulator": "DE OSBC", "format": "de_pdf",
           "url": "https://banking.delaware.gov/wp-json/wp/v2/media?search=Check-Seller-and-Money-Transmitter&orderby=date&order=desc&per_page=5",
           "register_url": "https://banking.delaware.gov/"},
    "ak": {"state": "AK", "regulator": "AK DBS", "format": "ak_pdf", "manual_ext": "pdf", "find_link": r"MSB[^\"']*Roster[^\"']*\.pdf[^\"']*",
           "url": "https://commerce.alaska.gov/web/dbs/ConsumerFinance/MoneyServiceBusinesses",
           "dated_pdf": "https://www.commerce.alaska.gov/web/Portals/3/pub/MSB%20Roster%20{date}.pdf",
           "known_edition": "20260319"},
}
UA = {"User-Agent": "Mozilla/5.0 (compatible; licence-tracker/1.0)"}
INACTIVE = re.compile(r"expired|surrender|revok|terminat|cancel|withdraw|inactive|closed|denied|suspend|null|void", re.I)
CITY_ST_ZIP = re.compile(r"([A-Za-z][A-Za-z .'-]+?),?\s+([A-Z]{2})\s+(\d{5})(?:-\d{4})?")
LEGAL_END = re.compile(r"^(.*?\b(?:Inc\.?|Incorporated|LLC|L\.L\.C\.|Corp\.?|Corporation|Company|Co\.|Ltd\.?|Limited|"
                       r"N\.A\.|LP|L\.P\.|PLC|Bank|Trust|Association))(?:\s|,|$)(.*)$")


# ---------------------------------------------------------------- helpers

class _Tables(HTMLParser):
    """Collect every <table> as rows of cell text."""

    def __init__(self):
        super().__init__()
        self.tables, self._row, self._cell, self._in = [], None, None, 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
            self._in += 1
        elif tag == "tr" and self._in:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.tables[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._in:
            self._in -= 1

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def html_tables(text: str) -> list:
    p = _Tables()
    p.feed(text)
    return [t for t in p.tables if t]


def html_lines(text: str) -> list:
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|h[1-6]|li|tr|section|article|dd|dt)>", "\n", text)
    text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", text))
    return [re.sub(r"[ \t ]+", " ", l).strip() for l in text.splitlines() if l.strip()]


NAME_HEADS = ("firm name", "entityname", "entity name", "licensee name", "licensee", "legal name", "company name",
              "company", "institution", "name of licensee", "name")


def _col(headers: list, *keys, exclude=()) -> int | None:
    hl = [re.sub(r"\s+", " ", h or "").strip().lower() for h in headers]
    for k in keys:
        for i, h in enumerate(hl):
            if (h == k or k in h) and not any(x in h for x in exclude):
                return i
    return None


def find_header(rows: list) -> int:
    for i, r in enumerate(rows[:25]):
        cells = [str(c or "").strip().lower() for c in r]
        if any(c in NAME_HEADS or c.startswith(("firm name", "licensee name", "entityname", "legal name", "company name"))
               for c in cells):
            return i
    return 0


def city_of(text: str) -> tuple:
    m = None
    for m in CITY_ST_ZIP.finditer(text or ""):
        pass
    if not m:
        return "", ""
    city = re.sub(r"^.*\b(?:suite|ste\.?|floor|fl\.?|unit|#)\s*\S+\s+", "", m.group(1), flags=re.I).strip(" ,")
    city = re.split(r"\s{2,}|\d+\s", city)[-1].strip(" ,")
    return city.title() if city.isupper() else city, m.group(2)


def rows_to_records(code: str, headers: list, rows: list, spec: dict) -> tuple:
    c = {
        "name": _col(headers, *NAME_HEADS, exclude=("dba", "doing business", "trade", "officer", "contact")),
        "dba": _col(headers, "dba", "doing business", "trade name"),
        "lic": _col(headers, "license number", "license no", "license #", "licence number", "file/license",
                    "license id", "lic #", "lic. no", "license", exclude=("type", "status", "date", "expir")),
        "nmls": _col(headers, "nmls"),
        "status": _col(headers, "status"),
        "type": _col(headers, "license type", "licence type", "type"),
        "addr": _col(headers, "primary address", "address line 1", "addressline1", "street", "address"),
        "city": _col(headers, "city", exclude=("state, zip",)),
        "csz": _col(headers, "city, state", "city/state"),
        "state": _col(headers, "statecode", "state", exclude=("city",)),
        "phone": _col(headers, "phone", "telephone"),
        "area": _col(headers, "areacode"), "prefix": _col(headers, "prefix"), "number": _col(headers, "number",
                                                                                          exclude=("license", "nmls", "phone", "file")),
        "date": _col(headers, "initial approval", "approval date", "issue date", "original", "effective"),
    }
    info = {"headers": headers, "rows": len(rows), "columns": {k: (headers[v] if v is not None else None) for k, v in c.items()}}
    if c["name"] is None:
        raise RuntimeError(f"{code.upper()}: no firm-name column in {headers}")

    def g(row, k):
        i = c[k]
        return str(row[i]).strip() if i is not None and i < len(row) and row[i] is not None else ""

    out, types = {}, {}
    for row in rows:
        name = g(row, "name")
        if not name or name.lower() in NAME_HEADS:
            continue
        typ = g(row, "type")
        types[typ] = types.get(typ, 0) + 1
        if spec.get("type_keep") and typ and not re.search(spec["type_keep"], typ, re.I):
            continue
        status = g(row, "status")
        phone = g(row, "phone") or "-".join(x for x in (g(row, "area"), g(row, "prefix"), g(row, "number")) if x)
        phone = re.sub(r"^call\s+", "", phone, flags=re.I)
        city = g(row, "city")
        st = g(row, "state")
        if not city and g(row, "csz"):
            city, st2 = city_of(g(row, "csz"))
            st = st or st2
            if not city:
                city = g(row, "csz").split(",")[0].strip()
        if not city and g(row, "addr"):
            city, st2 = city_of(g(row, "addr"))
            st = st or st2
        lic = g(row, "lic")
        rec = make_record(code, spec, name, lic, status, city, phone,
                          {"dba": g(row, "dba"), "address": g(row, "addr"), "hq_state": st, "nmls_id": g(row, "nmls"),
                           "licence_type_code": typ}, g(row, "date"))
        out[rec.key] = rec
    info["type_values"] = sorted(types.items(), key=lambda kv: -kv[1])[:10]
    info["kept"] = len(out)
    return list(out.values()), info


def make_record(code, spec, name, lic, status, city, phone, extra, date="") -> Record:
    st = spec["state"]
    ident = re.sub(r"\s+", "", lic) or match_name(name)
    active = not (status and INACTIVE.search(status))
    from .fincen import us_date
    return Record(
        key=f"us_{code}:{ident}", source=f"us_{code}", jurisdiction="US", regulator=spec["regulator"],
        name=re.sub(r"\s+", " ", name).strip(), licence_type=f"Money transmitter licence ({st})",
        active=active, status_detail=status or "Licensed (on the state's published list)",
        status_date=us_date(date) if date else "", reference=lic, city=city,
        register_url=spec.get("register_url") or spec["url"],
        extra={**{k: v for k, v in extra.items() if v}, "phone": phone, "licence_state": st},
    )


# ---------------------------------------------------------------- per-format parsers

def parse_table_file(code, raw: bytes, spec) -> tuple:
    if raw[:2] == b"\xd0\xcf":  # old .xls
        import xlrd
        book = xlrd.open_workbook(file_contents=raw)
        sh = book.sheet_by_index(0)
        every = [[str(sh.cell_value(r, c)) for c in range(sh.ncols)] for r in range(sh.nrows)]
    else:
        h, rows = read_table(raw)
        every = [h] + rows
    i = find_header(every)
    return rows_to_records(code, [str(x or "").strip() for x in every[i]], every[i + 1:], spec)


def parse_fl_zip(code, raw: bytes, spec) -> tuple:
    z = zipfile.ZipFile(io.BytesIO(raw))
    names = [n for n in z.namelist() if not n.endswith("/")]
    if not names:
        raise RuntimeError("Florida zip is empty")
    inner = max(names, key=lambda n: z.getinfo(n).file_size)
    recs, info = parse_table_file(code, z.read(inner), {**spec, "type_keep": spec.get("type_keep", r"FT2|part\s*ii\b|transmit")})
    info["inner_file"] = inner
    return recs, info


def parse_ca_html(code, raw: bytes, spec) -> tuple:
    lines = html_lines(raw.decode("utf-8", "replace"))
    out, info = {}, {"headers": ["(CA directory blocks)"], "rows": 0, "columns": {}}
    for i, line in enumerate(lines):
        m = re.match(r"License\s*#\s*:?\s*(\S+)", line, re.I)
        if not m:
            continue
        info["rows"] += 1
        block = [lines[i]]
        for b in lines[i + 1:i + 14]:
            if re.match(r"License\s*#", b, re.I):
                break
            block.append(b)
        if len(block) > 2 and not re.match(r"(DFPI Contact|Phone|Address|Officer|Institution)", block[-1], re.I):
            block = block[:-1]  # the next firm's heading
        name = ""
        for prev in reversed(lines[max(0, i - 4):i]):
            if not re.match(r"(DFPI Contact|Phone|Address|Officer Name|Institution Name)\b", prev, re.I):
                name = prev
                break
        lab = {}
        for b in block[1:]:
            if re.match(r"License\s*#", b, re.I):
                break
            mm = re.match(r"(Institution Name|Officer Name|Phone|DFPI Contact)\s*:\s*(.*)", b, re.I)
            if mm:
                lab[mm.group(1).lower()] = mm.group(2).strip()
        name = lab.get("institution name") or name
        addr = " ".join(b for b in block if CITY_ST_ZIP.search(b))
        city, st = city_of(addr)
        rec = make_record(code, spec, name, m.group(1), "", city, lab.get("phone", ""),
                          {"officer": lab.get("officer name", ""), "hq_state": st})
        out[rec.key] = rec
    info["kept"] = len(out)
    return list(out.values()), info


def parse_html_table(code, raw: bytes, spec) -> tuple:
    tables = html_tables(raw.decode("utf-8", "replace"))
    best = None
    for t in tables:
        i = find_header(t)
        if i < len(t) and _col(t[i], *NAME_HEADS) is not None and (best is None or len(t) > len(best[0])):
            best = (t, i)
    if not best:
        raise RuntimeError(f"{code.upper()}: no licensee table found on the page ({len(tables)} tables)")
    t, i = best
    rows = [r for r in t[i + 1:] if r != t[i]]
    return rows_to_records(code, t[i], rows, spec)


def pdf_lines(raw: bytes) -> list:
    import pdfplumber
    out = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for page in pdf.pages:
            out += [l.strip() for l in (page.extract_text() or "").splitlines() if l.strip()]
    return out


DE_SKIP = re.compile(r"^(State of Delaware|OFFICE OF THE STATE BANK|Licensees and Existing|Check Seller|Page \d|"
                     r"Filing Count|Branches|Locations|License|Report)", re.I)


def parse_de_pdf(code, raw: bytes, spec) -> tuple:
    lines = pdf_lines(raw)
    out, info = {}, {"headers": lines[:3], "rows": 0, "columns": {}, "first_text_lines": lines[:40]}
    starts = [i for i, l in enumerate(lines)
              if re.match(r"^\d{4,7}\s+\S", l) and not CITY_ST_ZIP.search(l)
              and not re.match(r"^\d{4,7}\s+(Branches|Locations|Filing|Contact)", l, re.I)]
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        chunk = lines[i:end]
        text = " ".join(chunk)
        ms = re.search(r"Filing Status:\s*(.*?)\s*(?:Expires Date:\s*(\S+)|$)", text)
        if not ms:
            continue  # a branch line or page furniture
        info["rows"] += 1
        m = re.match(r"^(\d{4,7})\s+(.*)$", chunk[0])
        head = m.group(2)
        head = re.split(r"\s+(?=\d+\s+\w)", head, maxsplit=1)[0] if CITY_ST_ZIP.search(head) else head
        name = re.sub(r"\s+D/B/A\s+.*$", "", head, flags=re.I).strip()
        dba = (re.search(r"D/B/A\s+(.*)$", head, re.I) or [None, ""])[1]
        rest = text[len(chunk[0]):]
        addr = re.split(r"Contact:|Filing Status:", rest)[0].strip()
        mc = re.search(r"Contact:\s*(.*?)\s*-\s*(\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4})", text)
        city, st = city_of(addr)
        rec = make_record(code, spec, name, m.group(1), ms.group(1).strip(), city, mc.group(2) if mc else "",
                          {"address": addr, "hq_state": st, "expires": ms.group(2) or "", "dba": dba})
        rec.licence_type = "Check seller / money transmitter licence (DE)"
        out[rec.key] = rec
    info["kept"] = len(out)
    return list(out.values()), info


def ak_rows(pages: list) -> list:
    """pages: [[{text, x0, top}, ...], ...] (pdfplumber words). Columns are found from the header
    ("Filing Description", "DBA / Trade Name", "Business/Physical Address", "Filing Status",
    "File/License #"); a row starts where the licence-number column has a number, and the next
    lines up to the following row continue its name, trade name and address."""
    rows = []
    for words in pages:
        hdr = {}
        for w in words:
            t = w["text"]
            if t.startswith("DBA") and "dba" not in hdr:
                hdr["dba"], hdr["top"] = w["x0"], w["top"]
            elif t.startswith("Business/Physical"):
                hdr["addr"] = w["x0"]
            elif t.startswith("File/License"):
                hdr["lic"] = w["x0"]
            elif t == "Status" and "status" not in hdr:
                hdr["status_end"] = w["x0"]
        status_x = None
        for w in words:
            if w["text"] == "Filing" and abs(w["top"] - hdr.get("top", -99)) < 4 and w["x0"] > hdr.get("addr", 1e9):
                status_x = w["x0"]
        if not all(k in hdr for k in ("dba", "addr", "lic", "top")) or status_x is None:
            continue
        lines = {}
        for w in words:
            if w["top"] <= hdr["top"] + 4:
                continue
            lines.setdefault(round(w["top"] / 3), []).append(w)
        cur = None
        for key in sorted(lines):
            ws = sorted(lines[key], key=lambda w: w["x0"])
            text = " ".join(w["text"] for w in ws)
            if re.match(r"^Page \d+ of \d+$", text) or text.strip() == "End of Listing":
                continue
            ws = [w for w in ws if not re.match(r"^(End|of|Listing)$", w["text"]) or "End of Listing" not in text]
            cols = {"name": [], "dba": [], "addr": [], "status": [], "lic": []}
            for w in ws:
                x = w["x0"]
                # values sit a little left of their column headings: allow a margin
                col = ("lic" if x >= hdr["lic"] - 12 else "status" if x >= status_x - 12 else
                       "addr" if x >= hdr["addr"] - 12 else "dba" if x >= hdr["dba"] - 12 else "name")
                cols[col].append(w["text"])
            c = {k: " ".join(v) for k, v in cols.items()}
            if re.fullmatch(r"\d{3,8}", c["lic"]):
                cur = {"name": c["name"], "dba": c["dba"], "addr": [c["addr"]] if c["addr"] else [],
                       "status": c["status"], "lic": c["lic"]}
                rows.append(cur)
            elif cur is not None:
                if c["name"]:
                    cur["name"] += " " + c["name"]
                if c["dba"]:
                    cur["dba"] = (cur["dba"] + " " + c["dba"]).strip()
                if c["addr"]:
                    cur["addr"].append(c["addr"])
    return rows


def parse_ak_pdf(code, raw: bytes, spec) -> tuple:
    import io
    import pdfplumber
    pages = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_words(keep_blank_chars=False, use_text_flow=False))
    rows = ak_rows(pages)
    out, info = {}, {"headers": [], "rows": len(rows), "columns": {},
                     "first_text_lines": [" | ".join([r["name"], r["dba"], " / ".join(r["addr"]), r["status"], r["lic"]])
                                          for r in rows[:40]]}
    for r in rows:
        name = re.sub(r"\s+", " ", r["name"]).strip()
        if not name:
            continue
        city, st = city_of(r["addr"][-1] if r["addr"] else "")
        rec = make_record(code, spec, name, r["lic"], r["status"], city, "",
                          {"address": ", ".join(r["addr"]), "hq_state": st, "dba": r["dba"]})
        prev = out.get(rec.key)
        if prev and r["dba"] and r["dba"] not in (prev.extra.get("dba") or ""):
            prev.extra["dba"] = "; ".join(x for x in (prev.extra.get("dba"), r["dba"]) if x)
            continue
        out[rec.key] = rec
    info["kept"] = len(out)
    return list(out.values()), info


PARSERS = {"fl_zip": parse_fl_zip, "ca_html": parse_ca_html, "table": parse_table_file,
           "html_table": parse_html_table, "de_pdf": parse_de_pdf, "ak_pdf": parse_ak_pdf}


# ---------------------------------------------------------------- source

def manual_file(ctx, name: str):
    """A file you downloaded yourself into data/manual/ (newest one for this source)."""
    d = ctx.data_dir / "manual"
    if not d.exists():
        return None
    files = sorted((p for p in d.iterdir() if p.is_file() and p.stem.lower() == name.lower()),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


class USStateSource(Source):
    def __init__(self, code: str, spec: dict):
        self.code = code
        self.spec = spec
        self.name = f"us_{code}"
        self.label = f"US {spec['state']} money transmitter licensees ({spec['regulator']})"
        self.fingerprint = f"usstate-v1:{spec['format']}" + ("-v3" if spec["format"] == "ak_pdf" else "")

    def _get(self, ctx, url):
        if not robots_allowed(ctx.session, url):
            raise SourceNotReady(f"{url} is disallowed by robots.txt")
        r = ctx.session.get(url, timeout=120, headers=UA)
        r.raise_for_status()   # a site that refuses the tracker (403) is respected, never worked around
        return r

    def resolve_url(self, ctx) -> str:
        spec = self.spec
        if spec["format"] == "de_pdf":
            items = self._get(ctx, spec["url"]).json()
            pdfs = sorted((i for i in items if str(i.get("source_url", "")).lower().endswith(".pdf")),
                          key=lambda i: i.get("date", ""), reverse=True)
            if not pdfs:
                raise RuntimeError("Delaware: no licensee PDF found in the site's media list")
            return pdfs[0]["source_url"]
        if spec.get("page"):
            try:
                page = self._get(ctx, spec["page"]).text
                links = re.findall(r"""href=["']([^"']*?""" + spec["page_link"] + r"""[^"']*)["']""", page, re.I)
            except Exception as e:
                ctx.log.info("%s: could not read %s (%s); using the last known file address", self.name, spec["page"], e)
                links = []
            if links:
                monthly = [l for l in links if re.search(r"month", l, re.I)] or links
                return urljoin(spec["page"], htmlmod.unescape(monthly[0]))
            try:  # no match: save every download link on the page, so the pattern can be fixed in one go
                found = re.findall(r"""href=["']([^"']+\.(?:zip|xlsx?|csv|txt)[^"']*)["']""", page, re.I)
                (ctx.data_dir / "debug").mkdir(parents=True, exist_ok=True)
                (ctx.data_dir / "debug" / f"{self.name}_page_links.txt").write_text("\n".join(found) or "(no download links)")
            except Exception:
                pass
            return spec["url"]
        if spec.get("find_link"):
            page = self._get(ctx, spec["url"]).text
            m = re.search(r"""href=["']([^"']*?""" + spec["find_link"] + r"""[^"']*)["']""", page, re.I)
            if not m:
                raise RuntimeError(f"{spec['state']}: download link not found on {spec['url']}")
            return urljoin(spec["url"], htmlmod.unescape(m.group(1)))
        return spec["url"]

    def _missing(self, ctx, url: str = "", clear: bool = False):
        """The regulator's own link points at a file that isn't there (404). Usually temporary (a file
        being regenerated), so the last list is kept and the run isn't failed; after 7 days it becomes a
        warning so a permanent move doesn't go unnoticed."""
        import datetime as dt
        import json
        p = ctx.data_dir / "state" / f"{self.name}_missing.json"
        if clear:
            if p.exists():
                p.unlink()
            return None
        st = json.loads(p.read_text()) if p.exists() else {}
        since = st.get("since") or ctx.today
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"since": since, "url": url}))
        days = (dt.date.fromisoformat(ctx.today) - dt.date.fromisoformat(since)).days
        err = SourceNotReady(f"the {self.spec['state']} regulator's download link is broken on their side "
                             f"(file not found since {since}: {url}); keeping the last list")
        err.quiet = days < 7
        return err

    def _dated_pdf(self, ctx, spec) -> tuple:
        """Alaska: the roster PDF is named after its edition date ("MSB Roster 20260319.pdf") and its
        list page refuses GitHub's servers. Fetch the newest known edition directly; once a day, also
        look for a newer edition dated in the last 7 days. If the site refuses the file itself (403),
        that is recorded and the tracker stops asking."""
        import datetime as dt
        import json
        state_p = ctx.data_dir / "state" / f"{self.name}_editions.json"
        st = json.loads(state_p.read_text()) if state_p.exists() else {}
        if st.get("refused"):
            err = SourceNotReady(f"off: the {spec['state']} site refused the roster PDF on {st['refused']} "
                                 "(its firms still come through the FinCEN list)")
            err.quiet = True
            raise err
        today = dt.date.fromisoformat(ctx.today)
        known = st.get("edition") or spec["known_edition"]
        tries = [known]
        if st.get("probed") != ctx.today:
            tries = [(today - dt.timedelta(days=d)).strftime("%Y%m%d") for d in range(0, 8)
                     if (today - dt.timedelta(days=d)).strftime("%Y%m%d") > known] + tries
        found, raw = "", b""
        for date in tries:
            url = spec["dated_pdf"].format(date=date)
            if not robots_allowed(ctx.session, url):
                raise SourceNotReady(f"{url} is disallowed by robots.txt")
            r = ctx.session.get(url, timeout=120, headers=UA)
            if r.status_code == 403:
                st["refused"] = ctx.today
                state_p.parent.mkdir(parents=True, exist_ok=True)
                state_p.write_text(json.dumps(st))
                err = SourceNotReady(f"off: the {spec['state']} site refuses GitHub's servers (HTTP 403); "
                                     "its firms still come through the FinCEN list")
                err.quiet = True
                raise err
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                found, raw = date, r.content
                break
        st["probed"] = ctx.today
        if found:
            st["edition"] = found
        state_p.parent.mkdir(parents=True, exist_ok=True)
        state_p.write_text(json.dumps(st))
        if not found:
            raise RuntimeError(f"{spec['state']} roster not found (tried editions {', '.join(tries)})")
        ctx.log.info("%s: roster edition %s", self.name, found)
        return spec["dated_pdf"].format(date=found), raw

    def fetch(self, ctx: Context) -> list:
        overrides = ((ctx.source_config("us_states") or {}).get("overrides") or {}).get(self.code) or {}
        spec = {**self.spec, **overrides}
        self.spec = spec
        manual = manual_file(ctx, self.name)
        if manual and spec.get("manual_only"):
            url, raw = str(manual), manual.read_bytes()
        elif spec.get("dated_pdf"):
            url, raw = self._dated_pdf(ctx, spec)
        else:
            try:
                url = self.resolve_url(ctx)
                raw = self._get(ctx, url).content
                self._missing(ctx, clear=True)
            except Exception as e:
                if "404" in str(e) or "410" in str(e):
                    raise self._missing(ctx, url=locals().get("url", spec.get("url")))
                blocked = "403" in str(e) or isinstance(e, SourceNotReady)
                if not blocked:
                    raise
                if not manual:
                    err = SourceNotReady(
                        f"the {spec['state']} site blocks automated downloads; download the list yourself from "
                        f"{spec.get('register_url') or spec['url']} and upload it to data/manual/ as {self.name}"
                        f".{spec.get('manual_ext', 'xlsx')}")
                    err.quiet = True   # an expected, permanent block: logged as information, not a warning
                    raise err
                else:
                    url, raw = str(manual), manual.read_bytes()
        err, info, recs = None, {}, []
        try:
            recs, info = PARSERS[spec["format"]](self.code, raw, spec)
        except Exception as e:
            err = e
        write_diagnostics(ctx, self.name, [(url, raw, info, err)])
        if err:
            raise err
        if not recs:
            raise RuntimeError(f"{spec['state']} list parsed but no licensees found; see data/debug/{self.name}_sample.txt")
        if spec["format"] in ("de_pdf", "ak_pdf"):
            for r in recs:
                r.register_url = url  # the PDF itself (its address changes with each edition)
        return recs


def us_state_sources(cfg: dict) -> dict:
    """Enabled by `sources: us_states: {enabled: true}` in config.yaml;
    individual states can be switched off with `skip: [de, ak]`."""
    conf = (cfg.get("sources") or {}).get("us_states") or {}
    if not conf or conf.get("enabled", True) is False:
        return {}
    skip = {str(x).lower() for x in conf.get("skip") or []}
    return {f"us_{code}": USStateSource(code, spec) for code, spec in STATES.items() if code not in skip}
