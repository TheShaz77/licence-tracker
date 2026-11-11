"""Company details beyond contacts: LinkedIn page, headcount, revenue and extra websites.

Free, public, commercially usable sources only (no API keys, nothing that has to be logged into):

1. Wikidata (CC0): each firm is matched to a Wikidata item by its LEI, or by its exact name within its
   country. From the item: official website(s) (P856), LinkedIn company page (P4264), number of
   employees (P1128) and total revenue (P2139), each with its year. If the firm itself has no LinkedIn
   page there but its parent company (P749) does, the parent's page is given and marked as such.
2. Companies House free Accounts Data (UK, Crown copyright, free to re-use): the iXBRL accounts every
   UK company files. From them: average number of employees and turnover, for the latest year filed.
   Firms are matched by their Companies House number (the FCA register gives it), or by exact name using
   Companies House's free Basic Company Data file.
3. The firm's own website (read by the contacts job, see contacts.py): LinkedIn link in its header or
   footer, schema.org "numberOfEmployees", and the domain it redirects to.

What is NOT used, and why: LinkedIn itself (its terms and robots.txt forbid automated access), search
engines (same), Crunchbase / ZoomInfo / Apollo / OpenCorporates (paid, need keys, or forbid copying),
SEC EDGAR (requires each user to give a personal email in every request).
When no LinkedIn page is found, the sheet gives a LinkedIn company-search link for the firm, to click.
"""
from __future__ import annotations

import csv
import io
import os
import re
import tempfile
import time
import zipfile
from collections import Counter
from urllib.parse import quote, urljoin

from .sources.national import match_name

UA = "licence-tracker/1.0 (company details lookup; https://github.com)"
SPARQL = "https://query.wikidata.org/sparql"
CH_BASE = "https://download.companieshouse.gov.uk/"


# ------------------------------------------------------------------ firm website helpers

LI_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(company|school|showcase)/([A-Za-z0-9%_.\-~]+)", re.I)


def linkedin_in(html: str, name_hint: str = "") -> str:
    """The firm's LinkedIn company page linked from its own website (most-linked one wins)."""
    slugs = Counter()
    for kind, slug in LI_RE.findall(html or ""):
        slug = slug.strip(".-_")
        if not slug or slug.lower() in ("linkedin", "company", "share", "sharearticle", "login", "feed"):
            continue
        slugs[(kind.lower(), slug)] += 1
    if not slugs:
        return ""
    hint = match_name(name_hint)
    best = max(slugs, key=lambda k: (slugs[k] + (2 if hint and match_name(k[1].replace("-", " ")) in hint else 0),
                                     k[0] == "company"))
    return f"https://www.linkedin.com/{best[0]}/{best[1]}"


def employees_in(html: str) -> int:
    """schema.org numberOfEmployees on the firm's own site (JSON-LD or microdata)."""
    m = re.search(r'"numberOfEmployees"\s*:\s*(?:\{[^}]*?"(?:value|maxValue|minValue)"\s*:\s*"?(\d[\d,]*)|"?(\d[\d,]*))',
                  html or "")
    if not m:
        m2 = re.search(r'itemprop="numberOfEmployees"[^>]*content="(\d[\d,]*)"', html or "")
        return int(m2.group(1).replace(",", "")) if m2 else 0
    n = int((m.group(1) or m.group(2)).replace(",", ""))
    return n if 0 < n < 5_000_000 else 0


def add_search_links(records: list):
    """For the sheet only (not the CSV): where no LinkedIn page was found, a LinkedIn company-search link."""
    for r in records:
        if not r.get("linkedin") and r.get("name"):
            r["linkedin"] = search_link(r["name"])
            r["linkedin_source"] = "not found - link is a LinkedIn search"


def search_link(name: str) -> str:
    return "https://www.linkedin.com/search/results/companies/?keywords=" + quote(name or "")


# ------------------------------------------------------------------ formatting

def fmt_headcount(n, year="", src="") -> str:
    try:
        n = int(float(n))
    except (TypeError, ValueError):
        return ""
    bits = [b for b in (str(year)[:4], src) if b]
    return f"{n:,}" + (f" ({', '.join(bits)})" if bits else "")


def fmt_money(v, cur="", year="", src="") -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    for div, suf in ((1e9, "bn"), (1e6, "m"), (1e3, "k")):
        if v >= div:
            s = f"{v / div:.2f}".rstrip("0").rstrip(".") + suf
            break
    else:
        s = f"{v:.0f}"
    bits = [b for b in (str(year)[:4], src) if b]
    return (cur + " " if cur else "") + s + (f" ({', '.join(bits)})" if bits else "")


