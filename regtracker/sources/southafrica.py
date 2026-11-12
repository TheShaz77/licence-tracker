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
        m = re.match(r"^\d{1,4}\s*(?=[A-Za-z])(.+)$", line)     # "1 Absa Bank ..." and "100Absa Bank ..."
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
        m = re.match(r"^\d{1,4}\s*(?=[A-Za-z(])(.+)$", line)   # "1ACI Worldwide ..." or "1 ACI Worldwide ..."
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


PHONE = re.compile(r"(\+?27[\s\d]{8,12}|0\d{2}\s*-?\s*\d{3}[\s-]?\d{4})")
WEB = re.compile(r"((?:https?://)?www\.[^\s]+|https?://[^\s]+)", re.I)
BANK_END = re.compile(r"\b(Limited|Ltd|Bank|Branch|Banking Corporation|N\.A\.|AG|plc|PLC|Co-operative)\b\.?"
                      r"(\s*\([^)]*\))?\s*$")


TITLE = re.compile(r"^(Mr|Ms|Mrs|Dr|Prof|Adv)\.?\s")
BANK_LEGAL = re.compile(r"^(.*?\b(?:Limited|Ltd|N\.A\.|AG|PLC|plc|Berhad|S\.A\.))(?:\s*\([^)]*\))?(?=\s|$)")
BANK_WORD = re.compile(r"^((?:\S+\s+){0,5}?\S*(?:Bank|Branch|Corporation|Co-operative))(?:\s*\([^)]*\))?(?=\s|$)")


CEO_RE = r"\b(Mr|Ms|Mrs|Dr|Prof|Adv)\.?\s+[A-Z][\w'-]*(?:\s+(?:van|de|du|der|le)?\s*[A-Z][\w'-]+){0,1}"
ROLE_WORDS = re.compile(r"\b(Chief|Executive|Officer|Group|Interim|Managing|Director|Country|Head|General|Acting|"
                        r"Principal|Officer|CEO|First)\b", re.I)


def _bank_name(before: str):
    """Longest sensible name at the start of the line: up to a legal suffix, else up to the last
    'Bank'/'Branch' within the first few words."""
    m = BANK_LEGAL.match(before)
    if m and len(m.group(1).split()) <= 9:
        return m
    best = None
    for m in re.finditer(r"\b(Bank|Branch|Corporation|Co-operative)\b(?:\s*\([^)]*\))?", before):
        if len(before[:m.end()].split()) <= 6:
            best = m
    if best:
        class _M:
            def __init__(self, end): self._end = end
            def group(self, i): return before[:self._end]
            def end(self): return self._end
        return _M(best.end())
    return None


