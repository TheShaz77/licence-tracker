"""Direct national registers, configured in config.yaml under `national:`.

Each country entry lists one or more downloadable files (CSV or Excel) and
how to read them. The same tolerant reader handles them all, so adding a
country whose regulator offers a download is usually a config change:

  national:
    NL:
      regulator: DNB
      files:
        - url: https://www.dnb.nl/en-GB/registerdownloadcomplete/TheNetherlands/csv
          # licence type from a column: first matching pattern wins
          type_patterns:
            "electronic money|elektronisch geld": E-money institution
            "payment (service|institution)|betaalinstelling|betaaldienst": Payment institution
    LT:
      regulator: LB
      files:
        - url: https://www.lb.lt/en/sfi-financial-market-participants?market=1&type=6&export=csv
          licence_type: Payment institution      # every row in this file

Every run also saves a short sample of each file to data/debug/ so the reader
can be checked against the real layout.
"""
from __future__ import annotations

import csv
import gzip
import io
import re
import unicodedata

from ..model import Record, norm_key
from .base import Context, Source

NAME_COLS = ["name", "title", "statutory name", "statutaire naam", "organisation name",
             "organisation", "institution", "entity name", "company name", "naam", "pavadinimas",
             "ENT_NAM", "entityName", "nazov", "obchodne meno", "obchodné meno", "nazovSubjektu",
             "trade name", "name of licensee", "licensee name", "licensee", "operator", "company",
             "name of company", "name of institution", "institution name", "firm name", "legal name",
             "nombre", "razon social", "razón social", "denominacion", "denominación", "entidad", "nombre de la entidad",
             "denominación social", "dénomination", "denomination", "raison sociale", "nom", "nom de l'établissement",
             "établissement", "etablissement", "nome", "razão social", "instituição", "naziv", "emri", "emri i subjektit",
             "unvan", "kuruluş unvanı", "kurulus unvani", "kuruluş adı", "kurulus adi", "ünvanı", "nama", "nama perusahaan", "nama penyelenggara",
             "name of entity", "entity", "name of the entity", "name of bank", "bank name", "bank", "provider",
             "service provider", "name of provider", "company name", "name of licensee (english)", "vasp name",
             "name of the company", "name of institution", "registered name", "licensed entity", "licence holder",
             "license holder", "名称及其他", "机构名称", "公司名称", "商号", "名称", "商号又は名称", "商号等", "業者名", "登録業者名", "氏名又は名称", "商号・名称"]
TYPE_COLS = ["register", "register name", "type", "licence type", "license type", "category",
             "authorisation", "authorization", "vergunning", "registertype", "activity",
             "business form", "rūšis", "EntityType", "entity type", "typ", "typSubjektu", "kodTypu",
             "typ subjektu", "type code"]
ID_COLS = ["relation number", "relatienummer", "registration number", "register number",
           "reference", "reference number", "code", "company code", "kvk", "kvk number",
           "chamber of commerce number", "national id", "id", "uid", "kodas", "ENT_NAT_REF_COD",
           "EntityCode", "ico", "ičo", "licence no", "licence number", "license no", "license number",
           "registration no", "ce reference", "登録番号"]
LEI_COLS = ["lei", "lei code", "lei-code", "ENT_COD_LEI", "ENT_LEI"]
WEB_COLS = ["website", "web site", "web", "url", "website address", "homepage", "ホームページ"]
EMAIL_COLS = ["email", "e-mail", "email address"]
PHONE_COLS = ["phone", "telephone", "phone number", "telephone number", "tel", "tel no", "電話番号", "telefono", "teléfono"]
ADDRESS_COLS = ["address", "local address", "local_address", "head office", "business address",
                "registered address", "本店所在地", "所在地", "本店等所在地", "主たる営業所の所在地"]
CITY_COLS = ["city", "town", "place", "localidad", "ciudad", "municipio", "ville", "plaats", "vestigingsplaats", "miestas", "municipality",
             "ENT_TOW_CIT_RES", "obec", "mesto"]
END_COLS = ["valid till", "valid to", "valid until", "end date", "expiry date", "withdrawn", "date of withdrawal"]
DATE_COLS = ["valid from", "start date", "registration date", "date of registration", "licence date",
             "date", "ingangsdatum", "datum", "effective date", "ENT_AUT", "datumVzniku", "effective_date",
           "date of licence", "date of registration", "registration date", "登録年月日",
           "date of submission of licence application", "licence issued", "license issued", "issuance date",
           "date of issue", "date of authorisation", "date of authorization", "fecha de autorización",
           "fecha de inscripción", "fecha de registro", "date d'agrément", "date d'agrement", "data de autorização"]
STATUS_COLS = ["status", "state", "stav", "ENT_WDR", "ENT_DAT_WDR"]
INACTIVE_RE = re.compile(r"withdrawn|revoked|cancel|expired|terminated|lapsed|inactive|ingetrokken|"
                         r"panaikint|atšaukt|beëindigd", re.I)


