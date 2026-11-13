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
    assert len(tabs["FI (all countries)"]) == 2 and len(tabs["United Kingdom (FI)"]) == 2
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
    assert "Malta (iGaming)" in tabs and "iGaming (all countries)" in tabs and len(tabs["FI (all countries)"]) == 1


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


def test_source_time_limit():
    import time
    import pytest
    from regtracker.cli import SourceTimeout, time_limit
    with pytest.raises(SourceTimeout):
        with time_limit(0.2, "slow"):
            time.sleep(2)
    with time_limit(0, "no limit"):
        time.sleep(0.01)


def test_licences_tab_left_out_when_sheet_too_big(monkeypatch):
    import logging
    from regtracker import sheets
    monkeypatch.setenv("SHEET_CELL_LIMIT", "50")
    reqs = []

    class WS:
        def __init__(self, t, i): self.title, self.id = t, i

    class Sh:
        def worksheets(self): return [WS("Licences", 5)]
        def batch_update(self, body):
            reqs.append(body)
            return {"replies": [{"addSheet": {"properties": {"sheetId": 9}}} for _ in body["requests"]]}
        def values_batch_update(self, body): pass
        def fetch_sheet_metadata(self, params): return {"sheets": []}
    recs = [{"name": f"Firm {i}", "jurisdiction": "GB", "active": True, "source": "fca", "licence_type": "EMI"}
            for i in range(3)]
    sheets.publish(recs, [], {}, logging.getLogger("t"), sh=Sh())
    flat = [r for b in reqs for r in b["requests"]]
    assert {"deleteSheet": {"sheetId": 5}} in flat


def test_text_regex_sections_and_continuation_rows(monkeypatch):
    """Bahamas: a row keeps the heading it sits under (non-active licensees are left out).
    Moldova: history rows under a firm are folded in, so a withdrawn licence shows as inactive."""
    from regtracker.sources import national as n
    text = ("Authorized Dealers\nLIC0049 Bank of The Bahamas Ltd. Resident Bank & Trust Authorized Dealer N-7118 (242) 326-2560\n"
            "LIC0022 Butterfield Trust (Bahamas) Ltd. Resident Trust Authorized Agent N-3242 (242) 393-8622\n"
            "Non-Active Licensees\nLIC0227 Belgravia Bank & Trust Co. Ltd. Non-Resident Bank & Trust Not an Authorized Agent "
            "or Authorized Dealer N-65 (242) 323-8574\nRegistered Representatives\nLIC0049 Bank of The Bahamas Ltd. N-7118 (242) 326-2560\n")
    monkeypatch.setattr(n, "page_text_lines", lambda raw: raw.decode().splitlines())
    spec = {"format": "text_regex", "url": "u", "ignore_case": False, "licence_type": "Bank",
            "row_regex": r"^(?P<id>LIC\d{4})\s+(?P<name>.+?)\s+(?:Non-)?Resident\s+(?P<type>Bank & Trust|Bank)\s+(?:Not an )?"
                         r"Authorized\b.*?\s(?P<phone>\(\d{3}\)\s*\d{3}-\d{4})\s*$",
            "section_regex": r"^(Authorized Dealers|Non-Active Licensees)\s*$",
            "exclude_column": "section", "exclude_pattern": "Non-Active"}
    recs, _ = n.parse_file(text.encode(), spec, "BS", "CBOB")
    assert [(r.name, r.reference) for r in recs] == [("Bank of The Bahamas Ltd.", "LIC0049")]

    html = ("<table><tr><th>N/O</th><th>Name</th><th>Licence</th></tr>"
            "<tr><td>1.</td><td>Alpha Pay SRL</td><td>nr 1</td></tr>"
            "<tr><td>2.</td><td>Beta Pay SRL</td><td>nr 2</td></tr>"
            "<tr><td>nr 3 din 2015</td><td>Licența retrasă, conform Hotărârii</td></tr></table>")
    spec = {"format": "html_table", "url": "u", "name_column": "Name", "continuation_of": "N/O", "entry_pattern": r"^\d+\.$",
            "status_column": "notes", "inactive_pattern": "retras", "licence_type": "PI"}
    recs, _ = n.parse_file(html.encode(), spec, "MD", "BNM")
    assert [(r.name, r.active) for r in recs] == [("Alpha Pay SRL", True), ("Beta Pay SRL", False)]


