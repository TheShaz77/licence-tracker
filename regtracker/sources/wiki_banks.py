"""Banks for countries whose regulator publishes no machine-readable bank list (or blocks automated
access): Wikidata (CC0) entities that are banks in that country, plus the names on Wikipedia's
"List of banks in <country>" article. Both are meant for automated use through their APIs.

Unofficial: the sheet labels these rows "per Wikidata / Wikipedia; licence not checked".
Config (config.yaml):

    wiki_banks:
      countries: [MX, TW, ID, ...]
      pages: {US: List of banks in the United States}   # optional title overrides
"""
from __future__ import annotations

import re
import time

from ..countries import COUNTRY_NAMES
from ..model import Record
from .base import Context, Source, SourceNotReady
from .national import match_name

WIKI_API = "https://en.wikipedia.org/w/api.php"
WD_API = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
UA = {"User-Agent": "licence-tracker/1.0 (personal register tracker; https://github.com)"}

NOT_BANKS = re.compile(
    r"^(list of|lists of|banks? in|banking in|banking|bank$|economy of|ministry|government of|"
    r"central bank|reserve bank|monetary (authority|agency|board)|banque centrale|banco central|bank of the republic|"
    r"stock exchange|securities|financial services (commission|authority)|deposit insurance|"
    r"(.* )?bankers'? association|islamic banking|microfinance in|finance in|credit union$|"
    r"world bank|international monetary|bank for international|african development|asian development|"
    r"inter-american|european (bank|investment)|swift|visa|mastercard|wikipedia|category|file)\b", re.I)
BANKISH = re.compile(
    r"bank|banco|banque|banca\b|banka|bankas|bancorp|bancshares|cr[eé]dit|caisse|savings|"
    r"microfinance|mutual|co-?operative|trust\b|finance|financial|capital|investment|islamic|"
    r"sparkasse|fund\b|exchange|remittance|money|pay\b|payments?|wallet|fintech", re.I)
CENTRAL = re.compile(r"\bcentral\b|reserve bank|monetary (authority|agency|board)|national bank of the republic", re.I)


def wiki_names(name: str) -> list:
    n = {"US": "United States"}.get(name, name)
    return [f"List of banks in {n}", f"List of banks in the {n}", f"List of banks of {n}",
            f"List of banks and credit unions in {n}"]


def parse_wikitext(text: str, country_name: str = "") -> list:
    """(name, article title) pairs from a list article's wikitext."""
    body = re.split(r"\n==\s*(See also|References|External links|Notes|Former banks|Defunct banks|"
                    r"Defunct|Former|Historical banks|Mergers)\s*==", text, flags=re.I)[0]
    out, seen = [], set()
    own = re.compile(rf"^(the )?(central )?bank of (the )?{re.escape(country_name)}$", re.I) if country_name else None
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith(("{{", "|}", "{|", "!", "[[File:", "[[Category:", "[[Image:")):
            continue
        for target, label in re.findall(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]", line):
            name = re.sub(r"\s+", " ", (label or target)).strip()
            if (":" in target or NOT_BANKS.match(name) or CENTRAL.search(name) or (own and own.match(name))
                    or not BANKISH.search(name + " " + target) or len(name) < 3 or len(name) > 90):
                continue
            k = match_name(name)
            if k and k not in seen:
                seen.add(k)
                out.append((name, target.strip()))
            break      # one bank per line; later links are owners, cities or notes
        if not re.search(r"\[\[", line) and line.startswith(("*", "#")):
            name = re.sub(r"^[*#:\s]+|<ref.*$|\{\{.*?\}\}|\(.*?\)", "", line).strip(" .,;")
            k = match_name(name)
            if (name and BANKISH.search(name) and not NOT_BANKS.match(name) and not CENTRAL.search(name)
                    and k and k not in seen and len(name) < 80):
                seen.add(k)
                out.append((name, ""))
    return out


SPARQL_Q = """
SELECT DISTINCT ?item ?itemLabel ?website ?inception WHERE {
  ?country wdt:P297 "%s" .
  ?item wdt:P17 ?country ; wdt:P31 ?type .
  ?type wdt:P279* wd:Q22687 .
  FILTER NOT EXISTS { ?item wdt:P576 [] }
  FILTER NOT EXISTS { ?item wdt:P31/wdt:P279* wd:Q66344 }
  OPTIONAL { ?item wdt:P856 ?website }
  OPTIONAL { ?item wdt:P571 ?inception }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,fr,es,pt,ar,ru,zh,mul". }
}
LIMIT 3000
"""


