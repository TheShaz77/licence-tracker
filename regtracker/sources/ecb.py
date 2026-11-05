"""EU credit institutions (banks) from the ECB's daily list of Monetary
Financial Institutions (MFIs).

The ECB publishes the full EU MFI population daily as a delimited file whose
name carries the date, so the adapter reads the download page for the current
link. Only rows whose category is a credit institution are kept.
"""
from __future__ import annotations

import csv
import gzip
import io
import re
from urllib.parse import urljoin

from ..model import Record, norm_key
from .base import Context, Source

PAGE = "https://www.ecb.europa.eu/stats/financial_corporations/list_of_financial_institutions/html/daily_list-MID.en.html"
LINK_RE = re.compile(r'href="([^"]*mfi_csv_\d{6}\.csv\.gz)"', re.I)

# candidate column names, matched ignoring case/punctuation
COLS = {
    "id": ["RIAD_CODE", "RIAD CODE", "MFI_ID", "MFI ID"],
    "lei": ["LEI"],
    "name": ["NAME", "MFI_NAME", "INSTITUTION_NAME"],
    "country": ["COUNTRY_OF_REGISTRATION", "COUNTRY", "COUNTRY_CODE", "HOST"],
    "city": ["CITY", "TOWN"],
    "category": ["CATEGORY", "TYPE", "MFI_TYPE", "SECTOR"],
    "head_name": ["HEAD_NAME", "HEAD_OF_BRANCH_NAME"],
    "head_country": ["HEAD_COUNTRY_OF_REGISTRATION", "HEAD_COUNTRY"],
}
CREDIT_RE = re.compile(r"credit\s*inst|deposit[- ]taking|s\.?122", re.I)


def pick(headers: list, names: list):
    idx = {norm_key(h): i for i, h in enumerate(headers)}
    for n in names:
        if norm_key(n) in idx:
            return idx[norm_key(n)]
    return None


def decode_bytes(raw: bytes) -> str:
    """The ECB publishes the list as UTF-16 (with a byte-order mark)."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw[:1000].count(b"\x00") > 100:
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def parse_mfi(text: str, excluded: set) -> list:
    # The ECB file mixes \r\n and bare \r line breaks; normalise them first.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    first_line = next((l for l in text.split("\n", 20) if l.strip()), "")
    delim = max("\t;,|", key=first_line.count)
    # Tab files from the ECB contain stray quote marks in names; don't let
    # them swallow whole blocks of rows.
    quoting = csv.QUOTE_NONE if delim == "\t" else csv.QUOTE_MINIMAL
    rows = csv.reader(io.StringIO(text), delimiter=delim, quoting=quoting)
    headers = []
    for headers in rows:
        if any(h.strip() for h in headers):
            break
    headers = [h.strip().strip("﻿").strip('"') for h in headers]
    col = {k: pick(headers, v) for k, v in COLS.items()}
    if col["id"] is None or col["name"] is None or col["category"] is None:
        raise RuntimeError(f"ECB MFI file layout changed; headers are: {headers}")

    def get(row, k):
        i = col[k]
        return row[i].strip().strip('"') if i is not None and i < len(row) else ""

    out = {}
    for row in rows:
        if not row or not CREDIT_RE.search(get(row, "category")):
            continue
        country = get(row, "country").upper()[:2]
        if country in excluded:
            continue
        rid = get(row, "id")
        branch = bool(get(row, "head_name"))
        out[rid] = Record(
            key=f"ecb:{rid}",
            source="ecb",
            jurisdiction=country,
            regulator=f"{country} NCA / ECB",
            name=get(row, "name"),
            licence_type="Credit institution (branch)" if branch else "Credit institution",
            active=True,
            status_detail=get(row, "category") + (f"; branch of {get(row, 'head_name')} ({get(row, 'head_country')})" if branch else ""),
            reference=rid,
            lei=get(row, "lei"),
            city=get(row, "city"),
            register_url=PAGE,
        )
    return list(out.values())


class ECBSource(Source):
    name = "ecb"
    label = "EU banks (ECB list of credit institutions)"

    def fetch(self, ctx: Context) -> list:
        cfg = ctx.source_config("ecb")
        url = cfg.get("url")
        if not url:
            page = ctx.session.get(PAGE, timeout=60)
            page.raise_for_status()
            links = [l for l in LINK_RE.findall(page.text) if "update" not in l.lower()]
            if not links:
                raise RuntimeError("Couldn't find the ECB MFI download link; set sources.ecb.url")
            url = urljoin(PAGE, links[0])
        ctx.log.info("ECB: downloading %s", url)
        r = ctx.session.get(url, timeout=300)
        r.raise_for_status()
        raw = r.content
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        text = decode_bytes(raw)
        excluded = set(ctx.excluded_countries) | set(cfg.get("exclude_jurisdictions") or [])
        recs = []
        err = None
        try:
            recs = parse_mfi(text, excluded)
        except Exception as e:
            err = e
        write_diagnostics(ctx, url, raw, text, len(recs), err)
        if err:
            raise err
        if not recs:
            raise RuntimeError("ECB file parsed but no credit institutions matched; "
                               "see data/debug/ecb_sample.txt")
        return recs


def write_diagnostics(ctx: Context, url: str, raw: bytes, text: str, n: int, err):
    """Save a small, public sample of the file so the parser can be checked."""
    try:
        from collections import Counter
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        first = next((l for l in lines if l.strip()), "")
        delim = max("\t;,|", key=first.count)
        hdr = [h.strip().strip('"') for h in first.split(delim)]
        cat_i = next((i for i, h in enumerate(hdr) if "CATEG" in h.upper() or h.upper() == "TYPE"), None)
        cats = Counter()
        if cat_i is not None:
            for l in lines[1:]:
                parts = l.split(delim)
                if len(parts) > cat_i:
                    cats[parts[cat_i].strip().strip('"')] += 1
        out = [f"url: {url}", f"bytes: {len(raw)}  lines: {len(lines)}  records parsed: {n}",
               f"error: {err!r}", f"delimiter: {delim!r}", f"headers: {hdr}",
               f"line-break counts: CRLF={text.count(chr(13) + chr(10))} CR={text.count(chr(13))} LF={text.count(chr(10))}",
               "category values: " + repr(cats.most_common(25)), "", "first 12 lines:"]
        out += [repr(l[:400]) for l in lines[:12]]
        p = ctx.data_dir / "debug" / "ecb_sample.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(out) + "\n", encoding="utf-8")
    except Exception as e:  # diagnostics must never break the run
        ctx.log.warning("ECB diagnostics not written: %s", e)
