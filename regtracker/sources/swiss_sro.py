"""Switzerland: which AML self-regulatory organisation (SRO) a firm belongs to.

No SRO publishes its member list, and FINMA's SRO member search returns at
most 20 results per query, so there is no full list to download. Instead,
firms are looked up one at a time by name, with a small number of lookups per
run and results cached for 90 days:

  * names you list in data/ch_sro_watch.txt (one per line), which become rows
    in the sheet when FINMA shows them as SRO members, and
  * Swiss firms already in the sheet from other registers (FINMA, FinCEN or
    FINTRAC firms located in Switzerland), which get their SRO filled in.

The search page is read with a headless browser, like the Bank of Canada lists.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from ..http import robots_allowed
from ..model import Record
from .base import Context, Source, SourceNotReady
from .national import match_name

SEARCH_URL = "https://www.finma.ch/fr/autorisation/organisme-d-autoregulation-oar/recherche-de-membres-oar/"
SROS = [  # (pattern seen in results, display name)
    (r"\bVQF\b", "VQF"), (r"PolyReg", "PolyReg"), (r"\bARIF\b", "ARIF"), (r"SO-?FIT", "SO-FIT"),
    (r"\bAOOS\b", "AOOS"), (r"SAV\s*/\s*SNV|Anwaltsverband|avocats", "SRO SAV/SNV"),
    (r"SRO[- ]?SVV|Versicherungsverband", "SRO-SVV"), (r"SVIG|Investmentgesellschaften", "SRO SVIG"),
    (r"Treuhand\s*Suisse", "SRO-Treuhand Suisse"), (r"Leasingverband|\bSLV\b", "SRO SLV"),
    (r"OAR-?G\b", "OAR-G"),
]
SWISS = re.compile(r"\b(switzerland|suisse|schweiz|svizzera|zurich|zürich|geneva|genève|zug|basel|lugano|bern|lausanne)\b", re.I)


def state_path(ctx):
    return ctx.data_dir / "state" / "ch_sro.json"


def load(ctx) -> dict:
    p = state_path(ctx)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def save(ctx, cache: dict):
    p = state_path(ctx)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=0, sort_keys=True, ensure_ascii=False))


def watch_names(ctx) -> list:
    p = ctx.data_dir / "ch_sro_watch.txt"
    if not p.exists():
        return []
    return [l.split("#")[0].strip() for l in p.read_text(encoding="utf-8").splitlines() if l.split("#")[0].strip()]


def is_swiss(r: dict) -> bool:
    ex = r.get("extra") or {}
    if r.get("jurisdiction") == "CH":
        return True
    where = " ".join(str(ex.get(k, "")) for k in ("foreign_location", "address", "hq_state")) + " " + str(r.get("city", ""))
    return bool(SWISS.search(where))


def query_for(name: str) -> str:
    words = re.sub(r"[^\w\s&-]", " ", name).split()
    forms = {"ag", "sa", "gmbh", "sàrl", "sarl", "ltd", "llc", "inc", "limited", "plc", "s.a.", "kg"}
    words = [w for w in words if w.lower() not in forms]
    return " ".join(words[:3]) or name[:30]


def _sro_of(text: str) -> str:
    for pat, sro in SROS:
        if re.search(pat, text or "", re.I):
            return sro
    return ""


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def match_results(name: str, text: str, payloads: list) -> dict:
    """Find the firm in the search results (JSON the page loaded, else the
    page text) and the SRO shown next to it. {"sro": ""} when not found."""
    want = match_name(name)

    def same(other: str) -> bool:
        m = match_name(other)
        return bool(want) and (m == want or (len(want) >= 6 and len(m) >= 4 and (want in m or m in want)))

    for obj in _walk(payloads or []):
        vals = [str(v) for v in obj.values() if isinstance(v, (str, int))]
        if any(same(v) for v in vals):
            sro = _sro_of(" ".join(vals))
            if sro:
                return {"sro": sro, "matched_name": next(v for v in vals if same(v))}
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for i, l in enumerate(lines):
        if same(l):
            sro = _sro_of(" ".join(lines[i + 1:i + 4]))
            if sro:
                return {"sro": sro, "matched_name": l}
    return {"sro": ""}


def search(page, query: str) -> str:
    box = page.locator("input[type=search], input[type=text]").first
    box.fill("")
    box.fill(query)
    box.press("Enter")
    page.wait_for_timeout(3500)
    return page.inner_text("body")


class CHSROSource(Source):
    name = "ch_sro"
    label = "Switzerland SRO members (FINMA SRO member search, one firm at a time)"
    fingerprint = "ch-sro-v1"

    def fetch(self, ctx: Context) -> list:
        from ..store import load_snapshot
        cfg = ctx.source_config(self.name)
        per_run = int(cfg.get("lookups_per_run", 40))
        recheck = int(cfg.get("recheck_days", 90))
        cache = load(ctx)
        today = dt.date.fromisoformat(ctx.today)

        names = [(n, "watch") for n in watch_names(ctx)]
        for src in ("finma", "fincen", "fintrac", "boc", "mica"):
            for r in load_snapshot(ctx.data_dir, src).values():
                if is_swiss(r) and r.get("active"):
                    names.append((r.get("name", ""), "sheet"))
        due = []
        for n, kind in names:
            k = match_name(n)
            c = cache.get(k)
            if k and (not c or (today - dt.date.fromisoformat(c.get("checked", "2000-01-01"))).days >= recheck):
                if (n, kind) not in due:
                    due.append((n, kind))
        if due and not robots_allowed(ctx.session, SEARCH_URL):
            raise SourceNotReady(f"{SEARCH_URL} is disallowed by robots.txt")
        if due:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as e:
                raise SourceNotReady("Playwright is not installed") from e
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                payloads = []

                def grab(resp):
                    try:
                        if "json" in (resp.headers.get("content-type") or ""):
                            payloads.append(resp.json())
                    except Exception:
                        pass
                page.on("response", grab)
                page.goto(SEARCH_URL, wait_until="networkidle", timeout=90000)
                sample = []
                for n, kind in due[:per_run]:
                    payloads.clear()
                    try:
                        text = search(page, query_for(n))
                    except Exception as e:
                        ctx.log.warning("SRO search for %r failed: %s", n, e)
                        continue
                    if len(sample) < 3:
                        sample.append({"query": query_for(n), "text": text[-3000:], "json": payloads[:2]})
                    hit = match_results(n, text, list(payloads))
                    cache[match_name(n)] = {"name": n, "kind": kind, "checked": ctx.today, **hit}
                browser.close()
            save(ctx, cache)
            try:
                (ctx.data_dir / "debug").mkdir(parents=True, exist_ok=True)
                (ctx.data_dir / "debug" / "ch_sro_sample.json").write_text(json.dumps(sample, indent=1, ensure_ascii=False))
            except Exception:
                pass
            ctx.log.info("SRO lookups: %d done this run, %d still due", min(len(due), per_run), max(0, len(due) - per_run))

        recs = []
        for k, c in cache.items():
            if c.get("kind") == "watch" and c.get("sro"):
                recs.append(Record(
                    key=f"ch_sro:{k}", source="ch_sro", jurisdiction="CH", regulator=c["sro"],
                    name=c.get("matched_name") or c["name"], licence_type="SRO member (AML) – " + c["sro"],
                    active=True, status_detail=f"Shown as {c['sro']} member in FINMA's SRO search ({c['checked']})",
                    register_url=SEARCH_URL))
        if not recs and not cache:
            raise SourceNotReady("no Swiss firms to look up yet (add names to data/ch_sro_watch.txt)")
        return recs


def load_cache(data_dir) -> dict:
    from pathlib import Path
    p = Path(data_dir) / "state" / "ch_sro.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def apply(records: list, cache: dict) -> int:
    """Fill the 'Swiss SRO' column on Swiss firms from the lookup cache."""
    n = 0
    for r in records:
        if not is_swiss(r) and r.get("source") != "ch_sro":
            continue
        c = cache.get(match_name(r.get("name", "")))
        if c and c.get("sro"):
            r["ch_sro"] = c["sro"]
            n += 1
    return n


# ---- SRO member lists published in full --------------------------------------------------------------
# Two SROs publish who their members are, through their own websites:
#   * ARIF (Geneva): its member list page is filled from the site's WordPress data ("membre" entries)
#   * SO-FIT (Geneva): its "Recherche affiliés / assujettis" page searches a list by name; there is no
#     download, so the list is read with a set of short generic searches whose results together cover it.
# Both sites' robots.txt allow this. Members are financial intermediaries supervised for anti-money-
# laundering by the SRO (payment, FX, crypto, trading, asset management and similar businesses).
ARIF_URL = "https://arif.ch/wp-json/wp/v2/membre"
ARIF_PAGE = "https://arif.ch/membres/liste-des-membres/"
SOFIT_URL = "https://so-fit.ch/wp-json/search"
SOFIT_PAGE = "https://so-fit.ch/recherche/"
SOFIT_QUERIES = [" sa", " ag", "sàrl", "sarl", "gmbh", "ltd", "inc", "llc", "s.a", "fin", "cap", "inv", "tra", "man",
                 "ass", "con", "ent", "ion", "ter", "ges", "int", "pro", "ver", "and", "ale", "ara", "ari", "oli",
                 "eri", "ana", "ine", "one", "ore", "ste", "ust", "ers", "ell", "ies", "ica", "ial", "art"]


def _get_json(ctx, url, params=None, tries=3):
    import time
    last = None
    for i in range(tries):
        try:
            r = ctx.session.get(url, params=params, timeout=60,
                                headers={"User-Agent": "Mozilla/5.0 (compatible; licence-tracker)"})
            r.raise_for_status()
            return r.json()
        except Exception as e:      # the SO-FIT server drops a connection now and then
            last = e
            time.sleep(3 * (i + 1))
    raise last


def arif_members(ctx) -> list:
    import html as _h
    out, page = [], 1
    while page < 30:
        d = _get_json(ctx, ARIF_URL, {"per_page": 100, "page": page, "_fields": "title,slug,modified"})
        if not d:
            break
        out += [_h.unescape(x["title"]["rendered"]).strip() for x in d if x.get("title", {}).get("rendered")]
        if len(d) < 100:
            break
        page += 1
    return out


def sofit_members(ctx) -> dict:
    import time
    found = {}
    for q in SOFIT_QUERIES:
        d = _get_json(ctx, SOFIT_URL, {"q": q})
        if isinstance(d, dict):
            found.update(d)
        time.sleep(1)
    return found


class CHSROListsSource(Source):
    """Members of Swiss AML self-regulatory organisations whose member lists are public (ARIF, SO-FIT)."""
    name = "ch_sro_lists"
    label = "Switzerland SRO members (ARIF, SO-FIT)"
    removal_is_event = True

    def fetch(self, ctx: Context) -> list:
        recs, problems = {}, []
        if robots_allowed(ctx.session, ARIF_URL):
            try:
                for name in arif_members(ctx):
                    k = match_name(name)
                    recs[f"ch_sro_lists:arif:{k}"] = Record(
                        key=f"ch_sro_lists:arif:{k}", source=self.name, jurisdiction="CH", regulator="ARIF (SRO)",
                        name=name, licence_type="Financial intermediary - AML SRO member (ARIF)", active=True,
                        status_detail="Listed on ARIF's member list", register_url=ARIF_PAGE)
            except Exception as e:
                problems.append(f"ARIF: {e}")
        else:
            problems.append("ARIF: disallowed by robots.txt")
        if robots_allowed(ctx.session, SOFIT_URL):
            try:
                rows = sofit_members(ctx)
                ids = sorted(int(i) for i in rows if str(i).isdigit())
                if ids and len(ids) < (ids[-1] + 1) * 0.9:
                    ctx.log.warning("SO-FIT: %d of about %d listed firms found by the searches", len(ids), ids[-1] + 1)
                for row in rows.values():
                    name, canton, kind = (list(row) + ["", "", ""])[:3]
                    kind = (kind or "").strip()
                    if kind != "OAR":          # "OS" entries are portfolio managers/trustees under FINMA's OS regime
                        continue
                    k = match_name(name)
                    recs[f"ch_sro_lists:sofit:{k}"] = Record(
                        key=f"ch_sro_lists:sofit:{k}", source=self.name, jurisdiction="CH", regulator="SO-FIT (SRO)",
                        name=name.strip(), licence_type="Financial intermediary - AML SRO member (SO-FIT)", active=True,
                        city=f"Canton {canton}".strip() if canton else "",
                        status_detail="Found in SO-FIT's member search (OAR)", register_url=SOFIT_PAGE)
            except Exception as e:
                problems.append(f"SO-FIT: {e}")
        else:
            problems.append("SO-FIT: disallowed by robots.txt")
        for p in problems:
            ctx.log.warning("%s: %s", self.name, p)
        if not recs:
            raise SourceNotReady("; ".join(problems) or "no SRO members read")
        return list(recs.values())
