"""UK FCA Financial Services Register API: EMIs, small EMIs, APIs, SPIs and cryptoasset firms.

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
import re
import os
import time
from pathlib import Path

from ..http import RateLimiter
from ..model import Record, guess_business_type, tolerant_get
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
        self.crypto = set(crypto_frns(ctx))
        self.exclude = tuple((ctx.source_config("fca").get("exclude_types")) or ("RAISP",))
        self.agents = tuple(ctx.source_config("fca").get("agents", ["emd", "psd"]) or ())
        self.cfd = cfd_enabled(ctx)

    def get(self, path: str):
        """GET an API path. Returns the 'Data' payload, None when the firm
        doesn't exist, or raises after repeated errors."""
        body = self.get_body(path)
        return (body.get("Data") or None) if isinstance(body, dict) else None

    def get_body(self, path: str):
        """GET an API path and return the whole JSON body (Data plus ResultInfo for searches)."""
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
            return body
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
        data = data if isinstance(data, dict) else None
        if data and not getattr(self, "_mlr_sampled", False) and tolerant_get(data, "MLRs Status"):
            self._mlr_sampled = True  # one raw example, to confirm how crypto firms are marked
            try:
                p = self.data_dir / "debug" / "fca_mlr_sample.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"frn": frn, "firm": data}, indent=1, ensure_ascii=False))
            except Exception:
                pass
        return data

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


CRYPTO_TYPE = "Cryptoasset firm (MLR registered)"
CLASSIFY_VERSION = 2   # 2 = also keeps e-money/payment agents and crypto firms: re-scan what was scanned before
AGENT_INACTIVE = ("cancel", "revok", "no longer", "former", "lapsed", "ceased", "removed", "withdrawn")
AGENT_FIELDS = (("emd", ("E-Money Agent Status", "EMoney Agent Status"), "E-money agent",
                 ("E-Money Agent Effective Date",)),
                ("psd", ("PSD Agent Status",), "Payment agent (PSD)", ("PSD Agent Effective date", "PSD Agent Effective Date")))


def agent_status(firm: dict, kinds=("emd", "psd")):
    """(licence type, active, status text, date) for firms registered as an
    e-money (EMD) or payment (PSD) agent, else None."""
    for kind, fields, ltype, dates in AGENT_FIELDS:
        if kind not in kinds:
            continue
        val = str(tolerant_get(firm, *fields)).strip()
        if val and val.lower() not in ("n/a", "none", "not applicable"):
            active = not any(w in val.lower() for w in AGENT_INACTIVE)
            return ltype, active, val, str(tolerant_get(firm, *dates))[:10]
    return None


def crypto_status(frn: int, firm: dict, crypto_frns=()) -> tuple:
    """(is a cryptoasset firm?, registration active?). The FCA marks crypto
    firms with Firm type "MLD cryptoassets"; firms that are also EMIs, PIs or
    banks keep their main type, so those come from the FCA's list of
    registered cryptoasset firms (data/fca_crypto_frns.txt)."""
    mlr = str(tolerant_get(firm, "MLRs Status", "MLR Status")).strip().lower()
    btype = str(tolerant_get(firm, "Business Type", "Firm Type")).lower()
    is_crypto = "crypto" in btype or int(frn) in crypto_frns
    if not is_crypto:
        return False, False
    active = mlr.startswith("mlrs registered") or mlr == "registered" or (
        not mlr and str(tolerant_get(firm, "Status")).strip().lower() not in INACTIVE_FIRM_STATUSES)
    return True, active


