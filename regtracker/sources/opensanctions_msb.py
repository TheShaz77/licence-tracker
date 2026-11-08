"""United States: FinCEN MSB registrants as republished by OpenSanctions.

FinCEN's own site asks automated tools not to download its MSB list (robots.txt),
so this reads OpenSanctions' daily copy of it instead: the full entity file
(entities.ftm.json), which keeps each firm's MSB activity codes and address.

LICENCE: OpenSanctions data is free only for non-commercial use (CC BY-NC 4.0).
Their terms say "any use inside a for-profit business requires a data license",
which includes prospecting for an employer.
"""
from __future__ import annotations

import hashlib
import json

from ..model import Record
from .base import Context, Source
from .fincen import ACTIVITIES
from .national import match_name, write_diagnostics

URL = "https://data.opensanctions.org/datasets/latest/us_fincen_msb/entities.ftm.json"
SITE = "https://www.opensanctions.org/datasets/us_fincen_msb/"


def _p(ent: dict, prop: str) -> list:
    return [v for v in (ent.get("properties") or {}).get(prop, []) if v]


def parse(raw: bytes) -> tuple:
    ents = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line:
            try:
                ents.append(json.loads(line))
            except ValueError:
                pass
    addresses = {e["id"]: e for e in ents if e.get("schema") == "Address"}
    schemas = {}
    firms = {}
    for e in ents:
        sc = e.get("schema", "")
        schemas[sc] = schemas.get(sc, 0) + 1
        if sc in ("Address", "Person") or not _p(e, "name"):
            continue
        name = _p(e, "name")[0].strip()
        addr = next((addresses[a] for a in _p(e, "addressEntity") if a in addresses), None)
        city = (_p(addr, "city") or [""])[0] if addr else ""
        state = (_p(addr, "state") or [""])[0] if addr else ""
        zipc = ((_p(addr, "postalCode") or [""])[0] if addr else "")[:5]
        full = (_p(addr, "full") or [""])[0] if addr else (_p(e, "address") or [""])[0]
        k = match_name(name) + ":" + (zipc or city.lower())
        first = (e.get("first_seen") or "")[:10]
        f = firms.get(k)
        codes = sorted(set(c for c in _p(e, "sector") if c.isdigit()) | set((f or {}).get("codes", [])))
        if f is None or first < f["first"]:
            first_seen = first
        else:
            first_seen = f["first"]
        firms[k] = {"name": name, "city": city, "state": state, "full": full, "codes": codes, "first": first_seen,
                    "aliases": sorted(set(_p(e, "alias")) | set((f or {}).get("aliases", []))),
                    "country": (_p(e, "country") or ["us"])[0]}
    out = []
    for k, f in firms.items():
        mt = "409" in f["codes"]
        out.append(Record(
            key="fincen_os:" + hashlib.sha1(k.encode()).hexdigest()[:12], source="fincen_os", jurisdiction="US",
            regulator="FinCEN", name=f["name"],
            licence_type="MSB – money transmitter" if mt else "MSB – other activities", active=True,
            status_detail="On FinCEN's MSB registrant list (via OpenSanctions)", status_date=f["first"],
            city=f["city"].title() if f["city"].isupper() else f["city"],
            services="; ".join(ACTIVITIES.get(c, f"Activity {c}") for c in f["codes"]),
            register_url=SITE,
            extra={"address": f["full"], "hq_state": f["state"], "dba": "; ".join(f["aliases"])[:300],
                   "foreign_location": "" if f["country"] in ("us", "usa") else f["country"].upper()}))
    return out, {"headers": sorted(schemas), "rows": len(ents), "kept": len(out), "columns": {},
                 "type_values": sorted(schemas.items(), key=lambda kv: -kv[1])}


class OpenSanctionsMSBSource(Source):
    name = "fincen_os"
    label = "US FinCEN MSBs (OpenSanctions copy; non-commercial licence)"
    fingerprint = "fincen-os-v2"

    def fetch(self, ctx: Context) -> list:
        url = ctx.source_config(self.name).get("url") or URL
        # data.opensanctions.org's robots.txt (Disallow: /) is aimed at search-engine crawlers. OpenSanctions'
        # own guide ("Downloading the data and keeping it fresh") tells users to re-fetch these files on a
        # timer, at most every 6 hours, without login; this source runs at most once every 20 hours.
        r = ctx.session.get(url, timeout=600)
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
            raise RuntimeError(f"OpenSanctions MSB copy has only {len(recs)} firms; see data/debug/fincen_os_sample.txt")
        return recs
