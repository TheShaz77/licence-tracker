"""Fill in website, email and phone for every firm, where they can be found.

Three free steps, cheapest and most official first. Results are cached in
data/state/contacts.json and only ever fill blanks: a value a register gives
is never overwritten.

1. Official sources
   - UK banks (PRA list): the FCA register API, looked up by FRN.
   - Lithuania: the Bank of Lithuania's page for each firm (email, address).
2. Wikidata: official website by LEI (mostly larger banks).
3. The firm's own website: homepage plus its contact / imprint pages
   ("Impressum", "mentions légales", ...), for email and phone. This runs as
   its own background job (python -m regtracker contacts --hours N), reads
   robots.txt, waits between requests and re-checks a firm after 180 days.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from .model import tolerant_get
from .sources.national import match_name

UA = "Mozilla/5.0 (compatible; licence-tracker/1.0; +https://github.com) contact-details lookup"
STATE = "contacts.json"        # registers + Wikidata (written by the 2-hourly run)
WEB_STATE = "contacts_web.json"  # firm websites (written by the contacts job)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
CONTACT_WORDS = ("contact", "kontakt", "impressum", "imprint", "legal-notice", "legal notice", "mentions",
                 "about", "aviso-legal", "aviso legal", "contatti", "contacto", "contato", "kontakty",
                 "yhteystiedot", "kapcsolat", "kontaktai", "get in touch", "reach us", "nous-contacter",
                 "colofon", "kundenservice", "customer service")
GENERIC = ("info", "contact", "hello", "hi", "support", "office", "enquiries", "inquiries", "mail", "service",
           "customerservice", "kontakt", "compliance", "admin", "sales", "team", "help", "general")
BAD_EMAIL = ("example.", "sentry", "wixpress", "domain.com", "email.com", "yourcompany", "@2x", ".png", ".jpg",
             ".gif", ".svg", ".webp", "noreply", "no-reply", "u003e")
PHONE_LABEL = re.compile(r"(?:phone|tel(?:ephone|efon|éfono|efono)?|t[ée]l\.?|call us|hotline|puh(?:elin)?\.?|"
                         r"telefoon|ph\.?)\s*[:.]?\s*(\+?[\d(][\d\s().\-/]{6,}\d)", re.I)
TEL_RE = re.compile(r"\+?\d[\d\s().\-/]{7,}\d")


def load(data_dir: Path, name: str = STATE) -> dict:
    p = data_dir / "state" / name
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def load_all(data_dir: Path) -> dict:
    """Both caches together, for apply(). The two jobs write separate files so
    their commits never clash."""
    return {**load(data_dir, WEB_STATE), **load(data_dir, STATE)}


def save(data_dir: Path, cache: dict, name: str = STATE):
    p = data_dir / "state" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=0, sort_keys=True, ensure_ascii=False))
    tmp.replace(p)


def apply(records: list, cache: dict) -> dict:
    """Fill blank website/email/phone from the cache. Returns fill counts."""
    n = {"website": 0, "email": 0, "phone": 0}
    for r in records:
        c = cache.get(r.get("key", "")) or {}
        site = norm_site(r.get("website") or "") or c.get("website", "")
        dom = cache.get("site:" + domain(site), {}) if site else {}
        if not c and not dom:
            continue
        src = []
        for f in ("website", "email", "phone"):
            have = r.get(f) or (r.get("extra") or {}).get(f)
            if have:
                continue
            for layer in (c, dom):
                if layer.get(f):
                    r[f] = layer[f]
                    n[f] += 1
                    src.append(layer.get(f + "_from", ""))
                    break
        if src:
            r["contact_source"] = ", ".join(sorted({s for s in src if s}))
    from .enrich import apply as enrich_apply   # LinkedIn, headcount, revenue, other websites
    n["company_details"] = enrich_apply(records, cache, domain)
    return n


# ------------------------------------------------------------------ helpers

def norm_site(s: str) -> str:
    s = (s or "").strip().split()[0] if (s or "").strip() else ""
    if not s or "@" in s:
        return ""
    if not s.lower().startswith("http"):
        s = "https://" + s.lstrip("/")
    return s.rstrip("/")


def domain(url: str) -> str:
    host = urlparse(norm_site(url)).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def decode_cfemail(hexstr: str) -> str:
    """Cloudflare 'email protection' obfuscation."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except Exception:
        return ""


