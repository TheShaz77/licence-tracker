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
    iGaming: 'Crypto casino', 'PSP', 'Platform / game provider', 'Affiliate' or 'Operator'.
    Each is marked as a guess."""
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
    """Affiliate / PSP / crypto casino from the name (and website); otherwise operator when the
    licence covers offering gambling to players, platform / game provider when it is a B2B one."""
    lic = re.sub(r"\(guess\)", "", licence or "")
    if _IG_AFF.search(name or "") or re.search(r"affiliate|marketing", lic, re.I):
        return "Affiliate (guess)"
    if _IG_PSP.search(name or ""):
        return "PSP (guess)"
    if _IG_CRYPTO.search(f"{name} {website}"):
        return "Crypto casino (guess)"
    if re.search(r"vendor|support services", lic, re.I) and not _IG_B2C.search(lic):
        return "Supplier / service provider (guess)"
    if _IG_B2C.search(lic):
        return "Operator - casino / sportsbook (guess)"
    if _IG_B2B.search(f"{name} {lic}"):
        return "Platform / game provider (guess)"
    return "Operator - casino / sportsbook (guess)"
