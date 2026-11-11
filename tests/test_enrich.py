import logging
import time
import zipfile

from regtracker import enrich as e

log = logging.getLogger("t")

IXBRL = """<html><head><ix:header><ix:resources>
<xbrli:context id="cur"><xbrli:entity/><xbrli:period><xbrli:startDate>2024-04-01</xbrli:startDate>
<xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:context id="prev"><xbrli:period><xbrli:endDate>2024-03-31</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:context id="seg"><xbrli:entity><xbrli:segment><xbrldi:explicitMember dimension="x">y</xbrldi:explicitMember>
</xbrli:segment></xbrli:entity><xbrli:period><xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:unit id="GBP"><xbrli:measure>iso4217:GBP</xbrli:measure></xbrli:unit>
</ix:resources></ix:header></head><body>
<ix:nonNumeric name="bus:EntityCurrentLegalOrRegisteredName" contextRef="cur">ACME PAYMENTS <b>LIMITED</b></ix:nonNumeric>
<ix:nonFraction name="core:TurnoverRevenue" contextRef="cur" unitRef="GBP" scale="3" format="ixt:num-dot-decimal">1,250</ix:nonFraction>
<ix:nonFraction name="core:TurnoverRevenue" contextRef="prev" unitRef="GBP" scale="3">900</ix:nonFraction>
<ix:nonFraction name="core:TurnoverRevenue" contextRef="seg" unitRef="GBP" scale="3">99999</ix:nonFraction>
<ix:nonFraction name="core:AverageNumberEmployeesDuringPeriod" contextRef="cur" unitRef="pure">42</ix:nonFraction>
<ix:nonFraction name="core:AverageNumberEmployeesDuringPeriod" contextRef="prev" unitRef="pure">30</ix:nonFraction>
</body></html>"""


def test_parse_accounts_latest_total():
    res = e.parse_accounts(IXBRL)
    assert res["employees"] == 42 and res["revenue"] == 1_250_000 and res["currency"] == "GBP"
    assert res["period"] == "2025-03-31" and res["name"] == "ACME PAYMENTS LIMITED"


def test_parse_accounts_zero_dash_employees_and_plain_xbrl():
    t = IXBRL.replace(">42<", ">-<").replace('unitRef="pure">-', 'unitRef="pure" format="ixt2:fixed-zero">-')
    assert e.parse_accounts(t)["employees"] == 0
    xml = ('<xbrli:context id="c1"><xbrli:period><xbrli:endDate>2023-12-31</xbrli:endDate></xbrli:period>'
           '</xbrli:context><uk-gaap:Turnover contextRef="c1" unitRef="EUR">2500000</uk-gaap:Turnover>'
           '<xbrli:unit id="EUR"><xbrli:measure>iso4217:EUR</xbrli:measure></xbrli:unit>')
    r = e.parse_accounts(xml)
    assert r["revenue"] == 2_500_000 and r["currency"] == "EUR"


def test_read_accounts_zip_only_wanted_and_newer(tmp_path):
    p = tmp_path / "a.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Prod223_0001_01234567_20250331.html", IXBRL)
        z.writestr("Prod223_0001_99999999_20250331.html", IXBRL)
    cache = {"ch:SC000001": {"employees": 5, "period": "2026-01-01"}}
    n = e.read_accounts_zip(str(p), {"01234567", "SC000001"}, cache, time.monotonic() + 60)
    assert n == 1 and cache["ch:01234567"]["employees"] == 42 and "ch:99999999" not in cache
    # an older filing never replaces a newer one
    cache["ch:01234567"]["period"] = "2026-03-31"
    assert e.read_accounts_zip(str(p), {"01234567"}, cache, time.monotonic() + 60) == 0


def test_ch_number():
    assert e.ch_number("7762021") == "07762021"
    assert e.ch_number("SC12345") == "SC012345"
    assert e.ch_number(" OC123456 ") == "OC123456"
    assert e.ch_number("") == "" and e.ch_number("None") == ""


def test_linkedin_and_employees_from_website():
    html = ('<a href="https://www.linkedin.com/shareArticle?x">s</a>'
            '<a href="https://uk.linkedin.com/company/acme-payments/">in</a>'
            '<a href="https://www.linkedin.com/company/acme-payments">in</a>'
            '<a href="https://www.linkedin.com/in/some-person">ceo</a>'
            '<script type="application/ld+json">{"@type":"Organization","numberOfEmployees":'
            '{"@type":"QuantitativeValue","value":"250"}}</script>')
    assert e.linkedin_in(html, "Acme Payments Ltd") == "https://www.linkedin.com/company/acme-payments"
    assert e.employees_in(html) == 250
    assert e.linkedin_in("<p>no links</p>") == "" and e.employees_in("") == 0


