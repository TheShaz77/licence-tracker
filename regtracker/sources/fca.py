"""UK FCA Financial Services Register API: EMIs, small EMIs, APIs, SPIs, RAISPs.

The free API only looks firms up one FRN at a time (50 requests / 10 s), with
no "list every EMI" call. So this adapter keeps its own index:

1. Seed (one-off, resumable): walk the FRN ranges in config, keep every firm
   that has a "PSD / EMD Status". Runs in chunks via `python -m regtracker
   fca-seed`.
2. Daily: re-check every tracked firm's status, and scan a window around the
   newest FRNs to catch new authorisations.

State lives in data/state/fca.json and is committed back to the repo.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ..http import RateLimiter
from ..model import Record, tolerant_get
from .base import Context, Source, SourceNotReady

API = "https://register.fca.org.uk/services/V0.1"
SEARCH_URL = "https://register.fca.org.uk/s/search?q={frn}&type=Companies"

# FCA "PSD / EMD Status" values (Register Extract handbook, April 2026)
# -> (normalised licence type, active?)
PSD_STATUS_MAP = {
    "authorised electronic money institution": ("Authorised EMI", True),
    "small electronic money institution": ("Small EMI", True),
    "authorised payment institution": ("Authorised Payment Institution", True),
    "small payment institution": ("Small Payment Institution", True),
    "registered account information service provider": ("RAISP", True),
    "cancelled - authorised emi": ("Authorised EMI", False),
    "cancelled - small emi": ("Small EMI", False),
    "cancelled - authorised pi": ("Authorised Payment Institution", False),
    "cancelled - small pi": ("Small Payment Institution", False),
    "cancelled - raisp": ("RAISP", False),
    "emd revoked - authorised emi": ("Authorised EMI", False),
    "emd revoked - small emi": ("Small EMI", False),
    "psd revoked pi": ("Payment Institution", False),
    "eea authorised": ("EEA firm (passported)", True),
    "eea authorised - contractual run-off": ("EEA firm (passported)", False),
    "eea authorised - supervised run-off": ("EEA firm (passported)", False),
    "eea authorised - temporary permission": ("EEA firm (temporary permission)", True),
}
INACTIVE_FIRM_STATUSES = {
    "no longer authorised", "cancelled", "revoked", "lapsed",
    "registration revoked", "no longer registered",
}


def classify(psd_status: str, firm_status: str):
    """Return (licence_type, active) or None if the firm isn't a payment /
    e-money firm we track."""
    key = (psd_status or "").strip().lower().replace("–", "-")
    if not key or key == "psd2 excluded":
        return None
    if key in PSD_STATUS_MAP:
        ltype, active = PSD_STATUS_MAP[key]
    elif "electronic money" in key or "emi" in key:
        ltype, active = "E-money institution (other)", not key.startswith(("cancel", "emd revoked"))
    elif "payment" in key or key.endswith(" pi"):
        ltype, active = "Payment institution (other)", not key.startswith(("cancel", "psd revoked"))
    elif "account information" in key or "raisp" in key:
        ltype, active = "RAISP", not key.startswith("cancel")
    else:
        return None
    if (firm_status or "").strip().lower() in INACTIVE_FIRM_STATUSES:
        active = False
    return ltype, active


class FCAClient:
    def __init__(self, ctx: Context, per_second: float):
        # strip(): pasted secrets often carry an invisible trailing line break
        email = os.environ.get("FCA_API_EMAIL", "").strip()
        key = os.environ.get("FCA_API_KEY", "").strip()
        if not email or not key:
            raise SourceNotReady("FCA_API_EMAIL / FCA_API_KEY secrets are not set")
        self.s = ctx.session
        self.headers = {"X-Auth-Email": email, "X-Auth-Key": key,
                        "Accept": "application/json"}
        self.rl = RateLimiter(per_second)
        self.log = ctx.log
        self.errors = 0
        self.data_dir = ctx.data_dir

    def get(self, path: str):
        """GET an API path. Returns the 'Data' payload, None when the firm
        doesn't exist, or raises after repeated errors."""
        for attempt in range(6):
            self.rl.wait()
            try:
                r = self.s.get(API + path, headers=self.headers, timeout=30)
            except Exception as e:  # network blip
                self.log.warning("FCA %s: %s (attempt %d)", path, e, attempt + 1)
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 429:
                self.log.warning("FCA rate limit hit; pausing 65s")
                time.sleep(65)
                continue
            if r.status_code == 404:
                return None
            if r.status_code in (401, 403):
                raise SourceNotReady(f"FCA API rejected the credentials (HTTP {r.status_code})")
            if r.status_code >= 400:
                self.log.warning("FCA %s: HTTP %s", path, r.status_code)
                time.sleep(5 * (attempt + 1))
                continue
            try:
                body = r.json()
            except ValueError:
                return None
            if not isinstance(body, dict):
                return None
            # The FCA reports login problems inside the JSON body
            # (FSR-API-01-01-11 Unauthorised, FSR-API-01-01-21 Forbidden),
            # sometimes with HTTP 200, so check the code, not just HTTP status.
            code = str(body.get("Status") or "")
            if code.startswith("FSR-API-01-") and not code.endswith("-00"):
                raise SourceNotReady(
                    f"FCA API rejected the email/key ({code}: {body.get('Message', '')}). "
                    "Check the FCA_API_EMAIL and FCA_API_KEY secrets.")
            data = body.get("Data")
            return data or None
        self.errors += 1
        raise RuntimeError(f"FCA {path} failed repeatedly")

    def verify(self):
        """Look up a firm that certainly exists (Barclays Bank Plc, FRN 122702).
        If that comes back empty the credentials or the API are broken, so stop
        rather than record every FRN as 'not found'."""
        if not self.firm(122702):
            raise SourceNotReady(
                "FCA API returned nothing for a known firm (FRN 122702). Check the "
                "FCA_API_EMAIL and FCA_API_KEY secrets, or try again later.")

    def firm(self, frn: int):
        data = self.get(f"/Firm/{frn}")
        if isinstance(data, list):
            data = data[0] if data else None
        return data if isinstance(data, dict) else None

    def address(self, frn: int) -> dict:
        try:
            data = self.get(f"/Firm/{frn}/Address") or []
        except Exception:
            return {}
        if not getattr(self, "_sampled", False) and data:
            self._sampled = True
            try:
                p = self.data_dir / "debug" / "fca_address_sample.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"frn": frn, "address_response": data}, indent=1, ensure_ascii=False))
            except Exception:
                pass
        if isinstance(data, dict):
            data = [data]
        best = {}
        for a in data:
            t = str(tolerant_get(a, "Address Type")).lower()
            if not best or "principal" in t:
                best = a
        return best