def test_find_link_follows_a_chain_and_reads_json_lists(monkeypatch):
    from regtracker.sources import national as n
    pages = {
        "https://x.test/index": '<a href="/editions/list-as-at-02-october-2026">List as at 02-October-2026</a>'
                                '<a href="/editions/list-as-at-30-october-2026">List as at 30-October-2026</a>',
        "https://x.test/editions/list-as-at-30-october-2026": '<a href="https://cdn.x.test/doc-abc.pdf">Download</a>',
        "https://x.test/wp-json/media": '[{"source_url":"https:\\/\\/x.test\\/uploads\\/2026\\/07\\/DARE-Firms-as-at-30-June-2026-1.pdf",'
                                        '"description":{"rendered":"<a href=\'https:\\/\\/x.test\\/uploads\\/2026\\/08\\/DARE-Firms-as-at-31-July-2026-1.pdf\'>f<\\/a>"}}]',
    }

    class R:
        def __init__(self, t): self.text = t
        def raise_for_status(self): pass

    class S:
        def get(self, url, **kw): return R(pages[url])

    class Ctx:
        session = S()
    monkeypatch.setattr("regtracker.http.robots_allowed", lambda *a, **k: True)
    assert n.find_link(Ctx, {"page": "https://x.test/index", "link_chain": ["list-as-at", r"\.pdf"]}) == "https://cdn.x.test/doc-abc.pdf"
    assert n.find_link(Ctx, {"page": "https://x.test/wp-json/media", "link_pattern": r"DARE-Firms-as-at[^\"\s]*\.pdf"}) \
        == "https://x.test/uploads/2026/08/DARE-Firms-as-at-31-July-2026-1.pdf"


def test_post_json_register_and_name_value_properties(tmp_path, monkeypatch):
    """Jersey: the register page loads its list with a POST; the reader sends the same request and
    reads the {"Name": .., "Value": ..} properties as named columns."""
    import json
    from regtracker import cli
    from regtracker.sources import national as n
    body = {"Results": [{"Title": "MoonPay Jersey Limited", "Url": "/x/1",
                         "Properties": [{"Name": "LawSection", "Value": "Virtual Asset Service Provider (VASP)"}]},
                        {"Title": "CoinShares (Jersey) Limited", "Url": "/x/2",
                         "Properties": [{"Name": "LawSection", "Value": "Fund Services Business (FSB), Virtual Asset Service Provider (VASP)"}]}]}
    sent = []

    class R:
        content = json.dumps(body).encode()
        def raise_for_status(self): pass

    class S:
        def post(self, url, json=None, **kw):
            sent.append((url, json))
            return R()
        def get(self, url, **kw):
            raise AssertionError("GET not expected")

    monkeypatch.setattr("regtracker.http.robots_allowed", lambda *a, **k: True)
    monkeypatch.setattr(n, "robots_allowed", lambda *a, **k: True, raising=False)
    cfg = {"national": {"JE": {"regulator": "JFSC", "files": [{
        "label": "JFSC VASPs", "url": "https://www.jerseyfsc.org/umbraco/api/VaspEntitiesSearch/GetResults",
        "post_json": {"Keyword": "", "PageSize": 1000, "PageNumber": 1, "Refiners": []},
        "name_column": "Title", "status_column": "LawSection", "licence_type": "Virtual asset service provider (JFSC)"}]}}}
    ctx = cli.make_context(cfg, tmp_path)
    ctx.session = S()
    src = n.NationalSource("JE", cfg["national"]["JE"])
    recs = src.fetch(ctx)
    assert sent and sent[0][1]["PageSize"] == 1000
    assert sorted(r.name for r in recs) == ["CoinShares (Jersey) Limited", "MoonPay Jersey Limited"]
    assert all(r.licence_type == "Virtual asset service provider (JFSC)" for r in recs)


def test_taiwan_head_office_listing_ods():
    """Taiwan Banking Bureau Table 13: code + Chinese name row, then English name + website row,
    in two side-by-side panels."""
    import io
    import pytest
    odf = pytest.importorskip("odf")
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.table import Table, TableRow, TableCell
    from odf.text import P
    from regtracker.sources import national as n
    doc = OpenDocumentSpreadsheet()
    t = Table(name="T13")

    def row(cells):
        tr = TableRow()
        for c in cells:
            tc = TableCell()
            tc.addElement(P(text=c))
            tr.addElement(tc)
        t.addElement(tr)
    row(["", "表十三 本國金融機構總機構一覽表"])
    row(["", "Table 13. Listing of Head Offices"])
    row(["", "中華民國115年 6月底", "", "", "", "End of June 2026"])
    head = ["總行代號", "總行代號Code", "銀行別Institutions", "銀行別", "", "總行地址", "電話及網址"]
    row(head + ["", ""] + head)
    row(["004", "004", "臺灣銀行　#", "", "", "臺北市", "02-2349", "", "", "396", "396", "街口電子支付股份有限公司", "", "", "台北", "02-1"])
    row(["", "", "Bank Of Taiwan", "", "", "", "www.bot.com.tw", "", "", "", "", "Jkopay Co., Ltd", "", "", "", "www.jkopay.com"])
    doc.spreadsheet.addElement(t)
    buf = io.BytesIO()
    doc.save(buf)
    recs, _ = n.parse_file(buf.getvalue(), {"format": "tw_head_offices", "url": "u"}, "TW", "FSC")
    got = {r.name: (r.licence_type, r.website, r.reference) for r in recs}
    assert got == {"Bank Of Taiwan": ("Bank (FSC)", "https://www.bot.com.tw", "004"),
                   "Jkopay Co., Ltd": ("Electronic payment institution (FSC)", "https://www.jkopay.com", "396")}
    assert "June 2026" in recs[0].status_detail


