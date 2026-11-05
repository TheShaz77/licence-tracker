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


def parse_mfi(text: str, excluded: set) -> list:
    # The ECB file mixes \r\n and bare \r line breaks; normalise them first.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t;,|")
    except csv.Error:
        dialect = csv.excel_tab
    rows = csv.reader(io.StringIO(text), dialect)
    headers = [h.strip().strip("﻿") for h in next(rows)]
    col = {k: pick(headers, v) for k, v in COLS.items()}
    if col["id"] is None or col["name"] is None or col["category"] is None:
        raise RuntimeError(f"ECB MFI file layout changed; headers are: {headers}")

    def get(row, k):
        i = col[k]
        return row[i].strip() if i is not None and i < len(row) else ""

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
        text = raw.decode("utf-8-sig", errors="replace")
        excluded = set(ctx.excluded_countries) | set(cfg.get("exclude_jurisdictions") or [])
        return parse_mfi(text, excluded)