def parse_banks(lines: list, url: str, kind: str) -> list:
    """Prudential Authority bank lists. The PDF's columns (name, address, CEO,
    phone, website) come out of text extraction interleaved; the one stable
    thing is that each bank's name sits on the same line as its phone number."""
    lines = [re.sub(r"[\u2010-\u2015]", "-", l).strip() for l in lines]
    out = {}
    for i, l in enumerate(lines):
        ph = PHONE.search(l)
        if not ph or re.match(r"^(Name\s+Address|Page \d)", l, re.I):
            continue
        before = l[:ph.start()].strip()
        ceo = ""
        mc = re.search(CEO_RE, before)
        if mc:
            ceo = mc.group(0)
            before = (before[:mc.start()] + " " + before[mc.end():]).strip()
        m = _bank_name(before)
        if not m:
            # name wrapped onto the next line: "Access Bank (South Africa) Sandton ..." / "Limited 2196 ..."
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if re.match(r"^(Limited|Ltd)\b", nxt):
                words = before.split()
                name = " ".join(words[:-1] if len(words) > 2 else words) + " Limited"
                city = words[-1] if len(words) > 2 else ""
            else:
                continue
        else:
            name = m.group(1).strip()
            city = before[m.end():].strip()
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if re.match(r"^(Limited|Ltd)\b", nxt) and not re.search(r"\b(Limited|Ltd)$", name):
            name += " " + re.match(r"^(Limited|Ltd)", nxt).group(1)
        if city.startswith(("of ", "Of ")):                  # "State Bank" + "of India"
            name, city = name + " " + city, ""
        if len(re.findall(r"[A-Za-z]{3,}", name)) <= 1 or name.startswith(("Co.", "Corporation", "N.A.")):
            # the start of the name is on one of the lines above, e.g. "China Construction Bank" / "Corporation"
            for back in lines[max(0, i - 3):i][::-1]:
                mb = re.match(r"^((?:[A-Z][\w&'.-]*\s+){0,4}\w*[Bb]ank(?:\s+[Oo]f\s+[A-Z]\w+)?,?)(?=\s|$)", back.strip())
                if mb:
                    name = mb.group(1) + " " + name
                    break
            if any("Branch" in x for x in lines[i + 1:i + 3]) and "Branch" not in name:
                name += " (Johannesburg Branch)" if any("Johannesburg Branch" in x for x in lines[i + 1:i + 3]) else ""
        if re.search(r"\d|,", city) or len(city) > 25:
            city = ""
        if re.match(r"^(P\s*O\s*Box|\d)", name, re.I) or len(name) < 4:
            continue
        curator = "curatorship" in l.lower()
        name = re.sub(r"\s*\((under )?curatorship\)", "", name, flags=re.I).strip()
        window = " ".join(lines[max(0, i - 1):i + 3])
        wm = WEB.search(l) or WEB.search(window)
        web = wm.group(1).rstrip(".,;") if wm else ""
        if web and not web.lower().startswith("http"):
            web = "https://" + web
        if not ceo:
            for near in lines[max(0, i - 2):i + 2]:
                mc = re.search(CEO_RE, near)
                if mc:
                    ceo = mc.group(0)
                    break
        if not city:
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            city = re.sub(r"\s+", " ", ROLE_WORDS.sub(" ", re.sub(r"^(Limited|Ltd)\b", "", nxt))).strip()
            city = " ".join(w for w in city.split("/")[0].split() if w[:1].isupper())
            city = "" if re.search(r"\d|Limited|Ltd", city) else city
        city = re.sub(r"\s*\d{4}$", "", re.sub(WEB, "", city)).strip(" ,")
        key = "za_banks:" + match_name(name)
        out[key] = Record(key=key, source="za_banks", jurisdiction="ZA", regulator="SARB PA", name=name,
                          licence_type=kind, active=not curator,
                          status_detail="Registered bank" + (" (under curatorship)" if curator else ""),
                          city=city[:40], website=web, register_url=url or PA_PAGE,
                          extra={"phone": ph.group(1).strip(), "ceo": ceo})
    return list(out.values())


CASP_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
               "october", "november", "december")
CASP_GONE = re.compile(r"\b(declined|refused|rejected|withdrawn|lapsed|cancelled|canceled|suspended|"
                       r"no longer|removed|revoked)\b", re.I)


def casp_edition(lines: list) -> tuple:
    """(year, month) of a CASP list, from its title lines ("... - APRIL 2024", "as at 31 March 2026")."""
    head = " ".join(lines[:12])
    m = re.search(r"\b(" + "|".join(CASP_MONTHS) + r")\s+(20\d{2})\b", head, re.I)
    return (int(m.group(2)), CASP_MONTHS.index(m.group(1).lower()) + 1) if m else (0, 0)


_CASP_ROW = re.compile(r"^(?:\d{1,3}\s+){1,2}(\d{3,6})\s+(?!CAT\b|•)(.+?)(?:\s+CAT\b.*|\s*•.*)?$", re.I)
_CASP_BARE = re.compile(r"^(?:\d{1,3}\s+){1,2}(\d{3,6})(?:\s+(?:CAT\b|•).*)?$", re.I)
_CASP_NO_ROWNUM = re.compile(r"^(\d{3,6})\s+([A-Z0-9].*?(?:\(PTY\)\s*LTD|LIMITED|LTD|CC|INC|NPC)\.?)(?:\s+CAT\b.*|\s*•.*)?$", re.I)
_CASP_NAME_LINE = re.compile(r"^(?!•|CAT\b|LIST OF|NO\.|LICENCE|CATEGORY|[A-Z]\.\s)[A-Z0-9][A-Za-z0-9 &'’.,()/\-]{3,90}$")
_CASP_SECTION = re.compile(r"^([A-Z])\.\s+(?=.*\b(?:LIST OF|CASPs?|LICENCES?)\b)", re.I)