def record_from_firm(frn: int, firm: dict, address: dict | None = None, crypto_frns=(), exclude=("RAISP",),
                     agents=("emd", "psd")):
    psd = tolerant_get(firm, "PSD / EMD Status", "PSD/EMD Status")
    status = tolerant_get(firm, "Status")
    cls = classify(psd, status)
    if cls and cls[0] in exclude:
        cls = None
    is_crypto, crypto_active = crypto_status(frn, firm, crypto_frns)
    agent_date = ""
    if not cls and not is_crypto:
        ag = agent_status(firm, agents)
        if not ag:
            return None
        cls = (ag[0], ag[1])
        psd, agent_date = ag[2], ag[3]
    if cls and is_crypto:
        ltype, active = f"{cls[0]} + cryptoasset firm", cls[1] or crypto_active
    elif cls:
        ltype, active = cls
    else:
        ltype, active = CRYPTO_TYPE, crypto_active
    mlr = str(tolerant_get(firm, "MLRs Status", "MLR Status")).strip()
    if mlr and not psd:
        psd = mlr
    elif mlr and is_crypto:
        psd = f"{psd}; {mlr}"
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
        status_date=agent_date or str(tolerant_get(firm, "PSD / EMD Effective Date", "MLRs Status Effective Date",
                                                   "Status Effective Date"))[:10],
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


# ---------------------------------------------------------------- CFD providers ---
# FCA permissions list the investment types a firm may deal in. A firm that may deal in contracts
# for differences, spread bets or rolling spot forex is a CFD/FX provider (retail broker, prime
# broker or liquidity provider). Only authorised firms are checked (one extra call each).
CFD_RX = re.compile(r"contracts?\W{0,3}for\W{0,3}differences?|spread\W?bet|rolling spot forex", re.I)
CFD_TYPE = "CFD / FX provider (FCA permission to deal in CFDs)"


def cfd_enabled(ctx_or_cfg) -> bool:
    cfg = ctx_or_cfg if isinstance(ctx_or_cfg, dict) else ctx_or_cfg.source_config("fca")
    return bool((cfg.get("cfd") or {}).get("enabled", False))


def needs_cfd_check(firm: dict) -> bool:
    status = str(tolerant_get(firm, "Status")).strip().lower()
    return status.startswith("authorised") or status == "eea authorised"


DEALING_PERMS = re.compile(r"dealing in investments as (principal|agent)|arranging \(?bringing about\)? deals|"
                           r"making arrangements with a view to transactions|operating an? (multilateral|organised) trading",
                           re.I)


def _perm_dict(perms):
    """The permissions as {permission name: details} (the API gives a dict or a list of dicts)."""
    data = perms.get("Data", perms) if isinstance(perms, dict) else perms
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        merged = {}
        for x in data:
            merged.update(x)
        data = merged
    return data


def cfd_kinds(perms) -> list:
    """Which CFD-type investments the firm may deal in or arrange (CFDs under 'Managing
    investments' alone, as at many fund managers, don't count)."""
    data = _perm_dict(perms)
    if isinstance(data, dict) and data:
        text = json.dumps({k: v for k, v in data.items() if DEALING_PERMS.search(str(k))}, ensure_ascii=False)
    else:
        text = json.dumps(perms, ensure_ascii=False) if perms else ""
    text = re.sub(r"\(\s*excluding[^)]*\)", "", text, flags=re.I)   # "CFDs (excluding spread bets ...)"
    found = []
    for label, rx in (("CFDs", r"contracts?\W{0,3}for\W{0,3}differences?"), ("spread bets", r"spread\W?bet"),
                      ("rolling spot forex", r"rolling spot forex")):
        if re.search(rx, text, re.I):
            found.append(label)
    return found


def cfd_clients(perms) -> str:
    """'retail clients' when a dealing/arranging permission covers retail customers, else
    'professional clients only' (or '' when the permissions don't say)."""
    data = _perm_dict(perms)
    if not isinstance(data, dict):
        return ""
    text = json.dumps({k: v for k, v in data.items() if DEALING_PERMS.search(str(k))}, ensure_ascii=False)
    if not re.search(r"customer type", text, re.I):
        return ""
    return "retail clients" if re.search(r"\bretail\b|\"all\"", text, re.I) else "professional clients only"


