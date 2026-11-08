"""Command line entry point.

  python -m regtracker run                 # daily: fetch, diff, publish
  python -m regtracker run --sources eba   # just one source
  python -m regtracker fca-seed --hours 5.5
  python -m regtracker doctor              # fetch everything, print a sample, save nothing
  python -m regtracker contacts --hours 5 # read firm websites for email/phone
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

from . import canada, compare, contacts, notify, sheets, usa
from .diff import SuspiciousDrop, check_drop, diff, friendly_labels
from .http import make_session
from .sources import ALL_SOURCES
from .sources.base import Context, SourceNotReady
from .sources import fca as fca_mod
from .sources.national import national_sources
from .sources.usstates import us_state_sources
from .store import (append_changes_csv, load_snapshot, recent_changes,
                    save_snapshot, write_licences_csv)

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("regtracker")


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def make_context(cfg: dict, data_dir: Path) -> Context:
    excluded = {c.strip().upper() for c in cfg.get("excluded_countries") or []}
    ctx = Context(config=cfg, session=make_session(),
                  today=dt.datetime.now(dt.timezone.utc).date().isoformat(),
                  data_dir=data_dir, excluded_countries=excluded, log=log)
    ctx.now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    return ctx


def all_sources(cfg: dict) -> dict:
    """Built-in sources plus the national registers configured in config.yaml."""
    return {**ALL_SOURCES, **national_sources(cfg), **us_state_sources(cfg)}


def covered_countries(cfg: dict, data_dir: Path) -> dict:
    """Countries whose national register is live: {country: its snapshot}.
    For these the sheet shows national data and EU-register changes are only
    logged for comparison."""
    out = {}
    for name, src in national_sources(cfg).items():
        snap = load_snapshot(data_dir, name)
        if snap:
            out[src.country] = snap
    return out


def enabled_sources(cfg: dict, only: str | None) -> list:
    known = all_sources(cfg)
    names = [n.strip() for n in only.split(",")] if only else [
        n for n, c in (cfg.get("sources") or {}).items() if (c or {}).get("enabled", True) and n != "us_states"
    ] + list(national_sources(cfg)) + list(us_state_sources(cfg))
    unknown = [n for n in names if n not in known]
    if unknown:
        sys.exit(f"Unknown source(s): {', '.join(unknown)}. Known: {', '.join(known)}")
    return [known[n] for n in names]


def assemble(cfg: dict, data_dir: Path, ctx: Context, covered: dict | None = None) -> list:
    """Master list = every source's latest snapshot (failed sources keep the last good one)."""
    covered = covered_countries(cfg, data_dir) if covered is None else covered
    records = []
    switched_off = {n for n, c in (cfg.get("sources") or {}).items() if (c or {}).get("enabled", True) is False}
    for name in all_sources(cfg):
        if name in switched_off:
            continue  # source turned off in config.yaml: its old snapshot is not shown
        snap = load_snapshot(data_dir, name).values()
        if name == "eba":  # national register replaces the EU copy where we have one
            snap = [r for r in snap if r.get("jurisdiction") not in covered]
        records.extend(snap)
    # While the one-off FCA scan is running, show the UK firms found so far.
    # They are not diffed (no alerts) until the scan completes.
    if not load_snapshot(data_dir, "fca"):
        preview = fca_mod.preview_records(ctx)
        if preview:
            log.info("FCA: showing %d UK firms found so far (scan still running)", len(preview))
            records.extend(preview)
    if any(r.get("source") in ("fintrac", "boc") for r in records):
        log.info("Canada MSB/PSP linking: %s", canada.link(records))
    from .sources import swiss_sro
    swiss_sro.apply(records, swiss_sro.load_cache(data_dir))
    if any(r.get("source") in ("fincen", "fincen_os") or str(r.get("source", "")).startswith("us_") for r in records):
        log.info("US MSB/state licence linking: %s",
                 usa.link(records, contacts.load(data_dir, contacts.WEB_STATE)))
    return records


def cmd_contacts(args, cfg, data_dir) -> int:
    """Background job: read firms' own websites for email and phone."""
    ctx = make_context(cfg, data_dir)
    records = assemble(cfg, data_dir, ctx)
    contacts.apply(records, contacts.load(data_dir))  # websites found via registers/Wikidata
    c = cfg.get("contacts") or {}
    contacts.crawl(ctx, records, args.hours, log, workers=int(c.get("parallel_sites", 8)),
                   recheck_days=int(c.get("recheck_days", 180)))
    return 0