def parse_casps(lines: list, url: str = "") -> list:
    """The FSCA's list of crypto asset service providers. Two layouts are read:
    2024: "12 Luno (Pty) Ltd 53314 CAT I & II ..."      (row number, name, FSP number)
    2026: "12 53314 LUNO (PTY) LTD CAT I & II ..."      (row number, FSP number, name), in sections
          A. licensed / B. crypto category removed / C. lapsed / D. provisionally withdrawn.
    Firms in sections B-D are kept but marked as no longer licensed."""
    out = {}
    year, month = casp_edition(lines)
    edition = f", {CASP_MONTHS[month - 1].title()} {year} edition" if year else ""
    section_gone, pending = "", None

    def add(fsp, name, rest=""):
        name = re.sub(r"\s+", " ", name).strip(" -|")
        if len(name) < 3 or not re.search(r"[A-Za-z]{2}", name):
            return
        gone = CASP_GONE.search(rest or "")
        why = (gone.group(1).lower() if gone else "") or section_gone
        out[fsp] = Record(key="za_casp:" + fsp, source="za_casp", jurisdiction="ZA", regulator="FSCA",
                          name=name, licence_type="Crypto asset service provider (FSP licence)", active=not why,
                          status_detail=(f"No longer a licensed CASP: {why}" if why else "Licensed CASP") + " (FSCA list" + edition + ")",
                          reference=fsp, register_url=url or FSCA_HOME, extra={"fsp_number": fsp})

    for i, line in enumerate(lines):
        line = line.strip()
        sec = _CASP_SECTION.match(line)
        if sec or re.search(r"\blist of\b.*\b(lapsed|withdrawn|removed|declined)\b", line, re.I):
            g = CASP_GONE.search(line)
            section_gone = ("crypto category removed" if g and g.group(1).lower() == "removed" else g.group(1).lower()) if g else ""
            pending = None
            continue
        if re.search(r"\b(FSP Number|FSP No|Name of|List of|Act, No\.|Page \d+ of)\b", line, re.I):
            continue
        m = _CASP_ROW.match(line) or _CASP_NO_ROWNUM.match(line)
        if m:
            add(m.group(1), m.group(2))
            pending = None
            continue
        m = _CASP_BARE.match(line)
        if m:                       # the name wrapped onto a line of its own below
            pending = (m.group(1), i)
            continue
        if pending and i - pending[1] <= 4 and _CASP_NAME_LINE.match(line) \
                and not re.search(r"services|management|advice|advi ce", line, re.I):
            add(pending[0], line)
            pending = None
            continue
        # 2024 layout: each firm row starts with its row number: "12 Luno (Pty) Ltd 53314 CAT I & II ..."
        m = re.match(r"^\d{1,4}\.?\s+(.+?)\s+(?:FSP\s*(?:No\.?)?\s*)?(\d{3,6})(?:\s+(.*))?$", line)
        if m:
            add(m.group(2), m.group(1), m.group(3) or "")
    return list(out.values())


# ---------------------------------------------------------------- sources

def sheet_lines(raw: bytes) -> list:
    """A spreadsheet as text lines (cells joined by spaces), so the PDF parsers can read it too."""
    from .national import read_table
    if raw[:2] == b"PK":
        h, rows = read_table(raw)
        rows = [h] + rows
    else:
        import xlrd
        sh = xlrd.open_workbook(file_contents=raw).sheet_by_index(0)
        rows = [sh.row_values(r) for r in range(sh.nrows)]
    out = []
    for r in rows:
        cells = []
        for c in r:
            if c is None or str(c).strip() == "":
                continue
            c = str(int(c)) if isinstance(c, float) and c.is_integer() else str(c).strip()
            cells.append(c)
        if cells:
            out.append(" ".join(cells))
    return out