def cfd_record(frn: int, firm: dict, kinds: list, address: dict | None = None, clients: str = "") -> dict:
    address = address or {}
    status = str(tolerant_get(firm, "Status")).strip()
    return Record(
        key=f"fca_cfd:{frn}", source="fca_cfd", jurisdiction="GB", regulator="FCA",
        name=str(tolerant_get(firm, "Organisation Name", "Name")).strip(),
        licence_type=CFD_TYPE, active=True,
        status_detail=f"FCA {status}; may deal in " + ", ".join(kinds) + (f" ({clients})" if clients else ""),
        status_date=str(tolerant_get(firm, "Status Effective Date"))[:10], reference=str(frn),
        city=str(tolerant_get(address, "Town")), website=str(tolerant_get(address, "Website Address", "Website")),
        email=_email_from(address) or _email_from(firm), register_url=SEARCH_URL.format(frn=frn),
        extra={"vertical": "CFD", "business_type": guess_business_type(str(tolerant_get(firm, "Organisation Name", "Name")),
                                                                     " ".join(kinds) + " " + clients),
               "clients": clients,
               "phone": str(tolerant_get(address, "Phone Number")),
               "postcode": str(tolerant_get(address, "Postcode")),
               "companies_house": str(tolerant_get(firm, "Companies House Number"))},
    ).to_dict()


def _apply_cfd(client, st: dict, frn: int, firm, perms) -> None:
    """Update st['cfd'] for one firm from its prefetched permissions (perms=None: not checked)."""
    if perms is None:
        return
    kinds = cfd_kinds(perms) if firm else []
    key = str(frn)
    if kinds:
        prev = st.setdefault("cfd", {}).get(key)
        addr = None if prev and prev.get("_enriched") else client.address(frn)
        d = cfd_record(frn, firm, kinds, addr, cfd_clients(perms))
        if addr is None and prev:
            for k in ("city", "website", "email"):
                d[k] = prev.get(k, "")
            d["extra"] = {**prev.get("extra", {}), **{k: v for k, v in d["extra"].items() if v}}
        d["_enriched"] = True
        st["cfd"][key] = d
    elif key in st.get("cfd", {}):
        st["cfd"].pop(key)       # no longer authorised for CFDs


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
                     "done": False, "checked": 0, "classify_version": CLASSIFY_VERSION},
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
    kw = {"crypto_frns": getattr(client, "crypto", ()), "exclude": getattr(client, "exclude", ("RAISP",)),
          "agents": getattr(client, "agents", ("emd", "psd"))}
    if enrich and (not prev or prev.get("_enriched") != ENRICH_VERSION):
        rec0 = record_from_firm(frn, firm, **kw)
        if rec0:
            addr = client.address(frn)
    rec = record_from_firm(frn, firm, addr, **kw)
    if not rec:
        if prev:  # no longer a type we track (e.g. RAISPs now excluded)
            st["tracked"].pop(str(frn), None)
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
    if replan(st, cfg, ctx.log):
        save_state(ctx, st)
    cs = cfd_seed_state(st, cfg)
    if sd.get("done") and (cs is None or cs.get("done")):
        ctx.log.info("FCA seed already complete (%d firms tracked)", len(st["tracked"]))
        return st
    client = FCAClient(ctx, float(cfg.get("requests_per_second", 4.5)))
    client.verify()
    if cs is not None and not (st.get("cfd_search") or {}).get("done"):
        # quick pass first: firms whose names look like CFD / FX brokers (see CFD_SEARCH_TERMS)
        cfd_search(client, st, ctx, time.monotonic() + hours * 3600 * 0.6, int(cfg.get("parallel_requests", 8)),
                   (cfg.get("cfd") or {}).get("search_terms"))
    if sd.get("done"):
        deadline = time.monotonic() + hours * 3600
        run_cfd_seed(client, st, cs, ctx, deadline, int(cfg.get("parallel_requests", 8)))
        ctx.log.info("FCA seed progress: %s", seed_progress(st))
        return st
    enrich = bool(cfg.get("enrich_address", True))
    dropped = [f for f, r in st["tracked"].items()
               if r.get("licence_type") in client.exclude]
    for f in dropped:
        st["tracked"].pop(f)
    if dropped:
        ctx.log.info("FCA: %d firms of excluded types (%s) removed", len(dropped), ", ".join(client.exclude))
    extra = _watchlist(ctx)
    deadline = time.monotonic() + hours * 3600
    since_save = 0
    # firms found earlier whose contact details were read by an older version
    stale = [int(f) for f, r in st["tracked"].items() if enrich and r.get("_enriched") != ENRICH_VERSION]
    if stale:
        ctx.log.info("FCA: re-reading contact details for %d firms found earlier", len(stale))
        check_many(client, st, stale, enrich, ctx, int(cfg.get("parallel_requests", 8)))
        save_state(ctx, st)
    # FRNs listed by hand and the FCA's cryptoasset firms are checked first
    # (crypto ones every chunk, so firms already tracked as EMIs pick up the label)
    todo = [f for f in extra if str(f) not in st["tracked"] or f in client.crypto]
    if todo:
        check_many(client, st, todo, enrich, ctx, int(cfg.get("parallel_requests", 8)))
        save_state(ctx, st)
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
    if cs is not None and sd.get("done") and time.monotonic() < deadline:
        run_cfd_seed(client, st, cs, ctx, deadline, workers)
    ctx.log.info("FCA seed progress: %s", seed_progress(st))
    return st