def decode_bytes(raw: bytes) -> str:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw[:1000].count(b"\x00") > 100:
        return raw.decode("utf-16-le", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    # mostly UTF-8 with a few bad bytes: keep UTF-8 rather than mangling every accent
    utf = raw.decode("utf-8-sig", errors="replace")
    if utf.count("\ufffd") <= max(20, len(raw) // 20000):
        return utf
    return raw.decode("cp1252", errors="replace")


def _flatten(d: dict, prefix: str = "") -> dict:
    """One flat dict per record. EBA-style 'Properties' lists are merged in."""
    out = {}
    for k, v in d.items():
        if k == "Properties" and isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    for pk, pv in item.items():
                        out[pk] = pv[0] if isinstance(pv, list) and pv else pv
            continue
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        elif isinstance(v, list):
            out[key] = v[0] if v and not isinstance(v[0], (dict, list)) and len(v) == 1 else (
                "; ".join(str(x) for x in v if not isinstance(x, (dict, list))))
        else:
            out[key] = "" if v is None else v
    return out


def _records_in_json(doc):
    """Find the largest list of objects anywhere in a JSON document."""
    best = []
    stack = [doc]
    while stack:
        x = stack.pop()
        if isinstance(x, list):
            dicts = [y for y in x if isinstance(y, dict)]
            if len(dicts) > len(best):
                best = dicts
            stack.extend(y for y in x if isinstance(y, (list, dict)))
        elif isinstance(x, dict):
            stack.extend(v for v in x.values() if isinstance(v, (list, dict)))
    return best


def read_table(raw: bytes, sheets: str = "first") -> tuple:
    """Return (headers, rows) from CSV/TSV text, an .xlsx/.xls file or JSON.
    sheets="all" reads every worksheet (each with its own header row)."""
    text0 = raw.lstrip()[:1]
    if text0 in (b"{", b"["):
        import json
        doc = json.loads(decode_bytes(raw))
        recs = [_flatten(r) for r in _records_in_json(doc)]
        headers = []
        for r in recs:
            for k in r:
                if k not in headers:
                    headers.append(k)
        rows = [[str(r.get(h, "")).strip() for h in headers] for r in recs]
        return headers, rows
    if raw[:2] == b"PK" or raw[:4] == b"\xd0\xcf\x11\xe0":  # xlsx / old xls
        books = []
        if raw[:2] == b"PK":
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            for ws in (wb.worksheets if sheets == "all" else wb.worksheets[:1]):
                books.append([[_cell(c) for c in r] for r in ws.iter_rows(values_only=True)])
        else:
            import xlrd
            wb = xlrd.open_workbook(file_contents=raw)
            for sh in (wb.sheets() if sheets == "all" else wb.sheets()[:1]):
                books.append([[_cell(c) for c in sh.row_values(i)] for i in range(sh.nrows)])
        if len(books) > 1:
            heads, allrows = None, []
            for b in books:
                h, r = _header_split([x for x in b if any(x)])
                if heads is None:
                    heads = h
                allrows += [_align(row, h, heads) for row in r]
            return heads or [], allrows
        rows = books[0] if books else []
    else:
        text = decode_bytes(raw).replace("\r\n", "\n").replace("\r", "\n")
        first = next((l for l in text.split("\n", 50) if l.strip()), "")
        delim = max(";\t,|", key=first.count)
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter=delim)]
    rows = [r for r in rows if any(c for c in r)]
    return _header_split(rows)


def _cell(c) -> str:
    if c is None:
        return ""
    if isinstance(c, float) and c.is_integer():
        return str(int(c))
    if hasattr(c, "isoformat"):
        return c.isoformat()[:10]
    return re.sub(r"\s+", " ", str(c)).strip()


def _align(row: list, h: list, heads: list) -> list:
    """A row from a sheet with headers h, re-ordered to the first sheet's headers."""
    pos = {hkey(x): i for i, x in enumerate(h)}
    return [row[pos[hkey(x)]] if hkey(x) in pos and pos[hkey(x)] < len(row) else "" for x in heads]


def _header_split(rows: list, extra_names: tuple = ()) -> tuple:
    """header = first row that names a known name column (skips title lines)."""
    names = {hkey(n) for n in list(NAME_COLS) + list(extra_names)}
    for i, r in enumerate(rows[:25]):
        if any(hkey(c) in names for c in r):
            return r, rows[i + 1:]
    return (rows[0], rows[1:]) if rows else ([], [])


def read_pdf_table(raw: bytes, spec: dict) -> tuple:
    """Tables in a PDF, all pages joined; repeated header rows on later pages are dropped."""
    import pdfplumber
    allrows = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for settings in (None, {"vertical_strategy": "text", "horizontal_strategy": "text"}):
            # ruled tables first; tables without grid lines are found from text alignment
            for page in pdf.pages:
                for t in (page.extract_tables(settings) if settings else page.extract_tables()) or []:
                    for r in t:
                        cells = [re.sub(r"\s+", " ", c or "").strip() for c in r]
                        if any(cells):
                            allrows.append(cells)
            if allrows:
                break
    extra = (spec["name_column"],) if spec.get("name_column") else ()
    h, rows = _header_split(allrows, extra)
    hk = [hkey(x) for x in h]
    rows = [r for r in rows if [hkey(x) for x in r] != hk]
    width = len(h)
    rows = [r + [""] * (width - len(r)) if len(r) < width else r for r in rows]
    return h, rows


def page_text_lines(raw: bytes) -> list:
    """Text lines of a PDF or an HTML page."""
    if raw[:4] == b"%PDF":
        import pdfplumber
        out = []
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for page in pdf.pages:
                out += [l.strip() for l in (page.extract_text() or "").splitlines() if l.strip()]
        return out
    import html as htmlmod
    text = decode_bytes(raw)
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d|td|th|dd|dt)>", "\n", text)
    text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", text))
    return [re.sub(r"[ \t\xa0]+", " ", l).strip() for l in text.split("\n") if l.strip()]