class _ZASource(Source):
    def _get(self, ctx, url):
        if not robots_allowed(ctx.session, url):
            raise SourceNotReady(f"{url} is disallowed by robots.txt")
        r = ctx.session.get(url, timeout=120, headers=UA)
        r.raise_for_status()
        return r

    manual_hint = ""

    def _manual(self, ctx) -> list:
        """Files you downloaded yourself into data/manual/ (za_tppp.pdf, za_banks_mutual.pdf, ...)."""
        d = ctx.data_dir / "manual"
        if not d.exists():
            return []
        return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in (".pdf", ".xlsx", ".xls")
                      and (p.stem.lower() == self.name or p.stem.lower().startswith(self.name + "_")))

    def fetch(self, ctx):
        manual = self._manual(ctx)
        try:
            return self.fetch_web(ctx)
        except SourceNotReady as e:
            if not manual:
                raise SourceNotReady(f"{e}. Download it in your browser and upload it to data/manual/ as "
                                     f"{self.manual_hint or self.name + '.pdf'}") from None
        return self._run(ctx, [(p, self.parse_for_file(p)) for p in manual])

    def parse_for_file(self, path):
        raise NotImplementedError

    def _run(self, ctx, jobs: list) -> list:
        """jobs: [(pdf url or local file, parse function taking (lines, url))]."""
        notes, recs = [], []
        for url, parse in jobs:
            if hasattr(url, "read_bytes"):
                raw, url = url.read_bytes(), str(url)
            else:
                raw = self._get(ctx, url).content
            err, got, lines = None, [], []
            try:
                lines = sheet_lines(raw) if raw[:2] in (b"PK", b"\xd0\xcf") else pdf_lines(raw)
                got = parse(lines, url)
            except Exception as e:
                err = e
            dbg = getattr(parse, "last", None) or lines[:40]
            notes.append((url, raw, {"rows": len(lines), "kept": len(got), "columns": {},
                                     "first_text_lines": dbg}, err))
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
    name, label, fingerprint = "za_tppp", "South Africa third-party payment providers (SARB)", "za-tppp-v2"
    manual_hint = "za_tppp.pdf (SARB > Payments > Regulation > public register of TPPPs)"

    def parse_for_file(self, path):
        return parse_tppp

    def fetch_web(self, ctx):
        url = latest(self._page_links(ctx, NPS_PAGE, r"tppp"))
        if not url:
            raise RuntimeError("SARB: TPPP public register link not found on " + NPS_PAGE)
        return self._run(ctx, [(url, parse_tppp)])


class ZASOSource(_ZASource):
    name, label, fingerprint = "za_so", "South Africa payment system operators (SARB)", "za-so-v1"
    manual_hint = "za_so.pdf (SARB > Payments > Regulation > public register of system operators)"

    def parse_for_file(self, path):
        return lambda lines, u: parse_operators(lines, u, "za_so", "SO")

    def fetch_web(self, ctx):
        links = [l for l in self._page_links(ctx, NPS_PAGE, r"(public-list-so|SO%20Public|SO Public)") if "pch" not in l.lower()]
        if not links:
            raise RuntimeError("SARB: SO public register link not found on " + NPS_PAGE)
        return self._run(ctx, [(latest(links), lambda lines, u: parse_operators(lines, u, "za_so", "SO"))])


def parse_pchso(lines: list, url: str = "") -> list:
    """PCH system operators: '2011/006482/07 Mastercard Southern Africa Mastercard Southern Africa (Pty) Ltd 1. ATM & SSD PCH',
    followed by further numbered PCH lines for the same operator."""
    out, cur = {}, None
    for line in lines:
        reg = SA_REG.match(line.strip())
        if reg:
            rest = line.strip()[reg.end():]
            rest, _, first_pch = rest.partition(" 1. ")
            m = re.search(r"(.+?)\s+((?:\S+\s+){0,8}?\S*\(Pty\)\s*Ltd|.+?Limited)\s*$", rest)
            if m and m.group(2).strip() != rest.strip():
                trading, name = m.group(1).strip(), m.group(2).strip()
                # registered name usually repeats the trading name: start it where the repeat begins
                words = rest.split()
                for k in range(1, len(words)):
                    if " ".join(words[k:]).lower().startswith(" ".join(words[:min(2, k)]).lower()):
                        trading, name = " ".join(words[:k]), " ".join(words[k:])
                        break
            else:
                trading, name = "", rest.strip()
            cur = Record(key="za_pchso:" + reg.group(1), source="za_pchso", jurisdiction="ZA", regulator="SARB",
                         name=name, licence_type="Payment clearing house system operator (SARB)", active=True,
                         status_detail="Authorised PCH system operator", reference=reg.group(1),
                         register_url=url or NPS_PAGE,
                         extra={"trading_name": trading if trading.lower() != name.lower() else "",
                                "company_reg_no": reg.group(1)})
            cur.services = first_pch.strip()
            out[cur.key] = cur
        elif cur and re.match(r"^\d{1,2}\.\s+\S", line.strip()):
            cur.services = "; ".join(x for x in (cur.services, re.sub(r"^\d{1,2}\.\s+", "", line.strip())) if x)
    return list(out.values())


class ZAPCHSOSource(_ZASource):
    name, label, fingerprint = "za_pchso", "South Africa payment clearing house system operators (SARB)", "za-pchso-v1"
    manual_hint = "za_pchso.pdf (SARB > Payments > Regulation > public register of PCH system operators)"

    def parse_for_file(self, path):
        return parse_pchso

    def fetch_web(self, ctx):
        links = [l for l in self._page_links(ctx, NPS_PAGE, r"PCH")
                 if re.search(r"list|register", l, re.I) and not re.search(r"notice|transition|directive", l, re.I)]
        if not links:
            raise RuntimeError("SARB: PCH SO public register link not found on " + NPS_PAGE)
        return self._run(ctx, [(latest(links), parse_pchso)])