def cfd_seed_state(st: dict, cfg: dict) -> dict | None:
    """The CFD sub-scan: FRNs the payment scan has already passed (checked without CFD permissions)
    plus the older range 100000-499999 where long-established brokers sit. FRNs the payment scan
    still has to cover get their CFD check there, inline."""
    if not cfd_enabled(cfg):
        return None
    cs = st.get("cfd_seed")
    if cs is None:
        sd = st["seed"]
        extra = [list(map(int, r)) for r in ((cfg.get("cfd") or {}).get("extra_ranges") or [[100000, 499999]])]
        payment_left = [] if sd.get("done") else [list(r) for r in sd["ranges"][sd["range_index"]:]]
        if payment_left:
            payment_left[0] = [max(payment_left[0][0], sd["cursor"]), payment_left[0][1]]
        everything = extra + [list(map(int, r)) for r in (sd.get("config_ranges") or sd["ranges"])]
        ranges = [r for r in _subtract(everything, payment_left) if r[0] <= r[1]]
        cs = st["cfd_seed"] = {"ranges": ranges or [[0, -1]], "range_index": 0,
                               "cursor": (ranges or [[0]])[0][0], "checked": 0, "done": not ranges}
    return cs


def cfd_scan(client, st: dict, frns: list, ctx, workers: int = 8):
    """CFD-only lookups (firm + permissions for authorised firms); the payment index is untouched."""
    from concurrent.futures import ThreadPoolExecutor

    def fetch(frn):
        try:
            firm = client.firm(frn)
            perms = None
            if firm and needs_cfd_check(firm):
                perms = client.get(f"/Firm/{frn}/Permissions") or {}
            return frn, firm, perms, None
        except SourceNotReady:
            raise
        except Exception as e:
            return frn, None, None, e

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for frn, firm, perms, err in pool.map(fetch, frns):
            if err is not None:
                ctx.log.warning("FCA CFD FRN %s skipped: %s", frn, err)
                continue
            _apply_cfd(client, st, frn, firm, perms)


def run_cfd_seed(client, st: dict, cs: dict, ctx, deadline: float, workers: int):
    since_save = 0
    while time.monotonic() < deadline and not cs.get("done"):
        ri = cs["range_index"]
        if ri >= len(cs["ranges"]):
            cs["done"] = True
            break
        lo, hi = cs["ranges"][ri]
        start = max(cs["cursor"], lo)
        if start > hi:
            cs["range_index"] = ri + 1
            if cs["range_index"] < len(cs["ranges"]):
                cs["cursor"] = cs["ranges"][cs["range_index"]][0]
            continue
        batch = list(range(start, min(hi, start + BATCH - 1) + 1))
        cfd_scan(client, st, batch, ctx, workers)
        cs["cursor"] = batch[-1] + 1
        cs["checked"] = cs.get("checked", 0) + len(batch)
        since_save += len(batch)
        if since_save >= 2000:
            save_state(ctx, st)
            since_save = 0
            ctx.log.info("FCA CFD scan: at FRN %d, %d CFD/FX providers found so far", batch[-1], len(st.get("cfd", {})))
    save_state(ctx, st)


