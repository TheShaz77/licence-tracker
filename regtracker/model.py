"""Common record shape shared by every source, plus small helpers."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# Column order used for the CSV export and the "Licences" sheet tab.
SHEET_COLUMNS = [
    ("jurisdiction", "Jurisdiction"),
    ("country", "Country"),
    ("regulator", "Regulator"),
    ("name", "Firm name"),
    ("flag", "Do not contact / competitor"),
    ("flag_reason", "Flag reason"),
    ("licence_type", "Licence type"),
    ("status", "Status"),
    ("status_detail", "Status detail"),
    ("status_date", "Status / authorisation date"),
    ("reference", "Reference no."),
    ("lei", "LEI"),
    ("city", "City"),
    ("website", "Website"),
    ("email", "Email"),
    ("phone", "Phone"),
    ("contact_source", "Contact found via"),
    ("linkedin", "LinkedIn"),
    ("linkedin_source", "LinkedIn found via"),
    ("headcount", "Headcount (year, source)"),
    ("revenue", "Revenue (year, source)"),
    ("other_websites", "Other websites"),
    ("psp_status", "PSP status (Canada)"),
    ("ch_sro", "Swiss SRO"),
    ("pix_status", "Pix (Brazil)"),
    ("fincen_status", "FinCEN MSB (US)"),
    ("us_states", "States of operation (FinCEN, US)"),
    ("us_licences", "State MT licences (US published lists)"),
    ("services", "Services"),
    ("passports", "Passported to"),
    ("register_url", "Register link"),
    ("source", "Source"),
    ("first_seen", "First seen"),
    ("last_changed", "Last changed"),
]

# Columns of the "Country (CFD)" tabs.
CFD_COLUMNS = [
    ("country", "Country"),
    ("regulator", "Regulator"),
    ("name", "Firm name"),
    ("flag", "Do not contact / competitor"),
    ("flag_reason", "Flag reason"),
    ("business_type", "Business type (guess from name + licence, unverified)"),
    ("licence_type", "Licence / permission"),
    ("status", "Status"),
    ("status_detail", "Status detail"),
    ("status_date", "Status / authorisation date"),
    ("reference", "Reference no."),
    ("lei", "LEI"),
    ("city", "City"),
    ("address", "Address"),
    ("website", "Website"),
    ("email", "Email"),
    ("phone", "Phone"),
    ("contact_source", "Contact found via"),
    ("linkedin", "LinkedIn"),
    ("linkedin_source", "LinkedIn found via"),
    ("headcount", "Headcount (year, source)"),
    ("revenue", "Revenue (year, source)"),
    ("other_websites", "Other websites"),
    ("register_url", "Register link"),
    ("source", "Source"),
    ("first_seen", "First seen"),
    ("last_changed", "Last changed"),
]

# iGaming uses the same row shape as CFD, but exposes a stable, filterable
# three-way category instead of the more granular CFD business-type wording.
IGAMING_COLUMNS = [
    (key, "Category" if key == "business_type" else header)
    for key, header in CFD_COLUMNS
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


# --------------------------------------------------- CFD business type ---
# A best guess from the firm's name and licence wording only. Shown as "(guess)" on the sheet;
# nobody has checked it against the firm's actual business.
_PROP = re.compile(r"\bprop(rietary)?\b|\bfunded\b|funding\b|\bprop ?firm|trader challenge|ftmo|\bevaluation", re.I)
_INST = re.compile(r"\bprime\b|liquidity|institutional|\bLP\b|market ?maker|\bclearing\b|\bbank\b|\bbanque\b|\bbanca\b|"
                   r"\bAG & co\b|capital markets|\bglobal markets\b|securities (europe|international)|trading (house|firm)\b|"
                   r"jane street|citadel|virtu|optiver|susquehanna|xtx|jump trading|flow traders|hudson river|tower research",
                   re.I)


def guess_business_type(name: str, licence: str = "", vertical: str = "CFD", website: str = "") -> str:
    """CFD: 'Retail broker', 'Institutional / prime broker or LP' or 'Prop trading firm'.
    iGaming: one of the three sheet categories: Operator, Service Provider or Affiliate."""
    if vertical == "iGaming":
        return guess_igaming_type(name, licence, website)
    if _PROP.search(name or ""):
        return "Prop trading firm (guess)"
    lic = licence or ""
    if re.search(r"professional clients only|wholesale clients only", lic, re.I) or (
            re.search(r"wholesale clients", lic, re.I) and not re.search(r"retail", lic, re.I)):
        return "Institutional / prime broker or liquidity provider (guess)"
    if _INST.search(name or "") and not re.search(r"retail clients", licence or "", re.I):
        return "Institutional / prime broker or liquidity provider (guess)"
    return "Retail broker (guess)"


_IG_AFF = re.compile(r"affiliat|\bmedia\b|marketing|\bleads?\b|traffic|\bads\b|advertis|\bseo\b|"
                     r"publishing|comparison|reviews?\b|tipster", re.I)
_IG_PSP = re.compile(r"\bpay|payment|processing|merchant|e-?wallet|\bpsp\b|acquir|skrill|neteller|trustly|"
                     r"paysafe|nuvei|worldpay|ecommpay|muchbetter|astropay|jeton", re.I)
_IG_B2B = re.compile(r"software|supply|supplier|\bb2b\b|gaming machine technical|host(ing)?\b|network services|"
                     r"critical|associate|core service|casino software provider|category 2\b|\(2\)|"
                     r"\bstudios?\b|\bgames\b|technolog|platform|solutions|\btech\b|systems|"
                     r"aggregat|live dealer|game provider", re.I)
_IG_CRYPTO = re.compile(r"crypto|\bcoin|bitcoin|\bbtc|\beth\b|stake\.|bc\.game|blockchain|\bchain|satoshi|doge|"
                        r"usdt|tether|\btoken|web3|defi\b", re.I)


_IG_B2C = re.compile(r"casino(?! software)|\bbet|betting|bingo|lotter|category 1|licence - full|client provider|\bb2c\b|"
                     r"sportsbook|poker", re.I)


def guess_igaming_type(name: str, licence: str = "", website: str = "") -> str:
    """Return the stable iGaming category used on every country sheet.

    Regulator wording wins where it identifies affiliate, B2C or B2B/vendor
    activity. Names and websites fill gaps in registers with generic licences.
    """
    lic = re.sub(r"\(guess\)", "", licence or "")
    if _IG_AFF.search(name or "") or re.search(r"affiliate|marketing", lic, re.I):
        return "Affiliate"
    if re.search(r"vendor|support services|supplier|service provider", lic, re.I) and not _IG_B2C.search(lic):
        return "Service Provider"
    if _IG_B2C.search(lic):
        return "Operator"
    if _IG_PSP.search(name or "") or _IG_B2B.search(f"{name} {lic}"):
        return "Service Provider"
    # Crypto describes how an operator accepts value, not a fourth business
    # category. Generic gambling licences also default to operator.
    if _IG_CRYPTO.search(f"{name} {website}"):
        return "Operator"
    return "Operator"


# ---------------------------------------------------------------- FI / Digital Assets split
import re as _re

# A record goes on the "Digital Assets" tabs when its licence, or the services it is registered for,
# are crypto / virtual-asset ones (VASP, CASP, DASP, DPT, MiCA, MLR cryptoasset registration, an MSB
# registered to deal in virtual currency, ...). Everything else is a financial institution ("FI").
_DIGITAL_LICENCE = _re.compile(
    r"crypto|virtual[ -]?(?:asset|currenc(?:y|ies))|digital[ -]?(?:asset|payment token|token|currenc(?:y|ies))|blockchain|"
    r"\btoken|bitlicen|stablecoin|ativos virtuais|activos virtuales|kripto|\bDLT\b", _re.I)
_DIGITAL_ACRONYM = _re.compile(r"\b(?:VASP|CASP|DASP|DPT|MiCA|PSAV|SPSAV|PSAN|VAITOS|DABA|DARE)\b")
_DIGITAL_SERVICE = _re.compile(
    r"crypto|virtual[ -]?(?:asset|currenc(?:y|ies))|digital[ -]?(?:asset|payment token|token|currenc(?:y|ies))|\bDPT\b", _re.I)
SEGMENTS = ("FI", "Digital Assets")


def segment_of(r: dict) -> str:
    """'Digital Assets' or 'FI' for a licence record (lead-generation verticals are handled separately)."""
    return "Digital Assets" if "Digital Assets" in segments_of(r) else "FI"


def segments_of(r: dict) -> tuple[str, ...]:
    """Sheet segments for a licence record.

    A licence that is inherently a crypto / virtual-asset licence belongs only
    on Digital Assets. A general financial licence whose registered services
    include both virtual-currency and conventional services belongs on both
    sets of tabs. This is common for Canadian MSBs.
    """
    lic = r.get("licence_type") or ""
    if _DIGITAL_LICENCE.search(lic) or _DIGITAL_ACRONYM.search(lic):
        return ("Digital Assets",)
    ex = r.get("extra") or {}
    services = " ".join(str(x) for x in (r.get("services"), ex.get("services"), ex.get("activities")) if x)
    if not services or not _DIGITAL_SERVICE.search(services):
        return ("FI",)

    # Remove the phrases that identify the digital activity. If a meaningful
    # service remains, this is a mixed-purpose financial firm and must also
    # stay visible to users of the FI tabs.
    conventional = _DIGITAL_SERVICE.sub(" ", services)
    conventional = _re.sub(r"[|,;/+\-]+|\band\b|\bservices?\b", " ", conventional, flags=_re.I)
    return ("FI", "Digital Assets") if _re.search(r"[a-z0-9]", conventional, _re.I) else ("Digital Assets",)
