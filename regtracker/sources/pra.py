"""UK banks and building societies from the PRA's monthly lists (Bank of England).

The CSV links change every month, so the adapter reads the PRA page and picks
the current links. Override with `sources.pra.urls` in config.yaml if the page
layout changes.
"""
from __future__ import annotations

import csv
import io
import re
from urllib.parse import urljoin

from ..model import Record
from .base import Context, Source

PAGE = "https://www.bankofengland.co.uk/prudential-regulation/authorisations/which-firms-does-the-pra-regulate"
SEARCH_URL = "https://register.fca.org.uk/s/search?q={frn}&type=Companies"

FRN_RE = re.compile(r"^\d{5,7}$")
LEI_RE = re.compile(r"^[A-Z0-9]{18}\d{2}$")


def find_links(html: str, base: str) -> dict:
    links = {}
    for href in re.findall(r'href="([^"]+)"', html, flags=re.I):
        h = href.lower()
        if ".csv" not in h:
            continue
        url = urljoin(base, href.replace("&amp;", "&"))
        if "building" in h and "societ" in h:
            links.setdefault("building_societies", url)
        elif "bank" in h:
            links.setdefault("banks", url)
    return links


def section_type(section: str, list_kind: str) -> str:
    s = section.lower()
    if list_kind == "building_societies" or "building societ" in s:
        return "Building society"
    if "outside" in s or "branch" in s or "non-uk" in s or "overseas" in s or "eea" in s:
        return "Bank (UK branch of overseas bank)"
    return "Bank (UK-incorporated)"


def parse_list(text: str, list_kind: str) -> list:
    """Tolerant parser: firm rows are any row with an FRN-looking cell;
    single-cell rows in between are section headings."""
    out = []
    section = ""
    for row in csv.reader(io.StringIO(text)):
        cells = [c.strip().strip("﻿") for c in row]
        nonempty = [c for c in cells if c]
        if not nonempty:
            continue
        frn = next((c for c in cells if FRN_RE.match(c)), None)
        if not frn:
            if len(nonempty) == 1 and not nonempty[0].lower().startswith(("firm name", "list of")):
                section = nonempty[0]
            continue
        name = next((c for c in cells if c and not FRN_RE.match(c) and not LEI_RE.match(c)), "")
        lei = next((c for c in cells if LEI_RE.match(c)), "")
        out.append(Record(
            key=f"pra:{frn}",
            source="pra",
            jurisdiction="GB",
            regulator="PRA",
            name=name,
            licence_type=section_type(section, list_kind),
            active=True,
            status_detail=section or ("PRA-regulated " + list_kind.replace("_", " ")),
            reference=frn,
            lei=lei,
            register_url=SEARCH_URL.format(frn=frn),
        ))
    return out


class PRASource(Source):
    name = "pra"
    label = "UK PRA (banks & building societies)"

    def fetch(self, ctx: Context) -> list:
        cfg = ctx.source_config("pra")
        urls = cfg.get("urls") or {}
        if not urls:
            r = ctx.session.get(PAGE, timeout=60,
                                headers={"User-Agent": "Mozilla/5.0 (compatible; licence-tracker)"})
            r.raise_for_status()
            urls = find_links(r.text, PAGE)
        if "banks" not in urls:
            raise RuntimeError("Couldn't find the PRA banks CSV link; set sources.pra.urls.banks")
        recs = {}
        for kind, url in urls.items():
            ctx.log.info("PRA: downloading %s list", kind)
            r = ctx.session.get(url, timeout=60)
            r.raise_for_status()
            text = r.content.decode("utf-8-sig", errors="replace")
            for rec in parse_list(text, kind):
                recs[rec.key] = rec
        return list(recs.values())
