"""South Africa: payments, banks and crypto.

  za_tppp   SARB public register of third-party payment providers (TPPPs),
            with their sponsoring bank(s). Since 11 Aug 2026 the SARB (not PASA)
            registers TPPPs and publishes the list as a dated PDF.
  za_so     SARB public register of payment system operators (SOs)
  za_banks  Prudential Authority lists: locally controlled banks, foreign
            controlled banks, branches of foreign banks, mutual banks (PDFs,
            with address, CEO, phone and website)
  za_casp   FSCA list of approved crypto asset service providers (PDF)

PDF links change with each edition, so each run reads the regulator's page and
follows the current link.
"""
from __future__ import annotations

import html as htmlmod
import re
from urllib.parse import urljoin

from ..http import robots_allowed
from ..model import Record
from .base import Context, Source, SourceNotReady
from .national import match_name, write_diagnostics
from .usstates import pdf_lines

UA = {"User-Agent": "Mozilla/5.0 (compatible; licence-tracker/1.0)"}
NPS_PAGE = "https://www.resbank.co.za/en/home/what-we-do/payments-and-settlements/regulation"
PA_PAGE = "https://www.resbank.co.za/en/home/what-we-do/Prudentialregulation/sa-registered-banks-and-representative-offices"
FSCA_CASP = ("https://www.fsca.co.za/Regulatory%20Frameworks/Documents/"
             "List%20of%20approved%20crypto%20asset%20service%20providers%20%28CASPs%29.pdf")
SA_REG = re.compile(r"\b(\d{4}/\d{5,7}/\d{2})\b")
SPONSORS = ["Absa Bank Limited", "Access Bank South Africa Limited", "African Bank Limited", "Bidvest Bank Limited",
            "Capitec Bank Limited", "Discovery Bank Limited", "FirstRand Bank Limited", "GoTyme Bank Limited",
            "Investec Bank Limited", "Nedbank Limited", "The Standard Bank of South Africa Limited",
            "Sasfin Bank Limited", "TymeBank Limited", "Ubank Limited", "Old Mutual Bank"]
SERVICE_COLS = ["ATM", "credit card", "debit card", "Diners Club", "Amex", "DebiCheck", "fleet card",
                "EFT credit", "EFT debit", "RTC", "IMMS", "cash", "Rapid Payments", "registered mandate"]


def clean_name(s: str) -> str:
    """Drop the service tick-marks ("x", "XXXX", "Visa Mastercard") that PDF text
    extraction glues onto the end of entity names."""
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"(\s+[xX]+)+\s*$", "", s)                   # " x x", " XX"
        s = re.sub(r"(?<=[a-z)\.])X+\s*$", "", s)                # "ltdXXXX"
        s = re.sub(r"\s+(Visa|Mastercard|Diners Club|Diners|Amex)(\s.*)?$", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" ,;-")


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _date_key(url: str) -> tuple:
    u = re.sub(r"%20|[_+]", " ", url)
    m = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})", u)
    if m:
        return int(m.group(3)), int(m.group(2)), int(m.group(1))
    m = re.search(r"\b([A-Za-z]{3,9})[\s-]+(\d{4})", u)
    if m and m.group(1)[:3].lower() in MONTHS:
        return int(m.group(2)), MONTHS[m.group(1)[:3].lower()], 0
    m = re.search(r"(20\d{2})", u)
    return (int(m.group(1)), 0, 0) if m else (0, 0, 0)


def latest(links: list) -> str:
    """The newest edition among dated PDF links."""
    return max(links, key=_date_key) if links else ""


def _links(page: str, base: str, pattern: str) -> list:
    out = []
    for href in re.findall(r"""href=["']([^"']+\.pdf[^"']*)["']""", page, re.I):
        h = htmlmod.unescape(href)
        if re.search(pattern, h, re.I):
            u = urljoin(base, h)
            if u not in out:
                out.append(u)
    return out


# ---------------------------------------------------------------- parsers

def parse_tppp(lines: list, url: str = "") -> list:
    out = {}
    for line in lines:
        m = re.match(r"^\d+\s+(.+)$", line)
        reg = SA_REG.search(line) if m else None
        if not reg:
            continue
        rest = m.group(1)
        reg = SA_REG.search(rest)
        sponsor = next((b for b in sorted(SPONSORS, key=len, reverse=True) if rest.lower().startswith(b.lower())), "")
        sponsor = sponsor or rest[:reg.start()].strip()
        name = clean_name(rest[reg.end():])
        if not name:
            continue
        e = out.setdefault(reg.group(1), {"name": name, "sponsors": []})
        if sponsor and sponsor not in e["sponsors"]:
            e["sponsors"].append(sponsor)
    return [Record(key="za_tppp:" + reg, source="za_tppp", jurisdiction="ZA", regulator="SARB", name=e["name"],
                   licence_type="Third-party payment provider (TPPP)", active=True,
                   status_detail="Registered TPPP; sponsoring bank(s): " + "; ".join(e["sponsors"]),
                   reference=reg, register_url=url or NPS_PAGE,
                   extra={"company_reg_no": reg, "sponsoring_banks": "; ".join(e["sponsors"])})
            for reg, e in out.items()]


