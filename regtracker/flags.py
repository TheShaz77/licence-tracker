"""'Do not contact' and competitor flags.

Every firm on the sheet is checked against:
  * flags/do_not_contact.txt - the do-not-contact list, one company per line;
  * flags/groups.yaml - researched groups: other names / entities of listed companies, the companies
    that own them, and OpenPayd's competitors;
  * data/state/flag_entities.json - related entities found automatically in GLEIF (the global LEI
    register's parent / subsidiary records) and Wikidata (parent organisation, owned by, subsidiary).
    `expand()` fills this a slice at a time (GLEIF allows about 60 requests a minute) and refreshes
    each company monthly.

Flagged firms get "DO NOT CONTACT" or "COMPETITOR" in the sheet's flag column (rows turn red) and the
reason, e.g. "Owns Bitstamp Limited (on the do-not-contact list) - per GLEIF".
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path

import yaml

DNC = "DO NOT CONTACT"
COMP = "COMPETITOR"
ROOT = Path(__file__).resolve().parent.parent / "flags"
GLEIF = "https://api.gleif.org/api/v1"
SPARQL = "https://query.wikidata.org/sparql"
UA = {"User-Agent": "licence-tracker/1.0 (personal register tracker)"}

# words that make a name too generic to match on its own
GENERIC = set("""global exchange capital markets market trading trade finance financial pay payment payments group
services service solutions solution digital international holdings holding limited ltd investments investment
partners technologies technology tech consulting marketing media ventures venture assets asset trust bank life
academy fund funds management systems system network networks money transfer transfers fx forex crypto
securities prime one first new world europe european uk us asia africa the and of company corp corporation inc llc
plc sa ag gmbh uab ou oü sro sp zoo bv nv pty pte""".split())


def _clean(name: str) -> str:
    n = name or ""
    n = re.sub(r"https?://", "", n)
    n = re.sub(r"^[^@\s]+@", "", n)                    # pavel@getfractal.xyz -> getfractal.xyz
    n = re.sub(r"/+$", "", n)
    n = re.sub(r"\((old[^)]*|with subfunds[^)]*|blockfills|errante|multibank group|bitrefill|koala crypto)\)", " ", n, flags=re.I)
    n = re.sub(r"\s+-\s+(old application|crypto arbitrage.*)$", " ", n, flags=re.I)
    n = re.sub(r"\b(old record|old app(lication)?)\b", " ", n, flags=re.I)
    return re.sub(r"\s+", " ", n).strip(" .,-")


def _key(name: str) -> str:
    from .sources.national import match_name
    return match_name(_clean(name))


def _words(name: str) -> list:
    from .sources.national import LEGAL_FORMS
    s = re.sub(r"[^a-z0-9 ]", " ", LEGAL_FORMS.sub(" ", _clean(name).lower()))
    return [w for w in s.split() if w]


class Flagger:
    def __init__(self, data_dir: Path | None = None, root: Path = ROOT):
        self.exact, self.prefix, self.patterns = {}, [], []
        dnc_file = root / "do_not_contact.txt"
        groups = yaml.safe_load((root / "groups.yaml").read_text()) if (root / "groups.yaml").exists() else {}
        names = [l.strip() for l in dnc_file.read_text(encoding="utf-8").splitlines()] if dnc_file.exists() else []
        names = [n for n in names if n and n.lower() != "account name"]
        self.dnc_names = names
        for n in names:
            reason = f"On the do-not-contact list ({n})"
            k = _key(n)
            if len(k) < 3:
                continue
            self.exact.setdefault(k, (DNC, reason))
            w = _words(n)
            if len(w) >= 2 and len(k) >= 8 and not all(x in GENERIC for x in w):
                self.prefix.append((tuple(w), DNC, f"Name starts with '{n}' (on the do-not-contact list)"))
        for g, spec in (groups.get("dnc_groups") or {}).items():
            note = spec.get("note")
            for rx in spec.get("match") or []:
                self.patterns.append((re.compile(rx, re.I), DNC,
                                      f"Entity of {g}, which is on the do-not-contact list" + (f" - {note}" if note else "")))
            for o in spec.get("owners") or []:
                if len(_key(o)) >= 3:
                    self.exact.setdefault(_key(o), (DNC, f"Owns {g}, which is on the do-not-contact list"
                                                     + (f" - {note}" if note else "")))
        for g, spec in (groups.get("competitors") or {}).items():
            for rx in spec.get("match") or []:
                self.patterns.append((re.compile(rx, re.I), COMP, f"OpenPayd competitor ({g})"))
            if len(_key(g.split(" (")[0])) >= 4:
                self.exact.setdefault(_key(g.split(" (")[0]), (COMP, f"OpenPayd competitor ({g})"))
        self.competitor_groups = list((groups.get("competitors") or {}).keys())
        # related entities found in GLEIF / Wikidata
        if data_dir:
            st = load_state(data_dir)
            for k, v in (st.get("entities") or {}).items():
                if k and k not in self.exact:
                    self.exact[k] = (v["kind"], v["reason"])

    def flag(self, name: str):
        """(kind, reason) or None."""
        if not name:
            return None
        k = _key(name)
        hit = self.exact.get(k) if len(k) >= 3 else None
        low = _clean(name).lower()
        best = None
        if hit:
            kind, reason = hit
            src = re.search(r"\(([^)]*)\)$", reason)
            if reason.startswith("On the do-not-contact list") and src and _clean(src.group(1)).lower() != low:
                reason = f"Same name as '{src.group(1)}' on the do-not-contact list (different legal form / country)"
            best = (kind, reason)
        else:
            w = tuple(_words(name))
            for p, kind, reason in (self.prefix if len(k) >= 3 else []):
                if w[:len(p)] == p:      # whole words: "Pay Perform" matches "Pay Perform OU", not "Pay Performance"
                    best = (kind, reason)
                    break
        if best is None or best[0] != DNC:
            for rx, kind, reason in self.patterns:
                if rx.search(low):
                    if kind == DNC or best is None:
                        best = (kind, reason)
                    if kind == DNC:
                        break
        return best


def annotate(records: list, data_dir: Path) -> int:
    """Adds 'flag' / 'flag_reason' to each record dict; returns how many were flagged."""
    try:
        f = Flagger(data_dir)
    except Exception:
        return 0
    n = 0
    for r in records:
        hit = f.flag(r.get("name", ""))
        if hit:
            r["flag"], r["flag_reason"] = hit
            n += 1
    return n


# ------------------------------------------------------------------ related entities ----

def state_path(data_dir: Path) -> Path:
    return Path(data_dir) / "state" / "flag_entities.json"


def load_state(data_dir: Path) -> dict:
    p = state_path(data_dir)
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"checked": {}, "entities": {}}


def _get(session, url, params=None):
    r = session.get(url, params=params, headers={**UA, "Accept": "application/vnd.api+json"}, timeout=40)
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return r.json()


def _gleif_name(rec: dict) -> str:
    return (((rec.get("attributes") or {}).get("entity") or {}).get("legalName") or {}).get("name", "")


def gleif_related(session, name: str, pause: float = 1.1) -> list:
    """[(related legal name, relation)] for a company: its parents ('parent') and subsidiaries ('child')."""
    key = _key(name)
    found = _get(session, f"{GLEIF}/lei-records", {"filter[fulltext]": _clean(name), "page[size]": 15})
    time.sleep(pause)
    leis = [d["id"] for d in found.get("data") or [] if _key(_gleif_name(d)) == key][:3]
    out = []
    for lei in leis:
        for rel, path in (("parent", "direct-parent"), ("parent", "ultimate-parent"),
                          ("child", "direct-children"), ("child", "ultimate-children")):
            j = _get(session, f"{GLEIF}/lei-records/{lei}/{path}", {"page[size]": 100} if rel == "child" else None)
            time.sleep(pause)
            data = j.get("data") or []
            for d in data if isinstance(data, list) else [data]:
                n = _gleif_name(d)
                if n and _key(n) != key:
                    out.append((n, rel))
    return out


WD_Q = """
SELECT DISTINCT ?name ?otherLabel ?rel WHERE {
  VALUES ?name { %s }
  ?item rdfs:label ?name .
  { ?item wdt:P749 ?other . BIND("parent" AS ?rel) } UNION { ?item wdt:P127 ?other . BIND("parent" AS ?rel) }
  UNION { ?item wdt:P355 ?other . BIND("child" AS ?rel) } UNION { ?other wdt:P749 ?item . BIND("child" AS ?rel) }
  FILTER NOT EXISTS { ?other wdt:P31 wd:Q5 }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,mul". }
}
"""


def wikidata_related(session, names: list) -> list:
    """[(company name, related name, relation)] for names that are exact English Wikidata labels."""
    labels = []
    for n in names:
        c = _clean(n)
        c2 = re.sub(r"\s+(limited|ltd\.?|llc|inc\.?|plc|ag|gmbh|uab|oü|ou|s\.?a\.?|b\.?v\.?|pty|pte)$", "", c, flags=re.I)
        for x in {c, c2}:
            if x:
                labels.append(x.replace("\\", "").replace('"', ""))
    if not labels:
        return []
    q = WD_Q % " ".join(f'"{l}"@en' for l in labels)
    r = session.get(SPARQL, params={"query": q, "format": "json"},
                    headers={**UA, "Accept": "application/sparql-results+json"}, timeout=90)
    r.raise_for_status()
    out = []
    for b in r.json().get("results", {}).get("bindings", []):
        other = b.get("otherLabel", {}).get("value", "")
        if other and not re.fullmatch(r"Q\d+", other):
            out.append((b["name"]["value"], other, b["rel"]["value"]))
    return out


def expand(ctx, seconds: float = 900, refresh_days: int = 30) -> dict:
    """Look up related entities for the next slice of companies; returns a short summary."""
    data_dir = Path(ctx.data_dir)
    st = load_state(data_dir)
    checked, ents = st.setdefault("checked", {}), st.setdefault("entities", {})
    f = Flagger(None)
    today = dt.date.fromisoformat(ctx.today)
    todo = [(n, DNC) for n in f.dnc_names] + [(g.split(" (")[0], COMP) for g in f.competitor_groups]
    todo = [(n, k) for n, k in todo
            if not checked.get(n) or (today - dt.date.fromisoformat(checked[n])).days >= refresh_days]
    deadline = time.monotonic() + seconds
    done = added = 0

    def add(rel_name, kind, reason):
        nonlocal added
        k = _key(rel_name)
        if len(k) < 4 or k in ents or k in f.exact:
            return
        ents[k] = {"name": rel_name, "kind": kind, "reason": reason}
        added += 1

    # Wikidata first: one query covers 40 companies
    for i in range(0, len(todo), 40):
        if time.monotonic() > deadline:
            break
        batch = todo[i:i + 40]
        try:
            for name, other, rel in wikidata_related(ctx.session, [n for n, _ in batch]):
                kind = dict((_clean(n).lower(), k) for n, k in batch).get(name.lower(), DNC)
                if kind == COMP and rel == "parent":
                    continue        # a competitor's owner is not itself a competitor
                if kind == DNC:
                    reason = (f"Owns {name} (on the do-not-contact list) - per Wikidata" if rel == "parent"
                              else f"Subsidiary of {name} (on the do-not-contact list) - per Wikidata")
                else:
                    reason = f"Subsidiary of OpenPayd competitor {name} - per Wikidata"
                add(other, kind, reason)
        except Exception as e:
            ctx.log.info("flags: Wikidata lookup failed: %s", e)
        time.sleep(1)
    for name, kind in todo:
        if time.monotonic() > deadline:
            break
        try:
            for other, rel in gleif_related(ctx.session, name):
                if kind == COMP and rel == "parent":
                    continue
                if kind == DNC:
                    reason = (f"Owns {name} (on the do-not-contact list) - per GLEIF" if rel == "parent"
                              else f"Subsidiary of {name} (on the do-not-contact list) - per GLEIF")
                else:
                    reason = f"Subsidiary of OpenPayd competitor {name} - per GLEIF"
                add(other, kind, reason)
            checked[name] = ctx.today
            done += 1
        except Exception as e:
            ctx.log.info("flags: GLEIF lookup for %r failed: %s", name, e)
            time.sleep(5)
    p = state_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=0, sort_keys=True, ensure_ascii=False))
    left = len(todo) - done
    ctx.log.info("flags: checked %d companies for related entities (%d still to do), %d new related entities, "
                 "%d in total", done, left, added, len(ents))
    return {"checked": done, "left": left, "added": added, "total": len(ents)}
