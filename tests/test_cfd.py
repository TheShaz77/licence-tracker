import json

from regtracker import sheets
from regtracker.model import guess_business_type
from regtracker.sources import fca
from regtracker.sources.national import _date_key, country_code, parse_file


def test_cfd_permissions_only_dealing_counts():
    deal = {"Dealing in Investments as Principal": [
        {"Customer Type": ["Retail", "Professional"]},
        {"Investment Type": ["Contracts for Differences (excluding spread bets and rolling spot forex contracts)"]}]}
    assert fca.cfd_kinds(deal) == ["CFDs"]
    assert fca.cfd_clients(deal) == "retail clients"
    managing = {"Managing Investments": [{"Investment Type": ["Contracts for Differences"]}]}
    assert fca.cfd_kinds(managing) == []
    assert fca.cfd_kinds([deal]) == ["CFDs"]


def test_business_type_guess():
    assert guess_business_type("FTMO Evaluation Ltd") == "Prop trading firm (guess)"
    assert guess_business_type("Finalto Prime Ltd").startswith("Institutional")
    assert guess_business_type("CMC Markets UK plc", "CFDs retail clients") == "Retail broker (guess)"
    assert guess_business_type("Acme Markets", "CFDs professional clients only").startswith("Institutional")


def test_country_names_and_month_dates():
    assert country_code("IRELAND") == "IE"
    assert country_code("Czech Republic") == "CZ"
    assert country_code("de") == "DE"
    assert _date_key("afs_lic_202602.csv") > _date_key("afs_lic_202512.csv")


def test_per_row_country_and_require_pattern():
    doc = {"response": {"docs": [
        {"ae_entityName": "IG Europe GmbH", "ae_lei": "L1", "ae_homeMemberState": "GERMANY", "ae_officeType": "Head office"},
        {"ae_entityName": "IG Europe GmbH", "ae_lei": "L1", "ae_homeMemberState": "GERMANY", "ae_officeType": "Branch"}]}}
    spec = {"url": "x", "name_column": "ae_entityName", "id_column": "ae_lei", "country_column": "ae_homeMemberState",
            "exclude_column": "ae_officeType", "exclude_pattern": "branch", "licence_type": "IF",
            "_prefix": "cfd", "_vertical": "CFD"}
    recs, _ = parse_file(json.dumps(doc).encode(), spec, "EU", "ESMA")
    assert [(r.key, r.jurisdiction, r.extra["vertical"]) for r in recs] == [("cfd_eu:L1", "DE", "CFD")]
    tsv = "AFS_LIC_NUM\tAFS_LIC_NAME\tAFS_LIC_CONDITION\n1\tA Markets\tmake a market for derivatives\n2\tB Advice\tadvice\n"
    spec = {"url": "x", "name_column": "AFS_LIC_NAME", "id_column": "AFS_LIC_NUM", "licence_type": "AFS",
            "require_column": "AFS_LIC_CONDITION", "require_pattern": "make a market"}
    recs, _ = parse_file(tsv.encode(), spec, "AU", "ASIC")
    assert [r.name for r in recs] == ["A Markets"]


def test_cfd_tabs_are_separate():
    recs = [{"name": "IG Markets", "jurisdiction": "GB", "active": True, "source": "fca_cfd", "licence_type": "CFD",
             "extra": {"vertical": "CFD", "business_type": "Retail broker (guess)"}},
            {"name": "IG Markets Ltd", "jurisdiction": "GB", "active": True, "source": "cfd_x", "licence_type": "Other",
             "extra": {"vertical": "CFD"}},
            {"name": "Wise", "jurisdiction": "GB", "active": True, "source": "fca", "licence_type": "EMI"}]
    tabs = sheets.build_tabs(recs, [], {})
    assert len(tabs["Licences"]) == 2 and len(tabs["United Kingdom (GB)"]) == 2
    assert len(tabs["United Kingdom (CFD)"]) == 2          # the two CFD records merged into one row
    assert "CFD + Other" in tabs["United Kingdom (CFD)"][1]


def test_igaming_type_guess_and_tabs():
    from regtracker.model import guess_business_type as g
    assert g("Affiliate Media Ltd", "associate", "iGaming") == "Affiliate (guess)"
    assert g("Paysafe Payment Solutions", "", "iGaming") == "PSP (guess)"
    assert g("Spin Ltd", "Remote casino", "iGaming", "bitcoincasino.io") == "Crypto casino (guess)"
    assert g("Relax Gaming Ltd", "Casino Software Provider Authorization - B2B", "iGaming").startswith("Platform")
    assert g("Bet Co", "Remote betting + Gambling software", "iGaming").startswith("Operator")
    recs = [{"name": "Bet Co", "jurisdiction": "MT", "active": True, "source": "igm_mt", "licence_type": "B2C",
             "extra": {"vertical": "iGaming", "business_type": "Operator (guess)"}}]
    tabs = sheets.build_tabs(recs, [], {})
    assert "Malta (iGaming)" in tabs and "iGaming (all countries)" in tabs and len(tabs["Licences"]) == 1


