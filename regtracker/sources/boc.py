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
        "refused": "PSP refused (RPAA)", "revoked": "PSP registration revoked (RPAA)",
        "withdrawn": "PSP application withdrawn (RPAA)"}
DETAIL_URL = PAGES["registered"] + "psp-registry-details/?account_id={id}"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SKIP_HOSTS = ("bankofcanada", "banqueducanada", "museedelabanque", "museum", "canada.ca", "gc.ca", "facebook.", "twitter.", "x.com",
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


def best_json_for_table(bodies: list, dom_entries: list) -> list:
    """Among the JSON lists a page loaded, the one whose firms are the ones its
    table shows (both Bank pages load the big registered list; the applicants
    page shows other firms)."""
    dom_names = {match_name(next(iter(e.values()), "")) for e in dom_entries} - {""}
    best, best_score = [], 0
    for body in bodies:
        recs = entries_from_json([body])
        if not recs:
            continue
        names = set()
        for r in recs:
            n = normalise_entry(r)
            names.add(match_name(n["legal"]))
            names.add(match_name(n["trade"]))
        score = len(dom_names & names) if dom_names else len(recs)
        if score > best_score:
            best, best_score = recs, score
    if dom_names and best_score < max(1, len(dom_names) // 2):
        return []  # nothing loaded matches the table: read the table instead
    return best


def normalise_entry(e: dict) -> dict:
    """Map an entry's key names to ours. The Bank's feed uses en_legal_name /
    fr_legal_name (often only one is filled), main_trade_name,
    head_office_country, status, group_label, registration_date, created_on,
    has_violations and id."""
    keys = list(e.keys())

    def g(k):
        v = e.get(k) if k else ""
        return re.sub(r"<[^>]+>", " ", str(v if v is not None else "")).strip()

    legal = next((g(k) for k in keys if "legal" in k.lower() and g(k)), "")
    if not legal:
        legal = g(pick_key(keys, "name", exclude=("trade", "operat", "contact")) or pick_key(keys, "title"))
    k_trade = pick_key(keys, "trade") or pick_key(keys, "operat")
    k_date = (pick_key(keys, "registration_date") or pick_key(keys, "date", "regist")
              or pick_key(keys, "date", "appl") or pick_key(keys, "date"))
    k_applied = (pick_key(keys, "created") or pick_key(keys, "date", "appl") or pick_key(keys, "submitted")
                 or pick_key(keys, "date", "of"))
    k_link = pick_key(keys, "link") or pick_key(keys, "detail") or pick_key(keys, "permalink") or pick_key(keys, "href")
    k_id = pick_key(keys, "account_id") or pick_key(keys, "id", exclude=("valid", "provid", "width")) \
        or pick_key(keys, "number")
    return {"legal": legal, "trade": g(k_trade), "country": g(pick_key(keys, "country")),
            "date": g(k_date), "applied": g(k_applied), "violations": g(pick_key(keys, "violation")),
            "email": g(pick_key(keys, "email") or pick_key(keys, "courriel")),
            "website": g(pick_key(keys, "website") or pick_key(keys, "url", exclude=("detail", "link"))),
            "id": g(k_id), "link": g(k_link), "status": g(pick_key(keys, "status")),
            "group": g(pick_key(keys, "group"))}


def kind_of(e: dict, page_kind: str) -> str:
    """registered / applicant / refused / revoked / withdrawn, from the entry's
    own status fields; the page it came from only as a last resort."""
    s = f"{e.get('group', '')} {e.get('status', '')}".lower()
    if re.search(r"revok|cancel|terminat", s):
        return "revoked"
    if re.search(r"refus|denied|reject|declin", s):
        return "refused"
    if re.search(r"withdr|abandon", s):
        return "withdrawn"
    if re.search(r"(?<!un)(?<!not )regist", s):
        return "registered"
    if re.search(r"appl|review|pending|submit|progress|assess", s):
        return "applicant"
    return page_kind


DETAIL_LABELS = ["Registration status", "Registration date", "Application status", "Date of application",
                 "Application date", "Contact information", "Legal name", "Trade names", "Trade name",
                 "Mailing address", "Head office address", "Head office city", "Head office province",
                 "Head office province/territory", "Head office country", "Phone number", "Email address",
                 "Website", "Websites", "Retail payment activities", "Payment functions", "Agents",
                 "Mandataries", "Agents and mandataries", "Violations", "Registration information",
                 "Applicant information"]


def parse_detail_text(text: str) -> dict:
    """Labelled fields from a detail page ('Email address' -> value, ...)."""
    labels = sorted(DETAIL_LABELS, key=len, reverse=True)
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    out, cur = {}, None
    for line in lines:
        hit = next((lab for lab in labels if line.lower() == lab.lower()), None)
        if hit is None:  # "Label value" on one line
            hit = next((lab for lab in labels if line.lower().startswith(lab.lower() + " ")), None)
            if hit:
                out.setdefault(hit, line[len(hit):].strip())
                cur = None
                continue
        if hit:
            cur = hit
            continue
        if cur and cur not in out:
            out[cur] = line
    if not out:  # all on one line (collapsed text)
        flat = re.sub(r"\s+", " ", text or "")
        pat = "|".join(re.escape(l) for l in labels)
        parts = re.split(f"({pat})", flat)
        for i in range(1, len(parts) - 1, 2):
            out.setdefault(parts[i], parts[i + 1].strip())
    return out


def contacts_from_page(text: str, hrefs: list, json_bodies: list = ()) -> tuple:
    """(email, website) from a detail page: labelled fields first, then links."""
    f = parse_detail_text(text)
    email = (f.get("Email address") or "").split()[0] if f.get("Email address") else ""
    site = (f.get("Website") or f.get("Websites") or "").split()[0] if (f.get("Website") or f.get("Websites")) else ""
    if email and not EMAIL_RE.fullmatch(email):
        email = ""
    if site and not re.match(r"(https?://|www\.|[a-z0-9-]+\.[a-z]{2,})", site, re.I):
        site = ""
    if site and not site.lower().startswith("http"):
        site = "https://" + site
    if email and site:
        return email, site.rstrip("/.,;")
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
    email = email or (emails[0] if emails else "")
    if not site and email:
        dom = email.split("@")[-1].lower()
        site = next((s for s in sites if dom in urlparse(s).netloc.lower()), "")
    # a site only when it matches the email's domain or the page gave one explicitly:
    # other links on the page are usually the Bank's own
    return email, site.rstrip("/.,;")


def to_record(kind: str, e: dict, detail: dict, page_url: str) -> Record | None:
    name = e["legal"] or detail.get("legal") or e["trade"]
    if not name:
        return None
    key = f"boc:{e['id']}" if e.get("id") else f"boc:{match_name(name) or norm_key(name)}"
    viol = e.get("violations", "")
    date = e["date"] if kind == "registered" else (e.get("applied") or e["date"])
    trade = e["trade"] or detail.get("trade", "")
    return Record(
        key=key, source="boc", jurisdiction="CA", regulator="Bank of Canada",
        name=name, licence_type=TYPE[kind], active=kind in ("registered", "applicant"),
        status_detail=TYPE[kind] + (f" ({e['status']})" if e.get("status") and e["status"].lower() not in TYPE[kind].lower() else "")
        + (f"; trade name {trade}" if trade and trade != name else "")
        + (f"; head office {e['country']}" if e["country"] else "")
        + (f"; violations: {viol}" if viol and viol.lower() not in ("0", "no", "none", "-", "false") else ""),
        status_date=_iso(date) or date[:10], reference=e.get("id", ""),
        city=detail.get("city", ""),
        website=detail.get("website") or e.get("website", ""), email=detail.get("email") or e.get("email", ""),
        register_url=e.get("link") or page_url,
        extra={"trade_name": trade, "head_office_country": e["country"], "phone": detail.get("phone", ""),
               "address": detail.get("address", "")},
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
    fingerprint = "boc-v3"   # bump when keys or parsing change, so the next run re-baselines quietly

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
                 "revoked": ["Legal name", "Main trade name", "Head office country", "Date"],
                 "withdrawn": ["Legal name", "Main trade name", "Head office country", "Date"]}
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

            entries, templates, status_counts = {}, {}, {}
            for page_kind, url in pages.items():
                notes.append(f"{page_kind}: {url}")
                try:
                    bodies, text, rows, hrefs = _load(page, url, notes)
                except Exception as ex:
                    notes.append(f"  FAILED {ex!r}")
                    if page_kind in ("registered", "applicant"):
                        raise
                    continue
                from_dom = _entries_from_rows(rows, heads.get(page_kind, []))
                from_json = best_json_for_table(bodies, from_dom)
                # the page's own table decides: use the data only if it is what the table shows
                use = from_json if from_json and len(from_json) >= len(from_dom) else from_dom
                notes.append(f"  entries: {len(from_json)} from matching data, {len(from_dom)} in the table -> using "
                             f"{'data' if use is from_json else 'table'}")
                # detail-page address pattern for this page, from its own links
                for h in hrefs:
                    m = re.search(r"([?&](?:account_id|id)=)([0-9a-f-]{8,})", h, re.I)
                    if m:
                        templates[page_kind] = h.replace(m.group(2), "{id}")
                        break
                link_by_name = {match_name(c[0]): l[0] for c, l in rows if c and l}
                for raw in use:
                    e = normalise_entry(raw)
                    if not e["id"] and e["link"]:
                        m = re.search(r"[?&](?:account_id|id)=([0-9a-f-]{8,})", e["link"], re.I)
                        if m:
                            e["id"] = m.group(1)
                    k = kind_of(e, page_kind)
                    sk = f"{e['group'] or '-'} / {e['status'] or '-'}"
                    status_counts[sk] = status_counts.get(sk, 0) + 1
                    ident = e["id"] or match_name(e["legal"] or e["trade"])
                    if not ident:
                        continue
                    if ident in entries:  # same firm on both pages (the Bank serves one list)
                        continue
                    if not e["link"]:
                        e["link"] = link_by_name.get(match_name(e["legal"]), "")
                    e["_kind"] = k
                    entries[ident] = e
                if use:
                    notes.append(f"  first entry raw: {json.dumps(use[0], ensure_ascii=False)[:600]}")
            notes.append(f"status / group values (counted per page): {status_counts}")
            notes.append(f"detail page patterns: {templates}")
            for e in entries.values():
                if not e["link"] and e["id"]:
                    tpl = templates.get(e["_kind"]) or templates.get("registered") or DETAIL_URL
                    e["link"] = tpl.replace("{id}", e["id"])
                elif e["link"] and not e["link"].startswith("http"):
                    e["link"] = urljoin(PAGES["registered"], e["link"])
            kinds = {}
            for e in entries.values():
                kinds[e["_kind"]] = kinds.get(e["_kind"], 0) + 1
            notes.append(f"firms by status: {kinds}")

            # 2. detail pages for email / website (registered first, then applicants; capped per run)
            todo = []
            for ident, e in entries.items():
                c = cache.get(ident)
                if (c is None or c.get("v") != 2) and e["link"]:
                    todo.append((e["_kind"], ident, e))
            # applicants first (fewer, and the ones you're most likely to contact), then registered
            first = cfg.get("details_first", "applicant")
            order = {first: 0, "registered" if first != "registered" else "applicant": 1}
            todo.sort(key=lambda t: order.get(t[0], 2))
            done = 0
            for _, ident, e in todo[:per_run]:
                dnotes = []
                try:
                    bodies, _, _, hrefs = _load(page, e["link"], dnotes, wait_rows=False)
                    try:
                        page.wait_for_function("() => /Email address|Legal name|Phone number/i.test(document.body.innerText)",
                                               timeout=15000)
                    except Exception:
                        pass
                    text = page.inner_text("body")
                    hrefs = page.evaluate("() => [...document.querySelectorAll('a[href]')].map(a => a.href)")
                    f = parse_detail_text(text)
                    email, site = contacts_from_page(text, hrefs, bodies)
                    cache[ident] = {"v": 2, "email": email, "website": site, "seen": ctx.today,
                                    "phone": f.get("Phone number", ""), "city": f.get("Head office city", ""),
                                    "address": f.get("Head office address", ""), "legal": f.get("Legal name", ""),
                                    "trade": (f.get("Trade names") or f.get("Trade name") or "").split(",")[0].strip(),
                                    "status": f.get("Registration status") or f.get("Application status", "")}
                    if done < 2:
                        notes.append(f"detail sample {e['link']}: {json.dumps(cache[ident], ensure_ascii=False)}")
                        notes.extend(dnotes[:5])
                    done += 1
                except Exception as ex:
                    notes.append(f"detail {e['link']}: {ex!r}")
                time.sleep(float(cfg.get("detail_delay_seconds", 1.0)))
            left = max(0, len(todo) - done)
            notes.append(f"detail pages: {done} fetched this run, {left} still to do, "
                         f"{sum(1 for v in cache.values() if v.get('v') == 2)} done in total")
            browser.close()

        state_p.parent.mkdir(parents=True, exist_ok=True)
        state_p.write_text(json.dumps(cache, indent=0, sort_keys=True))
        for ident, e in entries.items():
            r = to_record(e["_kind"], e, cache.get(ident, {}), PAGES.get(e["_kind"], PAGES["registered"]))
            if r:
                recs[r.key] = r
        if not any(r.licence_type == TYPE["registered"] for r in recs.values()):
            raise RuntimeError("Bank of Canada: no registered PSPs read; see data/debug/boc_sample.txt")
        return list(recs.values())


def _save_notes(ctx: Context, notes: list):
    p = ctx.data_dir / "debug" / "boc_sample.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(notes) + "\n", encoding="utf-8")
