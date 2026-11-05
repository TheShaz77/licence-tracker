"""EU/EEA payment and e-money institutions from the EBA PSD2 central register.

The EBA publishes the whole register nightly as a zipped JSON "golden copy".
Two requests: the file metadata, then the zip (about 20 MB; ~220 MB of JSON
inside). Entities carry the supervising authority (CA_OwnerID, e.g. IE_CBI),
which gives the jurisdiction.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile

from ..model import Record, first, join
from .base import Context, Source

META_URL = "https://euclid.eba.europa.eu/register/api/filemetadata"
SEARCH_URL = "https://euclid.eba.europa.eu/register/pir/search"

TYPE_LABELS = {
    "PSD_PI": "Payment institution",
    "PSD_EMI": "E-money institution",
    "PSD_AISP": "Account information service provider",
    "PSD_EPI": "Exempted (small) payment institution",
    "PSD_EEMI": "Exempted (small) e-money institution",
    "PSD_ENL": "Entitled under national law",
    "PSD_EXC": "Excluded from PSD2 scope",
    "PSD_BR": "EEA branch",
    "PSD_AG": "Agent",
}
DEFAULT_TYPES = ["PSD_PI", "PSD_EMI", "PSD_AISP", "PSD_EPI", "PSD_EEMI"]

SERVICE_LABELS = {
    "ES_010": "E-money issuance",
    "PS_010": "Cash placement", "PS_020": "Cash withdrawal",
    "PS_03A": "Direct debits", "PS_03B": "Card/device payments", "PS_03C": "Credit transfers",
    "PS_04A": "Direct debits (credit line)", "PS_04B": "Card payments (credit line)",
    "PS_04C": "Credit transfers (credit line)",
    "PS_05A": "Issuing payment instruments", "PS_05B": "Acquiring",
    "PS_060": "Money remittance", "PS_070": "Payment initiation (PIS)",
    "PS_080": "Account information (AIS)",
}

WITHDRAWN_KEY = re.compile(r"WDR|WITHDR|END_DAT|DAT_END|_END$", re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def merge_properties(props) -> dict:
    out = {}
    for p in props or []:
        if isinstance(p, dict):
            out.update(p)
    return out


def iter_entities(fh):
    """Yield entity dicts from the golden-copy JSON ([[disclaimer], [entities]])."""
    try:
        import ijson  # streaming: keeps memory low on GitHub runners
        for obj in ijson.items(fh, "item.item"):
            if isinstance(obj, dict) and "EntityType" in obj:
                yield obj
        return
    except ImportError:
        pass
    data = json.load(fh)

    def walk(x):
        if isinstance(x, dict):
            if "EntityType" in x:
                yield x
        elif isinstance(x, list):
            for y in x:
                yield from walk(y)
    yield from walk(data)


def services_summary(services, home: str):
    """Return (home-country services, passport countries)."""
    per_country = {}
    for item in services or []:
        if isinstance(item, dict):
            for cc, codes in item.items():
                codes = codes if isinstance(codes, list) else [codes]
                per_country.setdefault(cc, set()).update(str(c) for c in codes)
    home_codes = per_country.get(home) or set().union(*per_country.values()) if per_country else set()
    labels = sorted(SERVICE_LABELS.get(c, c) for c in home_codes)
    passports = sorted(cc for cc in per_country if cc != home)
    return "; ".join(labels), passports


def build_records(entities, include_types, excluded, today: str) -> list:
    by_code = {}
    for e in entities:
        etype = e.get("EntityType", "")
        if etype not in include_types:
            continue
        owner = e.get("CA_OwnerID", "")
        juris = owner.split("_")[0].upper() if owner else ""
        if juris == "EL":
            juris = "GR"  # EU code for Greece
        if juris in excluded:
            continue
        props = merge_properties(e.get("Properties"))
        code = e.get("EntityCode", "")
        auth = props.get("ENT_AUT")
        auth_dates = auth if isinstance(auth, list) else ([auth] if auth else [])
        withdrawn = ""
        for k, v in props.items():
            if WITHDRAWN_KEY.search(k):
                v = first(v)
                if isinstance(v, str) and DATE_RE.match(v) and v[:10] <= today:
                    withdrawn = v[:10]
        nat_ref = next((first(v) for k, v in props.items() if "NAT_REF" in k or "REF_COD" in k), "")
        lei = next((first(v) for k, v in props.items() if "LEI" in k), "")
        city = next((first(v) for k, v in props.items() if "TOW" in k or "CIT" in k), "")
        services, passports = services_summary(e.get("Services"), juris)
        rec = by_code.get(code)
        label = TYPE_LABELS.get(etype, etype)
        if rec:  # same institution listed under more than one type
            types = sorted(set(rec.licence_type.split(" + ")) | {label})
            rec.licence_type = " + ".join(types)
            rec.active = rec.active or not withdrawn
            continue
        by_code[code] = Record(
            key=f"eba:{code}",
            source="eba",
            jurisdiction=juris,
            regulator=owner,
            name=str(first(props.get("ENT_NAM", ""))).strip(),
            licence_type=label,
            active=not withdrawn,
            status_detail=("Withdrawn " + withdrawn) if withdrawn else "Authorised/registered",
            status_date=withdrawn or (str(auth_dates[0])[:10] if auth_dates else ""),
            reference=str(nat_ref or code.split("!")[-1]),
            lei=str(lei),
            city=str(city),
            services=services,
            passports=f"{len(passports)} countries" if passports else "",
            register_url=SEARCH_URL,
            extra={"entity_code": code, "authorisation_dates": join(auth_dates),
                   "passport_countries": ",".join(passports)},
        )
    return list(by_code.values())


class EBASource(Source):
    name = "eba"
    label = "EU/EEA payment & e-money institutions (EBA register)"

    def fetch(self, ctx: Context) -> list:
        cfg = ctx.source_config("eba")
        include = set(cfg.get("entity_types") or DEFAULT_TYPES)
        excluded = set(ctx.excluded_countries) | set(cfg.get("exclude_jurisdictions") or ["GB"])
        meta = ctx.session.get(META_URL, timeout=60).json()
        url = meta["golden_copy_path_context"].rstrip("/") + "/" + meta["latest_version_relative_zip_path"].lstrip("/")
        ctx.log.info("EBA: downloading %s (%s bytes)", url, meta.get("latest_version_relative_zip_size"))
        r = ctx.session.get(url, timeout=600)
        r.raise_for_status()
        blob = r.content
        want = (meta.get("sha256_hash") or "").lower()
        if want and hashlib.sha256(blob).hexdigest() != want:
            raise RuntimeError("EBA zip checksum mismatch (truncated download?)")
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            member = next(n for n in z.namelist() if n.lower().endswith(".json"))
            with z.open(member) as fh:
                return build_records(iter_entities(fh), include, excluded, ctx.today)