def emails_in(html: str) -> list:
    found = [decode_cfemail(h) for h in re.findall(r'data-cfemail="([0-9a-fA-F]+)"', html)]
    found += [decode_cfemail(h) for h in re.findall(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]+)", html)]
    found += [m.split("?")[0] for m in re.findall(r'mailto:([^"\'>\s]+)', html, re.I)]
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", text, flags=re.I)
    text = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", text, flags=re.I)
    found += EMAIL_RE.findall(text)
    out = []
    for e in found:
        e = e.strip().strip(".,;:").lower()
        e = re.sub(r"^(?:%20|\s)+", "", e)
        if EMAIL_RE.fullmatch(e) and not any(b in e for b in BAD_EMAIL) and e not in out:
            out.append(e)
    return out


def best_email(emails: list, site_domain: str = "") -> str:
    if not emails:
        return ""
    def score(e):
        local, _, dom = e.partition("@")
        same = 0 if site_domain and (dom == site_domain or dom.endswith("." + site_domain)
                                     or site_domain.endswith("." + dom)) else 1
        generic = 0 if any(local == g or local.startswith(g) for g in GENERIC) else 1
        return (same, generic, len(e))
    return sorted(emails, key=score)[0]


def phones_in(html: str) -> list:
    out = []
    for t in re.findall(r'href="tel:([^"]+)"', html, re.I):
        t = re.sub(r"[^\d+]", "", t.replace("%20", ""))
        if 8 <= len(re.sub(r"\D", "", t)) <= 15 and t not in out:
            out.append(t)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    for m in PHONE_LABEL.finditer(text):
        p = re.sub(r"\s+", " ", m.group(1)).strip(" -./")
        digits = re.sub(r"\D", "", p)
        if 8 <= len(digits) <= 15 and not re.fullmatch(r"(19|20)\d{6}", digits) and p not in out:
            out.append(p)
    return out


def contact_links(base: str, html: str, limit: int = 3) -> list:
    host = urlparse(base).netloc.lower().removeprefix("www.")
    links = []
    for href, label in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', html, re.I | re.S):
        u = urljoin(base + "/", href.strip())
        if urlparse(u).netloc.lower().removeprefix("www.") != host:
            continue
        hay = (href + " " + re.sub(r"<[^>]+>", " ", label)).lower()
        if any(w in hay for w in CONTACT_WORDS) and u not in links and not u.lower().endswith((".pdf", ".jpg", ".png")):
            links.append(u)
    # contact pages before about pages
    links.sort(key=lambda u: 0 if re.search(r"contact|kontakt|impressum|imprint|mentions|legal", u, re.I) else 1)
    return links[:limit]


# ------------------------------------------------------------------ 1. official sources

def from_fca(ctx, records: list, cache: dict, log, limit: int = 1000) -> int:
    """UK banks and building societies (PRA list) through the FCA API by FRN."""
    from .sources import fca as fca_mod
    from .sources.base import SourceNotReady
    todo = [r for r in records if r.get("source") == "pra" and r.get("reference") and r.get("key") not in cache]
    if not todo:
        return 0
    try:
        client = fca_mod.FCAClient(ctx, 1.0)  # gentle: the FCA scan may be running too
    except SourceNotReady as e:
        log.info("contacts: FCA lookups skipped (%s)", e)
        return 0
    done = 0
    for r in todo[:limit]:
        try:
            a = client.address(int(r["reference"]))
        except Exception:
            continue
        site =norm_site(str(tolerant_get(a, "Website Address", "Website")))
        phone = str(tolerant_get(a, "Phone Number"))
        email = fca_mod._email_from(a)
        cache[r["key"]] = {"website": site, "phone": phone, "email": email, "checked": ctx.today,
                           "website_from": "FCA register", "phone_from": "FCA register", "email_from": "FCA register"}
        done += 1
    return done


