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
           "url": "https://real.flofr.com/Public/MT/Money_Transmitters_monthly.zip",
           "register_url": "https://flofr.gov/education/public-information/registration-data-download"},
    "ca": {"state": "CA", "regulator": "CA DFPI", "format": "ca_html",
           "url": "https://dfpi.ca.gov/regulated-industries/money-transmitters/directory-of-money-transmitters/"},
    "ma": {"state": "MA", "regulator": "MA DOB", "format": "table",
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
    "ak": {"state": "AK", "regulator": "AK DBS", "format": "ak_pdf", "find_link": r"MSB[^\"']*Roster[^\"']*\.pdf[^\"']*",
           "url": "https://commerce.alaska.gov/web/dbs/ConsumerFinance/MoneyServiceBusinesses"},
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


def parse_de_pdf(code, raw: bytes, spec) -> tuple:
    lines = pdf_lines(raw)
    out, info = {}, {"headers": lines[:3], "rows": 0, "columns": {}}
    for i, line in enumerate(lines):
        m = re.match(r"^(\d{4,7})\s+(\S.*)$", line)
        if not m or re.match(r"^\d{4,7}\s+(Branches|Locations)", line, re.I):
            continue
        nxt = lines[i + 1:i + 6]
        if CITY_ST_ZIP.search(line) or not any(l.startswith(("Contact:", "Filing Status:")) for l in nxt[:2]):
            continue  # an address, a branch line or page furniture
        info["rows"] += 1
        addr = nxt[0] if nxt and not nxt[0].startswith(("Contact:", "Filing")) else ""
        phone = status = expires = ""
        for l in nxt:
            mc = re.match(r"Contact:\s*(.*?)\s*-\s*(\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4})", l)
            if mc:
                phone = mc.group(2)
            ms = re.match(r"Filing Status:\s*(.*?)\s*(?:Expires Date:\s*(\S+))?$", l)
            if ms:
                status, expires = ms.group(1), ms.group(2) or ""
                break
        city, st = city_of(addr)
        name = re.sub(r"\s+D/B/A\s+.*$", "", m.group(2), flags=re.I)
        rec = make_record(code, spec, name, m.group(1), status, city, phone,
                          {"address": addr, "hq_state": st, "expires": expires,
                           "dba": (re.search(r"D/B/A\s+(.*)$", m.group(2), re.I) or [None, ""])[1]})
        rec.licence_type = "Check seller / money transmitter licence (DE)"
        out[rec.key] = rec
    info["kept"] = len(out)
    return list(out.values()), info


def parse_ak_pdf(code, raw: bytes, spec) -> tuple:
    lines = pdf_lines(raw)
    out, info = {}, {"headers": lines[:4], "rows": 0, "columns": {}}
    pat = re.compile(r"^(?P<rest>.+?)\s+(?P<status>Approved(?:[- ][A-Za-z]+)*|Suspended|Revoked|Inactive|Expired|"
                     r"Surrendered|Terminated|Pending\S*)\s+(?P<lic>\d{3,8})$")
    for line in lines:
        m = pat.match(line)
        if not m:
            continue
        info["rows"] += 1
        rest = m.group("rest")
        lm = LEGAL_END.match(rest)
        if lm:
            name, addr = lm.group(1), lm.group(2)
        else:
            parts = re.split(r"\s(?=\d+\s)", rest, maxsplit=1)
            name, addr = parts[0], (parts[1] if len(parts) > 1 else "")
        city, st = city_of(addr)
        rec = make_record(code, spec, name, m.group("lic"), m.group("status"), city, "",
                          {"address": addr, "hq_state": st})
        out[rec.key] = rec
    info["kept"] = len(out)
    return list(out.values()), info


PARSERS = {"fl_zip": parse_fl_zip, "ca_html": parse_ca_html, "table": parse_table_file,
           "html_table": parse_html_table, "de_pdf": parse_de_pdf, "ak_pdf": parse_ak_pdf}


# ---------------------------------------------------------------- source

class USStateSource(Source):
    def __init__(self, code: str, spec: dict):
        self.code = code
        self.spec = spec
        self.name = f"us_{code}"
        self.label = f"US {spec['state']} money transmitter licensees ({spec['regulator']})"
        self.fingerprint = f"usstate-v1:{spec['format']}"

    def _get(self, ctx, url):
        if not robots_allowed(ctx.session, url):
            raise SourceNotReady(f"{url} is disallowed by robots.txt")
        r = ctx.session.get(url, timeout=120, headers=UA)
        r.raise_for_status()
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
        if spec.get("find_link"):
            page = self._get(ctx, spec["url"]).text
            m = re.search(r"""href=["']([^"']*?""" + spec["find_link"] + r""")["']""", page, re.I)
            if not m:
                raise RuntimeError(f"{spec['state']}: download link not found on {spec['url']}")
            return urljoin(spec["url"], htmlmod.unescape(m.group(1)))
        return spec["url"]

    def fetch(self, ctx: Context) -> list:
        overrides = ((ctx.source_config("us_states") or {}).get("overrides") or {}).get(self.code) or {}
        spec = {**self.spec, **overrides}
        self.spec = spec
        url = self.resolve_url(ctx)
        raw = self._get(ctx, url).content
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