# ------------------------------------------------------------------ 1. Wikidata

def _sparql(ctx, q: str, timeout: int = 90) -> list:
    r = ctx.session.get(SPARQL, params={"query": q, "format": "json"},
                        headers={"User-Agent": UA, "Accept": "application/sparql-results+json"}, timeout=timeout)
    r.raise_for_status()
    return r.json()["results"]["bindings"]


def _v(b, k):
    return (b.get(k) or {}).get("value", "")


def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1] if uri else ""


NAME_INDEX_Q = """
SELECT DISTINCT ?item ?label WHERE {
  ?c wdt:P297 "%s" .
  { ?item wdt:P17 ?c } UNION { ?item wdt:P159/wdt:P17 ?c }
  %s
  { ?item rdfs:label ?label FILTER(LANG(?label) IN ("en", "mul", "%s")) }
  UNION { ?item wdt:P1448 ?label }
  UNION { ?item skos:altLabel ?label FILTER(LANG(?label) IN ("en", "mul")) }
}
"""
HAS_DATA = "{ ?item wdt:P4264 [] } UNION { ?item wdt:P1128 [] } UNION { ?item wdt:P2139 [] } UNION { ?item wdt:P1278 [] }"
HAS_LI = "?item wdt:P4264 [] ."


def name_index(ctx, cc: str, log) -> dict:
    """match_name -> set of QIDs, for companies of one country that Wikidata has data for."""
    out = {}
    for pattern in (HAS_DATA, HAS_LI):  # the narrower query if the full one times out
        try:
            rows = _sparql(ctx, NAME_INDEX_Q % (cc, pattern, cc.lower()), timeout=70)
        except Exception as e:
            log.info("enrich: Wikidata name list for %s failed (%s)%s", cc, str(e)[:80],
                     "; trying LinkedIn-only" if pattern == HAS_DATA else "")
            time.sleep(2)
            continue
        for b in rows:
            k = match_name(_v(b, "label"))
            if len(k) >= 4:
                out.setdefault(k, set()).add(_qid(_v(b, "item")))
        return out
    return out


DETAIL_Q = {
    "web": """SELECT ?item ?site ?li ?par ?parLabel ?pli WHERE { VALUES ?item { %s }
  OPTIONAL { ?item wdt:P856 ?site }
  OPTIONAL { ?item wdt:P4264 ?li }
  OPTIONAL { ?item wdt:P749 ?par . OPTIONAL { ?par wdt:P4264 ?pli }
             OPTIONAL { ?par rdfs:label ?parLabel FILTER(LANG(?parLabel) = "en") } } }""",
    "emp": """SELECT ?item ?emp ?d WHERE { VALUES ?item { %s }
  ?item p:P1128 ?s . ?s ps:P1128 ?emp . OPTIONAL { ?s pq:P585 ?d } }""",
    "rev": """SELECT ?item ?rev ?cur ?d WHERE { VALUES ?item { %s }
  ?item p:P2139 ?s . ?s psv:P2139 ?v . ?v wikibase:quantityAmount ?rev ; wikibase:quantityUnit ?u .
  OPTIONAL { ?u wdt:P498 ?cur } OPTIONAL { ?s pq:P585 ?d } }""",
}


def item_details(ctx, qids: list, log) -> dict:
    out = {q: {"sites": [], "linkedin": "", "parent": "", "parent_linkedin": ""} for q in qids}
    vals = " ".join("wd:" + q for q in qids)
    for kind, q in DETAIL_Q.items():
        try:
            rows = _sparql(ctx, q % vals)
        except Exception as e:
            log.warning("enrich: Wikidata %s details failed: %s", kind, str(e)[:100])
            raise
        best = {}
        for b in rows:
            d = out.get(_qid(_v(b, "item")))
            if d is None:
                continue
            if kind == "web":
                site = _v(b, "site").rstrip("/")
                if site and site not in d["sites"]:
                    d["sites"].append(site)
                if _v(b, "li"):
                    d["linkedin"] = "https://www.linkedin.com/company/" + _v(b, "li")
                if _v(b, "pli") and not d["parent_linkedin"]:
                    d["parent_linkedin"] = "https://www.linkedin.com/company/" + _v(b, "pli")
                    d["parent"] = _v(b, "parLabel")
            else:
                val = _v(b, "emp" if kind == "emp" else "rev")
                date = _v(b, "d")[:10]
                key = _qid(_v(b, "item"))
                if val and (key not in best or date > best[key][1]):
                    best[key] = (val, date, _v(b, "cur"))
        for key, (val, date, cur) in best.items():
            if kind == "emp":
                out[key].update(employees=float(val), employees_year=date[:4])
            else:
                out[key].update(revenue=float(val), currency=cur, revenue_year=date[:4])
        time.sleep(1)
    return out