def parse_operators(lines: list, url: str, source: str, label: str) -> list:
    """SO and PCH SO public registers: trading name, registration no., registered name, service marks."""
    out = {}
    for line in lines:
        m = re.match(r"^\d+\s+(.+)$", line)
        if not m:
            continue
        rest = m.group(1)
        reg = SA_REG.search(rest) or re.search(r"\b(HRB\s*\d+|[A-Z]{1,3}\s?\d{5,9})\b", rest)
        if not reg:
            continue
        ref = re.sub(r"\s+", "", reg.group(1))
        trading = rest[:reg.start()].strip()
        name = clean_name(rest[reg.end():]) or trading
        if len(name) < 3:
            continue
        out[ref] = Record(key=f"{source}:{ref}", source=source, jurisdiction="ZA", regulator="SARB", name=name,
                          licence_type=f"Payment {label} (SARB)" if label != "SO" else "Payment system operator (SO)",
                          active=True, status_detail=f"Authorised {label}", reference=ref, register_url=url or NPS_PAGE,
                          extra={"trading_name": trading if trading.lower() != name.lower() else "",
                                 "company_reg_no": ref})
    return list(out.values())


PHONE = re.compile(r"(\+?27[\s\d]{8,12}|0\d{2}[\s-]?\d{3}[\s-]?\d{4})")
WEB = re.compile(r"((?:https?://)?www\.[^\s]+|https?://[^\s]+)", re.I)
BANK_END = re.compile(r"\b(Limited|Ltd|Bank|Branch|Banking Corporation|N\.A\.|AG|plc|PLC|Co-operative)\b\.?"
                      r"(\s*\([^)]*\))?\s*$")


def parse_banks(lines: list, url: str, kind: str) -> list:
    """Prudential Authority lists: blocks of name / address lines / CEO / title + phone + website."""
    out = {}

    def flush(block):
        if not block:
            return
        name = block[0]
        if re.match(r"^(name\b|page \d|locally|foreign controlled banks|branches of|mutual banks|registered)", name, re.I):
            return
        text = " ".join(block)
        curator = "curatorship" in text.lower()
        name = re.sub(r"\s*\((under )?curatorship\)", "", name, flags=re.I).strip()
        ceo = next((l for l in block if re.match(r"^(Mr|Ms|Mrs|Dr|Prof|Adv)\.?\s", l)), "")
        ph = PHONE.search(" ".join(l for l in block[1:] if l != ceo))
        wm = WEB.search(text)
        web = wm.group(1) if wm else ""
        if web and not web.lower().startswith("http"):
            web = "https://" + web
        addr = [l for l in block[1:] if l != ceo and not PHONE.search(l) and not WEB.search(l)]
        city = ""
        for l in reversed(addr):
            l2 = re.sub(r"\s*\d{4}\s*$", "", l).strip()
            if re.search(r"[A-Za-z]{3,}", l2) and not re.search(r"P\s*O\s*Box|Private Bag|Limited|Ltd", l2, re.I):
                city = l2
                break
        key = "za_banks:" + match_name(name)
        out[key] = Record(key=key, source="za_banks", jurisdiction="ZA", regulator="SARB PA", name=name,
                          licence_type=kind, active=not curator,
                          status_detail="Registered bank" + (" (under curatorship)" if curator else ""),
                          city=city, website=web, register_url=url or PA_PAGE,
                          extra={"phone": ph.group(1) if ph else "", "ceo": re.sub(r"\s+", " ", ceo),
                                 "address": ", ".join(addr)})

    block = []
    for line in lines:
        l = line.strip()
        if not l or re.match(r"^(Name\s+Address|Page \d+ of \d+|(Locally|Foreign) Controlled Banks|"
                             r"Branches of Foreign Banks|Mutual Banks|Registered Banks)\b", l, re.I):
            continue
        if BANK_END.search(l) and not PHONE.search(l) and (not block or any(PHONE.search(b) or WEB.search(b) for b in block)):
            last = max((i for i, b in enumerate(block) if PHONE.search(b) or WEB.search(b)), default=len(block) - 1)
            carry = block[last + 1:]           # first half of a name wrapped over two lines
            flush(block[:last + 1])
            block = [" ".join(carry + [l])]
        else:
            block.append(l)
    flush(block)
    return list(out.values())