def read_text_regex(raw: bytes, spec: dict) -> tuple:
    """Rows from page/PDF text with a regex that has named groups (name, and optionally id,
    date, city, address, type, website, email, phone). Optional 'between': [start, end]
    regexes cut the text to the part that holds the list."""
    if spec.get("raw_html"):          # match against the page's HTML itself (cards/lists that aren't tables)
        import html as htmlmod
        rx = re.compile(spec["row_regex"], re.I | re.S)
        groups = list(rx.groupindex)
        names = {"id": "reference"}
        rows = [[re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", m.group(g) or ""))).strip()
                 for g in groups] for m in rx.finditer(decode_bytes(raw))]
        return [names.get(g, g) for g in groups], rows
    lines = page_text_lines(raw)
    if spec.get("between"):
        start, end = spec["between"]
        i = next((k for k, l in enumerate(lines) if re.search(start, l, re.I)), 0)
        j = next((k for k, l in enumerate(lines[i + 1:], i + 1) if end and re.search(end, l, re.I)), len(lines))
        lines = lines[i:j]
    rx = re.compile(spec["row_regex"], re.I if spec.get("ignore_case", True) else 0)
    groups = list(rx.groupindex)
    names = {"id": "reference", "date": "date", "city": "city", "address": "address", "type": "type",
             "website": "website", "email": "email", "phone": "phone", "name": "name"}
    headers = [names.get(g, g) for g in groups]
    text = "\n".join(lines) if spec.get("multiline") else None
    rows = []
    for m in (rx.finditer(text) if text is not None else (m for l in lines for m in [rx.search(l)] if m)):
        rows.append([re.sub(r"\s+", " ", m.group(g) or "").strip() for g in groups])
    return headers, rows


def hkey(s) -> str:
    """Column-name key: lower case, letters and digits only (any script, so Japanese
    headers survive). The same as norm_key for plain ASCII names."""
    return re.sub(r"[\W_]", "", str(s).lower())


def _col(headers: list, names: list, override=None):
    idx = {}
    for i, h in enumerate(headers):
        idx.setdefault(hkey(h), i)
    for n in ([override] if override else []) + names:
        if n and hkey(n) in idx:
            return idx[hkey(n)]
    if override and hkey(override):   # an override may give just the start of a long column title
        for i, h in enumerate(headers):
            if hkey(h).startswith(hkey(override)):
                return i
    return None


NON_TARGET = re.compile(r"insur|seguro|assur|réassur|reinsur|pension|AFP\b|fondo|fund\b|funds|mutual fund|sociedades? de inversi|"
                        r"securit|valores|bolsa|stock|broker|corretaje|leasing|arrendamiento|factoring|cooperativ|"
                        r"credit union|microfinan|microcr|mutuelle|caisse populaire|pawn|empeño|asegurad", re.I)