def wikidata(ctx, records: list, cache: dict, log, seconds: float = 1800, refresh_days: int = 30) -> dict:
    """Match firms to Wikidata items (LEI, then exact name in the same country) and cache item details."""
    import datetime as dt
    deadline = time.monotonic() + seconds
    today = dt.date.fromisoformat(ctx.today)

    def stale(entry):
        try:
            return (today - dt.date.fromisoformat((entry or {}).get("checked", "2000-01-01"))).days >= refresh_days
        except ValueError:
            return True

    # LEI -> item
    leis = sorted({(r.get("lei") or "").strip().upper() for r in records} - {""})
    todo = [l for l in leis if len(l) == 20 and stale(cache.get("wd:lei:" + l))]
    n_lei = 0
    for i in range(0, len(todo), 200):
        if time.monotonic() > deadline:
            break
        chunk = todo[i:i + 200]
        try:
            rows = _sparql(ctx, "SELECT ?lei ?item WHERE { VALUES ?lei { %s } ?item wdt:P1278 ?lei . }"
                           % " ".join(f'"{l}"' for l in chunk))
        except Exception as e:
            log.warning("enrich: Wikidata LEI lookup failed: %s", str(e)[:100])
            break
        got = {_v(b, "lei"): _qid(_v(b, "item")) for b in rows}
        for l in chunk:
            cache["wd:lei:" + l] = {"q": got.get(l, ""), "checked": ctx.today}
        n_lei += len(got)
        time.sleep(1)

    # exact name within the country (rebuilt each run; only names that point at one item count)
    by_cc = {}
    for r in records:
        cc = (r.get("jurisdiction") or "").upper()
        if len(cc) == 2 and r.get("name"):
            by_cc.setdefault(cc, []).append(r)
    names = cache.get("wd:names") or {}
    done_cc = 0
    order = sorted(by_cc, key=lambda c: (cache.get("wd:cc", {}).get(c, ""), c))  # least recently done first
    for cc in order:
        if time.monotonic() > deadline:
            break
        idx = name_index(ctx, cc, log)
        for r in by_cc[cc]:
            hits = idx.get(match_name(r["name"]), set())
            if len(hits) == 1:
                names[r["key"]] = next(iter(hits))
        cache.setdefault("wd:cc", {})[cc] = ctx.today
        done_cc += 1
        time.sleep(1)
    cache["wd:names"] = names

    # details for every matched item not checked recently
    qids = {v["q"] for k, v in cache.items() if k.startswith("wd:lei:") and isinstance(v, dict) and v.get("q")}
    qids |= set(names.values())
    todo = sorted(q for q in qids if stale(cache.get("q:" + q)))
    n_items = 0
    for i in range(0, len(todo), 80):
        if time.monotonic() > deadline:
            break
        chunk = todo[i:i + 80]
        try:
            det = item_details(ctx, chunk, log)
        except Exception:
            break
        for q, d in det.items():
            d["checked"] = ctx.today
            cache["q:" + q] = d
        n_items += len(chunk)
    left = len(todo) - n_items
    log.info("enrich: Wikidata - %d LEIs matched, %d countries name-matched (%d firms in total), "
             "%d items read, %d left for next time", n_lei, done_cc, len(names), n_items, left)
    return {"lei_matches": n_lei, "countries": done_cc, "name_matches": len(names), "items": n_items, "left": left}


# ------------------------------------------------------------------ 2. Companies House accounts

def ch_number(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]", "", (s or "")).upper()
    if not s or s in ("0", "NONE", "NA"):
        return ""
    if s.isdigit():
        s = s.zfill(8)
    elif len(s) < 8 and re.match(r"^[A-Z]{2}\d+$", s):
        s = s[:2] + s[2:].zfill(6)
    return s if len(s) == 8 else ""