# Non-bank financial firms (the EMI / payment institution / MSB / VASP equivalents). Wikidata classes are
# found by their English label at query time, so no item numbers are hard-coded; companies count when
# they are an instance of such a class (or a subclass) or have it as their industry (P452).
FI_LABELS = {
    "crypto": ["cryptocurrency exchange", "digital currency exchange", "cryptocurrency company",
               "virtual asset service provider", "cryptocurrency wallet", "bitcoin exchange"],
    "pay": ["payment service provider", "payment processor", "payment system", "electronic money institution",
            "e-money", "electronic money", "mobile payment", "digital wallet", "mobile money",
            "payment company", "payment gateway", "payment institution"],
    "msb": ["money transfer", "remittance", "money transmitter", "bureau de change", "money changer",
            "money services business", "foreign exchange company", "money transfer company"],
}
FI_TYPES = {
    "crypto": "Crypto / virtual asset service provider (per Wikidata; licence not checked)",
    "pay": "Payment / e-money company (per Wikidata; licence not checked)",
    "msb": "Money transfer / FX company (per Wikidata; licence not checked)",
}
SPARQL_FI = """
SELECT DISTINCT ?item ?itemLabel ?website ?inception ?lbl WHERE {
  VALUES ?lbl { %s }
  ?cls rdfs:label ?lbl .
  ?country wdt:P297 "%s" .
  { ?item wdt:P31/wdt:P279* ?cls . } UNION { ?item wdt:P452/wdt:P279* ?cls . }
  { ?item wdt:P17 ?country . } UNION { ?item wdt:P159/wdt:P17 ?country . }
  FILTER NOT EXISTS { ?item wdt:P576 [] }
  FILTER NOT EXISTS { ?item wdt:P31/wdt:P279* wd:Q22687 }
  OPTIONAL { ?item wdt:P856 ?website }
  OPTIONAL { ?item wdt:P571 ?inception }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,fr,es,pt,ar,ru,zh,mul". }
}
LIMIT 2000
"""