ENRICH_VERSION = 2   # bump to re-read every tracked firm's address/contact details once


def _email_from(d: dict) -> str:
    """Any email-looking field the API returns (field name not documented)."""
    if not isinstance(d, dict):
        return ""
    for k, v in d.items():
        if "mail" in str(k).lower() and isinstance(v, str) and "@" in v:
            return v.strip()
    return ""


def record_from_firm(frn: int, firm: dict, address: dict | None = None):
    psd = tolerant_get(firm, "PSD / EMD Status", "PSD/EMD Status")
    status = tolerant_get(firm, "Status")
    cls = classify(psd, status)
    if not cls:
        return None
    ltype, active = cls
    address = address or {}
    return Record(
        key=f"fca:{frn}",
        source="fca",
        jurisdiction="GB",
        regulator="FCA",
        name=str(tolerant_get(firm, "Organisation Name", "Name")).strip(),
        licence_type=ltype,
        active=active,
        status_detail=f"{psd}" + (f" (firm status: {status})" if status else ""),
        status_date=str(tolerant_get(firm, "PSD / EMD Effective Date", "Status Effective Date"))[:10],
        reference=str(frn),
        city=str(tolerant_get(address, "Town")),
        website=str(tolerant_get(address, "Website Address", "Website")),
        email=_email_from(address) or _email_from(firm),
        register_url=SEARCH_URL.format(frn=frn),
        extra={
            "companies_house": str(tolerant_get(firm, "Companies House Number")),
            "postcode": str(tolerant_get(address, "Postcode")),
            "phone": str(tolerant_get(address, "Phone Number")),
        },
    )


# ---------------------------------------------------------------- state ---

def state_path(ctx: Context) -> Path:
    return ctx.data_dir / "state" / "fca.json"


def load_state(ctx: Context) -> dict:
    p = state_path(ctx)
    if p.exists():
        return json.loads(p.read_text())
    cfg = ctx.source_config("fca")
    ranges = cfg.get("seed_ranges") or [[900000, 999999], [500000, 899999],
                                         [1000000, 1100000], [100000, 499999]]
    return {"seed": {"ranges": ranges, "range_index": 0, "cursor": ranges[0][0],
                     "done": False, "checked": 0},
            "tracked": {}, "max_frn_seen": 0, "last_full_recheck": "",
            "last_wide_frontier": ""}