BANK_LISTS = [(r"Locally%20Controlled|Locally Controlled", "Bank (locally controlled)"),
              (r"Foreign%20Controlled|Foreign Controlled", "Bank (foreign controlled)"),
              (r"Branches%20Foreign|Branches Foreign", "Branch of a foreign bank"),
              (r"Mutual%20Banks|Mutual Banks", "Mutual bank")]


class ZABanksSource(_ZASource):
    name, label, fingerprint = "za_banks", "South Africa registered banks (SARB Prudential Authority)", "za-banks-v3"
    manual_hint = ("za_banks_locally.pdf, za_banks_foreign.pdf, za_banks_branches.pdf and za_banks_mutual.pdf "
                   "(SARB > Prudential Authority > SA registered banks)")

    def parse_for_file(self, path):
        stem = path.stem.lower()
        kind = ("Branch of a foreign bank" if "branch" in stem else "Mutual bank" if "mutual" in stem
                else "Bank (foreign controlled)" if "foreign" in stem else "Bank (locally controlled)")
        return lambda lines, u, k=kind: parse_banks(lines, u, k)

    def fetch_web(self, ctx):
        page = self._get(ctx, PA_PAGE).text
        jobs = []
        for pat, kind in BANK_LISTS:
            url = latest(_links(page, PA_PAGE, pat))
            if url:
                jobs.append((url, lambda lines, u, k=kind: parse_banks(lines, u, k)))
        if not jobs:
            raise RuntimeError("Prudential Authority: bank list links not found on " + PA_PAGE)
        return self._run(ctx, jobs)


FSCA_HOME = "https://www.fsca.co.za/"


def fsca_list_link(page: str) -> str:
    """The FSCA site links its current CASP list from a news item; the address
    (an _api/... document link) changes with each update. The page can list several
    editions, so the newest one (by the year and month in its link or label) is taken."""
    months = ["january", "february", "march", "april", "may", "june", "july", "august",
              "september", "october", "november", "december"]
    best, best_key = "", (-1, -1)
    for m in re.finditer(r"""<a[^>]+href=["']([^"']+)["'][^>]*>(.*?)</a>""", page, re.I | re.S):
        before = re.sub(r"<[^>]+>", " ", page[max(0, m.start() - 600):m.start()])
        label = re.sub(r"<[^>]+>", " ", m.group(2))
        ctxt = (before + " " + label).lower()
        href = htmlmod.unescape(m.group(1))
        if not ("crypto asset service provider" in ctxt and re.search(r"approved|licensed|list", ctxt)
                and re.search(r"_api/|\.pdf|\.xlsx", href, re.I)
                and "news" not in href.lower() and "press" not in label.lower()):
            continue
        blob = (href + " " + label).lower().replace("%20", " ")
        year = max([int(y) for y in re.findall(r"(20\d{2})", blob)] or [0])
        month = max([months.index(mo) + 1 for mo in months if mo in blob] or [0])
        if (year, month) > best_key or (best_key == (-1, -1)):
            best, best_key = urljoin(FSCA_HOME, href), (year, month)
    return best


