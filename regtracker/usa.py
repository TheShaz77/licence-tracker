"""Link US money services businesses across sources.

FinCEN (federal MSB registration) and the state money transmitter lists share
no ID, so firms are matched on normalised legal and DBA names. Fills:

  us_states     states the firm told FinCEN it operates in
  us_licences   states whose published list shows a money transmitter licence
                (+ states named on the firm's own website licence page, marked)
  fincen_status on state-list rows: whether the firm is on FinCEN's MSB list
"""
from __future__ import annotations

import re

from .sources.national import match_name

DBA_SPLIT = re.compile(r"\s+(?:dba|d/b/a|d\.b\.a\.?)\s*[:\-]?\s*|[;|]", re.I)


def _names(r: dict) -> list:
    ex = r.get("extra") or {}
    raw = [r.get("name", "")] + DBA_SPLIT.split(r.get("name", "")) + DBA_SPLIT.split(ex.get("dba", "") or "")
    out = []
    for n in raw:
        m = match_name(n)
        if len(m) >= 3 and m not in out:
            out.append(m)
    return out


def link(records: list, web_cache: dict | None = None) -> dict:
    """In place. Returns counts for the log."""
    state_rows = [r for r in records if str(r.get("source", "")).startswith("us_")]
    fincen = [r for r in records if r.get("source") in ("fincen", "fincen_os")]
    if not state_rows and not fincen:
        return {}
    published = sorted({(r.get("extra") or {}).get("licence_state", "") for r in state_rows} - {""})

    lic_by_name = {}
    for r in state_rows:
        if not r.get("active"):
            continue
        st = (r.get("extra") or {}).get("licence_state", "")
        for m in _names(r):
            lic_by_name.setdefault(m, set()).add(st)
    fin_by_name = {}
    for r in fincen:
        for m in _names(r):
            fin_by_name.setdefault(m, r)

    counts = {"fincen_with_state_licence": 0, "fincen_money_transmitters": 0, "state_rows_on_fincen": 0}
    none_text = "None on the published lists (" + ", ".join(published) + ")" if published else ""
    for r in fincen:
        ex = r.get("extra") or {}
        r["us_states"] = ex.get("us_states", "")
        states = set()
        for m in _names(r):
            states |= lic_by_name.get(m, set())
        site = _site_states(r, web_cache)
        parts = []
        if states:
            parts.append(", ".join(sorted(states)))
            counts["fincen_with_state_licence"] += 1
        if site:
            parts.append("per firm's website: " + ", ".join(s for s in site if s not in states))
        r["us_licences"] = "; ".join(p for p in parts if p and not p.endswith(": ")) or none_text
        if "money transmitter" in r.get("licence_type", "").lower():
            counts["fincen_money_transmitters"] += 1

    for r in state_rows:
        hit = None
        for m in _names(r):
            hit = fin_by_name.get(m)
            if hit:
                break
        states = set()
        for m in _names(r):
            states |= lic_by_name.get(m, set())
        r["us_licences"] = ", ".join(sorted(states)) if r.get("active") else ""
        if hit:
            counts["state_rows_on_fincen"] += 1
            r["fincen_status"] = "Registered – " + ("money transmitter" if "transmitter" in hit.get("licence_type", "").lower()
                                                      else "other MSB activities")
            r["us_states"] = hit.get("us_states") or (hit.get("extra") or {}).get("us_states", "")
            r["website"] = r.get("website") or hit.get("website", "")
        else:
            r["fincen_status"] = "Not found on FinCEN list (name may differ)"
    return counts


def _site_states(r: dict, web_cache: dict | None) -> list:
    if not web_cache or not r.get("website"):
        return []
    from .contacts import domain
    entry = web_cache.get("site:" + domain(r["website"])) or {}
    return entry.get("us_licence_states") or []