def guess_name_column(headers: list, rows: list):
    """The column that most looks like firm names, for tables whose name column has an unusual title."""
    skip = re.compile(r"addr|adres|direcc|endere|date|fecha|data|tel|phone|mail|web|site|url|no\.?$|n°|nº|"
                      r"number|num|code|cod|status|estado|type|tipo|city|ciudad|country|pa[ií]s|^#$|^s/?n$|sl\.?no", re.I)
    best, score = None, 0.0
    sample = [r for r in rows[:200] if any(r)]
    if not sample:
        return None
    for i, h in enumerate(headers):
        if skip.search(str(h).strip()):
            continue
        vals = [r[i] for r in sample if i < len(r) and r[i]]
        if len(vals) < max(2, len(sample) // 3):
            continue
        good = sum(1 for v in vals if re.search(r"[^\W\d_]{3}", v) and not re.fullmatch(r"[\d\W]+", v)
                   and len(v) < 160 and not re.search(r"^https?://|@", v))
        uniq = len(set(vals)) / len(vals)
        sc = good / len(sample) * (0.5 + 0.5 * uniq)
        if sc > score:
            best, score = i, sc
    return best if score >= 0.5 else None


class _Tables(__import__("html.parser", fromlist=["HTMLParser"]).HTMLParser):
    """Every <table> on a page as a list of rows of cell texts (links kept as text)."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._stack, self._row, self._cell = [], [], None, None
        self.sections, self._heading, self._htext = [], "", None

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3", "h4", "h5", "caption") and self._cell is None:
            self._htext = []
        if tag == "table":
            self._stack.append([])
            self._secstack = getattr(self, "_secstack", [])
            self._secstack.append(self._heading)
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4", "h5", "caption") and self._htext is not None:
            t = re.sub(r"\s+", " ", "".join(self._htext)).strip()
            if t:
                self._heading = t
                if tag == "caption" and getattr(self, "_secstack", None):
                    self._secstack[-1] = t
            self._htext = None
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self._stack:
            if any(self._row):
                self._stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())
            self.sections.append(self._secstack.pop() if getattr(self, "_secstack", None) else "")

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        if self._htext is not None:
            self._htext.append(data)


def read_html_table(raw: bytes, spec: dict) -> tuple:
    """(headers, rows) of the table whose header row matches spec['table_headers'] (regex)
    and not spec['table_exclude']; the largest table if neither is given."""
    p = _Tables()
    p.feed(decode_bytes(raw))
    want = re.compile(spec["table_headers"], re.I) if spec.get("table_headers") else None
    skip = re.compile(spec["table_exclude"], re.I) if spec.get("table_exclude") else None
    best, merged, mhead = None, [], None
    for t, sec in zip(p.tables, p.sections):
        if not t:
            continue
        head = " | ".join(t[0])
        if want and not want.search(head):
            continue
        if skip and (skip.search(head) or skip.search(sec)):
            continue
        if spec.get("all_tables"):
            if spec.get("columns"):     # tables with differently worded headers: line up by position
                cols = list(spec["columns"])
                mhead = cols + ["section"]
                merged += [(r + [""] * len(cols))[:len(cols)] + [sec] for r in t[1:]]
                continue
            mhead = mhead or t[0] + ["section"]
            merged += [_align(r, t[0], mhead[:-1]) + [sec] for r in t[1:]]
            continue
        if best is None or len(t) > len(best):
            best = t
    if spec.get("all_tables") and merged:
        return mhead, merged
    if not best:
        raise RuntimeError(f"no matching table on the page; table headers were: "
                           f"{[' | '.join(t[0])[:120] for t in p.tables if t]}")
    return best[0], best[1:]


def _iso(s: str) -> str:
    """Dates like 2026-10-31, 31.10.2026 or 31/10/2026 -> 2026-10-31 ('' if none)."""
    s = (s or "").strip()
    m = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    months = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                          "nov", "dec"], 1)}
    m = re.match(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", s)            # June 23, 2026
    if m and m.group(1).lower() in months:
        return f"{m.group(3)}-{months[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})", s)            # 23 June 2026
    if m and m.group(2).lower() in months:
        return f"{m.group(3)}-{months[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return ""


def parse_nbs_psd2(raw: bytes, spec: dict, country: str, regulator: str) -> tuple:
    """Slovakia (NBS) PSD2 feed: {"institutions": [{name, id, national_id, address,
    licenses: [{<type code>, <valid from>, <valid to>, scopes...}]}]}.
    Keeps licences whose type code is in spec['licence_codes']."""
    import datetime as dt
    import json
    today = spec.get("_today") or dt.date.today().isoformat()
    wanted = spec.get("licence_codes") or {"PI": "Payment institution", "EM": "E-money institution",
                                           "AISP": "Account information service provider"}
    doc = json.loads(decode_bytes(raw))
    insts = doc.get("institutions") if isinstance(doc, dict) else None
    if not isinstance(insts, list):
        insts = _records_in_json(doc)
    out, code_counts = [], {}
    for inst in insts:
        if not isinstance(inst, dict):
            continue
        name = str(inst.get("name") or "").strip()
        lic = inst.get("licenses") or inst.get("licences") or []
        types, actives, froms, ends = [], [], [], []
        for l in lic if isinstance(lic, list) else []:
            if not isinstance(l, dict):
                continue
            code = next((str(v) for k, v in l.items() if isinstance(v, str) and v in CODES_ALL), None)
            if code is None:  # fall back to the usual key names
                code = str(l.get("type") or l.get("license") or l.get("code") or "")
            code_counts[code] = code_counts.get(code, 0) + 1
            if code not in wanted:
                continue
            vf = next((_iso(str(v)) for k, v in l.items() if "from" in k.lower() or k.lower().endswith("od")), "")
            vt = next((_iso(str(v)) for k, v in l.items() if re.search(r"(^|_)to$|until|till|valid_?to|do$", k.lower())), "")
            types.append(wanted[code])
            froms.append(vf)
            ends.append(vt)
            actives.append(not vt or vt >= today)
        if not types or not name:
            continue
        active = any(actives)
        ref = str(inst.get("national_id") or inst.get("id") or "")
        addr = str(inst.get("address") or "")
        city = re.sub(r"^\d{3}\s?\d{2}\s*", "", addr.rsplit(",", 1)[-1].strip()) if "," in addr else ""
        out.append(Record(
            key=f"nat_{country.lower()}:{ref or norm_key(name)}", source=f"nat_{country.lower()}",
            jurisdiction=country, regulator=regulator, name=name,
            licence_type=" + ".join(sorted(set(types))), active=active,
            status_detail="NBS licence" + ("" if active else f" ended {max(e for e in ends if e)}"),
            status_date=min((f for f in froms if f), default=""), reference=ref,
            city=city, register_url=spec.get("register_url") or spec["url"],
            extra={"address": addr},
        ))
    info = {"headers": ["institutions[].licenses[]"], "columns": {"type": "licence code"},
            "rows": len(insts), "kept": len(out),
            "type_values": sorted(code_counts.items(), key=lambda kv: -kv[1])}
    return out, info


CODES_ALL = {"AISP", "APS", "B", "EM", "EUAISP", "EUB", "EUEM", "EUPI", "PI", "PZB", "PZPI"}


def parse_file(raw: bytes, spec: dict, country: str, regulator: str) -> tuple:
    """Return (records, info) for one downloaded file."""
    if spec.get("format") == "nbs_psd2":
        return parse_nbs_psd2(raw, spec, country, regulator)
    import datetime as dt
    today = spec.get("_today") or dt.date.today().isoformat()
    fmt = spec.get("format")
    if fmt == "html_table":
        headers, rows = read_html_table(raw, spec)
    elif fmt == "pdf_table":
        headers, rows = read_pdf_table(raw, spec)
    elif fmt == "text_regex":
        headers, rows = read_text_regex(raw, spec)
    else:
        headers, rows = read_table(raw, spec.get("sheets", "first"))
    if spec.get("columns") and not spec.get("all_tables"):   # a table without a header row: name the columns here
        rows = [headers] + rows
        headers = list(spec["columns"]) + [""] * max(0, len(headers) - len(spec["columns"]))
    elif spec.get("name_column") and _col(headers, [], spec["name_column"]) is None:
        # the real header row is further down (title lines above it)
        allrows = [headers] + rows
        for i, r in enumerate(allrows[:40]):
            if _col(r, [], spec["name_column"]) is not None:
                headers, rows = r, allrows[i + 1:]
                break
    if spec.get("squeeze"):            # PDFs with merged cells: drop the empty cells and line up by position
        headers = [h for h in headers if h.strip()]
        rows = [[x for x in r if x.strip()] for r in rows]
    c = {
        "name": _col(headers, NAME_COLS, spec.get("name_column")),
        "type": _col(headers, TYPE_COLS, spec.get("type_column")),
        "id": _col(headers, ID_COLS, spec.get("id_column")),
        "lei": _col(headers, LEI_COLS),
        "city": _col(headers, CITY_COLS),
        "date": _col(headers, DATE_COLS),
        "status": _col(headers, STATUS_COLS),
        "website": _col(headers, WEB_COLS),
        "email": _col(headers, EMAIL_COLS),
        "phone": _col(headers, PHONE_COLS),
        "address": _col(headers, ADDRESS_COLS, spec.get("address_column")),
        "end": _col(headers, END_COLS),
        "exclude": _col(headers, [], spec.get("exclude_column")) if spec.get("exclude_column") else None,
        "require": _col(headers, [], spec.get("require_column")) if spec.get("require_column") else None,
    }
    info = {"headers": headers, "columns": {k: (headers[v] if v is not None else None) for k, v in c.items()},
            "rows": len(rows)}
    if c["name"] is None:
        c["name"] = guess_name_column(headers, rows)
        info["columns"]["name"] = f"(guessed) {headers[c['name']]}" if c["name"] is not None else None
    if c["name"] is None:
        raise RuntimeError(f"{country}: no name column found; headers are {headers}")

    def get(row, k):
        i = c[k]
        return row[i].strip() if i is not None and i < len(row) else ""

    patterns = [(re.compile(p, re.I), t) for p, t in (spec.get("type_patterns") or {}).items()]
    fixed_type = spec.get("licence_type")
    exclude = re.compile(spec["exclude_pattern"], re.I) if spec.get("exclude_pattern") else None
    codes = spec.get("_codes") or {}
    out = {}
    type_values = {}
    for row in rows:
        name = get(row, "name")
        if name and spec.get("name_exclude") and re.search(spec["name_exclude"], name, re.I):
            continue                      # e.g. holding companies listed alongside banks
        if not name or len(name) < 3 or not re.search(r"[^\W\d_]{2}", name) or re.fullmatch(r"\(?[a-zçğışöü]\)?", name):
            continue                      # blank, sub-header rows such as "(a)", or numbers only
        raw_type = get(row, "type")
        if codes and raw_type in codes:
            raw_type = f"{raw_type} {codes[raw_type]}"
        type_values[raw_type] = type_values.get(raw_type, 0) + 1
        if exclude:
            hay = get(row, "exclude") if c["exclude"] is not None else (
                raw_type if c["type"] is not None else " ".join(row))
            if exclude.search(hay):
                continue
        ltype = fixed_type
        if patterns:
            hay = raw_type or " ".join(row)
            ltype = next((t for p, t in patterns if p.search(hay)), None) if not fixed_type else fixed_type
        if not fixed_type and not patterns:
            # a mixed list with no mapping given: keep the regulator's own type, minus clearly
            # out-of-scope sectors (insurance, pensions, funds, securities, leasing, credit unions)
            if NON_TARGET.search(raw_type or "") and not spec.get("keep_all"):
                continue
            ltype = raw_type or spec.get("default_type") or "Licensed financial institution"
        for pat, otype in (spec.get("type_overrides") or {}).items():
            if re.search(pat, get(row, "exclude") or raw_type, re.I):
                ltype = otype
                break
        if not ltype:
            continue
        if c["require"] is not None and not get(row, "require"):
            continue  # e.g. no Lithuanian company code = foreign firm passporting in
        ref = get(row, "id")
        name = re.sub(r"(\s*([\^#*]+|※\d*|\(\d\)|\[\d+\]))+$", "", name).strip()   # footnote marks: "^", "#", "※1"
        key = f"nat_{country.lower()}:{ref or norm_key(name) or hkey(name)}"
        status = get(row, "status")
        active = not (status and INACTIVE_RE.search(status))
        end = _iso(get(row, "end"))
        if end and end < today:
            active = False
            status = status or f"valid till {end}"
        if key in out:  # same firm on several registers
            r = out[key]
            types = sorted(set(r.licence_type.split(" + ")) | {ltype})
            r.licence_type = " + ".join(types)
            continue
        out[key] = Record(
            key=key, source=f"nat_{country.lower()}", jurisdiction=country,
            regulator=spec.get("regulator") or regulator,
            name=name, licence_type=ltype, active=active,
            status_detail=" / ".join(x for x in (raw_type, get(row, "exclude"), status) if x) or f"{regulator} register",
            status_date=_iso(get(row, "date")) or get(row, "date")[:10], reference=ref, lei=get(row, "lei"),
            city=get(row, "city"), register_url=spec.get("register_url") or spec["url"],
            website=get(row, "website"), email=get(row, "email").lower(),
            extra={k: v for k, v in (("phone", get(row, "phone")), ("address", get(row, "address"))) if v},
        )
    info["type_values"] = sorted(type_values.items(), key=lambda kv: -kv[1])[:40]
    info["kept"] = len(out)
    return list(out.values()), info


class NationalSource(Source):
    """One country's own register."""

    def __init__(self, country: str, cfg: dict):
        self.country = country.upper()
        self.cfg = cfg
        self.regulator = cfg.get("regulator", self.country)
        self.name = f"nat_{self.country.lower()}"
        self.label = f"{self.country} national register ({self.regulator})"
        import hashlib
        import json
        self.fingerprint = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]

    def fetch(self, ctx: Context) -> list:
        from ..http import robots_allowed
        from .base import SourceNotReady
        recs, notes, problems, failed = {}, [], [], set()
        status = {}
        session = quick_session(ctx.session)
        ctx = Context(**{**ctx.__dict__, "session": session})
        for spec in self.cfg.get("files") or []:
            if spec.get("enabled") is False:
                continue
            spec = {**spec, "_today": ctx.today}
            if spec.get("codes_url"):
                spec = {**spec, "_codes": fetch_codes(ctx, spec["codes_url"])}
            label = spec.get("label") or spec.get("url") or spec.get("page")
            if spec.get("probe") and probed_recently(ctx, self.name, label):
                status[label] = "probe saved earlier this week"
                continue
            if spec.get("probe") and spec.get("follow") and FOLLOW_BUDGET["left"] <= 0:
                status[label] = "probe waiting for the next run (page budget used up)"
                continue
            if spec.get("page") and not spec.get("probe"):
                try:
                    spec = {**spec, "url": find_link(ctx, spec)}
                except Exception as e:
                    problems.append(f"{label}: {e}")
                    failed.add(label)
                    continue
            if not robots_allowed(ctx.session, spec["url"]):
                problems.append(f"{label}: disallowed by the site's robots.txt")
                status[label] = "blocked by robots.txt"
                failed.add(label)
                continue
            try:
                r = ctx.session.get(spec["url"], timeout=(15, 90), headers={
                    "User-Agent": "Mozilla/5.0 (compatible; licence-tracker)",
                    "Accept": "text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
                              "application/json,text/html,*/*"})
                r.raise_for_status()
            except Exception as e:
                problems.append(f"{label}: {e}")
                status[label] = _short_error(e)
                failed.add(label)
                continue
            raw = r.content
            err, info, got = None, {}, []
            if spec.get("probe"):
                # not read yet: just keep a copy of the page (and of the pages it links to that look like
                # registers) so a reader can be written for it
                save_raw(ctx, self.name, label, raw)
                n = follow_links(ctx, self.name, label, spec, raw) if spec.get("follow") else 0
                status[label] = f"probe saved ({len(raw)} bytes, {n} linked pages)"
                ctx.log.info("%s: %s saved for inspection (probe, %d linked pages)", self.name, label, n)
                continue
            try:
                got, info = parse_file(raw, spec, self.country, self.regulator)
                if not got:
                    raise RuntimeError("no firms matched")
            except Exception as e:
                err = e
                problems.append(f"{label}: {e}")
                failed.add(label)
                save_raw(ctx, self.name, label, raw)
                status[label] = "read failed: " + str(e)[:200]
            notes.append((spec["url"], raw, info, err))
            if got:
                status[label] = f"ok: {len(got)} firms"
            for rec in got:
                rec.extra["file"] = label
                if rec.key in recs:
                    prev = recs[rec.key]
                    prev.licence_type = " + ".join(sorted(set(prev.licence_type.split(" + ")) |
                                                          set(rec.licence_type.split(" + "))))
                    prev.website = prev.website or rec.website
                    prev.email = prev.email or rec.email
                else:
                    recs[rec.key] = rec
        write_diagnostics(ctx, self.name, notes)
        write_status(ctx, self.country, status)
        for p in problems:
            ctx.log.warning("%s: %s", self.name, p)
        if not recs and not problems:
            err = SourceNotReady(f"{self.label}: pages saved for inspection only (probe), no list yet")
            err.quiet = True
            raise err
        if not recs:
            raise SourceNotReady(f"{self.label}: nothing read ({'; '.join(problems)[:500]}); "
                                 f"see data/debug/{self.name}_sample.txt")
        if failed:
            # a file couldn't be read this time: keep its firms from the last good run, so a
            # temporary outage isn't reported as removals
            from ..store import load_snapshot
            from ..model import Record as _R
            import dataclasses
            fields = {f.name for f in dataclasses.fields(_R)}
            kept = 0
            for key, old in load_snapshot(ctx.data_dir, self.name).items():
                if (old.get("extra") or {}).get("file") in failed and key not in recs:
                    recs[key] = _R(**{k: v for k, v in old.items() if k in fields})
                    kept += 1
            if kept:
                ctx.log.info("%s: kept %d firms from the last run for the files that failed", self.name, kept)
        return list(recs.values())


