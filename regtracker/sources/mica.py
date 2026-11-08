"""EU/EEA: MiCA crypto-asset registers published by ESMA (interim register).

ESMA publishes CSV files, refreshed as national regulators notify it:
  CASPS.csv  crypto-asset service providers: authorised CASPs (Article 63) and
             banks / investment firms / EMIs that notified crypto services (Article 60)
  EMTWP.csv  issuers of e-money tokens (stablecoins), one row per white paper

An entity listed twice (e.g. an authorisation later extended) is merged into one
record per LEI, with the union of services and passported countries.
"""
from __future__ import annotations

import csv
import io
import re

from ..http import robots_allowed
from ..model import Record
from .base import Context, Source, SourceNotReady
from .national import decode_bytes, match_name, write_diagnostics

BASE = "https://www.esma.europa.eu/sites/default/files/2024-12/"
REGISTER_URL = ("https://www.esma.europa.eu/esmas-activities/digital-finance-and-innovation/"
                "markets-crypto-assets-regulation-mica")
FIX_CC = {"EL": "GR", "SL": "SI", "UK": "GB"}
SERVICES = {
    "a": "custody", "b": "trading platform", "c": "exchange for funds", "d": "exchange for crypto",
    "e": "execution of orders", "f": "placing", "g": "reception & transmission of orders",
    "h": "advice", "i": "portfolio management", "j": "transfer services",
}
PHRASES = [("custody", "a"), ("trading platform", "b"), ("for funds", "c"), ("for other crypto", "d"),
           ("exchange of crypto-assets for other", "d"), ("execution of orders", "e"), ("placing", "f"),
           ("reception and transmission", "g"), ("advice", "h"), ("portfolio management", "i"),
           ("transfer services", "j")]


def cc(x: str) -> str:
    x = (x or "").strip().upper()[:2]
    return FIX_CC.get(x, x)


def eu_date(s: str) -> str:
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", (s or "").strip())
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else (s or "").strip()


def service_letters(text: str) -> list:
    t = (text or "").lower()
    found = {letter for phrase, letter in PHRASES if phrase in t}
    found |= set(re.findall(r"(?:^|\|)\s*([a-j])\.", t))
    return sorted(found)


def short_authority(name: str) -> str:
    m = re.search(r"\(([^)]+)\)\s*$", name or "")
    return m.group(1) if m else (name or "").strip()


def _rows(raw: bytes) -> list:
    text = decode_bytes(raw).lstrip("﻿")
    return list(csv.DictReader(io.StringIO(text)))


def parse_casps_info(raw: bytes) -> tuple:
    rows = _rows(raw)
    info = {"headers": list(rows[0].keys()) if rows else [], "rows": len(rows), "columns": {}}
    out = {}
    bases = {}
    for r in rows:
        name = (r.get("ae_lei_name") or "").strip()
        lei = (r.get("ae_lei") or "").strip().upper()
        if not name:
            continue
        basis = (r.get("ac_legal_basis") or "").strip()
        bases[basis] = bases.get(basis, 0) + 1
        kind = ("MiCA crypto services – notified financial firm (Art. 60)" if "60" in basis
                else "MiCA CASP (authorised)")
        key = "mica:" + (lei or match_name(name))
        letters = service_letters(r.get("ac_serviceCode", ""))
        countries = sorted({cc(x) for x in re.split(r"[|/,]|\s+I\s+|\s+", r.get("ac_serviceCode_cou", "")) if x.strip()})
        ended = (r.get("ac_authorisationEndDate") or "").strip()
        prev = out.get(key)
        if prev:
            letters = sorted(set(letters) | set(prev["letters"]))
            countries = sorted(set(countries) | set(prev["countries"]))
            start = min(eu_date(r.get("ac_authorisationNotificationDate")), prev["start"])
            if "Art. 60" in prev["kind"] and "authorised" in kind:
                pass
            else:
                kind = prev["kind"]
        else:
            start = eu_date(r.get("ac_authorisationNotificationDate"))
        out[key] = {"r": r, "kind": kind, "letters": letters, "countries": countries, "start": start,
                    "ended": ended, "lei": lei, "name": name}
    recs = []
    for key, e in out.items():
        r = e["r"]
        home = cc(r.get("ae_homeMemberState"))
        addr = (r.get("ae_address") or "").strip()
        city = ""
        parts = [p.strip() for p in addr.split(",") if p.strip()]
        if len(parts) >= 2:
            city = re.sub(r"^[\d\s-]+|\s*\d[\d\s-]*$", "", parts[-2]).strip() or parts[-2]
        site = (r.get("ae_website") or "").strip()
        if site and not re.match(r"https?://", site) and "." in site:
            site = "https://" + site
        recs.append(Record(
            key=key, source="mica", jurisdiction=home, regulator=short_authority(r.get("ae_competentAuthority", "")),
            name=e["name"], licence_type=e["kind"], active=not e["ended"],
            status_detail=(f"Authorisation ended {eu_date(e['ended'])}" if e["ended"]
                           else (r.get("ac_legal_basis") or "MiCA register").strip()),
            status_date=e["start"], reference=e["lei"], lei=e["lei"], city=city,
            website=site if re.match(r"https?://[^/\s]+\.[a-z]{2,}", site, re.I) else "",
            services="; ".join(f"{l}. {SERVICES[l]}" for l in e["letters"]),
            passports=", ".join(c for c in e["countries"] if c != home),
            register_url=REGISTER_URL,
            extra={"trading_name": (r.get("ae_commercial_name") or "").strip(), "address": addr,
                   "comments": (r.get("ac_comments") or "").strip()[:300]},
        ))
    info["kept"] = len(recs)
    info["type_values"] = sorted(bases.items(), key=lambda kv: -kv[1])
    return recs, info