def cmd_run(args, cfg, data_dir) -> int:
    ctx = make_context(cfg, data_dir)
    all_events, problems, status = [], [], []
    due_file = data_dir / "state" / "last_fetch.json"
    last_fetch = json.loads(due_file.read_text()) if due_file.exists() else {}
    for src in enabled_sources(cfg, args.sources):
        log.info("── %s", src.label)
        conf = (cfg.get("sources") or {}).get("us_states" if src.name.startswith("us_") else src.name) or {}
        min_hours = float(conf.get("min_hours", 0) or 0)
        fp_now = getattr(src, "fingerprint", None)
        fp_saved = data_dir / "snapshots" / f"{src.name}.settings"
        reader_changed = bool(fp_now) and fp_saved.exists() and fp_saved.read_text() != fp_now
        if min_hours and not args.sources and not reader_changed and load_snapshot(data_dir, src.name):
            last = last_fetch.get(src.name, "")
            if last and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)).total_seconds() < min_hours * 3600:
                log.info("%s: checked at %s; next check after %g hours", src.name, last[:16], min_hours)
                status.append(f"{src.name}: not due")
                continue
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
        last_fetch[src.name] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
        due_file.parent.mkdir(parents=True, exist_ok=True)
        due_file.write_text(json.dumps(last_fetch, indent=1, sort_keys=True))
        curr = {r.key: r.to_dict() for r in recs if r.jurisdiction not in ctx.excluded_countries}
        prev = load_snapshot(data_dir, src.name)
        fp = getattr(src, "fingerprint", None)
        fp_file = data_dir / "snapshots" / f"{src.name}.settings"
        if fp and prev and (not fp_file.exists() or fp_file.read_text() != fp):
            log.info("%s: settings changed since the last snapshot; starting a fresh baseline", src.name)
            prev = {}
        if fp:
            fp_file.parent.mkdir(parents=True, exist_ok=True)
            fp_file.write_text(fp)
        if not prev:
            _, merged = diff({}, curr, ctx.now, src.removal_is_event)
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
        events, merged = diff(prev, curr, ctx.now, src.removal_is_event, track_services)
        friendly_labels(events)
        save_snapshot(data_dir, src.name, merged)
        if src.name.startswith("nat_"):
            compare.log_events(data_dir, events, "national")
        elif src.name == "eba":
            covered = covered_countries(cfg, data_dir)
            shadow = [e for e in events if e["jurisdiction"] in covered]
            compare.log_events(data_dir, shadow, "eu")
            if shadow:
                log.info("eba: %d changes in %s logged for comparison only (national register is the source there)",
                         len(shadow), ", ".join(sorted({e["jurisdiction"] for e in shadow})))
            events = [e for e in events if e["jurisdiction"] not in covered]
        all_events.extend(events)
        c = Counter(e["event_label"] for e in events)
        log.info("%s: %d records, %d changes %s", src.name, len(curr), len(events), dict(c))
        status.append(f"{src.name}: ok ({len(events)} changes)")

    if all_events:
        append_changes_csv(data_dir, all_events)

    covered = covered_countries(cfg, data_dir)
    records = assemble(cfg, data_dir, ctx, covered)
    if (cfg.get("contacts") or {}).get("enabled", True) and not args.sources \
            and not args.no_contacts:
        try:
            log.info("Contacts from registers/Wikidata: %s", contacts.enrich_registers(ctx, records, log))
        except Exception:
            log.exception("Contact lookup failed (list still published)")
    log.info("Contact details filled in: %s", contacts.apply(records, contacts.load_all(data_dir)))
    write_licences_csv(data_dir, records)

    if covered:  # keep a CSV copy of the comparison in the repo too
        rows = compare.report(data_dir, covered, load_snapshot(data_dir, "eba"), ctx.now)
        import csv as _csv
        with open(data_dir / "source_check.csv", "w", newline="", encoding="utf-8") as f:
            _csv.writer(f).writerows(rows)

    from .http import BLOCKED
    for host, text in BLOCKED.items():  # what each site's robots.txt actually says, for checking other routes
        try:
            (data_dir / "debug").mkdir(parents=True, exist_ok=True)
            (data_dir / "debug" / f"robots_{host}.txt").write_text(text[:20000])
        except Exception:
            pass
    run_info = {"finished": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "sources_status": "; ".join(status), "changes": len(all_events),
                "problems": problems}
    (data_dir / "last_run.json").write_text(json.dumps(run_info, indent=2))

    if args.no_sheets:
        log.info("Skipping Google Sheets (--no-sheets)")
    elif sheets.enabled():
        try:
            extra = {"MiCA CASPs (EU-EEA)": sheets.mica_tab(records)}
            if covered:
                extra["Source check"] = compare.report(data_dir, covered, load_snapshot(data_dir, "eba"), ctx.now)
            sheets.publish(records, recent_changes(data_dir, int(cfg.get("sheet_change_rows", 5000))), run_info, log,
                           per_country=bool(cfg.get("sheet_country_tabs", True)), extra_tabs=extra)
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
    for name in all_sources(cfg):
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
    r.add_argument("--no-contacts", action="store_true", help="skip register/Wikidata contact lookups")
    s = sub.add_parser("fca-seed")
    s.add_argument("--hours", type=float, default=5.5)
    d = sub.add_parser("doctor")
    d.add_argument("--sources")
    sub.add_parser("status")
    c = sub.add_parser("contacts")
    c.add_argument("--hours", type=float, default=5.0)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config(Path(args.config))
    data_dir = Path(args.data)
    data_dir.mkdir(parents=True, exist_ok=True)
    return {"run": cmd_run, "fca-seed": cmd_seed, "doctor": cmd_doctor,
            "status": cmd_status, "contacts": cmd_contacts}[args.cmd](args, cfg, data_dir)
