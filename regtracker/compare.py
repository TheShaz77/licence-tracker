"""Measure how far the EU (EBA) register lags each national register.

For countries with a direct national source:
- national changes are alerted as normal;
- EBA changes for that country are not alerted (they'd be duplicates), but
  both are logged to data/source_events.csv with the time they were seen;
- the "Source check" tab pairs them up (same firm, same kind of change) and
  shows how many hours later the EU copy caught up, plus firms that one
  register lists and the other doesn't.
"""
from __future__ import annotations

import csv
import datetime as dt
from collections import defaultdict
from pathlib import Path

from .sources.national import match_name

EVENT_FILE = "source_events.csv"
FIELDS = ["seen_at", "country", "source", "family", "event", "name", "match", "licence_type"]
FAMILY = {"NEW_LICENCE": "added", "REINSTATED": "added", "REMOVED": "removed", "LICENCE_LOST": "removed"}
# EBA types comparable with national payment/e-money registers
COMPARABLE = ("Payment institution", "E-money institution", "Account information service provider",
              "Exempted (small) payment institution", "Exempted (small) e-money institution")


def log_events(data_dir: Path, events: list, source_kind: str):
    rows = []
    for e in events:
        fam = FAMILY.get(e["event"])
        if not fam:
            continue
        rows.append({"seen_at": e["date"], "country": e["jurisdiction"], "source": source_kind,
                     "family": fam, "event": e["event_label"], "name": e["name"],
                     "match": match_name(e["name"]), "licence_type": e.get("licence_type", "")})
    if not rows:
        return
    p = data_dir / EVENT_FILE
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def _load_events(data_dir: Path) -> list:
    p = data_dir / EVENT_FILE
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _ts(s: str) -> dt.datetime:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s[:16] if " " in s else s[:10], fmt)
        except ValueError:
            pass
    return dt.datetime.min


def _comparable(r: dict) -> bool:
    return any(t in (r.get("licence_type") or "") for t in COMPARABLE)


def report(data_dir: Path, covered: dict, eba_snapshot: dict, now: str) -> list:
    """Rows for the 'Source check' tab. covered = {country: national snapshot}."""
    now_t = _ts(now)
    out = [["Source check: national registers vs the EU (EBA) register"],
           ["Updated (UTC)", now],
           ["How to read this", "Positive 'EU lag (hours)' = the EU register showed the change that many hours "
                                "after the national register. Checks run every 2 hours, so ±2h is noise."],
           []]

    # 1. lag on matched changes
    ev = _load_events(data_dir)
    nat = [e for e in ev if e["source"] == "national"]
    eu = defaultdict(list)
    for e in ev:
        if e["source"] == "eu":
            eu[(e["country"], e["family"], e["match"])].append(e)
    lag_rows, lags = [], defaultdict(list)
    used = set()
    for n in nat:
        cands = [e for e in eu[(n["country"], n["family"], n["match"])] if id(e) not in used]
        nt = _ts(n["seen_at"])
        best = min(cands, key=lambda e: abs((_ts(e["seen_at"]) - nt).total_seconds()), default=None)
        if best and abs((_ts(best["seen_at"]) - nt).days) <= 60:
            used.add(id(best))
            hours = round((_ts(best["seen_at"]) - nt).total_seconds() / 3600, 1)
            lags[n["country"]].append(hours)
            lag_rows.append([n["country"], n["event"], n["name"], n["seen_at"], best["seen_at"], hours])
        else:
            waited = round((now_t - nt).total_seconds() / 3600, 1)
            lag_rows.append([n["country"], n["event"], n["name"], n["seen_at"], "not yet in EU register",
                             f"waiting {waited}h"])
    for (c, fam, m), lst in eu.items():
        for e in lst:
            if id(e) not in used and c in covered:
                lag_rows.append([c, e["event"], e["name"], "not seen in national register", e["seen_at"], ""])

    out.append(["Average EU lag by country", "Matched changes", "Average hours", "Worst hours"])
    for c in sorted(covered):
        v = lags.get(c, [])
        out.append([c, len(v), round(sum(v) / len(v), 1) if v else "", max(v) if v else ""])
    out.append([])

    # 2. who is listed where, right now
    out.append(["Listed differently right now", "Country", "Firm", "Licence type"])
    for c, nat_snap in sorted(covered.items()):
        nat_names = {match_name(r["name"]): r for r in nat_snap.values() if r.get("active")}
        eu_names = {match_name(r["name"]): r for r in eba_snapshot.values()
                    if r.get("active") and r.get("jurisdiction") == c and _comparable(r)}
        only_nat = sorted(set(nat_names) - set(eu_names))
        only_eu = sorted(set(eu_names) - set(nat_names))
        out.append([f"{c}: {len(nat_names)} in national register, {len(eu_names)} in EU register, "
                    f"{len(only_nat)} only national, {len(only_eu)} only EU"])
        for m in only_nat[:300]:
            r = nat_names[m]
            out.append(["Only in national register", c, r["name"], r.get("licence_type", "")])
        for m in only_eu[:300]:
            r = eu_names[m]
            out.append(["Only in EU register", c, r["name"], r.get("licence_type", "")])
    out.append([])

    # 3. the event log
    out.append(["Changes seen", "Country / event", "Firm", "National register saw it", "EU register saw it",
                "EU lag (hours)"])
    for row in sorted(lag_rows, key=lambda r: str(r[3]), reverse=True)[:2000]:
        out.append(["", f"{row[0]} · {row[1]}", row[2], row[3], row[4], row[5]])
    width = max(len(r) for r in out)
    return [r + [""] * (width - len(r)) for r in out]