def _date_key(url: str) -> tuple:
    """Newest-first sort key from a date in a file name/link text (2026-11-06, 06112026, 6-11-2026,
    'November 2026', '2026')."""
    from urllib.parse import unquote
    u = unquote(url)
    m = re.search(r"(20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?([0-2]\d|3[01])(?!\d)", u)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.search(r"(?<!\d)([0-2]?\d|3[01])[-_.](0?[1-9]|1[0-2])[-_.](20\d{2})", u)
    if m:
        return int(m.group(3)), int(m.group(2)), int(m.group(1))
    m = re.search(r"(?<!\d)([0-2]\d|3[01])(0[1-9]|1[0-2])(20\d{2})(?!\d)", u)
    if m:
        return int(m.group(3)), int(m.group(2)), int(m.group(1))
    months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    m = re.search(r"(?i)\b(" + "|".join(months) + r")[a-z]*[\s_.-]*(20\d{2})", u)
    if m:
        return int(m.group(2)), months.index(m.group(1).lower()[:3]) + 1, 0
    m = re.search(r"(20\d{2})", u)
    return (int(m.group(1)), 0, 0) if m else (0, 0, 0)


def find_link(ctx: Context, spec: dict) -> str:
    """The file linked from spec['page'] whose address or link text matches spec['link_pattern'];
    the newest by the date in its name when several match (or the first, with link_pick: first)."""
    import html as htmlmod
    from urllib.parse import urljoin
    from ..http import robots_allowed
    page = spec["page"]
    if not robots_allowed(ctx.session, page):
        raise RuntimeError(f"{page} is disallowed by robots.txt")
    r = ctx.session.get(page, timeout=90, headers={"User-Agent": "Mozilla/5.0 (compatible; licence-tracker)"})
    r.raise_for_status()
    rx = re.compile(spec["link_pattern"], re.I)
    found = []
    for href, text in re.findall(r"""<a\b[^>]*?href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""", r.text, re.I | re.S):
        href = htmlmod.unescape(href.strip())
        text = re.sub(r"<[^>]+>|\s+", " ", htmlmod.unescape(text)).strip()
        if rx.search(href) or rx.search(text):
            u = urljoin(page, href)
            if u not in [f[0] for f in found]:
                found.append((u, text))
    if not found:
        raise RuntimeError(f"no link matching {spec['link_pattern']!r} on {page}")
    if spec.get("link_pick") == "first":
        return found[0][0]
    return max(found, key=lambda f: max(_date_key(f[0]), _date_key(f[1])))[0]


