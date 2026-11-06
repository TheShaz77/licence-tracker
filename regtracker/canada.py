"""Link Canadian MSBs (FINTRAC) to the Bank of Canada PSP lists (RPAA).

There is no shared ID between the two, so firms are matched on legal and
operating/trade names, normalised (accents, punctuation and legal forms such
as Inc., Ltd., Corp. removed).
"""
from __future__ import annotations

from .sources.national import match_name

STATUS = {
    "PSP (RPAA registered)": "PSP registered",
    "PSP applicant (RPAA)": "PSP applied",
    "PSP refused (RPAA)": "PSP refused",
    "PSP registration revoked (RPAA)": "PSP revoked",
}
NONE = "No PSP registration or application"
RANK = {"PSP registered": 0, "PSP applied": 1, "PSP revoked": 2, "PSP refused": 3}


def link(records: list) -> dict:
    """Fill psp_status (and email/website where missing) on CA records, in place.
    Returns counts per status for the log."""
    index = {}
    for r in records:
        if r.get("source") != "boc":
            continue
        st = STATUS.get(r.get("licence_type", ""))
        if not st:
            continue
        r["psp_status"] = st
        names = [r.get("name", ""), (r.get("extra") or {}).get("trade_name", "")]
        for n in names:
            m = match_name(n)
            if len(m) < 3:
                continue
            cur = index.get(m)
            if cur is None or RANK[st] < RANK[cur["psp_status"]]:
                index[m] = r
    counts = {}
    for r in records:
        if r.get("source") != "fintrac":
            continue
        names = [r.get("name", "")] + [n for n in ((r.get("extra") or {}).get("other_names", "")).split("; ") if n]
        hit = None
        for n in names:
            m = match_name(n)
            if len(m) >= 3 and m in index:
                cand = index[m]
                if hit is None or RANK[cand["psp_status"]] < RANK[hit["psp_status"]]:
                    hit = cand
        if hit:
            r["psp_status"] = hit["psp_status"]
            r["email"] = r.get("email") or hit.get("email", "")
            r["website"] = r.get("website") or hit.get("website", "")
            hit["_matched_msb"] = True
        else:
            r["psp_status"] = NONE
        counts[r["psp_status"]] = counts.get(r["psp_status"], 0) + 1
    for r in records:
        if r.get("source") == "boc":
            r["status_detail"] = (r.get("status_detail", "") +
                                  ("; also a FINTRAC MSB" if r.pop("_matched_msb", False) else "; not found as a FINTRAC MSB"))
    return counts
