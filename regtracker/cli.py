"""Command line entry point.

  python -m regtracker run                 # daily: fetch, diff, publish
  python -m regtracker run --sources eba   # just one source
  python -m regtracker fca-seed --hours 5.5
  python -m regtracker doctor              # fetch everything, print a sample, save nothing
  python -m regtracker status              # FCA seed progress and snapshot sizes
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from collections import Counter
from pathlib import Path

import yaml

from . import notify, sheets
from .diff import SuspiciousDrop, check_drop, diff
from .http import make_session
from .sources import ALL_SOURCES
from .sources.base import Context, SourceNotReady
from .sources import fca as fca_mod
from .store import (append_changes_csv, load_snapshot, recent_changes,
                    save_snapshot, write_licences_csv)

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("regtracker")


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def make_context(cfg: dict, data_dir: Path) -> Context:
    excluded = {c.strip().upper() for c in cfg.get("excluded_countries") or []}
    return Context(config=cfg, session=make_session(),
                   today=dt.datetime.now(dt.timezone.utc).date().isoformat(),
                   data_dir=data_dir, excluded_countries=excluded, log=log)


def enabled_sources(cfg: dict, only: str | None) -> list:
    names = [n.strip() for n in only.split(",")] if only else [
        n for n, c in (cfg.get("sources") or {}).items() if (c or {}).get("enabled", True)]
    unknown = [n for n in names if n not in ALL_SOURCES]
    if unknown:
        sys.exit(f"Unknown source(s): {', '.join(unknown)}. Known: {', '.join(ALL_SOURCES)}")
    return [ALL_SOURCES[n] for n in names]


def cmd_run(args, cfg, data_dir) -> int:
    ctx = make_context(cfg, data_dir)
    all_events, problems, status = [], [], []
    for src in enabled_sources(cfg, args.sources):
        log.info("── %s", src.label)
        try:
            recs = src.fetch(ctx)
        except SourceNotReady as e:
            log.warning("%s skipped: %s", src.name, e)
            status.append(f"{src.name}: waiting ({e})")
            continue
        except Exception as e:
            log.exception("%s failed", src.name)
            problems.append(f"{src.label}: {e}")
            status.append(f"{src.name}: FAILED")
            continue
        curr = {r.key: r.to_dict() for r in recs if r.jurisdiction not in ctx.excluded_countries}
        prev = load_snapshot(data_dir, src.name)
        if not prev:
            _, merged = diff({}, curr, ctx.today, src.removal_is_event)
            save_snapshot(data_dir, src.name, merged)
            log.info("%s: baseline saved with %d records (no change alerts on the first run)", src.name, len(curr))
            status.append(f"{src.name}: baseline {len(curr)}")
            continue
        try:
            check_drop(prev, curr, float(cfg.get("drop_guard", 0.7)))
        except SuspiciousDrop as e:
            log.error("%s: %s", src.name, e)
            problems.append(f"{src.label}: {e}")
            status.append(f"{src.name}: FAILED (drop guard)")
            continue
        track_services = bool((cfg.get("sources", {}).get(src.name) or {}).get("track_services", True))
        events, merged = diff(prev, curr, ctx.today, src.removal_is_event, track_services)
        save_snapshot(data_dir, src.name, merged)
        all_events.extend(events)
        c = Counter(e["event_label"] for e in events)
        log.info("%s: %d records, %d changes %s", src.name, len(curr), len(events), dict(c))
        status.append(f"{src.name}: ok ({len(events)} changes)")

    if all_events:
        append_changes_csv(data_dir, all_events)

    # master list = every source's latest snapshot (failed sources keep yesterday's)
    records = []
    for name in ALL_SOURCES:
        records.extend(load_snapshot(data_dir, name).values())
    # While the one-off FCA scan is running, show the UK firms found so far.
    # They are not diffed (no alerts) until the scan completes.
    if not load_snapshot(data_dir, "fca"):
        preview = fca_mod.preview_records(ctx)
        if preview:
            log.info("FCA: showing %d UK firms found so far (scan still running)", len(preview))
            records.extend(preview)
    write_licences_csv(data_dir, records)

    run_info = {"finished": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "sources_status": "; ".join(status), "changes": len(all_events),
                "problems": problems}
    (data_dir / "last_run.json").write_text(json.dumps(run_info, indent=2))

    if args.no_sheets:
        log.info("Skipping Google Sheets (--no-sheets)")
    elif sheets.enabled():
        try:
            sheets.publish(records, recent_changes(data_dir, int(cfg.get("sheet_change_rows", 5000))), run_info, log,
                           per_country=bool(cfg.get("sheet_country_tabs", True)))
        except Exception as e:
            log.exception("Google Sheets update failed")
            problems.append(f"Google Sheets: {e}")
    else:
        log.info("Google Sheets not configured (GOOGLE_SERVICE_ACCOUNT_JSON / SHEET_ID); CSV written to data/")

    if notify.enabled() and (all_events or problems) and not args.no_email:
        try:
            notify.send(all_events, problems, log, ctx.today)
        except Exception:
            log.exception("Alert failed")
    log.info("Done: %s", run_info["sources_status"])
    return 1 if problems else 0


def cmd_seed(args, cfg, data_dir) -> int:
    ctx = make_context(cfg, data_dir)
    try:
        fca_mod.seed(ctx, args.hours)
    except SourceNotReady as e:
        log.error("%s", e)
        return 1
    return 0


def cmd_status(args, cfg, data_dir) -> int:
    ctx = make_context(cfg, data_dir)
    print("FCA seed:", fca_mod.seed_progress(fca_mod.load_state(ctx)))
    for name in ALL_SOURCES:
        snap = load_snapshot(data_dir, name)
        act = sum(1 for r in snap.values() if r.get("active"))
        print(f"{name:6} {len(snap):6} records, {act} active")
    return 0


def cmd_doctor(args, cfg, data_dir) -> int:
    ctx = make_context(cfg, data_dir)
    rc = 0
    for src in enabled_sources(cfg, args.sources):
        if src.name == "fca":
            client = fca_mod.FCAClient(ctx, 2)
            firm = client.firm(900562)
            print("\nFCA sample lookup FRN 900562:", json.dumps(firm, indent=1)[:1500])
            continue
        try:
            recs = src.fetch(ctx)
        except Exception as e:
            print(f"\n{src.name}: FAILED – {e}")
            rc = 1
            continue
        types = Counter(r.licence_type for r in recs)
        juris = Counter(r.jurisdiction for r in recs)
        print(f"\n{src.name}: {len(recs)} records")
        print("  types:", dict(types.most_common(10)))
        print("  jurisdictions:", dict(juris.most_common(12)))
        for r in recs[:3]:
            print("  e.g.", json.dumps(r.to_dict(), ensure_ascii=False)[:400])
    return rc


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="regtracker")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--data", default=str(ROOT / "data"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--sources")
    r.add_argument("--no-sheets", action="store_true")
    r.add_argument("--no-email", action="store_true")
    s = sub.add_parser("fca-seed")
    s.add_argument("--hours", type=float, default=5.5)
    d = sub.add_parser("doctor")
    d.add_argument("--sources")
    sub.add_parser("status")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config(Path(args.config))
    data_dir = Path(args.data)
    data_dir.mkdir(parents=True, exist_ok=True)
    return {"run": cmd_run, "fca-seed": cmd_seed, "doctor": cmd_doctor,
            "status": cmd_status}[args.cmd](args, cfg, data_dir)