def fetch_codes(ctx: Context, url: str) -> dict:
    """Code list (e.g. institution type codes -> names) as {code: name}."""
    try:
        import json
        r = ctx.session.get(url, timeout=60)
        r.raise_for_status()
        rows = [_flatten(x) for x in _records_in_json(json.loads(decode_bytes(r.content)))]
        out = {}
        for row in rows:
            keys = {norm_key(k): k for k in row}
            code_k = next((keys[k] for k in ("code", "kod", "id", "value", "key") if k in keys), None)
            name_k = next((keys[k] for k in ("nameen", "name", "nazov", "description", "popis", "label", "text")
                           if k in keys), None)
            if code_k and name_k:
                out[str(row[code_k])] = str(row[name_k])
        return out
    except Exception as e:
        ctx.log.warning("code list %s not read: %s", url, e)
        return {}


_BUNDLES = {}


def _chain_bundle(url: str):
    """A CA bundle (certifi + the site's missing intermediate certificates, fetched from the
    'CA Issuers' address in its certificate), or None if that can't be done."""
    import ssl
    import tempfile
    from urllib.parse import urlparse
    host = urlparse(url).hostname
    if host in _BUNDLES:
        return _BUNDLES[host]
    _BUNDLES[host] = None
    try:
        import certifi
        import requests
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding
        from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID
        pem = ssl.get_server_certificate((host, urlparse(url).port or 443), timeout=20)
        cert = x509.load_pem_x509_certificate(pem.encode())
        extra = []
        for _ in range(3):   # walk up at most three issuers
            try:
                aia = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
            except x509.ExtensionNotFound:
                break
            urls = [d.access_location.value for d in aia
                    if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS]
            if not urls:
                break
            data = requests.get(urls[0], timeout=20).content
            try:
                cert = x509.load_der_x509_certificate(data)
            except ValueError:
                cert = x509.load_pem_x509_certificate(data)
            extra.append(cert.public_bytes(Encoding.PEM).decode())
            if cert.issuer == cert.subject:
                break
        if not extra:
            return None
        f = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
        f.write(open(certifi.where()).read() + "\n" + "\n".join(extra))
        f.close()
        _BUNDLES[host] = f.name
    except Exception:
        return None
    return _BUNDLES[host]