def save_state(ctx: Context, st: dict):
    p = state_path(ctx)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=0, sort_keys=True))
    tmp.replace(p)


_MISSING = object()


def _check_frn(client: FCAClient, st: dict, frn: int, enrich: bool, firm=_MISSING) -> str:
    """Look up one FRN (or use a prefetched result) and update state.
    Returns 'tracked', 'other' or 'none'."""
    if firm is _MISSING:
        firm = client.firm(frn)
    if not firm:
        return "none"
    st["max_frn_seen"] = max(st.get("max_frn_seen", 0), frn)
    prev = st["tracked"].get(str(frn))
    addr = None
    if enrich and (not prev or prev.get("_enriched") != ENRICH_VERSION):
        rec0 = record_from_firm(frn, firm)
        if rec0:
            addr = client.address(frn)
    rec = record_from_firm(frn, firm, addr)
    if not rec:
        return "other"
    d = rec.to_dict()
    if addr is None and prev:  # keep earlier enrichment
        for k in ("city", "website", "email"):
            d[k] = prev.get(k, "")
        d["extra"] = {**prev.get("extra", {}), "companies_house": d["extra"].get("companies_house", "")}
        d["_enriched"] = prev.get("_enriched", False)
    else:
        d["_enriched"] = ENRICH_VERSION if enrich else False
    st["tracked"][str(frn)] = d
    return "tracked"


def seed(ctx: Context, hours: float) -> dict:
    """Continue the one-off FRN scan for up to `hours`. Safe to re-run."""
    cfg = ctx.source_config("fca")
    st = load_state(ctx)
    sd = st["seed"]
    if sd.get("done"):
        ctx.log.info("FCA seed already complete (%d firms tracked)", len(st["tracked"]))
        return st
    client = FCAClient(ctx, float(cfg.get("requests_per_second", 4.5)))
    client.verify()
    enrich = bool(cfg.get("enrich_address", True))
    extra = _watchlist(ctx)
    deadline = time.monotonic() + hours * 3600
    since_save = 0
    # firms found earlier whose contact details were read by an older version
    stale = [int(f) for f, r in st["tracked"].items() if enrich and r.get("_enriched") != ENRICH_VERSION]
    if stale:
        ctx.log.info("FCA: re-reading contact details for %d firms found earlier", len(stale))
        check_many(client, st, stale, enrich, ctx, int(cfg.get("parallel_requests", 8)))
        save_state(ctx, st)
    # FRNs listed by hand are checked first
    for frn in extra:
        if str(frn) not in st["tracked"]:
            _safe_check(client, st, frn, enrich, ctx)
    workers = int(cfg.get("parallel_requests", 8))
    while time.monotonic() < deadline:
        ri = sd["range_index"]
        if ri >= len(sd["ranges"]):
            sd["done"] = True
            break
        lo, hi = sd["ranges"][ri]
        start = max(sd["cursor"], lo)
        if start > hi:
            sd["range_index"] = ri + 1
            if sd["range_index"] < len(sd["ranges"]):
                sd["cursor"] = sd["ranges"][sd["range_index"]][0]
            continue
        batch = list(range(start, min(hi, start + BATCH - 1) + 1))
        check_many(client, st, batch, enrich, ctx, workers)
        sd["cursor"] = batch[-1] + 1
        sd["checked"] = sd.get("checked", 0) + len(batch)
        since_save += len(batch)
        if since_save >= 2000:
            save_state(ctx, st)
            since_save = 0
            ctx.log.info("FCA seed: at FRN %d, %d payment firms found so far",
                         batch[-1], len(st["tracked"]))
    save_state(ctx, st)
    ctx.log.info("FCA seed progress: %s", seed_progress(st))
    return st


def preview_records(ctx: Context) -> list:
    """UK firms found so far by an unfinished seed scan, as record dicts."""
    p = state_path(ctx)
    if not p.exists():
        return []
    st = json.loads(p.read_text())
    if st["seed"].get("done"):
        return []
    out = []
    for d in st["tracked"].values():
        d = {k: v for k, v in d.items() if not k.startswith("_")}
        d["first_seen"] = d.get("first_seen") or ""
        d["last_changed"] = d.get("last_changed") or ""
        out.append(d)
    return out


def seed_progress(st: dict) -> str:
    sd = st["seed"]
    total = sum(hi - lo + 1 for lo, hi in sd["ranges"])
    done = 0
    for i, (lo, hi) in enumerate(sd["ranges"]):
        if i < sd["range_index"]:
            done += hi - lo + 1
        elif i == sd["range_index"]:
            done += max(0, min(sd["cursor"], hi + 1) - lo)
    pct = 100.0 * done / total if total else 100.0
    return (f"{'complete' if sd.get('done') else f'{pct:.1f}% scanned'}; "
            f"{len(st['tracked'])} payment/e-money firms found")


