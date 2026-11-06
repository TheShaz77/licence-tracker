"""Canada: Bank of Canada payment service provider (PSP) lists under the
Retail Payment Activities Act (RPAA).

The Bank publishes these as web pages whose tables are filled in by the
page's script, with no download. So this source opens each page in a headless
browser (Playwright), captures the data the page loads, and falls back to
reading the rendered table. For each firm it then opens the detail page (the
one you get by clicking the name) to collect email and website, a capped
number per run, cached in data/state/boc_details.json.

Each run saves what it saw to data/debug/boc_sample.txt so the reader can be
checked against the real pages.
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urljoin, urlparse

from ..model import Record, norm_key
from .base import Context, Source, SourceNotReady
from .national import _flatten, _iso, _records_in_json, match_name

PAGES = {
    "registered": "https://www.bankofcanada.ca/regulatory-oversight/retail-payments/psp-registry/",
    "applicant": "https://www.bankofcanada.ca/regulatory-oversight/retail-payments/list-of-applicants/",
}
# where the refusal / revocation lists are linked from
ENFORCEMENT_PAGE = "https://www.bankofcanada.ca/regulatory-oversight/retail-payments/enforcement-decisions/"
TYPE = {"registered": "PSP (RPAA registered)", "applicant": "PSP applicant (RPAA)",
        "refused": "PSP refused (RPAA)", "revoked": "PSP registration revoked (RPAA)"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SKIP_HOSTS = ("bankofcanada.ca", "banqueducanada.ca", "canada.ca", "gc.ca", "facebook.", "twitter.", "x.com",
              "linkedin.", "youtube.", "instagram.", "google.", "apple.com", "microsoft.", "adobe.",
              "cloudflare", "jsdelivr", "w3.org", "schema.org", "wordpress", "gravatar")
URL_RE = re.compile(r"\b((?:https?://|www\.)[^\s<>\"')]+)", re.I)


# ---------------------------------------------------------------- pure helpers (tested offline)

def pick_key(keys, *needles, exclude=()):
    for k in keys:
        kl = k.lower()
        if all(n in kl for n in needles) and not any(x in kl for x in exclude):
            return k
    return None


def entries_from_json(bodies: list) -> list:
    """Best list of entity objects among captured JSON responses."""
    best = []
    for body in bodies:
        try:
            doc = json.loads(body)
        except Exception:
            continue
        recs = [_flatten(r) for r in _records_in_json(doc)]
        if not recs:
            continue
        keys = set().union(*[r.keys() for r in recs])
        if not any("name" in k.lower() or "nom" in k.lower() or "title" in k.lower() for k in keys):
            continue
        if len(recs) > len(best):
            best = recs
    return best


def normalise_entry(e: dict) -> dict:
    """Map an entry's unknown key names to ours."""
    keys = list(e.keys())
    k_legal = (pick_key(keys, "legal") or pick_key(keys, "name", exclude=("trade", "operat", "contact"))
               or pick_key(keys, "title"))
    k_trade = pick_key(keys, "trade") or pick_key(keys, "operat")
    k_country = pick_key(keys, "country")
    k_date = (pick_key(keys, "date", "appl") or pick_key(keys, "date", "regist") or pick_key(keys, "date"))
    k_viol = pick_key(keys, "violation")
    k_email = pick_key(keys, "email") or pick_key(keys, "courriel")
    k_web = pick_key(keys, "website") or pick_key(keys, "web") or pick_key(keys, "url", exclude=("detail", "link"))
    k_id = pick_key(keys, "id", exclude=("valid", "provid", "width")) or pick_key(keys, "number")
    k_link = pick_key(keys, "link") or pick_key(keys, "detail") or pick_key(keys, "permalink") or pick_key(keys, "href")

    def g(k):
        v = e.get(k) if k else ""
        return re.sub(r"<[^>]+>", " ", str(v or "")).strip()

    return {"legal": g(k_legal), "trade": g(k_trade), "country": g(k_country), "date": g(k_date),
            "violations": g(k_viol), "email": g(k_email), "website": g(k_web), "id": g(k_id),
            "link": g(k_link)}


def contacts_from_page(text: str, hrefs: list, json_bodies: list = ()) -> tuple:
    """(email, website) from a detail page's text, links and loaded JSON."""
    emails = [h[7:].split("?")[0] for h in hrefs if h.lower().startswith("mailto:")]
    blob = " ".join([text or ""] + list(json_bodies))
    emails += [m for m in EMAIL_RE.findall(blob)]
    emails = [e.strip(".,;") for e in emails
              if not any(s in e.lower() for s in ("bankofcanada", "banqueducanada", "@example", "wixpress", "sentry"))]
    sites = []
    for h in list(hrefs) + URL_RE.findall(blob):
        if h.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        u = h if h.lower().startswith("http") else "https://" + h
        host = urlparse(u).netloc.lower()
        if not host or any(s in host for s in SKIP_HOSTS):
            continue
        sites.append(u.rstrip("/.,;"))
    # prefer a site on the same domain as the email
    email = emails[0] if emails else ""
    site = ""
    if email:
        dom = email.split("@")[-1].lower()
        site = next((s for s in sites if dom in urlparse(s).netloc.lower()), "")
    site = site or (sites[0] if sites else "")
    return email, site