def quick_session(base):
    """Same headers as the main session, but one retry and no long waits: these are many small
    government sites, and a dead one must not hold up the run."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    if getattr(base, "_quick", False) or not isinstance(base, requests.Session):
        return base
    s = requests.Session()
    s.headers.update(base.headers)
    a = HTTPAdapter(max_retries=Retry(total=1, backoff_factor=2, status_forcelist=(502, 503, 504),
                                      allowed_methods=("GET", "HEAD")))
    s.mount("https://", a)
    s.mount("http://", a)
    s._quick = True
    orig = s.request

    def request(method, url, **kw):
        kw.setdefault("timeout", (15, 90))
        try:
            return orig(method, url, **kw)
        except requests.exceptions.SSLError:
            # many government sites don't send their intermediate certificate; fetch it from the
            # address in the site certificate (AIA) and verify again with it - verification stays on
            bundle = _chain_bundle(url)
            if not bundle:
                raise
            kw["verify"] = bundle
            return orig(method, url, **kw)
    s.request = request
    return s


def _short_error(e) -> str:
    t = str(e)
    m = re.search(r"(\d{3}) (?:Client|Server) Error", t)
    if m:
        return f"HTTP {m.group(1)}"
    for k, v in (("Read timed out", "timeout"), ("ConnectTimeout", "timeout (connect)"), ("NameResolution", "DNS: no such site"),
                 ("SSLError", "SSL error"), ("ConnectionRefused", "connection refused"), ("RemoteDisconnected", "disconnected"),
                 ("Max retries exceeded", "connection failed")):
        if k in t:
            return v
    return t[:160]


def write_status(ctx: Context, country: str, status: dict):
    """data/debug/national_status.json: what happened to each country's files on the last read."""
    import json
    p = ctx.data_dir / "debug" / "national_status.json"
    try:
        d = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        d = {}
    d[country] = {"date": ctx.today, **status}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=1, sort_keys=True, ensure_ascii=False))


FOLLOW_BUDGET = {"left": 600}   # linked pages fetched per run across all probes (the rest wait for the next run)


def probed_recently(ctx: Context, name: str, label: str, days: int = 6) -> bool:
    import json
    import datetime as dt
    p = ctx.data_dir / "state" / "raw_saved.json"
    try:
        st = json.loads(p.read_text()) if p.exists() else {}
        last = st.get(f"{name}__{re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')[:60]}")
        return bool(last) and (dt.date.fromisoformat(ctx.today) - dt.date.fromisoformat(last)).days < days
    except Exception:
        return False


