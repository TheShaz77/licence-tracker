"""Common record shape shared by every source, plus small helpers."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# Column order used for the CSV export and the "Licences" sheet tab.
SHEET_COLUMNS = [
    ("jurisdiction", "Jurisdiction"),
    ("regulator", "Regulator"),
    ("name", "Firm name"),
    ("licence_type", "Licence type"),
    ("status", "Status"),
    ("status_detail", "Status detail"),
    ("status_date", "Status / authorisation date"),
    ("reference", "Reference no."),
    ("lei", "LEI"),
    ("city", "City"),
    ("website", "Website"),
    ("email", "Email"),
    ("psp_status", "PSP status (Canada)"),
    ("services", "Services"),
    ("passports", "Passported to"),
    ("register_url", "Register link"),
    ("source", "Source"),
    ("first_seen", "First seen"),
    ("last_changed", "Last changed"),
]


@dataclass
class Record:
    key: str                 # globally unique, e.g. "fca:900562"
    source: str              # source id, e.g. "fca"
    jurisdiction: str        # ISO-2 country of the licensing authority
    regulator: str           # e.g. "FCA", "IE_CBI", "PRA"
    name: str
    licence_type: str        # normalised type, e.g. "Authorised EMI"
    active: bool
    status_detail: str = ""  # the register's own wording
    status_date: str = ""
    reference: str = ""      # FRN / national reference / RIAD code / UID
    lei: str = ""
    city: str = ""
    website: str = ""
    email: str = ""
    services: str = ""
    passports: str = ""
    register_url: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = "Active" if self.active else "Inactive"
        return d


def norm_key(s: str) -> str:
    """Lower-case and strip everything but letters and digits, for tolerant
    matching of field / column names across registers."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def tolerant_get(d: dict, *names: str, default=""):
    """Return the first value in d whose key matches any of names, ignoring
    case, spaces and punctuation."""
    if not isinstance(d, dict):
        return default
    index = {norm_key(k): v for k, v in d.items()}
    for n in names:
        v = index.get(norm_key(n))
        if v not in (None, ""):
            return v
    return default


def first(v):
    """Registers return a string in some records and a list in others."""
    if isinstance(v, list):
        return v[0] if v else ""
    return v if v is not None else ""


def join(v, sep=" | "):
    if isinstance(v, list):
        return sep.join(str(x) for x in v if x not in (None, ""))
    return "" if v is None else str(v)