# Firm-name words that CFD / FX brokers, market makers and prime brokers commonly use, plus well-known
# brand names. Searching the register for these finds most UK CFD firms in hours instead of the days the
# full FRN scan takes; the FRN scan still runs afterwards to catch firms with unusual names.
CFD_SEARCH_TERMS = [
    "markets", "trading", "trade", "fx", "forex", "currency", "currencies", "spread", "cfd", "derivatives",
    "brokers", "broker", "brokerage", "securities", "prime", "liquidity", "capital markets", "global markets",
    "index", "futures", "commodities", "investments", "invest", "financial", "capital", "wealth", "exchange",
    "ig ", "cmc", "plus500", "city index", "gain capital", "saxo", "interactive brokers", "oanda", "pepperstone",
    "ic markets", "admiral", "etoro", "xtb", "tickmill", "fxcm", "fxpro", "avatrade", "swissquote", "spreadex",
    "finalto", "lmax", "marex", "stonex", "trading 212", "capital.com", "skilling", "hycm", "blackbull",
    "darwinex", "equiti", "cityindex", "ayondo", "infinox", "vantage", "ig index", "ebc", "ads securities",
    "sucden", "amana", "atfx", "fxtm", "forextime", "hantec", "alpari", "exness", "thinkmarkets", "naga",
]


def cfd_search(client, st: dict, ctx, deadline: float, workers: int, terms: list | None = None) -> None:
    """Search the register by firm-name words, then check the permissions of every authorised firm found."""
    from urllib.parse import quote
    ss = st.setdefault("cfd_search", {"terms_done": [], "checked": [], "done": False})
    checked = set(ss.get("checked") or [])
    for term in terms or CFD_SEARCH_TERMS:
        if term in ss["terms_done"]:
            continue
        if time.monotonic() > deadline:
            return
        frns, first_page_refs = set(), None
        for page in range(1, 400):
            body = client.get_body(f"/Search?q={quote(term)}&type=firm&pgnp={page}") or {}
            data = body.get("Data") or [] if isinstance(body, dict) else []
            refs = []
            for d in data if isinstance(data, list) else []:
                ref = str(tolerant_get(d, "Reference Number") or "").strip()
                status = str(tolerant_get(d, "Status") or "").lower()
                if ref.isdigit():
                    refs.append(int(ref))
                    if status.startswith("authorised") or status.startswith("eea authorised"):
                        frns.add(int(ref))
            if not refs or refs == first_page_refs:
                break               # past the end (or the API ignored the page number)
            if page == 1:
                first_page_refs = refs
            info = body.get("ResultInfo") or {}
            try:
                if page * int(info.get("per_page") or 20) >= int(info.get("total_count") or 0):
                    break
            except (TypeError, ValueError):
                pass
        todo = sorted(f for f in frns if f not in checked)
        if todo:
            cfd_scan(client, st, todo, ctx, workers)
            checked |= set(todo)
        ss["terms_done"].append(term)
        ss["checked"] = sorted(checked)
        save_state(ctx, st)
        ctx.log.info("FCA CFD search %r: %d authorised firms checked, %d CFD/FX providers so far",
                     term, len(todo), len(st.get("cfd", {})))
    ss["done"] = True
    save_state(ctx, st)