BATCH = 200


def check_many(client, st, frns, enrich, ctx, workers: int = 8) -> dict:
    """Look up many FRNs with several requests in flight (each lookup takes
    ~0.8 s, so one at a time can't reach the FCA's rate limit). The shared
    rate limiter still caps the total. State is only touched here, in the
    main thread. Returns {frn: result}."""
    from concurrent.futures import ThreadPoolExecutor

    def fetch(frn):
        try:
            return frn, client.firm(frn), None
        except SourceNotReady:
            raise
        except Exception as e:
            return frn, None, e

    results = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for frn, firm, err in pool.map(fetch, [int(f) for f in frns]):
            if err is not None:
                ctx.log.warning("FCA FRN %s skipped: %s", frn, err)
                results[frn] = "error"
                continue
            try:
                results[frn] = _check_frn(client, st, frn, enrich, firm=firm)
            except SourceNotReady:
                raise
            except Exception as e:
                ctx.log.warning("FCA FRN %s skipped: %s", frn, e)
                results[frn] = "error"
    return results


def _safe_check(client, st, frn, enrich, ctx) -> str:
    try:
        return _check_frn(client, st, int(frn), enrich)
    except SourceNotReady:
        raise
    except Exception as e:
        ctx.log.warning("FCA FRN %s skipped: %s", frn, e)
        return "error"


def _watchlist(ctx: Context) -> list:
    p = ctx.data_dir / "fca_watch_frns.txt"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line.isdigit():
            out.append(int(line))
    return out


class FCASource(Source):
    name = "fca"
    label = "UK FCA (payment & e-money firms)"
    removal_is_event = False

    def fetch(self, ctx: Context) -> list:
        cfg = ctx.source_config("fca")
        st = load_state(ctx)
        if not st["seed"].get("done"):
            raise SourceNotReady("FCA seed scan still running: " + seed_progress(st))
        client = FCAClient(ctx, float(cfg.get("requests_per_second", 4.5)))
        client.verify()
        enrich = bool(cfg.get("enrich_address", True))

        # 1. Re-check tracked firms: active ones daily, all of them weekly.
        weekday_full = int(cfg.get("full_recheck_weekday", 6))  # Sunday
        import datetime as dt
        today = dt.date.fromisoformat(ctx.today)
        # once a week (runs are every 2 hours, so only the first run that day)
        full = (not st.get("last_full_recheck")
                or (today.weekday() == weekday_full and st.get("last_full_recheck") != ctx.today))
        frns = [int(f) for f, r in st["tracked"].items() if full or r.get("active")]
        for frn in _watchlist(ctx):
            if str(frn) not in st["tracked"]:
                frns.append(frn)
        ctx.log.info("FCA: re-checking %d firms (%s)", len(frns), "full" if full else "active only")
        workers = int(cfg.get("parallel_requests", 8))
        for i in range(0, len(frns), 1000):
            check_many(client, st, frns[i:i + 1000], enrich, ctx, workers)
            save_state(ctx, st)
        if full:
            st["last_full_recheck"] = ctx.today

        # 2. Frontier scan for newly issued FRNs.
        back = int(cfg.get("frontier_back", 3000))
        ahead = int(cfg.get("frontier_ahead", 2000))
        if full:
            back = int(cfg.get("frontier_back_weekly", 25000))
        top = st.get("max_frn_seen", 0)
        lo, hi = max(1, top - back), top + ahead
        ctx.log.info("FCA: scanning FRN window %d-%d for new firms", lo, hi)
        misses = 0
        frn = lo
        while frn <= hi or misses < 500:
            batch = list(range(frn, frn + BATCH))
            res = check_many(client, st, batch, enrich, ctx, workers)
            for f in batch:  # in FRN order
                if f > top:
                    found = res.get(f) in ("tracked", "other")
                    misses = 0 if found else misses + 1
                    if found:
                        hi = max(hi, f + ahead)
            frn = batch[-1] + 1
            if frn > hi and misses >= 500:
                break
        save_state(ctx, st)
        if client.errors:
            ctx.log.warning("FCA: %d lookups failed; those firms keep their previous status", client.errors)

        recs = []
        for d in st["tracked"].values():
            d = {k: v for k, v in d.items() if not k.startswith("_") and k != "status"}
            recs.append(Record(**d))
        return recs