def test_join_two_files():
    lic = b"Account Number,Activity\n1,Remote Casino\n1,Gambling Software\n2,Bingo\n"
    spec = {"url": "x", "name_column": "Account Name", "id_column": "Account Number", "type_from_row": True,
            "type_patterns": {"software": "B2B", "remote casino": "B2C"}, "_prefix": "igm", "_vertical": "iGaming",
            "_join": (["Account Number", "Account Name"], {"1": ["1", "Acme Ltd"], "2": ["2", "Hall Ltd"]}, "Account Number")}
    recs, _ = parse_file(lic, spec, "GB", "UKGC")
    assert [(r.name, r.licence_type) for r in recs] == [("Acme Ltd", "B2B + B2C")]


def test_cfd_name_search(tmp_path):
    import logging, time
    from regtracker.sources.base import Context
    (tmp_path / "state").mkdir()
    ctx = Context(config={}, session=None, today="2026-11-10", data_dir=tmp_path, excluded_countries=set(),
                  log=logging.getLogger("t"))
    perms = {"Dealing in Investments as Principal": [{"Customer Type": ["Retail"]},
                                                     {"Investment Type": ["Contracts for Differences"]}]}

    class Client:
        exclude = ()
        def get_body(self, path):
            page = int(path.rsplit("pgnp=", 1)[1])
            if page > 1:
                return {"Data": []}
            return {"Data": [{"Reference Number": "195355", "Status": "Authorised", "Name": "IG Markets"},
                             {"Reference Number": "111", "Status": "No longer authorised", "Name": "Old Markets"}],
                    "ResultInfo": {"page": "1", "per_page": "20", "total_count": "2"}}
        def firm(self, frn):
            return {"Organisation Name": "IG Markets Limited", "Status": "Authorised"}
        def get(self, path):
            return perms
        def address(self, frn):
            return {}
    st = {"seed": {}, "tracked": {}}
    fca.cfd_search(Client(), st, ctx, time.monotonic() + 60, 2, ["markets"])
    assert list(st["cfd"]) == ["195355"] and st["cfd_search"]["done"]


def test_igaming_supplier_and_affiliate_licences():
    from regtracker.model import guess_business_type as g
    assert g("Tipico Services Limited", "Marketing / affiliate services licence", "iGaming") == "Affiliate (guess)"
    assert g("Geocomply Inc.", "Internet gaming vendor registration", "iGaming").startswith("Supplier")


def test_wiki_banks_parsing_and_fetch(tmp_path):
    import logging
    from regtracker.sources.base import Context
    from regtracker.sources.wiki_banks import WikiBanksSource, parse_wikitext
    text = """Intro about [[Banking in Thailand]].
== Central bank ==
* [[Bank of Thailand]]
== Commercial banks ==
{| class="wikitable"
! Name !! Owner
|-
| [[Bangkok Bank]] || [[Sophonpanich family]]
|-
| [[Kasikornbank|Kasikorn Bank]] || listed
|-
| [[Krung Thai Bank]] || [[Ministry of Finance (Thailand)|Ministry of Finance]]
|}
* Thai Credit Bank
== See also ==
* [[List of banks in Asia]]
"""
    names = [n for n, _ in parse_wikitext(text, "Thailand")]
    assert names == ["Bangkok Bank", "Kasikorn Bank", "Krung Thai Bank", "Thai Credit Bank"]

    class Resp:
        def __init__(self, j): self.j = j
        def raise_for_status(self): pass
        def json(self): return self.j

    class Sess:
        def get(self, url, params=None, **kw):
            if "sparql" in url:
                return Resp({"results": {"bindings": [
                    {"item": {"value": "http://www.wikidata.org/entity/Q1"}, "itemLabel": {"value": "Bangkok Bank"},
                     "website": {"value": "https://www.bangkokbank.com"}},
                    {"item": {"value": "http://www.wikidata.org/entity/Q2"}, "itemLabel": {"value": "Q2"}}]}})
            if params and params.get("action") == "parse":
                return Resp({"parse": {"title": "List of banks in Thailand", "wikitext": text}})
            return Resp({"entities": {}})
    ctx = Context(config={}, session=Sess(), today="2026-11-10", data_dir=tmp_path, excluded_countries={"KP"},
                  log=logging.getLogger("t"))
    recs = WikiBanksSource({"countries": ["TH", "KP"]}).fetch(ctx)
    assert sorted(r.name for r in recs) == ["Bangkok Bank", "Kasikorn Bank", "Krung Thai Bank", "Thai Credit Bank"]
    assert next(r for r in recs if r.name == "Bangkok Bank").website == "https://www.bangkokbank.com"