class ZACASPSource(_ZASource):
    name, label, fingerprint = "za_casp", "South Africa crypto asset service providers (FSCA)", "za-casp-v3"
    manual_hint = "za_casp.pdf or za_casp.xlsx (FSCA website > list of approved crypto asset service providers)"

    def parse_for_file(self, path):
        return parse_casps

    def fetch_web(self, ctx):
        """Two public FSCA sources, best first:
        1. the FAIS export (www2.fsca.co.za/HTML/FAIS/FSPExport.xlsx, linked from "FAIS Verifications"):
           every authorised financial services provider with its product categories, kept current by the
           FSCA - firms authorised for crypto assets are taken from it;
        2. the "approved list of crypto asset service providers" PDF linked from the FSCA homepage (and
           any list linked from the FSCA's crypto press releases) - the newest edition wins.
        Only documents the FSCA links publicly are read."""
        notes = []
        try:
            recs = self._from_fais_export(ctx, notes)
            if recs:
                self._write_candidates(ctx, notes, (None, FAIS_EXPORT))
                return recs
        except SourceNotReady:
            raise
        except Exception as e:
            notes.append(f"{FAIS_EXPORT}\n    failed: {str(e)[:150]}")
        best = None
        for url in self._candidates(ctx, notes)[:int(ctx.source_config(self.name).get("max_candidates", 25))]:
            try:
                raw = self._get(ctx, url).content
                lines = sheet_lines(raw) if raw[:2] in (b"PK", b"\xd0\xcf") else pdf_lines(raw) if raw[:4] == b"%PDF" else []
                recs = parse_casps(lines, url) if lines else []
                notes.append(f"{url}\n    edition {casp_edition(lines)}  firms {len(recs)}  first line: {(lines or [''])[0][:90]!r}")
                for link in (_pdf_links(raw) if raw[:4] == b"%PDF" and len(recs) < 20 else []):
                    self._extra.append(link)          # a press release: follow the links inside it
                key = (casp_edition(lines), len(recs))
                if len(recs) >= 20 and (best is None or key > best[0]):
                    best = (key, url, raw, lines, recs)
            except SourceNotReady:
                raise
            except Exception as e:
                notes.append(f"{url}\n    failed: {str(e)[:150]}")
        self._write_candidates(ctx, notes, best)
        if not best:
            raise SourceNotReady("no list of approved crypto asset service providers found on the FSCA website; "
                                 "see data/debug/za_casp_candidates.txt")
        _, url, raw, lines, recs = best
        write_diagnostics(ctx, self.name, [(url, raw, {"rows": len(lines), "kept": len(recs), "columns": {},
                                                       "first_text_lines": lines[:40]}, None)])
        return recs

    def _from_fais_export(self, ctx, notes) -> list:
        raw = self._get(ctx, FAIS_EXPORT).content
        recs, info = parse_fais_export(raw)
        notes.append(f"{FAIS_EXPORT}\n    {info}")
        write_diagnostics(ctx, self.name, [(FAIS_EXPORT, raw, {"rows": info.get("rows", 0), "kept": len(recs),
                                                               "columns": info.get("columns", {}),
                                                               "first_text_lines": info.get("header", [])}, None)])
        return recs if len(recs) >= int(ctx.source_config(self.name).get("fais_min_firms", 50)) else []

    def _write_candidates(self, ctx, notes, best):
        try:
            d = ctx.data_dir / "debug"
            d.mkdir(parents=True, exist_ok=True)
            (d / "za_casp_candidates.txt").write_text(
                f"chosen: {best[1] if best else None}\n\n" + "\n".join(notes), encoding="utf-8")
        except Exception:
            pass

    def _candidates(self, ctx, notes) -> list:
        """Addresses that may hold a CASP list, most likely first. More are appended while reading
        (links found inside press-release PDFs), which is why the caller walks the same list object."""
        self._extra = out = _GrowingList()
        cfg_url = ctx.source_config(self.name).get("url")
        if cfg_url:
            out.append(cfg_url)
        try:
            home = self._get(ctx, FSCA_HOME).text
            for u in fsca_list_links(home):
                out.append(u)
        except SourceNotReady:
            raise
        except Exception as e:
            notes.append(f"homepage: {str(e)[:120]}")
        return out


class _GrowingList(list):
    def append(self, u):
        if u and u not in self:
            super().append(u)

    def __getitem__(self, k):          # a slice of it keeps growing with the original
        if isinstance(k, slice):
            return _Walk(self, k.stop)
        return super().__getitem__(k)


class _Walk:
    def __init__(self, items, limit):
        self.items, self.limit = items, limit

    def __iter__(self):
        i = 0
        while i < len(self.items) and (self.limit is None or i < self.limit):
            yield list.__getitem__(self.items, i)
            i += 1


FAIS_EXPORT = "https://www2.fsca.co.za/HTML/FAIS/FSPExport.xlsx"