LB_PAGE = "https://www.lb.lt/en/sfi-financial-market-participants/"
LT_FORMS = ("uab", "ab", "mb")


def lb_slugs(name: str) -> list:
    """The Bank of Lithuania's firm pages are named after the firm, e.g.
    "UAB Stanhope Financial" -> uab-stanhope-financial, "Curve Europe UAB" ->
    curve-europe-uab. The list itself is drawn by JavaScript, so we try the
    likely page names instead of paging through it."""
    import unicodedata
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    words = re.findall(r"[a-z0-9]+", n)
    if not words:
        return []
    core = [w for w in words if w not in LT_FORMS]
    forms = [w for w in words if w in LT_FORMS] or ["uab"]
    out = []
    for cand in (words, forms + core, core + forms, core):
        slug = "-".join(cand)
        if slug and slug not in out:
            out.append(slug)
    return out


def from_lb(ctx, records: list, cache: dict, log, limit: int = 120) -> int:
    """Lithuania: the Bank of Lithuania's page for each firm (email only;
    the pages carry no phone or website for the firm)."""
    lt = [r for r in records if r.get("source") == "nat_lt" and r.get("key") not in cache]
    done = 0
    for r in lt[:limit]:
        entry = {"checked": ctx.today}
        for slug in lb_slugs(r.get("name", "")):
            try:
                resp = ctx.session.get(LB_PAGE + slug, timeout=30, headers={"User-Agent": UA})
            except Exception:
                break
            if resp.status_code != 200 or "Authorization code" not in resp.text:
                continue
            emails = [e for e in emails_in(resp.text) if not e.endswith("@lb.lt")]
            entry.update({"email": best_email(emails), "email_from": "Bank of Lithuania", "page": LB_PAGE + slug})
            break
        cache[r["key"]] = entry
        done += 1
        time.sleep(0.3)
    if lt:
        log.info("contacts: Bank of Lithuania pages checked for %d firms (%d with email)",
                 done, sum(1 for r in lt[:limit] if cache[r["key"]].get("email")))
    return done


# ------------------------------------------------------------------ 2. Wikidata

def from_wikidata(ctx, records: list, cache: dict, log, batch: int = 150) -> int:
    """Official websites by LEI (P1278 -> P856)."""
    leis = {}
    for r in records:
        lei = (r.get("lei") or "").strip().upper()
        if len(lei) == 20 and not r.get("website") and not cache.get(r["key"], {}).get("website") \
                and cache.get("lei:" + lei) is None:
            leis.setdefault(lei, []).append(r["key"])
    if not leis:
        return 0
    found = 0
    items = list(leis)
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        q = ("SELECT ?lei ?site WHERE { VALUES ?lei { " + " ".join(f'"{l}"' for l in chunk) +
             " } ?item wdt:P1278 ?lei ; wdt:P856 ?site . }")
        try:
            r = ctx.session.get("https://query.wikidata.org/sparql", params={"query": q, "format": "json"},
                                headers={"User-Agent": UA, "Accept": "application/sparql-results+json"}, timeout=90)
            r.raise_for_status()
            rows = r.json()["results"]["bindings"]
        except Exception as e:
            log.warning("contacts: Wikidata query failed: %s", e)
            break
        got = {}
        for b in rows:
            got.setdefault(b["lei"]["value"], norm_site(b["site"]["value"]))
        for lei in chunk:
            site = got.get(lei, "")
            cache["lei:" + lei] = {"website": site, "checked": ctx.today}
            if site:
                found += 1
                for key in leis[lei]:
                    c = cache.setdefault(key, {})
                    c.update({"website": site, "website_from": "Wikidata", "checked": ctx.today})
        time.sleep(1)
    log.info("contacts: Wikidata websites found for %d of %d LEIs", found, len(items))
    return found


def enrich_registers(ctx, records: list, log) -> dict:
    """Steps 1 and 2 (quick, cached). Called from every run."""
    cache = load(ctx.data_dir)
    out = {}
    for name, fn in (("fca", from_fca), ("lb", from_lb), ("wikidata", from_wikidata)):
        try:
            out[name] = fn(ctx, records, cache, log)
        except Exception as e:
            log.warning("contacts: %s step failed: %s", name, e)
    save(ctx.data_dir, cache)
    return out


