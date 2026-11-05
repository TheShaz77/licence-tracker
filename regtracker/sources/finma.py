"""Switzerland: FINMA's daily list of authorised institutions (banks and
FinTech licence holders by default)."""
from __future__ import annotations

import csv
import io
import re

from ..model import Record
from .base import Context, Source

URL = "https://www.finma.ch/en/~/media/finma/dokumente/bewilligungstraeger/csv/uid.csv"
SEARCH_URL = "https://www.finma.ch/en/finma-public/authorised-institutions-individuals-and-products/"
DEFAULT_INCLUDE = r"bank|fintech"
DEFAULT_EXCLUDE = r"representative office"


def parse_finma(text: str, include: str, exclude: str) -> list:
    inc, exc = re.compile(include, re.I), re.compile(exclude, re.I)
    rows = csv.DictReader(io.StringIO(text), delimiter=";")
    grouped = {}
    for row in rows:
        row = {(k or "").strip().strip("﻿"): (v or "").strip() for k, v in row.items()}
        atype = row.get("AuthorisationTypeEN", "")
        if not inc.search(atype) or exc.search(atype):
            continue
        uid = row.get("UID", "")
        key = uid or f"{row.get('Name', '')}|{row.get('City', '')}"
        g = grouped.setdefault(key, {"row": row, "types": set()})
        g["types"].add(atype)
    out = []
    for key, g in grouped.items():
        row = g["row"]
        out.append(Record(
            key=f"finma:{key}",
            source="finma",
            jurisdiction="CH",
            regulator="FINMA",
            name=row.get("Name", ""),
            licence_type=" + ".join(sorted(g["types"])),
            active=True,
            status_detail="FINMA authorised",
            reference=row.get("UID", ""),
            city=row.get("City", ""),
            register_url=SEARCH_URL,
        ))
    return out


class FINMASource(Source):
    name = "finma"
    label = "Switzerland (FINMA banks & FinTech licences)"

    def fetch(self, ctx: Context) -> list:
        cfg = ctx.source_config("finma")
        r = ctx.session.get(cfg.get("url") or URL, timeout=60)
        r.raise_for_status()
        return parse_finma(r.content.decode("utf-8-sig", errors="replace"),
                           cfg.get("include_types", DEFAULT_INCLUDE),
                           cfg.get("exclude_types", DEFAULT_EXCLUDE))