class WikiBanksSource(Source):
    name = "wiki_banks"
    label = "Banks per Wikidata / Wikipedia (countries without a readable official list)"

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        import hashlib, json
        self.fingerprint = "wiki-banks-" + hashlib.sha256(json.dumps(self.cfg, sort_keys=True).encode()).hexdigest()[:10]

    def fetch(self, ctx: Context) -> list:
        cfg = self.cfg or ctx.config.get("wiki_banks") or {}
        countries = [c.upper() for c in (cfg.get("countries") or [])]
        fi_countries = [c.upper() for c in (cfg.get("fi_countries") or [])]
        if not countries and not fi_countries:
            err = SourceNotReady("no countries listed under wiki_banks: in config.yaml")
            err.quiet = True
            raise err
        pages = {k.upper(): v for k, v in (cfg.get("pages") or {}).items()}
        recs, problems, counts = {}, [], {}
        for cc in fi_countries:
            if cc in ctx.excluded_countries:
                continue
            try:
                n = 0
                for name, item, site, since, kind in self._wikidata_fi(ctx, cc):
                    k = f"wiki_banks:{cc.lower()}:fi:{match_name(name)}"
                    if not match_name(name) or k in recs or CENTRAL.search(name):
                        continue
                    recs[k] = Record(key=k, source="wiki_banks", jurisdiction=cc, regulator="(Wikidata)", name=name,
                                     licence_type=FI_TYPES[kind], active=True,
                                     status_detail="Listed in Wikidata as a payment, e-money, money transfer or "
                                                   "crypto company (unofficial)",
                                     website=site, status_date=since[:10], register_url=item)
                    n += 1
                counts[cc + " other FIs"] = n
            except Exception as e:
                problems.append(f"{cc} Wikidata FIs: {e}")
            time.sleep(float(cfg.get("pause", 0.5)))
        for cc in countries:
            if cc in ctx.excluded_countries:
                continue
            cname = COUNTRY_NAMES.get(cc, cc)
            got = 0
            try:
                for name, item, site, since in self._wikidata(ctx, cc):
                    k = f"wiki_banks:{cc.lower()}:{match_name(name)}"
                    if not match_name(name) or k in recs or NOT_BANKS.match(name) or CENTRAL.search(name):
                        continue
                    recs[k] = Record(key=k, source="wiki_banks", jurisdiction=cc, regulator="(Wikidata)", name=name,
                                     licence_type="Bank (per Wikidata; licence not checked)", active=True,
                                     status_detail="Listed as a bank in Wikidata (unofficial)", website=site,
                                     status_date=since[:10], register_url=item)
                    got += 1
            except Exception as e:
                problems.append(f"{cc} Wikidata: {e}")
            try:
                title, banks = self._wikipedia(ctx, pages.get(cc), cname)
                for name, target in banks:
                    k = f"wiki_banks:{cc.lower()}:{match_name(name)}"
                    if k in recs:
                        continue
                    url = "https://en.wikipedia.org/wiki/" + (target or title).replace(" ", "_")
                    recs[k] = Record(key=k, source="wiki_banks", jurisdiction=cc, regulator="(Wikipedia)", name=name,
                                     licence_type="Bank (per Wikipedia; licence not checked)", active=True,
                                     status_detail=f"Listed on Wikipedia's '{title}' (unofficial)", register_url=url)
                    got += 1
            except Exception as e:
                problems.append(f"{cc} Wikipedia: {e}")
            counts[cc] = got
            time.sleep(float(cfg.get("pause", 0.5)))
        ctx.log.info("wiki_banks: firms per country %s", counts)
        if problems:
            ctx.log.info("wiki_banks: %s", "; ".join(problems)[:1500])
        if not recs:
            raise RuntimeError("Wikidata / Wikipedia gave no banks: " + "; ".join(problems)[:500])
        self._fill_websites(ctx, recs)
        return list(recs.values())

    def _wikidata(self, ctx, cc: str) -> list:
        r = ctx.session.get(SPARQL, params={"query": SPARQL_Q % cc, "format": "json"},
                            headers={**UA, "Accept": "application/sparql-results+json"}, timeout=90)
        r.raise_for_status()
        out = []
        for b in r.json().get("results", {}).get("bindings", []):
            name = b.get("itemLabel", {}).get("value", "")
            item = b.get("item", {}).get("value", "")
            if not name or re.fullmatch(r"Q\d+", name):
                continue            # no label in any of the languages asked for
            out.append((name, item, b.get("website", {}).get("value", ""), b.get("inception", {}).get("value", "")))
        return out

    def _wikidata_fi(self, ctx, cc: str) -> list:
        kind_of = {lbl: kind for kind, lbls in FI_LABELS.items() for lbl in lbls}
        values = " ".join(f'"{l}"@en' for l in kind_of)
        r = ctx.session.get(SPARQL, params={"query": SPARQL_FI % (values, cc), "format": "json"},
                            headers={**UA, "Accept": "application/sparql-results+json"}, timeout=120)
        r.raise_for_status()
        out, seen = [], set()
        for b in r.json().get("results", {}).get("bindings", []):
            name = b.get("itemLabel", {}).get("value", "")
            item = b.get("item", {}).get("value", "")
            if not name or re.fullmatch(r"Q\d+", name) or item in seen:
                continue
            seen.add(item)
            kind = kind_of.get(b.get("lbl", {}).get("value", ""), "pay")
            out.append((name, item, b.get("website", {}).get("value", ""), b.get("inception", {}).get("value", ""), kind))
        return out

    def _wikipedia(self, ctx, override: str | None, cname: str) -> tuple:
        for title in ([override] if override else wiki_names(cname)):
            r = ctx.session.get(WIKI_API, params={"action": "parse", "page": title, "prop": "wikitext", "format": "json",
                                                  "formatversion": "2", "redirects": "1"}, headers=UA, timeout=60)
            r.raise_for_status()
            j = r.json()
            if "parse" in j:
                return j["parse"].get("title", title), parse_wikitext(j["parse"]["wikitext"], cname)
        return "", []

    def _fill_websites(self, ctx, recs: dict) -> None:
        """Websites (Wikidata P856) for banks found only on Wikipedia, looked up by article title."""
        want = {}
        for r in recs.values():
            if not r.website and "wikipedia.org/wiki/" in r.register_url:
                want.setdefault(r.register_url.rsplit("/wiki/", 1)[1].replace("_", " "), []).append(r)
        titles = list(want)
        for i in range(0, len(titles), 40):
            try:
                resp = ctx.session.get(WD_API, headers=UA, timeout=60, params={
                    "action": "wbgetentities", "sites": "enwiki", "titles": "|".join(titles[i:i + 40]),
                    "props": "claims|sitelinks", "sitefilter": "enwiki", "format": "json"})
                ents = resp.json().get("entities", {})
            except Exception as e:
                ctx.log.warning("Wikidata website lookup failed: %s", e)
                return
            for ent in ents.values():
                title = ((ent.get("sitelinks") or {}).get("enwiki") or {}).get("title", "")
                claims = (ent.get("claims") or {}).get("P856") or []
                if title in want and claims:
                    try:
                        site = claims[0]["mainsnak"]["datavalue"]["value"]
                    except (KeyError, IndexError, TypeError):
                        continue
                    for r in want[title]:
                        r.website = site