def cfd_progress(st: dict) -> str:
    cs = st.get("cfd_seed")
    if not cs:
        return ""
    total = sum(max(0, hi - lo + 1) for lo, hi in cs["ranges"])
    done = 0
    for i, (lo, hi) in enumerate(cs["ranges"]):
        if i < cs["range_index"]:
            done += hi - lo + 1
        elif i == cs["range_index"]:
            done += max(0, min(cs["cursor"], hi + 1) - lo)
    pct = 100.0 * done / total if total else 100.0
    srch = st.get("cfd_search") or {}
    s_txt = "name search done" if srch.get("done") else f"name search {len(srch.get('terms_done') or [])}/{len(CFD_SEARCH_TERMS)} terms"
    return (f"CFD scan {'complete' if cs.get('done') else f'{pct:.1f}% done'} ({s_txt}), "
            f"{len(st.get('cfd', {}))} CFD/FX providers")


def _intersect(a: list, b: list) -> list:
    out = []
    for lo, hi in a:
        for blo, bhi in b:
            l, h = max(lo, blo), min(hi, bhi)
            if l <= h:
                out.append([l, h])
    return out


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


def _scanned(sd: dict) -> list:
    """FRN intervals already scanned, from the saved progress."""
    out = list(sd.get("scanned", []))
    for i, (lo, hi) in enumerate(sd["ranges"]):
        if i < sd["range_index"]:
            out.append([lo, hi])
        elif i == sd["range_index"] and sd["cursor"] > lo:
            out.append([lo, min(sd["cursor"] - 1, hi)])
    return out


def _subtract(ranges: list, done: list) -> list:
    out = []
    for lo, hi in ranges:
        parts = [[lo, hi]]
        for dlo, dhi in done:
            nxt = []
            for a, b in parts:
                if dhi < a or dlo > b:
                    nxt.append([a, b])
                    continue
                if a < dlo:
                    nxt.append([a, dlo - 1])
                if b > dhi:
                    nxt.append([dhi + 1, b])
            parts = nxt
        out += parts
    return out


def replan(st: dict, cfg: dict, log=None) -> bool:
    """If seed_ranges in config.yaml changed (order, added or removed ranges),
    rebuild the remaining plan in the new order, keeping everything scanned."""
    sd = st["seed"]
    want = [list(map(int, r)) for r in (cfg.get("seed_ranges") or [])] or sd.get("config_ranges") or sd["ranges"]
    outdated = sd.get("classify_version", 1) < CLASSIFY_VERSION
    if not outdated and (sd.get("done") or want == sd.get("config_ranges", sd["ranges"])):
        return False
    scanned = _scanned(sd)
    remaining = _subtract(want, scanned)
    if outdated:
        # numbers scanned before agents/crypto were kept are scanned again, after the rest
        remaining += _intersect(scanned, want)
        scanned = []
        sd["classify_version"] = CLASSIFY_VERSION
        sd["done"] = False
    sd.update({"config_ranges": want, "scanned": scanned, "ranges": remaining or [[0, -1]],
               "range_index": 0, "cursor": (remaining or [[0]])[0][0]})
    if not remaining:
        sd["done"] = True
    if log:
        log.info("FCA seed: new range plan %s (already scanned: %d FRNs)", remaining,
                 sum(b - a + 1 for a, b in scanned))
    return True


def seed_progress(st: dict) -> str:
    sd = st["seed"]
    within = sd.get("config_ranges")
    scanned_before = 0
    if within:
        scanned_before = sum(b - a + 1 for a, b in _intersect(sd.get("scanned", []), within))
    total = sum(max(0, hi - lo + 1) for lo, hi in sd["ranges"]) + scanned_before
    done = scanned_before
    for i, (lo, hi) in enumerate(sd["ranges"]):
        if i < sd["range_index"]:
            done += hi - lo + 1
        elif i == sd["range_index"]:
            done += max(0, min(sd["cursor"], hi + 1) - lo)
    pct = 100.0 * done / total if total else 100.0
    cs = st.get("cfd_seed")
    both_done = sd.get("done") and (not cs or cs.get("done"))
    pay = "payment scan done" if sd.get("done") else f"{pct:.1f}% scanned"
    return (f"{'complete' if both_done else pay}; {len(st['tracked'])} payment/e-money firms found"
            + (f"; {cfd_progress(st)}" if cs else ""))


