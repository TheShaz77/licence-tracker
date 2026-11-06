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
             "ENT_NAM", "entityName", "nazov", "obchodne meno", "obchodné meno", "nazovSubjektu"]
TYPE_COLS = ["register", "register name", "type", "licence type", "license type", "category",
             "authorisation", "authorization", "vergunning", "registertype", "activity",
             "business form", "rūšis", "EntityType", "entity type", "typ", "typSubjektu", "kodTypu",
             "typ subjektu", "type code"]
ID_COLS = ["relation number", "relatienummer", "registration number", "register number",
           "reference", "reference number", "code", "company code", "kvk", "kvk number",
           "chamber of commerce number", "national id", "id", "uid", "kodas", "ENT_NAT_REF_COD",
           "EntityCode", "ico", "ičo"]
LEI_COLS = ["lei", "lei code", "lei-code", "ENT_COD_LEI", "ENT_LEI"]
CITY_COLS = ["city", "town", "place", "plaats", "vestigingsplaats", "miestas", "municipality",
             "ENT_TOW_CIT_RES", "obec", "mesto"]
END_COLS = ["valid till", "valid to", "valid until", "end date", "expiry date", "withdrawn", "date of withdrawal"]
DATE_COLS = ["valid from", "start date", "registration date", "date of registration", "licence date",
             "date", "ingangsdatum", "datum", "effective date", "ENT_AUT", "datumVzniku"]
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


def read_table(raw: bytes) -> tuple:
    """Return (headers, rows) from CSV/TSV text, an .xlsx file or JSON."""
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
    if raw[:2] == b"PK":  # xlsx
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = [["" if c is None else str(c).strip() for c in r] for r in ws.iter_rows(values_only=True)]
    else:
        text = decode_bytes(raw).replace("\r\n", "\n").replace("\r", "\n")
        first = next((l for l in text.split("\n", 50) if l.strip()), "")
        delim = max(";\t,|", key=first.count)
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter=delim)]
    rows = [r for r in rows if any(c for c in r)]
    # header = first row that names a known name column (skips title lines)
    for i, r in enumerate(rows[:15]):
        keys = {norm_key(c) for c in r}
        if any(norm_key(n) in keys for n in NAME_COLS):
            return r, rows[i + 1:]
    return (rows[0], rows[1:]) if rows else ([], [])


def _col(headers: list, names: list, override=None):
    idx = {norm_key(h): i for i, h in enumerate(headers)}
    for n in ([override] if override else []) + names:
        if n and norm_key(n) in idx:
            return idx[norm_key(n)]
    return None


def _iso(s: str) -> str:
    """Dates like 2026-10-31, 31.10.2026 or 31/10/2026 -> 2026-10-31 ('' if none)."""
    s = (s or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.match(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
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
    headers, rows = read_table(raw)
    c = {
        "name": _col(headers, NAME_COLS, spec.get("name_column")),
        "type": _col(headers, TYPE_COLS, spec.get("type_column")),
        "id": _col(headers, ID_COLS, spec.get("id_column")),
        "lei": _col(headers, LEI_COLS),
        "city": _col(headers, CITY_COLS),
        "date": _col(headers, DATE_COLS),
        "status": _col(headers, STATUS_COLS),
        "end": _col(headers, END_COLS),
        "exclude": _col(headers, [], spec.get("exclude_column")) if spec.get("exclude_column") else None,
        "require": _col(headers, [], spec.get("require_column")) if spec.get("require_column") else None,
    }
    info = {"headers": headers, "columns": {k: (headers[v] if v is not None else None) for k, v in c.items()},
            "rows": len(rows)}
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
        if not name:
            continue
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
        for pat, otype in (spec.get("type_overrides") or {}).items():
            if re.search(pat, get(row, "exclude") or raw_type, re.I):
                ltype = otype
                break
        if not ltype:
            continue
        if c["require"] is not None and not get(row, "require"):
            continue  # e.g. no Lithuanian company code = foreign firm passporting in
        ref = get(row, "id")
        key = f"nat_{country.lower()}:{ref or norm_key(name)}"
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
            key=key, source=f"nat_{country.lower()}", jurisdiction=country, regulator=regulator,
            name=name, licence_type=ltype, active=active,
            status_detail=" / ".join(x for x in (raw_type, get(row, "exclude"), status) if x) or f"{regulator} register",
            status_date=_iso(get(row, "date")) or get(row, "date")[:10], reference=ref, lei=get(row, "lei"),
            city=get(row, "city"), register_url=spec.get("register_url") or spec["url"],
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
        recs, notes = {}, []
        for spec in self.cfg.get("files") or []:
            spec = {**spec, "_today": ctx.today}
            if spec.get("codes_url"):
                spec = {**spec, "_codes": fetch_codes(ctx, spec["codes_url"])}
            r = ctx.session.get(spec["url"], timeout=120, headers={
                "User-Agent": "Mozilla/5.0 (compatible; licence-tracker)",
                "Accept": "text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*"})
            r.raise_for_status()
            raw = r.content
            err, info, got = None, {}, []
            try:
                got, info = parse_file(raw, spec, self.country, self.regulator)
            except Exception as e:
                err = e
            notes.append((spec["url"], raw, info, err))
            if err:
                write_diagnostics(ctx, self.name, notes)
                raise err
            for rec in got:
                if rec.key in recs:
                    prev = recs[rec.key]
                    prev.licence_type = " + ".join(sorted(set(prev.licence_type.split(" + ")) |
                                                          set(rec.licence_type.split(" + "))))
                else:
                    recs[rec.key] = rec
        write_diagnostics(ctx, self.name, notes)
        if not recs:
            raise RuntimeError(f"{self.label}: files downloaded but no firms matched; "
                               f"see data/debug/{self.name}_sample.txt")
        return list(recs.values())


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


def write_diagnostics(ctx: Context, name: str, notes: list):
    try:
        out = []
        for url, raw, info, err in notes:
            out += [f"url: {url}", f"bytes: {len(raw)}  starts: {raw[:8]!r}",
                    f"error: {err!r}", f"columns used: {info.get('columns')}",
                    f"headers: {info.get('headers')}", f"data rows: {info.get('rows')}  kept: {info.get('kept')}",
                    f"type values: {info.get('type_values')}", "first lines:"]
            try:
                text = decode_bytes(raw) if raw[:2] != b"PK" else "(xlsx)"
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
    r"akciova spolocnost|uab|ab|ad|as|a\s?s|bv|b\s?v|nv|n\s?v|ltd|limited|plc|llc|inc|sa|s\s?a|sas|sarl|srl|s\s?r\s?l|spa|"
    r"gmbh|ag|kg|eg|oy|oyj|sro|s\s?r\s?o|zrt|kft|doo|d\s?o\s?o|sp\s?z\s?o\s?o|ehf|asa|aps|a\s?s|"
    r"ou|oü|as|sia|ead|eood|ltd\.?|company|co|the|branch|filiaal|filialas|bijkantoor)\b", re.I)


def match_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[\"'„“”«»().,/&-]", " ", s)
    s = LEGAL_FORMS.sub(" ", s)
    return re.sub(r"[^a-z0-9]", "", s)
