"""Canada: FINTRAC Money Services Business (MSB) registry.

FINTRAC publishes the whole registry as CSV (and XLSX); it says the registry
is updated monthly. Includes Canadian and foreign MSBs, with status
Registered / Expired / Ceased / Revoked.
"""
from __future__ import annotations

import re

from ..model import Record
from .base import Context, Source
from .national import _iso, read_table, write_diagnostics

URL = "https://fintrac-canafe.canada.ca/msb-esm/reg-eng.csv"
SEARCH_URL = "https://fintrac-canafe.canada.ca/msb-esm/reg-eng"


def _find(headers: list, *needles, exclude: tuple = ()) -> int | None:
    """Index of the first header containing all needles (case-insensitive)."""
    for i, h in enumerate(headers):
        hl = h.lower()
        if all(n in hl for n in needles) and not any(x in hl for x in exclude):
            return i
    return None


def split_names(cell: str) -> list:
    """'Legal Name Inc. (Operating: Brand A; Brand B)' style cells -> names."""
    parts = re.split(r"[;\n|]+|\s{2,}", cell or "")
    out = []
    for p in parts:
        p = re.sub(r"^\s*(legal|operating)\s*(name)?\s*[:\-]\s*", "", p, flags=re.I).strip(" ,")
        if p and p not in out:
            out.append(p)
    return out


def parse_fintrac(raw: bytes) -> tuple:
    headers, rows = read_table_with_header(raw)
    c = {
        "name": _find(headers, "name"),
        "number": _find(headers, "registration number") or _find(headers, "msb", "number"),
        "status": _find(headers, "registration status") or _find(headers, "status"),
        "initial": _find(headers, "initial registration"),
        "expiry": _find(headers, "expiry"),
        "ceased": _find(headers, "cessation") or _find(headers, "ceased"),
        "revoked": _find(headers, "revocation") or _find(headers, "revoked", exclude=("status",)),
        "services": _find(headers, "service"),
        "address": _find(headers, "address"),
        "foreign": _find(headers, "foreign"),
        "website": _find(headers, "website") or _find(headers, "web site"),
        "inc_juris": _find(headers, "jurisdiction"),
    }
    info = {"headers": headers, "rows": len(rows),
            "columns": {k: (headers[v] if v is not None else None) for k, v in c.items()}}
    if c["name"] is None or c["number"] is None:
        raise RuntimeError(f"FINTRAC CSV layout changed; headers are {headers}")

    def get(row, k):
        i = c[k]
        return row[i].strip() if i is not None and i < len(row) else ""

    out, statuses = {}, {}
    for row in rows:
        num = get(row, "number")
        names = split_names(get(row, "name"))
        if not num or not names:
            continue
        status = get(row, "status") or "Unknown"
        statuses[status] = statuses.get(status, 0) + 1
        foreign = get(row, "foreign").lower() in ("y", "yes", "true", "1", "oui", "foreign")
        addr = get(row, "address")
        city = ""
        m = re.search(r",\s*([^,]+?),\s*([A-Z]{2})\b", addr)  # "..., Toronto, ON, M5V..."
        if m:
            city = m.group(1).strip()
        ended = get(row, "revoked") or get(row, "ceased") or get(row, "expiry")
        detail = status + (f" ({_iso(ended) or ended})" if ended and status.lower() != "registered" else "")
        out[num] = Record(
            key=f"fintrac:{num}", source="fintrac", jurisdiction="CA", regulator="FINTRAC",
            name=names[0], licence_type="Foreign MSB" if foreign else "MSB",
            active=status.lower().startswith("registered"), status_detail=detail,
            status_date=_iso(get(row, "initial")) or get(row, "initial"), reference=num,
            city=city, website=get(row, "website"), services=get(row, "services"),
            register_url=SEARCH_URL,
            extra={"other_names": "; ".join(names[1:]), "address": addr,
                   "expiry": _iso(get(row, "expiry")), "incorporated_in": get(row, "inc_juris")},
        )
    info["type_values"] = sorted(statuses.items(), key=lambda kv: -kv[1])
    info["kept"] = len(out)
    return list(out.values()), info


def read_table_with_header(raw: bytes) -> tuple:
    """Like read_table, but finds FINTRAC's header row (the one mentioning
    'registration' and 'name'), skipping any title lines."""
    headers, rows = read_table(raw)
    every = [headers] + rows
    for i, r in enumerate(every[:20]):
        joined = " ".join(r).lower()
        if "registration" in joined and "name" in joined:
            return [h.strip() for h in r], every[i + 1:]
    return headers, rows


class FINTRACSource(Source):
    name = "fintrac"
    label = "Canada FINTRAC MSB registry"

    def fetch(self, ctx: Context) -> list:
        cfg = ctx.source_config("fintrac")
        url = cfg.get("url") or URL
        r = ctx.session.get(url, timeout=180, headers={"User-Agent": "Mozilla/5.0 (compatible; licence-tracker)"})
        r.raise_for_status()
        err, info, recs = None, {}, []
        try:
            recs, info = parse_fintrac(r.content)
        except Exception as e:
            err = e
        write_diagnostics(ctx, self.name, [(url, r.content, info, err)])
        if err:
            raise err
        if not recs:
            raise RuntimeError("FINTRAC file parsed but no MSBs found; see data/debug/fintrac_sample.txt")
        return recs
