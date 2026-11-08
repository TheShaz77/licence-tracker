"""South Africa: banks, from Wikipedia's "List of banks in South Africa".

The official Prudential Authority lists are PDFs on the SARB website, whose
robots.txt asks automated tools not to download them. Wikipedia's list is
maintained from those SARB lists (the article says it was updated from them),
and Wikipedia's API is meant for automated use. Each bank's website comes from
Wikidata where the bank has an entry.

Unofficial: the sheet labels these rows "per Wikipedia".
"""
from __future__ import annotations

import re

from ..model import Record
from .base import Context, Source
from .national import match_name

API = "https://en.wikipedia.org/w/api.php"
PAGE = "List_of_banks_in_South_Africa"
PAGE_URL = "https://en.wikipedia.org/wiki/" + PAGE
UA = {"User-Agent": "licence-tracker/1.0 (personal register tracker; https://github.com)"}
NOT_BANKS = re.compile(r"^(south african reserve bank|reserve bank|prudential authority|south africa|banking|"
                       r"bank|list of|banks? in|economy of|financial services|johannesburg stock exchange)\b", re.I)
BANKISH = re.compile(r"bank|absa|capitec|nedbank|investec|finbond|gbs|ywbn|osk|ksk|gig|tyme|discovery|sasfin|"
                     r"rand|mercantile|ubank|grindrod|citi|hsbc|jpmorgan|j\.p\.|bnp|société|societe|standard|"
                     r"barclays|deutsche|china|habib|hbz|albaraka|access|mutual|co-operative|cooperative", re.I)


def parse_wikitext(text: str) -> list:
    """Bank names (and their Wikipedia article titles) from the list's wikitext."""
    body = re.split(r"\n==\s*(See also|References|External links|Notes)\s*==", text)[0]
    out, seen = [], set()
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith(("{{", "|}", "{|", "!", "[[File:", "[[Category:")):
            continue
        for target, label in re.findall(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]", line):
            name = (label or target).strip()
            if (":" in target or NOT_BANKS.match(name) or not BANKISH.search(name + " " + target)
                    or len(name) < 3):
                continue
            k = match_name(name)
            if k and k not in seen:
                seen.add(k)
                out.append((name, target.strip()))
            break  # one bank per line; later links are brands or notes, e.g. "FirstRand Bank (FNB)"
        # entries written without a link
        if not re.search(r"\[\[", line) and line.startswith(("*", "#")):
            name = re.sub(r"^[*#:\s]+|<ref.*$|\{\{.*?\}\}", "", line).strip()
            k = match_name(name)
            if name and BANKISH.search(name) and not NOT_BANKS.match(name) and k not in seen and len(name) < 80:
                seen.add(k)
                out.append((name, ""))
    return out


class ZABanksWikiSource(Source):
    name = "za_banks_wiki"
    label = "South Africa banks (Wikipedia list, unofficial)"
    fingerprint = "za-banks-wiki-v1"

    def fetch(self, ctx: Context) -> list:
        r = ctx.session.get(API, params={"action": "parse", "page": PAGE, "prop": "wikitext", "format": "json",
                                         "formatversion": "2", "redirects": "1"}, headers=UA, timeout=60)
        r.raise_for_status()
        banks = parse_wikitext(r.json()["parse"]["wikitext"])
        if len(banks) < 10:
            raise RuntimeError(f"Wikipedia list of South African banks gave only {len(banks)} banks")
        sites = self._websites(ctx, [t for _, t in banks if t])
        recs = []
        for name, title in banks:
            mutual = bool(re.search(r"mutual|co-?operative|\bKSK\b|\bOSK\b|\bGIG\b|\bYWBN\b|\bGBS\b", name, re.I))
            recs.append(Record(
                key="za_banks_wiki:" + match_name(name), source="za_banks_wiki", jurisdiction="ZA",
                regulator="SARB PA", name=name,
                licence_type="Mutual / co-operative bank (per Wikipedia)" if mutual else "Bank (per Wikipedia)",
                active=True, status_detail="Listed on Wikipedia's list of banks in South Africa (unofficial)",
                website=sites.get(title, ""),
                register_url="https://en.wikipedia.org/wiki/" + title.replace(" ", "_") if title else PAGE_URL))
        return recs

    def _websites(self, ctx, titles: list) -> dict:
        """Official websites from Wikidata (P856), looked up by Wikipedia article title."""
        out = {}
        for i in range(0, len(titles), 40):
            chunk = titles[i:i + 40]
            try:
                r = ctx.session.get("https://www.wikidata.org/w/api.php", headers=UA, timeout=60, params={
                    "action": "wbgetentities", "sites": "enwiki", "titles": "|".join(chunk), "props": "claims|sitelinks",
                    "sitefilter": "enwiki", "format": "json"})
                ents = r.json().get("entities", {})
            except Exception as e:
                ctx.log.warning("Wikidata website lookup failed: %s", e)
                break
            for ent in ents.values():
                title = ((ent.get("sitelinks") or {}).get("enwiki") or {}).get("title", "")
                claims = (ent.get("claims") or {}).get("P856") or []
                if title and claims:
                    try:
                        out[title] = claims[0]["mainsnak"]["datavalue"]["value"]
                    except (KeyError, IndexError, TypeError):
                        pass
        return out
