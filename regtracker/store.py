"""Snapshots and logs, kept as files in the repo's data/ folder."""
from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path

from .diff import CHANGE_COLUMNS
from .model import SHEET_COLUMNS


def snapshot_path(data_dir: Path, source: str) -> Path:
    return data_dir / "snapshots" / f"{source}.json.gz"


def load_snapshot(data_dir: Path, source: str) -> dict:
    p = snapshot_path(data_dir, source)
    if not p.exists():
        return {}
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


def save_snapshot(data_dir: Path, source: str, records: dict):
    p = snapshot_path(data_dir, source)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    # mtime=0 keeps the file byte-identical when nothing changed (clean git diffs)
    with open(tmp, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        gz.write(json.dumps(records, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    tmp.replace(p)


def sort_records(records: list) -> list:
    return sorted(records, key=lambda r: (not r.get("active"), r.get("jurisdiction", ""),
                                          r.get("licence_type", ""), r.get("name", "").lower()))


def record_row(r: dict) -> list:
    r = dict(r)
    r["status"] = "Active" if r.get("active") else "Inactive"
    extra = r.get("extra") or {}
    for k in ("phone", "email", "ch_sro", "pix_status"):  # some sources keep these in extra
        if not r.get(k) and extra.get(k):
            r[k] = extra[k]
    return [str(r.get(k, "") or "") for k, _ in SHEET_COLUMNS]


def write_licences_csv(data_dir: Path, records: list):
    p = data_dir / "licences.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([h for _, h in SHEET_COLUMNS])
        for r in sort_records(records):
            w.writerow(record_row(r))


def event_row(e: dict) -> list:
    return [str(e.get(k, "") or "") for k, _ in CHANGE_COLUMNS]


def append_changes_csv(data_dir: Path, events: list):
    p = data_dir / "changes.csv"
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow([h for _, h in CHANGE_COLUMNS])
        for e in events:
            w.writerow(event_row(e))


def recent_changes(data_dir: Path, limit: int = 5000) -> list:
    p = data_dir / "changes.csv"
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))[1:]
    return rows[-limit:]