def to_record(kind: str, e: dict, detail: dict, page_url: str) -> Record | None:
    name = e["legal"] or e["trade"]
    if not name:
        return None
    key = f"boc:{match_name(name) or norm_key(name)}"
    viol = e.get("violations", "")
    return Record(
        key=key, source="boc", jurisdiction="CA", regulator="Bank of Canada",
        name=name, licence_type=TYPE[kind], active=kind in ("registered", "applicant"),
        status_detail=TYPE[kind] + (f"; trade name {e['trade']}" if e["trade"] and e["trade"] != name else "")
        + (f"; head office {e['country']}" if e["country"] else "")
        + (f"; violations: {viol}" if viol and viol.lower() not in ("0", "no", "none", "-") else ""),
        status_date=_iso(e["date"]) or e["date"][:10], reference=e.get("id", ""),
        website=detail.get("website") or e.get("website", ""), email=detail.get("email") or e.get("email", ""),
        register_url=e.get("link") or page_url,
        extra={"trade_name": e["trade"], "head_office_country": e["country"]},
    )


# ---------------------------------------------------------------- browser part

def _browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SourceNotReady("Playwright is not installed (needed for the Bank of Canada pages)")
    return sync_playwright()


def _load(page, url: str, notes: list, wait_rows: bool = True) -> tuple:
    """Open url; return (json bodies, rendered text, [(row texts, row hrefs)], all hrefs)."""
    bodies = []

    def on_response(resp):
        try:
            ct = (resp.headers.get("content-type") or "").lower()
            if "json" in ct or resp.url.lower().split("?")[0].endswith(".json"):
                b = resp.text()
                if len(b) > 50:
                    bodies.append(b)
                    notes.append(f"  json {resp.status} {len(b):>8} bytes  {resp.url[:200]}")
        except Exception:
            pass

    page.on("response", on_response)
    page.goto(url, wait_until="networkidle", timeout=90000)
    if wait_rows:
        try:
            page.wait_for_function(
                "() => [...document.querySelectorAll('table tbody tr')].some(r => r.innerText.trim() && "
                "!/no entries|loading/i.test(r.innerText))", timeout=30000)
        except Exception:
            notes.append("  (no table rows appeared within 30s)")
        # DataTables: show every row if the plugin is there
        try:
            page.evaluate("() => { if (window.jQuery && jQuery.fn.dataTable) "
                          "{ jQuery('table').each(function(){ try { jQuery(this).DataTable().page.len(-1).draw(); } catch(e){} }); } }")
            page.wait_for_timeout(1500)
        except Exception:
            pass
    rows = page.evaluate("""() => [...document.querySelectorAll('table tbody tr')].map(r => [
        [...r.querySelectorAll('td,th')].map(c => c.innerText.trim()),
        [...r.querySelectorAll('a[href]')].map(a => a.href)])""")
    hrefs = page.evaluate("() => [...document.querySelectorAll('a[href]')].map(a => a.href)")
    text = page.inner_text("body")
    page.remove_listener("response", on_response)
    return bodies, text, rows, hrefs


def _entries_from_rows(rows: list, header_guess: list) -> list:
    out = []
    for cells, links in rows:
        if not cells or not any(cells) or re.search(r"no entries|loading", " ".join(cells), re.I):
            continue
        e = {header_guess[i] if i < len(header_guess) else f"col{i}": v for i, v in enumerate(cells)}
        if links:
            e["detail_link"] = links[0]
        out.append(e)
    return out