def test_wiki_other_fis(tmp_path):
    import logging
    from regtracker.sources.base import Context
    from regtracker.sources.wiki_banks import WikiBanksSource

    class Resp:
        def __init__(self, j): self.j = j
        def raise_for_status(self): pass
        def json(self): return self.j

    class Sess:
        def get(self, url, params=None, **kw):
            q = (params or {}).get("query", "")
            if "rdfs:label" in q:
                return Resp({"results": {"bindings": [
                    {"item": {"value": "http://www.wikidata.org/entity/Q9"}, "itemLabel": {"value": "Bitkub"},
                     "lbl": {"value": "cryptocurrency exchange", "xml:lang": "en"}},
                    {"item": {"value": "http://www.wikidata.org/entity/Q8"}, "itemLabel": {"value": "TrueMoney"},
                     "lbl": {"value": "mobile payment"}}]}})
            return Resp({"results": {"bindings": []}, "entities": {}})
    ctx = Context(config={}, session=Sess(), today="2026-11-10", data_dir=tmp_path, excluded_countries=set(),
                  log=logging.getLogger("t"))
    recs = WikiBanksSource({"fi_countries": ["TH"]}).fetch(ctx)
    assert {r.name: r.licence_type.split(" (")[0] for r in recs} == {
        "Bitkub": "Crypto / virtual asset service provider", "TrueMoney": "Payment / e-money company"}


def test_do_not_contact_and_competitor_flags(tmp_path):
    from regtracker.flags import COMP, DNC, Flagger, annotate
    (tmp_path / "flags").mkdir()
    (tmp_path / "flags" / "do_not_contact.txt").write_text("Account Name\nPay Perform OU\nGlobal Re LLC\nCapital.com\nMonex Ltd S.R.O.\n")
    (tmp_path / "flags" / "groups.yaml").write_text(
        "dnc_groups:\n  Bitstamp:\n    match: ['\\bbitstamp\\b']\n    owners: [Robinhood Markets, Inc.]\n"
        "competitors:\n  Modulr: {match: ['^modulr\\b']}\n")
    f = Flagger(None, root=tmp_path / "flags")
    assert f.flag("Pay Perform Limited")[0] == DNC
    assert f.flag("Global Reach Financial Solutions") is None          # whole words only
    assert f.flag("Capital Com SV Investments Ltd")[0] == DNC
    assert f.flag("Capital Computer Bureau") is None
    assert "different legal form" in f.flag("Monex Inc")[1]
    assert f.flag("Bitstamp Europe S.A.")[0] == DNC
    assert f.flag("ROBINHOOD MARKETS INC")[1].startswith("Owns Bitstamp")
    assert f.flag("Modulr FS Limited") == (COMP, "OpenPayd competitor (Modulr)")
    assert f.flag("Wise Payments Limited") is None


def test_flag_rows_turn_red():
    from regtracker import sheets
    calls = []

    class Sh:
        def fetch_sheet_metadata(self, params):
            return {"sheets": [{"properties": {"sheetId": 1, "title": "Licences"}, "conditionalFormats": [{}, {}]}]}
        def batch_update(self, body):
            calls.append(body)
    tabs = {"Licences": [["Jurisdiction", "Country", "Regulator", "Firm name", "Do not contact / competitor"], ["GB"] * 5]}
    sheets.highlight_flags(Sh(), tabs, {"Licences": 1})
    reqs = calls[0]["requests"]
    assert [list(r)[0] for r in reqs] == ["deleteConditionalFormatRule", "deleteConditionalFormatRule", "addConditionalFormatRule"]
    assert reqs[2]["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"][0]["userEnteredValue"] == '=$E2<>""'


def test_cfd_search_skips_failing_word(tmp_path):
    import logging, time
    from regtracker.sources.base import Context
    (tmp_path / "state").mkdir()
    ctx = Context(config={}, session=None, today="2026-11-10", data_dir=tmp_path, excluded_countries=set(),
                  log=logging.getLogger("t"))

    class Client:
        def get_body(self, path):
            if "q=financial" in path:
                raise RuntimeError("FCA /Search?q=financial failed repeatedly")
            return {"Data": []}
    st = {"seed": {}, "tracked": {}}
    fca.cfd_search(Client(), st, ctx, time.monotonic() + 60, 2, ["financial", "markets"])
    assert st["cfd_search"]["done"] and st["cfd_search"]["failed_terms"] == ["financial"]
    assert st["cfd_search"]["terms_done"] == ["financial", "markets"]
