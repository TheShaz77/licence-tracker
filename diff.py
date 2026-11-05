"""Compare two snapshots of one source and produce change events."""
from __future__ import annotations

import re

EVENT_LABELS = {
    "NEW_LICENCE": "New licence",
    "LICENCE_LOST": "Licence cancelled / revoked",
    "REMOVED": "Removed from register",
    "REINSTATED": "Licence reinstated",
    "TYPE_CHANGED": "Licence type changed",
    "NAME_CHANGED": "Name changed",
    "SERVICES_CHANGED": "Permissions changed",
}

CHANGE_COLUMNS = [
    ("date", "Detected"),
    ("event_label", "Event"),
    ("jurisdiction", "Jurisdiction"),
    ("regulator", "Regulator"),
    ("name", "Firm name"),
    ("licence_type", "Licence type"),
    ("old", "Before"),
    ("new", "After"),
    ("reference", "Reference no."),
    ("register_url", "Register link"),
    ("source", "Source"),
]


class SuspiciousDrop(Exception):
    """The register suddenly lists far fewer firms: almost always a broken
    download or a format change, not a mass revocation."""


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _event(kind, rec, today, old="", new=""):
    return {
        "date": today,
        "event": kind,
        "event_label": EVENT_LABELS[kind],
        "key": rec["key"],
        "source": rec["source"],
        "jurisdiction": rec.get("jurisdiction", ""),
        "regulator": rec.get("regulator", ""),
        "name": rec.get("name", ""),
        "licence_type": rec.get("licence_type", ""),
        "reference": rec.get("reference", ""),
        "register_url": rec.get("register_url", ""),
        "old": old,
        "new": new,
    }


def check_drop(prev: dict, curr: dict, threshold: float = 0.7):
    p = sum(1 for r in prev.values() if r.get("active"))
    c = sum(1 for r in curr.values() if r.get("active"))
    if p >= 50 and c < p * threshold:
        raise SuspiciousDrop(f"active firms fell from {p} to {c}; snapshot not updated")


def diff(prev: dict, curr: dict, today: str, removal_is_event: bool = True,
         track_services: bool = True):
    """prev/curr: {key: record dict}. Returns (events, merged snapshot).

    The merged snapshot keeps records that vanished from the register as
    inactive, so a later reappearance shows up as a reinstatement and the
    sheet keeps the history.
    """
    events = []
    merged = {}
    for key, c in curr.items():
        c = dict(c)
        p = prev.get(key)
        if p is None:
            c["first_seen"] = today
            c["last_changed"] = today
            if c.get("active"):
                events.append(_event("NEW_LICENCE", c, today, new=c.get("licence_type", "")))
            merged[key] = c
            continue
        c["first_seen"] = p.get("first_seen") or today
        c["last_changed"] = p.get("last_changed") or today
        changed = []
        if p.get("active") and not c.get("active"):
            changed.append(_event("LICENCE_LOST", c, today, p.get("status_detail", ""), c.get("status_detail", "")))
        elif not p.get("active") and c.get("active"):
            changed.append(_event("REINSTATED", c, today, p.get("status_detail", ""), c.get("status_detail", "")))
        if p.get("licence_type") != c.get("licence_type"):
            changed.append(_event("TYPE_CHANGED", c, today, p.get("licence_type", ""), c.get("licence_type", "")))
        if p.get("name") and _norm_name(p.get("name")) != _norm_name(c.get("name")):
            changed.append(_event("NAME_CHANGED", c, today, p.get("name", ""), c.get("name", "")))
        if (track_services and c.get("active") and p.get("services") and c.get("services")
                and p.get("services") != c.get("services")):
            changed.append(_event("SERVICES_CHANGED", c, today, p.get("services", ""), c.get("services", "")))
        if changed:
            c["last_changed"] = today
            events.extend(changed)
        merged[key] = c

    for key, p in prev.items():
        if key in curr:
            continue
        p = dict(p)
        if p.get("active") and removal_is_event:
            events.append(_event("REMOVED", p, today, p.get("status_detail", ""), "No longer listed"))
            p["active"] = False
            p["status"] = "Inactive"
            p["status_detail"] = f"Removed from register ({today})"
            p["last_changed"] = today
        merged[key] = p
    return events, merged