def test_formatting():
    assert e.fmt_headcount(1234, "2025-03-31", "Companies House accounts") == "1,234 (2025, Companies House accounts)"
    assert e.fmt_money(1_250_000, "GBP", "2025", "Wikidata") == "GBP 1.25m (2025, Wikidata)"
    assert e.fmt_money(3.4e9, "USD") == "USD 3.4bn"
    assert e.fmt_money(0) == ""


def test_apply_priorities_and_search_link():
    from regtracker.contacts import domain
    cache = {
        "wd:lei:LEI00000000000000001": {"q": "Q1"},
        "wd:names": {"k3": "Q3"},
        "q:Q1": {"sites": ["https://acme.com", "https://acme.co.uk"], "linkedin": "https://www.linkedin.com/company/acme",
                 "employees": 5000.0, "employees_year": "2023", "revenue": 2e9, "currency": "USD", "revenue_year": "2024"},
        "q:Q3": {"sites": ["https://beta.io"], "linkedin": "", "parent": "Beta Group",
                 "parent_linkedin": "https://www.linkedin.com/company/beta-group"},
        "site:acme.com": {"linkedin": "https://www.linkedin.com/company/acme-global", "employees": 10},
        "ch:01234567": {"employees": 42, "revenue": 1.25e6, "currency": "GBP", "period": "2025-03-31"},
    }
    recs = [
        {"key": "k1", "name": "Acme", "lei": "LEI00000000000000001", "website": "https://acme.com",
         "extra": {"companies_house": "1234567"}},
        {"key": "k2", "name": "Nobody Ltd", "website": ""},
        {"key": "k3", "name": "Beta", "website": ""},
    ]
    n = e.apply(recs, cache, domain)
    a, b, c = recs
    assert a["linkedin"] == "https://www.linkedin.com/company/acme-global" and a["linkedin_source"] == "firm website"
    assert a["headcount"] == "42 (2025, Companies House accounts)"
    assert a["revenue"] == "GBP 1.25m (2025, Companies House accounts)"
    assert a["other_websites"] == "https://acme.co.uk"
    assert c["website"] == "https://beta.io" and "parent company" in c["linkedin_source"]
    assert c["linkedin"].endswith("beta-group")
    assert not b.get("linkedin") and n["linkedin"] == 2
    e.add_search_links(recs)
    assert b["linkedin"].startswith("https://www.linkedin.com/search/results/companies/?keywords=Nobody")
    assert b["linkedin_source"].startswith("not found")
    assert a["linkedin"].endswith("acme-global")


class FakeResp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return {"results": {"bindings": self.data}}


class FakeSession:
    def __init__(self):
        self.queries = []

    def get(self, url, params=None, **kw):
        q = params["query"]
        self.queries.append(q)
        uri = "http://www.wikidata.org/entity/"
        if "wdt:P1278 ?lei" in q:
            return FakeResp([{"lei": {"value": "LEI00000000000000001"}, "item": {"value": uri + "Q1"}}])
        if "wdt:P297" in q:
            return FakeResp([{"item": {"value": uri + "Q2"}, "label": {"value": "Gamma Pay"}},
                             {"item": {"value": uri + "Q5"}, "label": {"value": "Twin"}},
                             {"item": {"value": uri + "Q6"}, "label": {"value": "Twin"}}])
        if "wdt:P856" in q:
            return FakeResp([{"item": {"value": uri + "Q2"}, "site": {"value": "https://gammapay.com/"},
                              "li": {"value": "gamma-pay"}}])
        if "P1128" in q:
            return FakeResp([{"item": {"value": uri + "Q2"}, "emp": {"value": "120"}, "d": {"value": "2022-01-01T00:00:00Z"}},
                             {"item": {"value": uri + "Q2"}, "emp": {"value": "150"}, "d": {"value": "2024-01-01T00:00:00Z"}}])
        return FakeResp([])


def test_wikidata_matching(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)

    class Ctx:
        session = FakeSession()
        today = "2026-11-11"
    recs = [{"key": "a", "name": "Gamma Pay Ltd", "jurisdiction": "GB", "lei": ""},
            {"key": "b", "name": "Twin", "jurisdiction": "GB"},          # two items with that name: skipped
            {"key": "c", "name": "Acme", "jurisdiction": "DE", "lei": "LEI00000000000000001"}]
    cache = {}
    out = e.wikidata(Ctx, recs, cache, log, seconds=60)
    assert cache["wd:names"] == {"a": "Q2"} and cache["wd:lei:LEI00000000000000001"]["q"] == "Q1"
    assert cache["q:Q2"]["linkedin"] == "https://www.linkedin.com/company/gamma-pay"
    assert cache["q:Q2"]["employees"] == 150 and cache["q:Q2"]["employees_year"] == "2024"
    assert out["name_matches"] == 1
    # a second run the same day does not ask for the same items again
    n = len(Ctx.session.queries)
    e.wikidata(Ctx, recs, cache, log, seconds=60)
    assert not any("VALUES ?item" in q for q in Ctx.session.queries[n:])