EMP_CONCEPTS = ("AverageNumberEmployeesDuringPeriod", "AverageNumberOfEmployees", "NumberOfEmployees")
REV_CONCEPTS = ("TurnoverRevenue", "Turnover", "Revenue", "RevenueFromContractsWithCustomers",
                "TurnoverGrossOperatingRevenue")


def _num(text: str, attrs: str) -> float | None:
    t = re.sub(r"<[^>]+>", "", text or "").strip()
    fmt = (re.search(r'\bformat="([^"]+)"', attrs) or [None, ""])[1].lower()
    if "zero" in fmt or t in ("-", "–", "—", ""):
        return 0.0
    if "comma" in fmt and "decimal" in fmt:      # 1.234,56
        t = t.replace(".", "").replace(" ", "").replace(",", ".")
    else:
        t = t.replace(",", "").replace(" ", "")
    try:
        v = float(t)
    except ValueError:
        return None
    sc = re.search(r'\bscale="(-?\d+)"', attrs)
    return v * (10 ** int(sc.group(1))) if sc else v


def parse_accounts(text: str) -> dict:
    """Employees and turnover (latest period, whole company) from one iXBRL / XBRL accounts file."""
    ctxs = {}
    for cid, body in re.findall(r'<(?:\w+:)?context\b[^>]*\bid="([^"]+)"[^>]*>(.*?)</(?:\w+:)?context>', text, re.S):
        if re.search(r"explicitMember|typedMember", body):
            continue                     # a breakdown (by segment, director, ...), not the total
        end = re.search(r"<(?:\w+:)?(?:endDate|instant)>\s*([\d-]{10})", body)
        ctxs[cid] = end.group(1) if end else ""
    units = {uid: m for uid, m in re.findall(
        r'<(?:\w+:)?unit\b[^>]*\bid="([^"]+)"[^>]*>.*?iso4217:([A-Z]{3})', text, re.S)}
    facts = []
    for attrs, body in re.findall(r"<ix:nonFraction\b([^>]*)>(.*?)</ix:nonFraction>", text, re.S | re.I):
        name = (re.search(r'\bname="(?:[\w-]+:)?([^"]+)"', attrs) or [None, ""])[1]
        facts.append((name, attrs, body))
    for _, name, attrs, body in re.findall(
            r"<([\w-]+):(" + "|".join(EMP_CONCEPTS + REV_CONCEPTS) + r")\b([^>]*)>([^<]*)</\1:\2>", text):
        facts.append((name, attrs, body))
    out = {}
    for name, attrs, body in facts:
        kind = "employees" if name in EMP_CONCEPTS else "revenue" if name in REV_CONCEPTS else ""
        if not kind:
            continue
        cref = (re.search(r'\bcontextRef="([^"]+)"', attrs) or [None, ""])[1]
        if cref not in ctxs:
            continue
        v = _num(body, attrs)
        if v is None or (kind == "revenue" and v <= 0):
            continue
        end = ctxs[cref]
        if kind not in out or end > out[kind][1]:
            unit = (re.search(r'\bunitRef="([^"]+)"', attrs) or [None, ""])[1]
            out[kind] = (v, end, units.get(unit, "GBP" if kind == "revenue" else ""))
    res = {}
    if "employees" in out:
        res.update(employees=int(round(out["employees"][0])), period=out["employees"][1])
    if "revenue" in out:
        res.update(revenue=out["revenue"][0], currency=out["revenue"][2] or "GBP")
        res["period"] = max(res.get("period", ""), out["revenue"][1])
    m = re.search(r'name="[\w-]+:EntityCurrentLegalOrRegisteredName"[^>]*>(.*?)</ix:nonNumeric>', text, re.S)
    if m:
        res["name"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()[:120]
    return res


MEMBER_RE = re.compile(r"_([A-Z0-9]{8})_(\d{8})\.(?:html|xhtml|xml)$", re.I)


def read_accounts_zip(path: str, wanted: set, cache: dict, deadline: float) -> int:
    """Parse the accounts of wanted companies inside one bulk zip. Returns how many were updated."""
    n = 0
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            m = MEMBER_RE.search(info.filename)
            if not m or m.group(1).upper() not in wanted:
                continue
            if time.monotonic() > deadline:
                break
            num, made_up = m.group(1).upper(), m.group(2)
            prev = cache.get("ch:" + num) or {}
            period = f"{made_up[:4]}-{made_up[4:6]}-{made_up[6:]}"
            if prev.get("period", "") >= period:
                continue
            try:
                text = z.read(info).decode("utf-8", "replace")
            except Exception:
                continue
            res = parse_accounts(text)
            if res.get("employees") is not None or res.get("revenue"):
                res.setdefault("period", period)
                cache["ch:" + num] = res
                n += 1
    return n


def _robots_ok(ctx, url: str):
    from .http import robots_allowed
    if not robots_allowed(ctx.session, url, UA):
        raise PermissionError(f"robots.txt disallows {url}")


def _download(ctx, url: str, log) -> str:
    _robots_ok(ctx, url)
    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        with ctx.session.get(url, stream=True, timeout=120, headers={"User-Agent": UA}) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
    except Exception:
        os.unlink(path)
        raise
    return path


def _links(ctx, page: str, pattern: str) -> list:
    _robots_ok(ctx, CH_BASE + page)
    r = ctx.session.get(CH_BASE + page, timeout=60, headers={"User-Agent": UA})
    r.raise_for_status()
    return sorted(set(re.findall(pattern, r.text)))


MONTHS = {m: i for i, m in enumerate(("January", "February", "March", "April", "May", "June", "July", "August",
                                      "September", "October", "November", "December"), 1)}


def _file_date(name: str) -> str:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    if m:
        return m.group(1)
    m = re.search(r"-([A-Z][a-z]+)(\d{4})\.zip$", name)
    return f"{m.group(2)}-{MONTHS.get(m.group(1), 0):02d}-28" if m else ""


def ch_names(ctx, records: list, cache: dict, log, deadline: float):
    """Companies House numbers for UK firms whose register gives none, by exact name (Basic Company Data)."""
    want = {}
    for r in records:
        if (r.get("jurisdiction") or "").upper() == "GB" and not ch_number((r.get("extra") or {}).get("companies_house", "")):
            k = match_name(r.get("name", ""))
            if len(k) >= 4:
                want.setdefault(k, []).append(r["key"])
    if not want:
        return 0
    files = _links(ctx, "en_output.html", r'href="(BasicCompanyDataAsOneFile-\d{4}-\d{2}-\d{2}\.zip)"')
    if not files or cache.get("ch:basic") == files[-1] or time.monotonic() > deadline - 900:
        return 0
    log.info("enrich: reading Companies House Basic Company Data (%s) for %d UK names", files[-1], len(want))
    path = _download(ctx, CH_BASE + files[-1], log)
    found = {}
    try:
        with zipfile.ZipFile(path) as z:
            member = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            with z.open(member) as f:
                rd = csv.reader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"))
                head = [h.strip() for h in next(rd)]
                i_name, i_num = head.index("CompanyName"), head.index("CompanyNumber")
                for row in rd:
                    if len(row) <= max(i_name, i_num):
                        continue
                    k = match_name(row[i_name])
                    if k in want:
                        found.setdefault(k, set()).add(row[i_num].strip())
    finally:
        os.unlink(path)
    names = {}
    for k, nums in found.items():
        if len(nums) == 1:
            for key in want[k]:
                names[key] = ch_number(next(iter(nums)))
    cache["ch:names"] = names
    cache["ch:basic"] = files[-1]
    log.info("enrich: %d UK firms matched to a Companies House number by name", len(names))
    return len(names)


def companies_house(ctx, records: list, cache: dict, log, seconds: float = 5400, save=None) -> dict:
    """Latest accounts (employees, turnover) for UK firms from the free bulk Accounts Data files.
    Monthly files (last 12 months) then daily files (last 60 days); newest first; each file read once."""
    deadline = time.monotonic() + seconds
    try:
        ch_names(ctx, records, cache, log, deadline)
    except Exception as e:
        log.warning("enrich: Companies House name matching skipped: %s", str(e)[:120])
    wanted = {ch_number((r.get("extra") or {}).get("companies_house", "")) for r in records} - {""}
    wanted |= set((cache.get("ch:names") or {}).values())
    if not wanted:
        return {"companies": 0}
    try:
        files = _links(ctx, "en_monthlyaccountsdata.html", r'href="(Accounts_Monthly_Data-[A-Za-z]+\d{4}\.zip)"')
        files += _links(ctx, "en_accountsdata.html", r'href="(Accounts_Bulk_Data-\d{4}-\d{2}-\d{2}\.zip)"')
    except Exception as e:
        log.warning("enrich: Companies House file list unavailable: %s", str(e)[:120])
        return {"companies": 0}
    done = set(cache.get("ch:files") or [])
    todo = sorted((f for f in files if f not in done), key=_file_date, reverse=True)
    log.info("enrich: Companies House accounts - %d UK companies wanted, %d files to read", len(wanted), len(todo))
    updated, read = 0, 0
    for name in todo:
        if time.monotonic() > deadline - 600:   # a monthly file takes a few minutes to fetch and scan
            break
        try:
            path = _download(ctx, CH_BASE + name, log)
        except Exception as e:
            log.warning("enrich: could not download %s: %s", name, str(e)[:100])
            continue
        try:
            n = read_accounts_zip(path, wanted, cache, deadline)
        except zipfile.BadZipFile:
            log.warning("enrich: %s is not a valid zip; skipped", name)
            n = 0
        finally:
            os.unlink(path)
        if time.monotonic() <= deadline:
            done.add(name)
        cache["ch:files"] = sorted(done)
        updated += n
        read += 1
        log.info("enrich: %s read, %d companies updated", name, n)
        if save:
            save()
    have = sum(1 for n in wanted if cache.get("ch:" + n))
    log.info("enrich: Companies House - %d files read this run, %d companies with accounts data (of %d)",
             read, have, len(wanted))
    return {"files": read, "updated": updated, "with_data": have, "wanted": len(wanted),
            "files_left": len(todo) - read}


# ------------------------------------------------------------------ filling the sheet columns

def apply(records: list, cache: dict, domain_of) -> dict:
    """Fill linkedin / headcount / revenue / other websites (and a blank website) on each record."""
    n = Counter()
    names = cache.get("wd:names") or {}
    ch_names_map = cache.get("ch:names") or {}
    for r in records:
        lei = (r.get("lei") or "").strip().upper()
        q = ((cache.get("wd:lei:" + lei) or {}).get("q") if lei else "") or names.get(r.get("key", ""), "")
        wd = cache.get("q:" + q) or {} if q else {}
        site = r.get("website") or ""
        if not site and wd.get("sites"):
            r["website"] = site = wd["sites"][0]
            r["contact_source"] = ", ".join(x for x in ((r.get("contact_source") or ""), "Wikidata") if x)
            n["website"] += 1
        web = cache.get("site:" + domain_of(site), {}) if site else {}
        chn = ch_number((r.get("extra") or {}).get("companies_house", "")) or ch_names_map.get(r.get("key", ""), "")
        ch = cache.get("ch:" + chn) or {} if chn else {}

        if not r.get("linkedin"):
            if web.get("linkedin"):
                r["linkedin"], r["linkedin_source"] = web["linkedin"], "firm website"
            elif wd.get("linkedin"):
                r["linkedin"], r["linkedin_source"] = wd["linkedin"], "Wikidata"
            elif wd.get("parent_linkedin"):
                r["linkedin"] = wd["parent_linkedin"]
                r["linkedin_source"] = f"Wikidata - parent company's page ({wd.get('parent') or 'parent'})"
        n["linkedin"] += bool(r.get("linkedin"))

        if not r.get("headcount"):
            if ch.get("employees") is not None:
                r["headcount"] = fmt_headcount(ch["employees"], ch.get("period", ""), "Companies House accounts")
            elif wd.get("employees"):
                r["headcount"] = fmt_headcount(wd["employees"], wd.get("employees_year", ""), "Wikidata")
            elif web.get("employees"):
                r["headcount"] = fmt_headcount(web["employees"], "", "firm website")
        n["headcount"] += bool(r.get("headcount"))

        if not r.get("revenue"):
            if ch.get("revenue"):
                r["revenue"] = fmt_money(ch["revenue"], ch.get("currency", "GBP"), ch.get("period", ""),
                                         "Companies House accounts")
            elif wd.get("revenue"):
                r["revenue"] = fmt_money(wd["revenue"], wd.get("currency", ""), wd.get("revenue_year", ""), "Wikidata")
        n["revenue"] += bool(r.get("revenue"))

        main = domain_of(site) if site else ""
        others = []
        for s in list(wd.get("sites") or []) + ([web["final_url"]] if web.get("final_url") else []):
            d = domain_of(s)
            if d and d != main and d not in [domain_of(o) for o in others]:
                others.append(s)
        if others:
            r["other_websites"] = " ; ".join(others[:5])
            n["other_websites"] += 1
    return dict(n)