def test_apex_interactive_report_paging(tmp_path, monkeypatch):
    """FSC Mauritius register: an Oracle APEX interactive report, 50 rows a page, next pages fetched
    the way the page's own "next" button does (POST wwv_flow.ajax with the page's hidden fields)."""
    from urllib.parse import parse_qs
    from regtracker import cli
    from regtracker.sources import national as n
    page = ('<input type="hidden" name="p_flow_id" value="110" id="pFlowId"><input type="hidden" name="p_flow_step_id" '
            'value="3" id="pFlowStepId"><input type="hidden" name="p_instance" value="12345" id="pInstance">'
            '<input type="hidden" id="pPageItemsProtected" value="PROT"><input type="hidden" value="SALT" id="pSalt">'
            '<input type="hidden" id="OPR_worksheet_id" value="111"><input type="hidden" id="OPR_report_id" value="222">'
            '<select name="P3_ACTS"></select><input name="P3_NAME">'
            '<script>apex.jQuery("#OPR").interactiveReport({"regionId":"OPR","searchField":false,"ajaxIdentifier":"AJAXID"});</script>')
    head = "<tr><th>Link</th><th>Name of Licensee</th><th>Date of Licence</th><th>Type of Licence</th><th>Annotations</th></tr>"

    def rows(a, b):
        return "".join(f"<tr><td></td><td>Dealer {i} Ltd</td><td>01/01/2025</td>"
                       f"<td>SEC-2.1B Investment Dealer (Full Service Dealer excluding Underwriting)</td><td></td></tr>"
                       for i in range(a, b))
    posts = []

    class R:
        def __init__(self, text, url="https://opr.test/ords/opr/r/fsc-opr/x?session=1"):
            self.text, self.url, self.content = text, url, text.encode()
        def raise_for_status(self): pass

    class S:
        def get(self, url, **kw):
            return R(page)
        def post(self, url, data=None, **kw):
            posts.append((url, data))
            start = int(data["p_widget_action_mod"].split("=")[1].split("max")[0])
            body = rows(start, min(start + 50, 121)) if start <= 120 else ""
            return R(f'<table class="a-IRR-table">{head}{body}</table>')

    monkeypatch.setattr(n, "robots_allowed", lambda *a, **k: True, raising=False)
    monkeypatch.setattr("regtracker.http.robots_allowed", lambda *a, **k: True)
    monkeypatch.setattr("time.sleep", lambda s: None)
    cfg = {"national": {"MU": {"regulator": "FSC", "files": [{
        "label": "FSC register", "url": "https://opr.test/ords/opr/r/fsc-opr/x", "apex_irr": "OPR",
        "format": "html_table", "name_column": "Name of Licensee", "type_column": "Type of Licence",
        "type_patterns": {r"SEC-2\.1": "Investment dealer"}}]}}}
    ctx = cli.make_context(cfg, tmp_path)
    ctx.session = S()
    recs = n.NationalSource("MU", cfg["national"]["MU"]).fetch(ctx)
    assert len(recs) == 120 and len(posts) == 3
    f = posts[1][1]
    assert posts[0][0] == "https://opr.test/ords/wwv_flow.ajax" and f["p_request"] == "PLUGIN=AJAXID"
    assert f["p_widget_action_mod"] == "pgR_min_row=51max_rows=50rows_fetched=50" and f["x01"] == "111" and f["x02"] == "222"
    assert '"n":"P3_ACTS"' in f["p_json"] and '"salt":"SALT"' in f["p_json"] and f["p_instance"] == "12345"


def test_config_has_no_duplicate_keys():
    """A repeated key in config.yaml silently overrides the first one (that hid a fix for a day)."""
    import yaml
    from pathlib import Path
    dups = []

    class Loader(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        seen = set()
        for k, _ in node.value:
            key = loader.construct_object(k, deep=deep)
            if key in seen:
                dups.append((key, k.start_mark.line + 1))
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)
    Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    cfg = yaml.load((Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"), Loader=Loader)
    assert dups == []
    assert "MU" in cfg["cfd"] and any(f.get("apex_irr") for f in cfg["cfd"]["MU"]["files"])