def parse_fais_export(raw: bytes) -> tuple:
    """Crypto-asset FSPs from the FSCA's FAIS export. The column names are found by meaning, not
    position (FSP number, name, status, product categories), so a re-ordered export still reads.
    A firm counts when any of its product / category cells mentions crypto assets."""
    from .national import read_table
    headers, rows = read_table(raw) if raw[:2] == b"PK" else (None, None)
    if headers is None:
        import xlrd
        sh = xlrd.open_workbook(file_contents=raw).sheet_by_index(0)
        allrows = [[str(c).strip() for c in sh.row_values(r)] for r in range(sh.nrows)]
        headers, rows = allrows[0], allrows[1:]

    def col(*pats):
        for p in pats:
            for i, h in enumerate(headers):
                if re.search(p, str(h or ""), re.I):
                    return i
        return None
    c_num = col(r"fsp\s*(no|num)", r"^fsp$", r"licen[cs]e\s*(no|num)")
    c_name = col(r"fsp\s*name", r"^name", r"company|entity|provider")
    c_status = col(r"status")
    c_trade = col(r"trading")
    c_prod = [i for i, h in enumerate(headers) if re.search(r"product|categor|sub.?cat|licen[cs]e\s*cat", str(h or ""), re.I)]
    info = {"rows": len(rows), "columns": {"fsp": c_num, "name": c_name, "status": c_status, "products": c_prod},
            "header": [str(h) for h in headers][:40]}
    if c_name is None:
        return [], info
    by_fsp = {}
    for r in rows:
        cells = [str(c or "").strip() for c in r]
        prod_text = " ".join(cells[i] for i in c_prod if i < len(cells)) if c_prod else " ".join(cells)
        if not re.search(r"crypto", prod_text, re.I):
            continue
        name = cells[c_name] if c_name < len(cells) else ""
        num = re.sub(r"\.0$", "", cells[c_num]) if c_num is not None and c_num < len(cells) else ""
        if not name:
            continue
        key = num or name.lower()
        status = cells[c_status] if c_status is not None and c_status < len(cells) else ""
        rec = by_fsp.get(key)
        if rec is None:
            gone = bool(status) and re.search(r"lapsed|withdrawn|suspend|debar|cancel|revok|inactive|final", status, re.I)
            by_fsp[key] = Record(
                key="za_casp:" + key, source="za_casp", jurisdiction="ZA", regulator="FSCA", name=name,
                licence_type="Crypto asset service provider (FSP licence)", active=not gone,
                status_detail=(status or "Authorised") + " - crypto assets (FSCA FAIS register)",
                reference=num, register_url="https://www.fsca.co.za/FAIS-Verifications/",
                extra={"fsp_number": num, "trading_name": cells[c_trade] if c_trade is not None and c_trade < len(cells) else ""})
    info["kept"] = len(by_fsp)
    return list(by_fsp.values()), info


def _pdf_links(raw: bytes) -> list:
    """Web addresses on the FSCA site that a PDF links to (press releases link the current list)."""
    out = []
    try:
        import io
        import pdfplumber
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for page in pdf.pages[:6]:
                for h in (page.hyperlinks or []):
                    u = (h.get("uri") or "").strip()
                    if re.search(r"fsca\.co\.za", u, re.I) and re.search(r"_api/|\.pdf|\.xlsx", u, re.I) and u not in out:
                        out.append(u)
    except Exception:
        pass
    return out


def fsca_list_links(page: str) -> list:
    """Every link on an FSCA page that looks like a CASP list or a crypto press release document."""
    out = []
    for m in re.finditer(r"""<a[^>]+href=["']([^"']+)["'][^>]*>(.*?)</a>""", page, re.I | re.S):
        before = re.sub(r"<[^>]+>", " ", page[max(0, m.start() - 600):m.start()])
        label = re.sub(r"<[^>]+>", " ", m.group(2))
        href = htmlmod.unescape(m.group(1))
        if (re.search(r"crypto\s*asset\s*service\s*provider|\bCASP", before + " " + label, re.I)
                and re.search(r"_api/|\.pdf|\.xlsx", href, re.I)):
            u = urljoin(FSCA_HOME, href)
            if u not in out:
                out.append(u)
    best = fsca_list_link(page)
    return ([best] if best else []) + [u for u in out if u != best]


# ---------------------------------------------------------------- ADLAs

ADLA_MANUAL = ("https://www.resbank.co.za/content/dam/sarb/what-we-do/financial-surveillance/"
               "financial-surveillance-documents/{year}/Currency%20and%20Exchanges%20Manual%20for%20ADLAs.pdf")
LEGAL = re.compile(r"(?:^|\s)(\(Pty\)\s*Ltd|\(Proprietary\)\s*Limited|Limited|Ltd|CC|Inc\.?|\(RF\))\.?\s*$", re.I)


AD_BANKS = re.compile(r"\bbank\b|nedbank|firstrand|investec|absa|capitec|sasfin|ubank|standard chartered|"
                      r"citibank|jpmorgan|j\.p\. morgan|hsbc|bnp|société|societe|deutsche|china construction", re.I)
CATEGORY = re.compile(r"categor(?:y|ies)\s+(one|two|three|four|five|1|2|3|4|5)\b[^A-Za-z]*(.*)$", re.I)


