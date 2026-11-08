"""United States: FinCEN MSB registrants as republished by OpenSanctions.

FinCEN's own site asks automated tools not to download its MSB list (robots.txt),
so this reads OpenSanctions' daily copy of it instead.

LICENCE: OpenSanctions data is free only for non-commercial use (CC BY-NC 4.0).
Their terms say "any use inside a for-profit business requires a data license",
which includes prospecting for an employer. This source is therefore switched
off in config.yaml until you decide your use qualifies or you have a licence.
"""
from __future__ import annotations

import csv
import io
import re

from ..http import robots_allowed
from ..model import Record
from .base import Context, Source, SourceNotReady
from .national import write_diagnostics

URL = "https://data.opensanctions.org/datasets/latest/us_fincen_msb/targets.simple.csv"
SITE = "https://www.opensanctions.org/datasets/us_fincen_msb/"
CITY_ST = re.compile(r"([A-Za-z .'-]+),\s*([A-Z]{2})\s+\d{5}")


def parse(raw: bytes) -> tuple:
    text = raw.decode("utf-8", "replace").lstrip("﻿")
    rows = list(csv.DictReader(io.StringIO(text)))
    info = {"headers": list(rows[0].keys()) if rows else [], "rows": len(rows), "columns": {}}
    out = []
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name or (r.get("schema") or "").lower() == "person":
            continue  # individuals (sole proprietors) are left out
        addr = (r.get("addresses") or "").split(";")[0].strip()
        m = None
        for m in CITY_ST.finditer(addr):
            pass
        out.append(Record(
            key="fincen_os:" + (r.get("id") or name), source="fincen_os", jurisdiction="US", regulator="FinCEN",
            name=name, licence_type="MSB (FinCEN registration)", active=True,
            status_detail="On FinCEN's MSB registrant list (via OpenSanctions)",
            status_date=(r.get("first_seen") or "")[:10], city=(m.group(1).strip().title() if m else ""),
            register_url=SITE,
            extra={"address": addr, "aliases": (r.get("aliases") or "")[:300],
                   "phone": (r.get("phones") or "").split(";")[0], "email": (r.get("emails") or "").split(";")[0],
                   "hq_state": m.group(2) if m else "", "last_seen": r.get("last_seen", "")}))
    info["kept"] = len(out)
    return out, info


class OpenSanctionsMSBSource(Source):
    name = "fincen_os"
    label = "US FinCEN MSBs (OpenSanctions copy; non-commercial licence)"
    fingerprint = "fincen-os-v1"

    def fetch(self, ctx: Context) -> list:
        url = ctx.source_config(self.name).get("url") or URL
        # data.opensanctions.org's robots.txt (Disallow: /) is aimed at search-engine crawlers. OpenSanctions'
        # own guide ("Downloading the data and keeping it fresh") tells users to re-fetch these files on a
        # timer, at most every 6 hours, without login; this source runs at most once every 20 hours.
        r = ctx.session.get(url, timeout=300)
        r.raise_for_status()
        err, info, recs = None, {}, []
        try:
            recs, info = parse(r.content)
        except Exception as e:
            err = e
        write_diagnostics(ctx, self.name, [(url, r.content[:200000], info, err)])
        if err:
            raise err
        if len(recs) < 1000:
            raise RuntimeError(f"OpenSanctions MSB copy has only {len(recs)} firms")
        return recs