class BoCSource(Source):
    name = "boc"
    label = "Canada Bank of Canada PSP lists (RPAA)"

    def fetch(self, ctx: Context) -> list:
        cfg = ctx.source_config("boc")
        per_run = int(cfg.get("details_per_run", 200))
        state_p = ctx.data_dir / "state" / "boc_details.json"
        cache = json.loads(state_p.read_text()) if state_p.exists() else {}
        notes, recs = [], {}
        try:
            return self._fetch(ctx, cfg, per_run, state_p, cache, notes, recs)
        finally:
            _save_notes(ctx, notes)

    def _fetch(self, ctx, cfg, per_run, state_p, cache, notes, recs) -> list:
        heads = {"registered": ["Legal name", "Main trade name", "Head office country", "Violations"],
                 "applicant": ["Legal name", "Main trade name", "Head office country", "Date of application"],
                 "refused": ["Legal name", "Main trade name", "Head office country", "Date"],
                 "revoked": ["Legal name", "Main trade name", "Head office country", "Date"]}
        with _browser() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 licence-tracker")
            pages = dict(PAGES)
            # find refusal / revocation pages from the enforcement page
            try:
                _, _, _, hrefs = _load(page, ENFORCEMENT_PAGE, notes, wait_rows=False)
                for h in hrefs:
                    hl = h.lower()
                    if "bankofcanada.ca" in hl and "refus" in hl and "refused" not in pages:
                        pages["refused"] = h
                    if "bankofcanada.ca" in hl and "revok" in hl and "revoked" not in pages:
                        pages["revoked"] = h
            except Exception as e:
                notes.append(f"enforcement page: {e!r}")

            entries_by_kind = {}
            for kind, url in pages.items():
                notes.append(f"{kind}: {url}")
                try:
                    bodies, text, rows, _ = _load(page, url, notes)
                except Exception as e:
                    notes.append(f"  FAILED {e!r}")
                    if kind in ("registered", "applicant"):
                        raise
                    continue
                from_json = entries_from_json(bodies)
                from_dom = _entries_from_rows(rows, heads.get(kind, []))
                use = from_json if len(from_json) >= len(from_dom) else from_dom
                notes.append(f"  entries: {len(from_json)} from data, {len(from_dom)} from table -> using "
                             f"{'data' if use is from_json else 'table'}")
                # detail links from the table, by name
                link_by_name = {}
                for cells, links in rows:
                    if cells and links:
                        link_by_name[match_name(cells[0])] = links[0]
                ents = []
                for raw in use:
                    e = normalise_entry(raw)
                    if not e["link"]:
                        e["link"] = link_by_name.get(match_name(e["legal"]), "")
                    if e["link"] and not e["link"].startswith("http"):
                        e["link"] = urljoin(url, e["link"])
                    ents.append(e)
                if use:
                    notes.append(f"  first entry raw: {json.dumps(use[0], ensure_ascii=False)[:600]}")
                    notes.append(f"  first entry mapped: {json.dumps(ents[0], ensure_ascii=False)[:400]}")
                entries_by_kind[kind] = ents

            # 2. detail pages for email / website (new firms first, capped per run)
            todo = []
            for kind, ents in entries_by_kind.items():
                for e in ents:
                    ck = e["link"] or e["legal"]
                    if e["email"] or e["website"]:
                        cache.setdefault(ck, {"email": e["email"], "website": e["website"]})
                    elif ck not in cache and e["link"]:
                        todo.append((kind, e, ck))
            todo.sort(key=lambda t: t[0] != "registered")  # registered PSPs first
            done = 0
            for kind, e, ck in todo[:per_run]:
                dnotes = []
                try:
                    bodies, text, _, hrefs = _load(page, e["link"], dnotes, wait_rows=False)
                    page.wait_for_timeout(800)
                    text = page.inner_text("body")
                    email, site = contacts_from_page(text, hrefs, bodies)
                    cache[ck] = {"email": email, "website": site, "seen": ctx.today}
                    if done < 2:
                        notes.append(f"detail sample {e['link']}: email={email!r} website={site!r}")
                        notes.append("  text: " + re.sub(r"\s+", " ", text)[:800])
                    done += 1
                except Exception as ex:
                    notes.append(f"detail {e['link']}: {ex!r}")
                time.sleep(float(cfg.get("detail_delay_seconds", 1.0)))
            notes.append(f"detail pages: {done} fetched this run, {max(0, len(todo) - done)} still to do, "
                         f"{len(cache)} cached")
            browser.close()

        state_p.parent.mkdir(parents=True, exist_ok=True)
        state_p.write_text(json.dumps(cache, indent=0, sort_keys=True))
        rank = {"registered": 0, "revoked": 1, "refused": 2, "applicant": 3}
        for kind in sorted(entries_by_kind, key=lambda k: rank.get(k, 9)):
            for e in entries_by_kind[kind]:
                r = to_record(kind, e, cache.get(e["link"] or e["legal"], {}), PAGES.get(kind, ""))
                if r and r.key not in recs:  # one row per firm, strongest status wins
                    recs[r.key] = r
        if not any(r.licence_type == TYPE["registered"] for r in recs.values()):
            raise RuntimeError("Bank of Canada: no registered PSPs read; see data/debug/boc_sample.txt")
        return list(recs.values())


def _save_notes(ctx: Context, notes: list):
    p = ctx.data_dir / "debug" / "boc_sample.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(notes) + "\n", encoding="utf-8")