def follow_links(ctx: Context, name: str, label: str, spec: dict, raw: bytes) -> int:
    """Save the pages/files linked from a probed page whose address or link text matches spec['follow']."""
    import time
    import html as htmlmod
    from urllib.parse import urljoin, urlparse
    from ..http import robots_allowed
    rx = re.compile(spec["follow"], re.I)
    page = spec["url"]
    seen, n = set(), 0
    for href, text in re.findall(r"""<a\b[^>]*?href\s*=\s*["']([^"'#]+)["'][^>]*>(.*?)</a>""", decode_bytes(raw), re.I | re.S):
        href = htmlmod.unescape(href.strip())
        text = re.sub(r"<[^>]+>|\s+", " ", htmlmod.unescape(text)).strip()
        if not (rx.search(href) or rx.search(text)) or href.lower().startswith(("mailto:", "javascript:")):
            continue
        u = urljoin(page, href)
        if u in seen or urlparse(u).netloc.split(":")[0].removeprefix("www.") != \
                urlparse(page).netloc.split(":")[0].removeprefix("www.") and not spec.get("follow_offsite"):
            continue
        seen.add(u)
        if len(seen) > int(spec.get("follow_max", 10)) or FOLLOW_BUDGET["left"] <= 0:
            break
        FOLLOW_BUDGET["left"] -= 1
        time.sleep(0.5)
        try:
            if not robots_allowed(ctx.session, u):
                continue
            r = ctx.session.get(u, timeout=(15, 90))
            if r.status_code == 200:
                save_raw(ctx, name, f"{label} > {text[:40] or href[-40:]}", r.content)
                n += 1
        except Exception as e:
            ctx.log.info("%s: follow %s failed: %s", name, u, e)
    return n


def save_raw(ctx: Context, name: str, label: str, raw: bytes):
    """A copy of a downloaded page/file (first 800 KB) in data/debug/raw/, for writing or fixing its
    reader. Refreshed at most once a week per file, to keep the repository small."""
    try:
        import json
        import datetime as dt
        state_p = ctx.data_dir / "state" / "raw_saved.json"
        st = json.loads(state_p.read_text()) if state_p.exists() else {}
        slug0 = f"{name}__{re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')[:60]}"
        last = st.get(slug0)
        if last and (dt.date.fromisoformat(ctx.today) - dt.date.fromisoformat(last)).days < 7:
            return
        st[slug0] = ctx.today
        state_p.parent.mkdir(parents=True, exist_ok=True)
        state_p.write_text(json.dumps(st, indent=0, sort_keys=True))
    except Exception:
        pass
    try:
        ext = ("pdf" if raw[:4] == b"%PDF" else "xlsx" if raw[:2] == b"PK" else "xls" if raw[:4] == b"\xd0\xcf\x11\xe0"
               else "json" if raw.lstrip()[:1] in (b"{", b"[") else "html" if b"<html" in raw[:2000].lower() else "txt")
        slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:60]
        p = ctx.data_dir / "debug" / "raw" / f"{name}__{slug}.{ext}"
        p.parent.mkdir(parents=True, exist_ok=True)
        for old in p.parent.glob(f"{name}__{slug}.*"):
            old.unlink()
        p.write_bytes(raw[:800_000])
    except Exception as e:
        ctx.log.warning("%s: raw copy not saved: %s", name, e)


def write_diagnostics(ctx: Context, name: str, notes: list):
    try:
        out = []
        for url, raw, info, err in notes:
            out += [f"url: {url}", f"bytes: {len(raw)}  starts: {raw[:8]!r}",
                    f"error: {err!r}", f"columns used: {info.get('columns')}",
                    f"headers: {info.get('headers')}", f"data rows: {info.get('rows')}  kept: {info.get('kept')}",
                    f"type values: {info.get('type_values')}", "first lines:"]
            if info.get("first_text_lines"):  # PDFs: the extracted text, not the raw bytes
                out += [repr(str(l)[:300]) for l in info["first_text_lines"]]
                out.append("")
                continue
            try:
                text = decode_bytes(raw) if raw[:2] not in (b"PK", b"%P") else "(binary)"
            except Exception:
                text = "(undecodable)"
            out += [repr(l[:300]) for l in text.replace("\r\n", "\n").split("\n")[:8]]
            out.append("")
        p = ctx.data_dir / "debug" / f"{name}_sample.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(out), encoding="utf-8")
    except Exception as e:
        ctx.log.warning("%s diagnostics not written: %s", name, e)


def national_sources(cfg: dict) -> dict:
    out = {}
    for country, c in (cfg.get("national") or {}).items():
        if (c or {}).get("enabled", True):
            s = NationalSource(country, c)
            out[s.name] = s
    return out


# ------------------------------------------------------ name matching ---

LEGAL_FORMS = re.compile(
    r"\b(uzdaroji akcine bendrove|akcine bendrove|joint stock company|private limited company|"
    r"public limited company|limited liability company|closed joint stock company|restricted activity|"
    r"sabiedriba ar ierobezotu atbildibu|akciju sabiedriba|spolocnost s rucenim obmedzenym|"
    r"akciova spolocnost|incorporated|corporation|corp|ltee|ulc|llp|uab|ab|ad|as|a\s?s|bv|b\s?v|nv|n\s?v|ltd|limited|plc|llc|inc|sa|s\s?a|sas|sarl|srl|s\s?r\s?l|spa|"
    r"gmbh|ag|kg|eg|oy|oyj|sro|s\s?r\s?o|zrt|kft|doo|d\s?o\s?o|sp\s?z\s?o\s?o|ehf|asa|aps|a\s?s|"
    r"ou|oü|as|sia|ead|eood|ltd\.?|company|co|the|branch|filiaal|filialas|bijkantoor)\b", re.I)


def match_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[\"'„“”«»().,/&-]", " ", s)
    s = LEGAL_FORMS.sub(" ", s)
    return re.sub(r"[^a-z0-9]", "", s)