# ------------------------------------------------------------------ 3. firm websites

class Fetcher:
    def __init__(self, session, timeout: int = 15):
        self.s = session
        self.timeout = timeout
        self.robots = {}

    def allowed(self, url: str) -> bool:
        from .http import rules_allow
        p = urlparse(url)
        root = f"{p.scheme}://{p.netloc}"
        text = self.robots.get(root)
        if text is None:
            try:
                r = self.s.get(root + "/robots.txt", timeout=self.timeout, headers={"User-Agent": UA})
                text = r.text if r.status_code == 200 else ""
            except Exception:
                text = ""
            self.robots[root] = text
        return rules_allow(text, url)      # looked up under the bot's name, licence-tracker

    def get(self, url: str):
        if not self.allowed(url):
            return None, "robots.txt"
        try:
            r = self.s.get(url, timeout=self.timeout, headers={"User-Agent": UA, "Accept": "text/html,*/*"},
                           allow_redirects=True)
        except Exception as e:
            return None, type(e).__name__
        ct = (r.headers.get("content-type") or "").lower()
        if r.status_code >= 400 or ("html" not in ct and "text" not in ct):
            return None, f"HTTP {r.status_code}"
        return r, ""


US_STATE_NAMES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Puerto Rico": "PR", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA",
    "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}


def licence_link(base: str, html: str) -> str:
    """A 'Licenses' / 'State licenses' / 'Regulatory disclosures' page on the firm's own site."""
    host = urlparse(base).netloc.lower().removeprefix("www.")
    for href, label in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', html, re.I | re.S):
        u = urljoin(base + "/", href.strip())
        if urlparse(u).netloc.lower().removeprefix("www.") != host:
            continue
        hay = (href + " " + re.sub(r"<[^>]+>", " ", label)).lower()
        if re.search(r"licens|regulatory[- ]disclos|state[- ]disclos", hay) and not u.lower().endswith(".pdf"):
            return u
    return ""


def licence_states(html: str) -> tuple:
    """States named on a firm's licence page, and its NMLS ID if shown."""
    text = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html))
    found = []
    for name, code in sorted(US_STATE_NAMES.items(), key=lambda kv: -len(kv[0])):
        pat = r"\b" + re.escape(name) + r"\b"
        if name in ("Virginia", "Washington", "Kansas"):
            pat = r"(?<!West )(?<!District of )\b" + re.escape(name) + r"\b(?! D\.?C)"
        if re.search(pat, text) and code not in found:
            found.append(code)
    nmls = re.search(r"NMLS\s*(?:ID|#|No\.?|Number)?\s*[:#]?\s*(\d{4,8})", text, re.I)
    return sorted(found), (nmls.group(1) if nmls else "")


SITE_VERSION = 2   # 2 = also looks for the LinkedIn page and headcount


def crawl_site(fetcher: Fetcher, site: str, pause: float = 1.0, us_licences: bool = False, name: str = "") -> dict:
    from .enrich import employees_in, linkedin_in
    site = norm_site(site)
    dom = domain(site)
    res = {"website": site, "email": "", "phone": "", "pages": 0, "note": "", "v": SITE_VERSION}
    r, why = fetcher.get(site)
    if r is None and site.startswith("https://"):
        r, why = fetcher.get("http://" + site[8:])
    if r is None:
        res["note"] = why
        return res
    final = r.url.rstrip("/")
    res["pages"] = 1
    html = r.text[:2_000_000]
    if domain(final) and domain(final) != dom:
        res["final_url"] = final            # the site redirects to another domain: a second website
    emails, phones = emails_in(html), phones_in(html)
    res["linkedin"], res["employees"] = linkedin_in(html, name), employees_in(html)
    for link in contact_links(final, html):
        if emails and phones and res["linkedin"]:
            break
        time.sleep(pause)
        r2, _ = fetcher.get(link)
        if r2 is None:
            continue
        res["pages"] += 1
        h2 = r2.text[:2_000_000]
        emails += [e for e in emails_in(h2) if e not in emails]
        phones += [p for p in phones_in(h2) if p not in phones]
        res["linkedin"] = res["linkedin"] or linkedin_in(h2, name)
        res["employees"] = res["employees"] or employees_in(h2)
    if us_licences:
        lic = licence_link(final, html)
        if lic:
            time.sleep(pause)
            r3, _ = fetcher.get(lic)
            if r3 is not None:
                res["pages"] += 1
                states, nmls = licence_states(r3.text[:2_000_000])
                if len(states) >= 2:  # one state name alone is usually just an address
                    res["us_licence_states"], res["licence_page"] = states, lic
                if nmls:
                    res["nmls_id"] = nmls
    res["email"] = best_email(emails, domain(final) or dom)
    res["phone"] = phones[0] if phones else ""
    return res