def parse_casps(lines: list, url: str = "") -> list:
    out = {}
    for line in lines:
        if re.search(r"\b(FSP Number|FSP No|Name of)\b", line, re.I):
            continue
        m = re.match(r"^(?:\d{1,4}\.?\s+)?(.+?)\s+(?:FSP\s*(?:No\.?)?\s*)?(\d{4,6})(?:\s+.*)?$", line)
        if not m:
            continue
        name = m.group(1).strip(" -|")
        if len(name) < 3 or not re.search(r"[A-Za-z]{2}", name):
            continue
        out[m.group(2)] = Record(key="za_casp:" + m.group(2), source="za_casp", jurisdiction="ZA", regulator="FSCA",
                                 name=name, licence_type="Crypto asset service provider (FSP licence)", active=True,
                                 status_detail="Approved CASP (FSCA list)", reference=m.group(2),
                                 register_url=url or FSCA_CASP, extra={"fsp_number": m.group(2)})
    return list(out.values())


# ---------------------------------------------------------------- sources

class _ZASource(Source):
    def _get(self, ctx, url):
        if not robots_allowed(ctx.session, url):
            raise SourceNotReady(f"{url} is disallowed by robots.txt")
        r = ctx.session.get(url, timeout=120, headers=UA)
        r.raise_for_status()
        return r

    def _run(self, ctx, jobs: list) -> list:
        """jobs: [(pdf url, parse function taking (lines, url))]."""
        notes, recs = [], []
        for url, parse in jobs:
            raw = self._get(ctx, url).content
            err, got, lines = None, [], []
            try:
                lines = pdf_lines(raw)
                got = parse(lines, url)
            except Exception as e:
                err = e
            notes.append((url, raw, {"rows": len(lines), "kept": len(got), "columns": {},
                                     "first_text_lines": lines[:40]}, err))
            if err:
                write_diagnostics(ctx, self.name, notes)
                raise err
            recs += got
        write_diagnostics(ctx, self.name, notes)
        if not recs:
            raise RuntimeError(f"{self.label}: nothing found; see data/debug/{self.name}_sample.txt")
        return recs

    def _page_links(self, ctx, page_url, pattern):
        return _links(self._get(ctx, page_url).text, page_url, pattern)


class ZATPPPSource(_ZASource):
    name, label, fingerprint = "za_tppp", "South Africa third-party payment providers (SARB)", "za-tppp-v1"

    def fetch(self, ctx):
        url = latest(self._page_links(ctx, NPS_PAGE, r"tppp"))
        if not url:
            raise RuntimeError("SARB: TPPP public register link not found on " + NPS_PAGE)
        return self._run(ctx, [(url, parse_tppp)])


class ZASOSource(_ZASource):
    name, label, fingerprint = "za_so", "South Africa payment system operators (SARB)", "za-so-v1"

    def fetch(self, ctx):
        links = [l for l in self._page_links(ctx, NPS_PAGE, r"(public-list-so|SO%20Public|SO Public)") if "pch" not in l.lower()]
        if not links:
            raise RuntimeError("SARB: SO public register link not found on " + NPS_PAGE)
        return self._run(ctx, [(latest(links), lambda lines, u: parse_operators(lines, u, "za_so", "SO"))])


class ZAPCHSOSource(_ZASource):
    name, label, fingerprint = "za_pchso", "South Africa payment clearing house system operators (SARB)", "za-pchso-v1"

    def fetch(self, ctx):
        links = self._page_links(ctx, NPS_PAGE, r"PCH")
        if not links:
            raise RuntimeError("SARB: PCH SO public register link not found on " + NPS_PAGE)
        return self._run(ctx, [(latest(links), lambda lines, u: parse_operators(lines, u, "za_pchso",
                                                                                 "clearing house system operator"))])


BANK_LISTS = [(r"Locally%20Controlled|Locally Controlled", "Bank (locally controlled)"),
              (r"Foreign%20Controlled|Foreign Controlled", "Bank (foreign controlled)"),
              (r"Branches%20Foreign|Branches Foreign", "Branch of a foreign bank"),
              (r"Mutual%20Banks|Mutual Banks", "Mutual bank")]


class ZABanksSource(_ZASource):
    name, label, fingerprint = "za_banks", "South Africa registered banks (SARB Prudential Authority)", "za-banks-v1"

    def fetch(self, ctx):
        page = self._get(ctx, PA_PAGE).text
        jobs = []
        for pat, kind in BANK_LISTS:
            url = latest(_links(page, PA_PAGE, pat))
            if url:
                jobs.append((url, lambda lines, u, k=kind: parse_banks(lines, u, k)))
        if not jobs:
            raise RuntimeError("Prudential Authority: bank list links not found on " + PA_PAGE)
        return self._run(ctx, jobs)


class ZACASPSource(_ZASource):
    name, label, fingerprint = "za_casp", "South Africa crypto asset service providers (FSCA)", "za-casp-v1"

    def fetch(self, ctx):
        url = ctx.source_config(self.name).get("url") or FSCA_CASP
        return self._run(ctx, [(url, parse_casps)])


ZA_SOURCES = [ZATPPPSource(), ZASOSource(), ZAPCHSOSource(), ZABanksSource(), ZACASPSource()]