BATCH = 200


def check_many(client, st, frns, enrich, ctx, workers: int = 8) -> dict:
    """Look up many FRNs with several requests in flight (each lookup takes
    ~0.8 s, so one at a time can't reach the FCA's rate limit). The shared
    rate limiter still caps the total. State is only touched here, in the
    main thread. Returns {frn: result}."""
    from concurrent.futures import ThreadPoolExecutor

    check_cfd = getattr(client, "cfd", False)

    def fetch(frn):
        try:
            firm = client.firm(frn)
            perms = None
            if check_cfd and firm and needs_cfd_check(firm):
                perms = client.get(f"/Firm/{frn}/Permissions") or {}
            elif check_cfd and str(frn) in st.get("cfd", {}):
                perms = {}            # was a CFD firm, now not authorised: drop it
            return frn, firm, perms, None
        except SourceNotReady:
            raise
        except Exception as e:
            return frn, None, None, e

    results = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for frn, firm, perms, err in pool.map(fetch, [int(f) for f in frns]):
            if err is not None:
                ctx.log.warning("FCA FRN %s skipped: %s", frn, err)
                results[frn] = "error"
                continue
            try:
                if check_cfd:
                    _apply_cfd(client, st, frn, firm, perms)
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


def _read_frns(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line.isdigit() and int(line) not in out:
            out.append(int(line))
    return out


def crypto_frns(ctx: Context) -> list:
    return _read_frns(ctx.data_dir / "fca_crypto_frns.txt")


def _watchlist(ctx: Context) -> list:
    out = _read_frns(ctx.data_dir / "fca_watch_frns.txt")
    return out + [f for f in crypto_frns(ctx) if f not in out]


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


class FCACFDSource(Source):
    """UK CFD / FX providers found by the FCA scan (firms permitted to deal in CFDs, spread bets or
    rolling spot forex). Shown in the 'United Kingdom (CFD)' tab."""
    name = "fca_cfd"
    label = "UK FCA CFD / FX providers"
    removal_is_event = True

    def fetch(self, ctx: Context) -> list:
        cfg = ctx.source_config("fca")
        if not cfd_enabled(cfg):
            err = SourceNotReady("CFD checks are off (fca: cfd: enabled: false)")
            err.quiet = True
            raise err
        st = load_state(ctx)
        cs = st.get("cfd_seed") or {}
        scanning = not (st["seed"].get("done") and cs.get("done"))
        # while the scan is still finding firms, every run is a quiet new baseline (no flood of
        # "new licence" alerts); once complete, changes are reported normally
        self.fingerprint = (f"fca-cfd-scanning-{len(st.get('cfd', {}))}-{cs.get('cursor', 0)}"
                            if scanning else "fca-cfd-v1")
        if not scanning:
            import datetime as dt
            weekday = int(cfg.get("full_recheck_weekday", 6))
            if dt.date.fromisoformat(ctx.today).weekday() == weekday and st.get("last_cfd_recheck") != ctx.today:
                client = FCAClient(ctx, float(cfg.get("requests_per_second", 4.5)))
                client.verify()
                frns = sorted(int(f) for f in st.get("cfd", {}))
                ctx.log.info("FCA CFD: weekly re-check of %d firms", len(frns))
                for i in range(0, len(frns), 500):
                    cfd_scan(client, st, frns[i:i + 500], ctx, int(cfg.get("parallel_requests", 8)))
                st["last_cfd_recheck"] = ctx.today
                save_state(ctx, st)
        recs = []
        for d in st.get("cfd", {}).values():
            d = {k: v for k, v in d.items() if not k.startswith("_") and k != "status"}
            recs.append(Record(**d))
        if not recs:
            err = SourceNotReady("FCA CFD scan has not found any firms yet: " + seed_progress(st))
            err.quiet = True
            raise err
        return recs