def crawl(ctx, records: list, hours: float, log, workers: int = 8, recheck_days: int = 180) -> dict:
    """Step 3: read firms' own websites for email and phone, within a time budget."""
    import datetime as dt
    reg = load(ctx.data_dir)
    cache = load(ctx.data_dir, WEB_STATE)
    deadline = time.monotonic() + hours * 3600
    today = dt.date.fromisoformat(ctx.today)
    sites, us_domains, names, again = {}, set(), {}, set()
    for r in records:
        if not r.get("active"):
            continue
        site = norm_site(r.get("website") or "") or reg.get(r.get("key"), {}).get("website", "")
        if not site:
            continue
        d = domain(site)
        if not d:
            continue
        c = cache.get("site:" + d)
        if c and (today - dt.date.fromisoformat(c.get("checked", "2000-01-01"))).days < recheck_days:
            if c.get("v", 1) >= SITE_VERSION:
                continue
            again.add(d)     # read before the LinkedIn / headcount check existed: read again, after new sites
        sites.setdefault(d, site)
        names.setdefault(d, r.get("name", ""))
        if r.get("jurisdiction") == "US":
            us_domains.add(d)
    todo = sorted(sites.items(), key=lambda kv: kv[0] in again)
    log.info("contacts: %d firm websites to read (%d of them again, for LinkedIn / headcount)", len(todo), len(again))
    fetcher = Fetcher(ctx.session)
    done, found = 0, {"email": 0, "phone": 0, "linkedin": 0}
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        it = iter(todo)
        def submit_next():
            try:
                d, site = next(it)
            except StopIteration:
                return False
            futures[pool.submit(crawl_site, fetcher, site, 1.0, d in us_domains, names.get(d, ""))] = d
            return True
        for _ in range(workers * 2):
            if not submit_next():
                break
        while futures:
            for fut in cf.as_completed(list(futures)):
                d = futures.pop(fut)
                try:
                    res = fut.result()
                except Exception as e:
                    res = {"website": sites[d], "email": "", "phone": "", "note": repr(e), "v": SITE_VERSION}
                old = cache.get("site:" + d) or {}
                for f in ("email", "phone", "us_licence_states", "licence_page", "nmls_id"):
                    if old.get(f) and not res.get(f):
                        res[f] = old[f]          # a re-read that fails never loses what was found before
                res["checked"] = ctx.today
                res["email_from"] = res["phone_from"] = "firm website"
                cache["site:" + d] = res
                done += 1
                found["email"] += bool(res.get("email"))
                found["phone"] += bool(res.get("phone"))
                found["linkedin"] += bool(res.get("linkedin"))
                if done % 200 == 0:
                    save(ctx.data_dir, cache, WEB_STATE)
                    log.info("contacts: %d websites read (%d emails, %d phones found)", done, found["email"], found["phone"])
                if time.monotonic() < deadline:
                    submit_next()
                break
    save(ctx.data_dir, cache, WEB_STATE)
    left = len(todo) - done
    log.info("contacts: %d websites read this run (%d with email, %d with phone, %d with LinkedIn); %d still to do",
             done, found["email"], found["phone"], found["linkedin"], left)
    return {"read": done, "left": left, **found}