def parse_emts_info(raw: bytes) -> tuple:
    rows = _rows(raw)
    info = {"headers": list(rows[0].keys()) if rows else [], "rows": len(rows), "columns": {}}
    out = {}
    for r in rows:
        name = (r.get("ae_lei_name") or "").strip()
        lei = (r.get("ae_lei") or "").strip().upper()
        if not name:
            continue
        key = "mica_emt:" + (lei or match_name(name))
        e = out.setdefault(key, {"r": r, "papers": 0, "first": eu_date(r.get("wp_authorisationNotificationDate"))})
        e["papers"] += 1
        e["first"] = min(e["first"], eu_date(r.get("wp_authorisationNotificationDate")))
    recs = []
    for key, e in out.items():
        r = e["r"]
        ended = (r.get("ac_authorisationEndDate") or "").strip()
        site = (r.get("ae_website") or "").strip()
        recs.append(Record(
            key=key, source="mica_emt", jurisdiction=cc(r.get("ae_homeMemberState")),
            regulator=short_authority(r.get("ae_competentAuthority", "")),
            name=(r.get("ae_lei_name") or "").strip(), licence_type="MiCA e-money token issuer",
            active=not ended,
            status_detail=f"{(r.get('ae_authorisation_other_emt') or 'EMT issuer').strip()}; {e['papers']} white paper(s)",
            status_date=e["first"], reference=(r.get("ae_lei") or "").strip(), lei=(r.get("ae_lei") or "").strip(),
            website=site if site.startswith("http") else "", register_url=REGISTER_URL,
            extra={"trading_name": (r.get("ae_commercial_name") or "").strip(),
                   "address": (r.get("ae_address") or "").strip()},
        ))
    info["kept"] = len(recs)
    return recs, info


def parse_casps(raw: bytes) -> list:
    return parse_casps_info(raw)[0]


def parse_emts(raw: bytes) -> list:
    return parse_emts_info(raw)[0]


class MiCASource(Source):
    """Both ESMA files in one source: CASPs (incl. Article 60 notifications) and EMT issuers."""
    name = "mica"
    label = "EU MiCA register (ESMA): crypto-asset service providers and e-money token issuers"
    fingerprint = "mica-v1"

    def fetch(self, ctx: Context) -> list:
        cfg = ctx.source_config(self.name)
        notes, recs = [], []
        for file, parser in (("CASPS.csv", parse_casps_info), ("EMTWP.csv", parse_emts_info)):
            url = (cfg.get("urls") or {}).get(file) or BASE + file
            if not robots_allowed(ctx.session, url):
                raise SourceNotReady(f"{url} is disallowed by robots.txt")
            r = ctx.session.get(url, timeout=120, headers={"User-Agent": "Mozilla/5.0 (compatible; licence-tracker/1.0)"})
            r.raise_for_status()
            err, info, got = None, {}, []
            try:
                got, info = parser(r.content)
            except Exception as e:
                err = e
            notes.append((url, r.content, info, err))
            if err:
                write_diagnostics(ctx, self.name, notes)
                raise err
            recs += got
        write_diagnostics(ctx, self.name, notes)
        if not recs:
            raise RuntimeError("ESMA MiCA files parsed but nothing found")
        for r in recs:
            r.source = "mica"
        return [x for x in recs if x.jurisdiction not in ctx.excluded_countries]