ADLA_CATS = {"one": "Category 1 (bureau de change)",
             "two": "Category 2 (bureau de change + remittances with partner MTOs)",
             "three (mto)": "Category 3 (money transfer operator)",
             "three (vtsp)": "Category 3 (value transfer service provider)",
             "three": "Category 3 (money transfer operator / value transfer provider)",
             "four": "Category 4 (bureau de change, MTO and value transfer)"}
ADLA_ROW = re.compile(r"^(.*?)\s*\b(One|Two|Three(?:\s*\((?:MTO|VTSP)\))?|Four)$")


def parse_adla(lines: list, url: str = "") -> list:
    """Section A.2(A) of the Currency and Exchanges Manual for ADLAs: one row per
    ADLA, 'name ... category' (One/Two/Three (MTO)/Three (VTSP)/Four). Long
    names wrap, e.g. 'Imali Express (Pty) Limited trading as Finex' / 'Two' /
    'Exchange'. Section (B), the authorised dealers, are banks and not ADLAs."""
    # the heading also appears in the table of contents ("... ....... 13"): use the one with firms under it
    heads = [i for i, l in enumerate(lines)
             if re.match(r"^\(A\)\s+Authorised Dealers in foreign exchange with limited authority\s*$", l.strip())]
    start = next((i for i in heads if sum(1 for l in lines[i + 1:i + 25] if ADLA_ROW.match(l.strip())) >= 3),
                 heads[0] if heads else None)
    parse_adla.last = lines[start:start + 120] if start is not None else lines[:120]
    if start is None:
        return []
    entries = []
    for l in lines[start + 1:]:
        l = l.strip()
        if re.match(r"^\(B\)\s+Authorised Dealers|^A\.3\b", l):
            break
        if (not l or re.match(r"^\d+ of \d+\b", l) or l.startswith("Currency and Exchanges Manual")
                or re.match(r"^(The offices|act, for the purposes|Name of entity|exchange with limited authority)", l)):
            continue
        m = ADLA_ROW.match(l)
        if m:
            name, cat = m.group(1).strip(), m.group(2).lower().replace("  ", " ")
            if name:
                entries.append({"name": name, "cat": cat})
            elif entries and not entries[-1]["cat"]:
                entries[-1]["cat"] = cat          # the category line of a wrapped name
            continue
        # no category on the line: a new firm (has a legal form or several words) or the rest of a wrapped name
        if entries and (len(l.split()) <= 2 and "(Pty)" not in l):
            entries[-1]["name"] += " " + l
        else:
            entries.append({"name": l, "cat": ""})
    out = {}
    for e in entries:
        name = re.sub(r"\s+", " ", e["name"]).strip(" ,;")
        if len(name) < 4 or not e["cat"]:
            continue
        cat = ADLA_CATS.get(re.sub(r"\s+", " ", e["cat"]), "Category " + e["cat"])
        trading = (re.search(r"\btrading as\s+(.+)$", name, re.I) or [None, ""])[1]
        legal = re.sub(r"\s+trading as\s+.+$", "", name, flags=re.I)
        key = "za_adla:" + match_name(legal)
        out[key] = Record(key=key, source="za_adla", jurisdiction="ZA", regulator="SARB FinSurv", name=legal,
                          licence_type="ADLA – " + cat, active=True,
                          status_detail="Listed in section A.2(A) of the Currency and Exchanges Manual for ADLAs",
                          register_url=url, extra={"adla_category": cat, "trading_name": trading})
    return list(out.values())


class ZAADLASource(_ZASource):
    name, label, fingerprint = "za_adla", "South Africa ADLAs (SARB Financial Surveillance)", "za-adla-v3"

    def parse_for_file(self, path):
        return parse_adla

    def fetch_web(self, ctx):
        import datetime as dt
        year = dt.date.fromisoformat(ctx.today).year
        for y in range(year, year - 6, -1):  # the manual is re-published under the year of its latest edition
            url = ADLA_MANUAL.format(year=y)
            if not robots_allowed(ctx.session, url):
                raise SourceNotReady(f"{url} is disallowed by robots.txt")
            r = ctx.session.get(url, timeout=120, headers=UA)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                return self._run(ctx, [(url, parse_adla)])
        raise RuntimeError("Currency and Exchanges Manual for ADLAs not found on the SARB website")


ZA_SOURCES = [ZATPPPSource(), ZASOSource(), ZAPCHSOSource(), ZABanksSource(), ZACASPSource(), ZAADLASource()]
