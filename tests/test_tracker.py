"""Offline tests: parsers on small fixtures, and the change-detection logic."""
import gzip
import io
import json
import logging
import zipfile
from pathlib import Path

import pytest

from regtracker import cli
from regtracker.diff import SuspiciousDrop, check_drop, diff
from regtracker.sources import fca, pra, eba, ecb, finma
from regtracker.store import load_snapshot

TODAY = "2026-11-03"


# ---------------------------------------------------------------- diff ---

def rec(key, active=True, ltype="Small EMI", name="Acme Ltd", services=""):
    return {"key": key, "source": "x", "jurisdiction": "GB", "regulator": "FCA", "name": name,
            "licence_type": ltype, "active": active, "status_detail": "s", "services": services}


def kinds(events):
    return sorted(e["event"] for e in events)


def test_new_lost_type_name_removed():
    prev = {"a": rec("a"), "b": rec("b"), "c": rec("c"), "d": rec("d"), "e": rec("e", active=False)}
    curr = {"a": rec("a"),                               # unchanged
            "b": rec("b", active=False),                 # lost
            "c": rec("c", ltype="Authorised EMI"),       # upgraded
            "e": rec("e", active=True),                  # reinstated
            "f": rec("f"),                               # new
            "g": rec("g", active=False)}                 # first seen inactive: no event
    prev["a"]["name"] = "Acme Limited"
    curr["a"]["name"] = "ACME LIMITED"                   # case only: not a name change
    curr["d2"] = rec("d2", name="x")
    events, merged = diff(prev, curr, TODAY)
    assert kinds(events) == sorted(["LICENCE_LOST", "TYPE_CHANGED", "REINSTATED",
                                    "NEW_LICENCE", "NEW_LICENCE", "REMOVED"])
    assert merged["d"]["active"] is False and "Removed" in merged["d"]["status_detail"]
    # removed firm stays inactive and does not alert again tomorrow
    events2, _ = diff(merged, {k: v for k, v in merged.items() if k != "d"}, "2026-11-04")
    assert events2 == []


def test_removal_not_event_for_lookup_sources():
    events, _ = diff({"a": rec("a")}, {}, TODAY, removal_is_event=False)
    assert events == []


def test_services_change():
    events, _ = diff({"a": rec("a", services="Acquiring")},
                     {"a": rec("a", services="Acquiring; Money remittance")}, TODAY)
    assert kinds(events) == ["SERVICES_CHANGED"]


def test_drop_guard():
    prev = {str(i): rec(str(i)) for i in range(100)}
    with pytest.raises(SuspiciousDrop):
        check_drop(prev, {str(i): rec(str(i)) for i in range(50)})
    check_drop(prev, {str(i): rec(str(i)) for i in range(95)})


# ----------------------------------------------------------------- FCA ---

@pytest.mark.parametrize("psd,status,expected", [
    ("Authorised Electronic Money Institution", "Authorised", ("Authorised EMI", True)),
    ("Small Payment Institution", "Registered", ("Small Payment Institution", True)),
    ("Registered Account Information Service Provider", "Registered", ("RAISP", True)),
    ("Cancelled - Small EMI", "Cancelled", ("Small EMI", False)),
    ("EMD Revoked – Authorised EMI", "", ("Authorised EMI", False)),
    ("Authorised Payment Institution", "No longer authorised", ("Authorised Payment Institution", False)),
    ("PSD2 Excluded", "Registered", None),
    ("", "Authorised", None),
])
def test_fca_classify(psd, status, expected):
    assert fca.classify(psd, status) == expected


def test_fca_record():
    firm = {"FRN": "900562", "Organisation Name": "Revolut Ltd", "Status": "Authorised",
            "PSD / EMD Status": "Authorised Electronic Money Institution",
            "PSD / EMD Effective Date": "2018-07-05T00:00:00"}
    r = fca.record_from_firm(900562, firm, {"Town": "London", "Website Address": "www.revolut.com"})
    assert r.key == "fca:900562" and r.licence_type == "Authorised EMI" and r.active
    assert r.status_date == "2018-07-05" and r.city == "London"


def test_fca_seed_and_daily(tmp_path, monkeypatch):
    """Seed over a fake FRN space, then a daily run that finds a new firm and a cancellation."""
    world = {
        900001: {"Organisation Name": "Pay One", "Status": "Authorised",
                 "PSD / EMD Status": "Authorised Payment Institution"},
        900003: {"Organisation Name": "Some Broker", "Status": "Authorised", "PSD / EMD Status": ""},
        900005: {"Organisation Name": "Emoney Co", "Status": "Registered",
                 "PSD / EMD Status": "Small Electronic Money Institution"},
    }

    class FakeClient:
        def __init__(self, ctx, rps):
            self.errors = 0

        def firm(self, frn):
            return world.get(frn)

        def address(self, frn):
            return {"Town": "London"}

        def verify(self):
            pass

    monkeypatch.setattr(fca, "FCAClient", FakeClient)
    cfg = {"sources": {"fca": {"seed_ranges": [[900000, 900010]], "frontier_back": 10,
                               "frontier_ahead": 5, "frontier_back_weekly": 10}}}
    ctx = cli.make_context(cfg, tmp_path)
    ctx.today = "2026-11-03"  # a Tuesday
    st = fca.seed(ctx, hours=0.01)
    assert st["seed"]["done"] and set(st["tracked"]) == {"900001", "900005"}
    src = fca.FCASource()
    first = {r.key: r.to_dict() for r in src.fetch(ctx)}
    assert len(first) == 2

    world[900005]["PSD / EMD Status"] = "Cancelled - Small EMI"
    world[900012] = {"Organisation Name": "New EMI", "Status": "Authorised",
                     "PSD / EMD Status": "Authorised Electronic Money Institution"}
    second = {r.key: r.to_dict() for r in src.fetch(ctx)}
    events, _ = diff(first, second, TODAY, removal_is_event=False)
    assert kinds(events) == ["LICENCE_LOST", "NEW_LICENCE"]


def test_fca_bad_key_in_body_stops(tmp_path, monkeypatch):
    """FSR-API-01-01-21 arrives with HTTP 200: must stop, not look like 'firm not found'."""
    from regtracker.sources.base import SourceNotReady

    class Resp:
        status_code = 200

        def json(self):
            return {"Status": "FSR-API-01-01-21", "Message": "API and Email key not found", "Data": None}

    class Sess:
        def get(self, *a, **k):
            return Resp()

    monkeypatch.setenv("FCA_API_EMAIL", "x@y.z")
    monkeypatch.setenv("FCA_API_KEY", "wrong")
    ctx = cli.make_context({}, tmp_path)
    ctx.session = Sess()
    client = fca.FCAClient(ctx, 1000)
    with pytest.raises(SourceNotReady, match="FSR-API-01-01-21"):
        client.verify()


def test_fca_not_found_code_is_none(tmp_path, monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return {"Status": "FSR-API-02-01-11", "Message": "Firm not found", "Data": None}

    class Sess:
        def get(self, *a, **k):
            return Resp()

    monkeypatch.setenv("FCA_API_EMAIL", "x@y.z")
    monkeypatch.setenv("FCA_API_KEY", "k")
    ctx = cli.make_context({}, tmp_path)
    ctx.session = Sess()
    assert fca.FCAClient(ctx, 1000).firm(123) is None


# ----------------------------------------------------------------- PRA ---

PRA_CSV = """List of PRA-regulated banks,,
Banks incorporated in the UK authorised to accept deposits,,
Firm Name,FRN,LEI
ABC International Bank Plc,149025,PQEBSF10YW5QXWIKTJ21
Monzo Bank Limited,730427,213800VBL5H7ZWLVH845
Banks incorporated outside the UK authorised to accept deposits through a UK branch,,
Firm Name,FRN,LEI
Some Overseas Bank,204567,
"""


def test_pra_parse():
    recs = pra.parse_list(PRA_CSV, "banks")
    by = {r.reference: r for r in recs}
    assert len(recs) == 3
    assert by["730427"].licence_type == "Bank (UK-incorporated)"
    assert by["730427"].lei == "213800VBL5H7ZWLVH845"
    assert by["204567"].licence_type == "Bank (UK branch of overseas bank)"


def test_pra_links():
    html = ('<a href="/-/media/boe/files/prudential-regulation/authorisations/banks-list-october-2026.csv?la=en&amp;hash=AB">Banks</a>'
            '<a href="/-/media/boe/files/x/building-societies-list-october-2026.csv">BS</a>')
    links = pra.find_links(html, pra.PAGE)
    assert links["banks"].startswith("https://www.bankofengland.co.uk/-/media/") and "&hash=AB" in links["banks"]
    assert "building-societies" in links["building_societies"]


# ----------------------------------------------------------------- EBA ---

EBA_DOC = [[{"disclaimer": "x"}], [
    {"CA_OwnerID": "IE_CBI", "EntityCode": "IE_CBI!C1", "EntityType": "PSD_EMI",
     "Properties": [{"ENT_NAM": "Stripe Ltd"}, {"ENT_AUT": ["2019-01-01"]}, {"ENT_TOW_CIT_RES": "Dublin"}],
     "Services": [{"IE": ["ES_010", "PS_05B"]}, {"DE": ["PS_05B"]}, {"FR": "PS_05B"}]},
    {"CA_OwnerID": "LT_BL", "EntityCode": "LT_BL!9", "EntityType": "PSD_PI",
     "Properties": [{"ENT_NAM": ["UAB Pay", "UAB Pay EN"]}, {"ENT_AUT": "2020-02-02"}],
     "Services": [{"LT": ["PS_060"]}]},
    {"CA_OwnerID": "LT_BL", "EntityCode": "LT_BL!9", "EntityType": "PSD_EMI",
     "Properties": [{"ENT_NAM": "UAB Pay"}], "Services": []},
    {"CA_OwnerID": "DE_BAFIN", "EntityCode": "DE_BAFIN!1", "EntityType": "PSD_EXC",
     "Properties": [{"ENT_NAM": "Excluded GmbH"}]},
    {"CA_OwnerID": "PL_PFSA", "EntityCode": "PL!7", "EntityType": "PSD_AG",
     "Properties": [{"ENT_NAM": "Agent"}]},
    {"CA_OwnerID": "NL_DNB", "EntityCode": "NL!2", "EntityType": "PSD_PI",
     "Properties": [{"ENT_NAM": "Gone BV"}, {"ENT_WDR": "2026-10-01"}]},
]]


def test_eba_parse():
    fh = io.BytesIO(json.dumps(EBA_DOC).encode())
    recs = eba.build_records(eba.iter_entities(fh), set(eba.DEFAULT_TYPES), {"GB"}, TODAY)
    by = {r.key: r for r in recs}
    assert set(by) == {"eba:IE_CBI!C1", "eba:LT_BL!9", "eba:NL!2"}
    s = by["eba:IE_CBI!C1"]
    assert s.jurisdiction == "IE" and s.city == "Dublin" and s.passports == "2 countries"
    assert "E-money issuance" in s.services and "Acquiring" in s.services
    assert by["eba:LT_BL!9"].licence_type == "E-money institution + Payment institution"
    assert by["eba:NL!2"].active is False


def test_eba_fetch_with_zip(tmp_path):
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as z:
        z.writestr("register.json", json.dumps(EBA_DOC))
    data = blob.getvalue()
    import hashlib

    class Resp:
        def __init__(self, content=b"", js=None):
            self.content, self._js, self.status_code = content, js, 200

        def json(self):
            return self._js

        def raise_for_status(self):
            pass

    class Sess:
        def get(self, url, **kw):
            if url == eba.META_URL:
                return Resp(js={"golden_copy_path_context": "https://x/dl/",
                                "latest_version_relative_zip_path": "f.zip",
                                "sha256_hash": hashlib.sha256(data).hexdigest()})
            assert url == "https://x/dl/f.zip"
            return Resp(content=data)

    ctx = cli.make_context({}, tmp_path)
    ctx.session = Sess()
    assert len(eba.EBASource().fetch(ctx)) == 3


# ----------------------------------------------------------------- ECB ---

ECB_TSV = ("RIAD_CODE\tLEI\tCOUNTRY_OF_REGISTRATION\tNAME\tBOX\tADDRESS\tPOSTAL\tCITY\tCATEGORY\tHEAD_COUNTRY_OF_REGISTRATION\tHEAD_NAME\n"
           "DE00001\t529900ABC\tDE\tDeutsche Bank AG\t\tTaunusanlage 12\t60325\tFrankfurt\tCredit Institution\t\t\n"
           "IE00002\t\tIE\tSome Bank Ireland Branch\t\t\t\tDublin\tCredit Institution\tFR\tSome Bank SA\n"
           "FR00003\t\tFR\tMoney Fund\t\t\t\tParis\tMoney Market Fund\t\t\n"
           "RU00004\t\tRU\tShould Not Appear\t\t\t\tMoscow\tCredit Institution\t\t\n")


def test_ecb_parse():
    recs = ecb.parse_mfi(ECB_TSV, {"RU"})
    by = {r.reference: r for r in recs}
    assert set(by) == {"DE00001", "IE00002"}
    assert by["IE00002"].licence_type == "Credit institution (branch)"
    assert by["DE00001"].city == "Frankfurt"


def test_ecb_layout_change_is_loud():
    with pytest.raises(RuntimeError, match="headers"):
        ecb.parse_mfi("A\tB\n1\t2\n", set())


# --------------------------------------------------------------- FINMA ---

FINMA_CSV = ('﻿"Name";"City";"AuthorisationTypeDE";"AuthorisationTypeFR";"AuthorisationTypeIT";"AuthorisationTypeEN";"UID"\n'
             '"UBS AG";"Zürich";"Bank";"Banque";"Banca";"Bank";"CHE-101.329.561"\n'
             '"ZKB";"Zürich";"Bank";"Banque";"Banca";"Bank";"CHE-108.954.607"\n'
             '"ZKB";"Zürich";"Depotbank";"x";"x";"Custodian bank";"CHE-108.954.607"\n'
             '"Some PM";"Zug";"V";"x";"x";"Portfolio manager";"CHE-1"\n'
             '"Foreign Rep";"Genf";"V";"x";"x";"Foreign bank representative office";""\n')


def test_finma_parse():
    recs = finma.parse_finma(FINMA_CSV.lstrip("﻿"), finma.DEFAULT_INCLUDE, finma.DEFAULT_EXCLUDE)
    by = {r.name: r for r in recs}
    assert set(by) == {"UBS AG", "ZKB"}
    assert by["ZKB"].licence_type == "Bank + Custodian bank"


# ------------------------------------------------------- end-to-end run ---

def test_cli_run_baseline_then_changes(tmp_path, monkeypatch):
    from regtracker.model import Record
    from regtracker.sources import ALL_SOURCES
    from regtracker.sources.base import Source

    state = {"recs": [Record("t:1", "test", "IE", "IE_CBI", "A", "E-money institution", True),
                      Record("t:2", "test", "MT", "MT_MFSA", "B", "Payment institution", True)]}

    class T(Source):
        name, label = "test", "Test"

        def fetch(self, ctx):
            return state["recs"]

    monkeypatch.setitem(ALL_SOURCES, "test", T())
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text("excluded_countries: [RU]\nsources:\n  test: {enabled: true}\n")
    args = ["--config", str(cfgp), "--data", str(tmp_path / "data"), "run", "--no-sheets", "--no-email"]
    assert cli.main(args) == 0
    assert not (tmp_path / "data" / "changes.csv").exists()          # baseline: no alerts
    state["recs"] = [Record("t:1", "test", "IE", "IE_CBI", "A", "Payment institution", True),
                     Record("t:3", "test", "RU", "RU_CBR", "C", "Bank", True)]   # sanctioned: dropped
    assert cli.main(args) == 0
    changes = (tmp_path / "data" / "changes.csv").read_text()
    assert "Licence type changed" in changes and "Removed from register" in changes
    assert "RU_CBR" not in changes
    lic = (tmp_path / "data" / "licences.csv").read_text()
    assert lic.count("\n") == 3  # header + 2 firms (B kept as Inactive)


# ------------------------------------------------------- GitHub alerts ---

def test_github_issue_alert(monkeypatch):
    from regtracker import notify
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "shaz/licence-tracker")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "shaz")
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    assert notify.enabled() and not notify.smtp_enabled()
    sent = {}

    class R:
        status_code = 201
        def raise_for_status(self): pass
        def json(self): return {"html_url": "https://github.com/x/1"}

    def fake_post(url, json=None, **kw):
        sent["url"], sent["json"], sent["auth"] = url, json, kw["headers"]["Authorization"]
        return R()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    ev = [{"event_label": "New licence", "jurisdiction": "GB", "regulator": "FCA", "name": "Pay | Co",
           "licence_type": "Small EMI", "old": "", "new": "Small EMI", "reference": "900001",
           "register_url": "https://register.fca.org.uk/s/search?q=900001"}] * 400
    notify.send(ev, ["EBA: timeout"], logging.getLogger("t"), TODAY)
    assert sent["url"] == "https://api.github.com/repos/shaz/licence-tracker/issues"
    assert sent["auth"] == "Bearer t"
    assert sent["json"]["title"].startswith("Licence changes 2026-11-03: 400 change(s)")
    body = sent["json"]["body"]
    assert "cc @shaz" in body and "EBA: timeout" in body and "Pay \\| Co" in body
    assert "100 more" in body and len(body) < 65536
    monkeypatch.setenv("ALERTS_VIA_GITHUB", "false")
    assert not notify.enabled()


def test_ecb_mixed_line_breaks():
    recs = ecb.parse_mfi(ECB_TSV.replace("\n", "\r", 2).replace("\n", "\r\n"), {"RU"})
    assert {r.reference for r in recs} == {"DE00001", "IE00002"}


def test_ecb_stray_quotes_and_title_free():
    t = ECB_TSV.replace("Deutsche Bank AG", 'Deutsche "Bank AG').replace("RIAD_CODE", '"RIAD_CODE"')
    recs = ecb.parse_mfi(t, {"RU"})
    assert {r.reference for r in recs} == {"DE00001", "IE00002"}


def test_ecb_utf16_file():
    raw = ECB_TSV.replace("\n", "\r\n").encode("utf-16")   # what the ECB actually serves
    recs = ecb.parse_mfi(ecb.decode_bytes(raw), {"RU"})
    assert {r.reference for r in recs} == {"DE00001", "IE00002"}
    assert ecb.decode_bytes("abc".encode("utf-8")) == "abc"


def test_fca_secrets_trailing_newline(tmp_path, monkeypatch):
    monkeypatch.setenv("FCA_API_EMAIL", "me@example.com\n")
    monkeypatch.setenv("FCA_API_KEY", " abc123\r\n")
    c = fca.FCAClient(cli.make_context({}, tmp_path), 5)
    assert c.headers["X-Auth-Email"] == "me@example.com" and c.headers["X-Auth-Key"] == "abc123"


def test_fca_seed_parallel_respects_rate_and_finds_all(tmp_path, monkeypatch):
    """With 0.2 s latency per lookup, parallel lookups should run at the rate
    limit (not 1/latency), find every firm, and keep FRN order in state."""
    import time as _t
    world = {f: {"Organisation Name": f"Firm {f}", "Status": "Authorised",
                 "PSD / EMD Status": "Small Payment Institution"} for f in range(900000, 900060, 3)}
    world[122702] = {"Organisation Name": "Barclays", "Status": "Authorised"}
    calls = []

    class SlowClient(fca.FCAClient):
        def __init__(self, ctx, rps):
            from regtracker.http import RateLimiter
            self.rl, self.errors, self.log = RateLimiter(rps), 0, ctx.log

        def firm(self, frn):
            self.rl.wait(); calls.append(_t.monotonic()); _t.sleep(0.2)
            return world.get(frn)

        def address(self, frn):
            return {}

    monkeypatch.setattr(fca, "FCAClient", SlowClient)
    cfg = {"sources": {"fca": {"seed_ranges": [[900000, 900099]], "requests_per_second": 40,
                               "parallel_requests": 8}}}
    ctx = cli.make_context(cfg, tmp_path)
    t0 = _t.monotonic()
    st = fca.seed(ctx, hours=1)
    took = _t.monotonic() - t0
    assert st["seed"]["done"] and len(st["tracked"]) == 20
    assert took < 6, took                      # sequential would take ~20 s
    # never more than 40 calls in any 1-second window (+1 tolerance)
    calls.sort()
    assert max(sum(1 for c in calls if a <= c < a + 1) for a in calls) <= 41


def test_fca_preview_shown_before_seed_completes(tmp_path):
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    st = {"seed": {"ranges": [[900000, 999999]], "range_index": 0, "cursor": 910000, "done": False},
          "tracked": {"900001": {**fca.record_from_firm(900001, {
              "Organisation Name": "Pay One", "Status": "Authorised",
              "PSD / EMD Status": "Small Electronic Money Institution"}).to_dict(), "_enriched": True}}}
    (data / "state" / "fca.json").write_text(json.dumps(st))
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text("sources:\n  pra: {enabled: false}\n")
    # no sources enabled except fca (which isn't ready): master list = preview only
    cfgp.write_text("sources:\n  fca: {enabled: true}\n")
    rc = cli.main(["--config", str(cfgp), "--data", str(data), "run", "--no-sheets", "--no-email"])
    lic = (data / "licences.csv").read_text()
    assert "Pay One" in lic and "Small EMI" in lic
    assert not (data / "changes.csv").exists()
    assert not (data / "snapshots" / "fca.json.gz").exists()


# -------------------------------------------------------- Google Sheet ---

class FakeWS:
    def __init__(self, title, sid):
        self.title, self.id = title, sid


class FakeSheet:
    """Records the requests publish() sends, like the Sheets API would get them."""
    def __init__(self, titles):
        self.tabs = {t: i for i, t in enumerate(titles)}
        self.batches, self.writes = [], []

    def worksheets(self):
        return [FakeWS(t, i) for t, i in self.tabs.items()]

    def batch_update(self, body):
        self.batches.append(body)
        replies = []
        for r in body["requests"]:
            if "addSheet" in r:
                sid = 100 + len(self.tabs)
                self.tabs[r["addSheet"]["properties"]["title"]] = sid
                replies.append({"addSheet": {"properties": {"sheetId": sid}}})
            else:
                replies.append({})
        return {"replies": replies}

    def values_batch_update(self, body):
        self.writes.append(body)


def test_sheet_country_tabs():
    from regtracker import sheets
    recs = [rec("a"), {**rec("b"), "jurisdiction": "IE", "name": "Irish EMI"},
            {**rec("c"), "jurisdiction": "IE"}, {**rec("d"), "jurisdiction": "E$"},
            {**rec("e"), "jurisdiction": "CH", "active": False}]
    sh = FakeSheet(["Sheet1", "Licences"])
    sheets.publish(recs, [], {"finished": "x"}, logging.getLogger("t"), sh=sh)
    assert len(sh.batches) == 2 and len(sh.writes) == 1        # a handful of requests in total
    added = [r["addSheet"]["properties"]["title"] for r in sh.batches[0]["requests"]]
    assert added == ["Summary", "Changes", "Ireland (IE)", "Switzerland (CH)", "United Kingdom (GB)", "Other"]
    reqs = sh.batches[1]["requests"]
    order = {sh_id: r["updateSheetProperties"]["properties"]["index"]
             for r in reqs if "updateSheetProperties" in r
             for sh_id in [r["updateSheetProperties"]["properties"]["sheetId"]]}
    assert order[sh.tabs["Summary"]] == 0 and order[sh.tabs["Other"]] == len(order) - 1
    assert {"deleteSheet": {"sheetId": 0}} in reqs                # empty default tab removed
    data = {d["range"]: d["values"] for d in sh.writes[0]["data"]}
    ie = data["'Ireland (IE)'!A1"]
    assert ie[0][0] == "Jurisdiction" and len(ie) == 3 and any("Irish EMI" in row for row in ie)
    assert len(data["'Licences'!A1"]) == 6
    assert len(data["'Changes'!A1"]) == 2                        # header + blanked row
    # second run: no tabs to add, still only a couple of requests
    sh.batches, sh.writes = [], []
    sheets.publish(recs, [], {"finished": "y"}, logging.getLogger("t"), sh=sh)
    assert not any("addSheet" in r for r in sh.batches[0]["requests"])


def test_sheet_big_list_is_chunked():
    from regtracker import sheets
    recs = [{**rec(str(i)), "jurisdiction": "DE" if i % 2 else "FR"} for i in range(9000)]
    sh = FakeSheet(["Licences"])
    sheets.publish(recs, [], {}, logging.getLogger("t"), sh=sh)
    rows = sum(len(d["values"]) for w in sh.writes for d in w["data"])
    assert rows >= 18000 and 4 <= len(sh.writes) <= 8
    assert all(sum(len(d["values"]) for d in w["data"]) <= 8000 for w in sh.writes)


# ------------------------------------------------ national registers ---

from regtracker.sources import national
from regtracker import compare

DNB_CSV = ("﻿Register;Statutory name;Trade name;Relation number;Place;Start date;Status\n"
           "Register of payment service providers;Mollie B.V.;Mollie;R123;Amsterdam;2013-06-01;\n"
           "Register of electronic money institutions;Bunq Payments B.V.;;R124;Amsterdam;2020-01-01;\n"
           "Register of banks;ING Bank N.V.;;R1;Amsterdam;1991-01-01;\n"
           "Register of payment service providers;Gone B.V.;;R125;Utrecht;2019-01-01;Withdrawn\n")
LB_CSV = ('"Title";"Type";"Business form";"Disputes with consumers";"Sanctions"\n'
          '"UAB „Paysera LT\'\'";"Electronic money institution";"EMI licensed in Lithuania";"0";"1"\n'
          '"UAB Pervesk";"Electronic money institution";"EMI licensed in Lithuania";"2";"0"\n')
NL_SPEC = {"url": "https://dnb/x.csv", "type_patterns": {
    "electronic money": "E-money institution", "payment service provider": "Payment institution"}}


def test_national_dnb_like_csv():
    recs, info = national.parse_file(DNB_CSV.encode("utf-8"), NL_SPEC, "NL", "DNB")
    by = {r.name: r for r in recs}
    assert set(by) == {"Mollie B.V.", "Bunq Payments B.V.", "Gone B.V."}     # bank skipped
    assert by["Mollie B.V."].licence_type == "Payment institution" and by["Mollie B.V."].reference == "R123"
    assert by["Bunq Payments B.V."].licence_type == "E-money institution"
    assert by["Gone B.V."].active is False and by["Mollie B.V."].city == "Amsterdam"
    assert info["columns"]["type"] == "Register"


def test_national_lb_like_csv_fixed_type_and_utf16_and_xlsx():
    spec = {"url": "https://lb/x", "licence_type": "E-money institution"}
    for raw in (LB_CSV.encode("utf-8"), LB_CSV.encode("utf-16")):
        recs, _ = national.parse_file(raw, spec, "LT", "LB")
        assert {r.licence_type for r in recs} == {"E-money institution"} and len(recs) == 2
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["List of payment institutions"]); ws.append(["Title", "Type"]); ws.append(["UAB X", "Payment institution"])
    b = io.BytesIO(); wb.save(b)
    recs, _ = national.parse_file(b.getvalue(), {"url": "u", "licence_type": "Payment institution"}, "LT", "LB")
    assert [r.name for r in recs] == ["UAB X"]


def test_national_no_name_column_is_loud():
    with pytest.raises(RuntimeError, match="no name column"):
        national.parse_file(b"foo;bar\n1;2\n", {"url": "u", "licence_type": "x"}, "LT", "LB")


def test_match_name():
    assert national.match_name("UAB „Paysera LT“") == national.match_name("Paysera LT, UAB")
    assert national.match_name("Mollie B.V.") == national.match_name("MOLLIE BV") == "mollie"


def test_cli_national_replaces_eba_and_measures_lag(tmp_path, monkeypatch):
    from regtracker.model import Record
    from regtracker.sources import ALL_SOURCES
    from regtracker.sources.base import Source
    world = {"eba": [Record("eba:1", "eba", "NL", "NL_DNB", "Mollie B.V.", "Payment institution", True),
                     Record("eba:2", "eba", "IE", "IE_CBI", "Stripe", "E-money institution", True)],
             "nl": [("Mollie B.V.", "payment service provider")]}

    class E(Source):
        name, label = "eba", "EBA"
        def fetch(self, ctx): return list(world["eba"])

    def nl_fetch(self, ctx):
        csv_text = "Register;Statutory name\n" + "".join(f"{t};{n}\n" for n, t in world["nl"])
        return national.parse_file(csv_text.encode(), NL_SPEC, "NL", "DNB")[0]

    monkeypatch.setitem(ALL_SOURCES, "eba", E())
    monkeypatch.setattr(national.NationalSource, "fetch", nl_fetch)
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text("sources:\n  eba: {enabled: true}\nnational:\n  NL:\n    regulator: DNB\n    files: [{url: u}]\n")
    data = tmp_path / "data"
    args = ["--config", str(cfgp), "--data", str(data), "run", "--no-sheets", "--no-email"]
    times = iter(["2026-11-06 08:17", "2026-11-06 10:17", "2026-11-06 14:17", "2026-11-06 16:17"])
    real = cli.make_context
    def ctx_at(cfg, d):
        c = real(cfg, d); c.now = next(times); return c
    monkeypatch.setattr(cli, "make_context", ctx_at)

    assert cli.main(args) == 0                                   # baselines
    lic = (data / "licences.csv").read_text()
    assert lic.count("Mollie") == 1 and ",DNB," in lic           # NL from DNB only, not duplicated
    assert "Stripe" in lic

    world["nl"].append(("Newpay B.V.", "electronic money institution"))
    assert cli.main(args) == 0                                   # national sees it at 10:17
    world["eba"].append(Record("eba:3", "eba", "NL", "NL_DNB", "NewPay BV", "E-money institution", True))
    world["eba"].append(Record("eba:4", "eba", "IE", "IE_CBI", "Irish New", "E-money institution", True))
    assert cli.main(args) == 0                                   # EU catches up at 14:17
    changes = (data / "changes.csv").read_text()
    assert changes.count("Newpay") == 1 and "NewPay BV" not in changes   # NL alert from DNB only
    assert "Irish New" in changes                                # IE still from EBA
    assert "2026-11-06 10:17" in changes
    check = (data / "source_check.csv").read_text()
    assert "Newpay B.V." in check and ",4.0" in check            # 10:17 -> 14:17 = 4 hours lag


SK_JSON = json.dumps({"iat": 1, "data": [
    {"EntityType": "PSD_PI", "EntityCode": "SK_NBS!1",
     "Properties": [{"ENT_NAM": "Pay SK s.r.o."}, {"ENT_NAT_REF_COD": "12345"}, {"ENT_TOW_CIT_RES": "Bratislava"}]},
    {"EntityType": "PSD_EMI", "EntityCode": "SK_NBS!2", "Properties": [{"ENT_NAM": ["Emoney a.s."]}]},
    {"EntityType": "PSD_AG", "EntityCode": "SK_NBS!3", "Properties": [{"ENT_NAM": "An Agent"}]},
]})
SK_SPEC = {"url": "u", "exclude_pattern": "PSD_AG|agent",
           "type_patterns": {"PSD_EMI|elektronick": "E-money institution", "PSD_PI|platobn": "Payment institution"}}


def test_national_json_eba_style():
    recs, info = national.parse_file(SK_JSON.encode(), SK_SPEC, "SK", "NBS")
    by = {r.name: r for r in recs}
    assert set(by) == {"Pay SK s.r.o.", "Emoney a.s."}
    assert by["Pay SK s.r.o."].licence_type == "Payment institution" and by["Pay SK s.r.o."].city == "Bratislava"
    assert by["Pay SK s.r.o."].reference == "12345" and by["Emoney a.s."].licence_type == "E-money institution"


def test_national_json_with_code_list():
    doc = json.dumps({"items": [{"nazov": "Firma A", "typ": "11", "ico": "1"},
                                {"nazov": "Firma B", "typ": "12", "ico": "2"},
                                {"nazov": "Agent C", "typ": "99", "ico": "3"}]})
    spec = {**SK_SPEC, "_codes": {"11": "Platobná inštitúcia", "12": "Inštitúcia elektronických peňazí",
                                  "99": "Agent platobnej inštitúcie"}}
    recs, _ = national.parse_file(doc.encode(), spec, "SK", "NBS")
    assert {(r.name, r.licence_type) for r in recs} == {("Firma A", "Payment institution"),
                                                       ("Firma B", "E-money institution")}


def _sk_doc(style):
    vf, vt = ("valid_from", "valid_to") if style == "snake" else ("validFrom", "validTo")
    def lic(code, frm, to=None, issuer="NBS"):
        return {"issuer": issuer, "type": code, vf: frm, vt: to, "scopes": ["AISP", "PISP"]}
    return json.dumps({"iat": 1, "institutions": [
        {"id": "31340890", "name": "365.bank, a. s.", "address": "Dvořákovo nábrežie 4, 81102 Bratislava",
         "country": "SK", "licenses": [lic("B", "1993-01-01", "2026-10-31")]},
        {"id": "1", "national_id": "50000001", "name": "Pay SK s.r.o.", "address": "Ulica 1, 82105 Bratislava",
         "country": "SK", "licenses": [lic("PI", "2019-05-01")]},
        {"id": "2", "name": "Old EMI a.s.", "address": "x, 04001 Košice", "country": "SK",
         "licenses": [lic("EM", "2015-01-01", "2026-09-30")]},
        {"id": "3", "name": "Foreign PI", "country": "DE", "licenses": [lic("EUPI", "2020-01-01", issuer="BaFin")]},
        {"id": "4", "name": "Agent s.r.o.", "country": "SK", "licenses": [lic("APS", "2020-01-01")]},
    ]})


@pytest.mark.parametrize("style", ["snake", "camel"])
def test_nbs_psd2_feed(style):
    spec = {"url": "https://subjekty.nbs.sk/api/psd2", "format": "nbs_psd2", "_today": "2026-11-06"}
    recs, info = national.parse_file(_sk_doc(style).encode(), spec, "SK", "NBS")
    by = {r.name: r for r in recs}
    assert set(by) == {"Pay SK s.r.o.", "Old EMI a.s."}            # bank, passported, agent left out
    assert by["Pay SK s.r.o."].licence_type == "Payment institution" and by["Pay SK s.r.o."].active
    assert by["Pay SK s.r.o."].reference == "50000001" and by["Pay SK s.r.o."].city == "Bratislava"
    assert by["Pay SK s.r.o."].status_date == "2019-05-01"
    assert by["Old EMI a.s."].active is False and "2026-09-30" in by["Old EMI a.s."].status_detail
    assert dict(info["type_values"])["EUPI"] == 1


LB_REAL = ("﻿Title;Type;Business form;Company code;Registration code;Licence type/kind;Authorization code;valid from;valid till\n"
           " Unzer Luxembourg S.A.;Payment institution;Payment institutions of EU Member States providing services in the Republic of Lithuania without a branch;; B 144133;;;;\n"
           "UAB Lt Pay;Payment institution;Payment institution licensed in the Republic of Lithuania;305000001;;Payment institution licence;LIC-1;2019-02-01;\n"
           "UAB Gone Pay;Payment institution;Payment institution licensed in the Republic of Lithuania;305000002;;Payment institution licence;LIC-2;2018-01-01;2026-05-31\n"
           "X branch;Payment institution;Branch of a payment institution of an EU Member State;;;;;;\n")


def test_lb_filters_passported_and_uses_validity():
    spec = {"url": "u", "licence_type": "Payment institution", "exclude_column": "Business form",
            "exclude_pattern": "Member State|branch|third countr", "_today": "2026-11-06"}
    recs, info = national.parse_file(LB_REAL.encode("utf-8"), spec, "LT", "LB")
    by = {r.name: r for r in recs}
    assert set(by) == {"UAB Lt Pay", "UAB Gone Pay"}
    assert by["UAB Lt Pay"].active and by["UAB Lt Pay"].status_date == "2019-02-01"
    assert by["UAB Gone Pay"].active is False and by["UAB Lt Pay"].reference == "305000001"


def test_settings_change_starts_fresh_baseline(tmp_path, monkeypatch):
    world = {"rows": [("UAB A", "Payment institution licensed in the Republic of Lithuania")] * 1 +
                     [(f"Foreign {i}", "EU Member States without a branch") for i in range(60)]}

    def fetch(self, ctx):
        text = "Title;Type;Business form\n" + "".join(f"{n};Payment institution;{f}\n" for n, f in world["rows"])
        spec = {"url": "u", "licence_type": "Payment institution", **(self.cfg["files"][0])}
        return national.parse_file(text.encode(), {**spec, "_today": ctx.today}, "LT", "LB")[0]

    monkeypatch.setattr(national.NationalSource, "fetch", fetch)
    cfgp = tmp_path / "c.yaml"
    data = tmp_path / "data"
    args = ["--config", str(cfgp), "--data", str(data), "run", "--no-sheets", "--no-email"]
    cfgp.write_text("sources: {}\nnational:\n  LT:\n    regulator: LB\n    files: [{url: u}]\n")
    assert cli.main(args) == 0                                       # baseline incl. foreign firms
    cfgp.write_text("sources: {}\nnational:\n  LT:\n    regulator: LB\n    files: [{url: u, exclude_column: Business form, "
                    "exclude_pattern: Member State}]\n")
    assert cli.main(args) == 0                                       # not a 'drop', no false removals
    assert not (data / "changes.csv").exists()
    assert len(load_snapshot(data, "nat_lt")) == 1


def test_match_name_lithuanian_forms():
    m = national.match_name
    assert m("Akcinė bendrovė Lietuvos paštas") == m("Joint stock company Lietuvos paštas") == m("AB Lietuvos paštas")
    assert m('UAB „Lux International Payment System“ (Restricted activity)') == m("Lux International Payment System, UAB")
    assert m("Uždaroji akcinė bendrovė „Foxpay“") == m("Foxpay, UAB")


def test_lb_require_company_code_and_aisp_override():
    text = ("Title;Type;Business form;Company code\n"
            "CRIF Realtime Ireland Limited;Payment institution;Account information service provider;\n"
            "UAB Cygnus;Payment institution;Account information service provider;305999999\n"
            "UAB Pay;Payment institution;Payment institutions holding a payment institution licence;305111111\n")
    spec = {"url": "u", "licence_type": "Payment institution", "exclude_column": "Business form",
            "exclude_pattern": "Member State", "require_column": "Company code",
            "type_overrides": {"^(Currency exchange operators, )?Account information service provider$":
                               "Account information service provider"}}
    recs, _ = national.parse_file(text.encode(), spec, "LT", "LB")
    assert {(r.name, r.licence_type) for r in recs} == {("UAB Cygnus", "Account information service provider"),
                                                       ("UAB Pay", "Payment institution")}


# ------------------------------------------------------------- Canada ---

from regtracker.sources import boc, fintrac
from regtracker import canada

FINTRAC_CSV = ("Organization Names (Legal and Operating),Business Address,Services Offered,MSB Registration Status,"
               "MSB Registration Number,Date Initial MSB Registration Approval,MSB Registration Expiry Date,"
               "MSB Registration Cessation Date (YYYY/MM/DD),MSB Registration Revocation Date (YYYY/MM/DD),"
               "Jurisdiction of Incorporation,Incorporation Number,Incorporation Date (YYYY/MM/DD),Foreign MSB Indicator\n"
               '"Paymentco Inc.; PayCo","1 King St W, Toronto, ON, M5H 1A1",Money transferring,Registered,M12345678,2019-15-05,2027-15-05,,,ON,1,2018/01/01,No\n'
               '"Wise Payments Canada Inc.","99 Bay St, Toronto, ON, M5J 2X2",Money transferring; Foreign exchange dealing,Registered,M22222222,2016/01/01,2028/01/01,,,BC,2,2015/01/01,No\n'
               '"Corner FX Ltd.","5 Main St, Vancouver, BC, V6B 1A1",Foreign exchange dealing,Registered,M33333333,2020/01/01,2028/01/01,,,BC,3,2019/01/01,No\n'
               '"Gone Remit Ltd.","2 Rue X, Montréal, QC, H2X 1A1",Money transferring,Revoked,M44444444,2017-01-01,,,2025-31-03,QC,4,2016/01/01,No\n'
               '"Foreign Pay GmbH","Berlin, Germany",Money transferring,Registered,M55555555,2021/01/01,2027/01/01,,,DE,5,2020/01/01,Yes\n')


def test_fintrac_parse():
    recs, info = fintrac.parse_fintrac(FINTRAC_CSV.encode("utf-8"))
    by = {r.reference: r for r in recs}
    assert len(recs) == 5 and info["columns"]["number"] == "MSB Registration Number"
    assert by["M12345678"].name == "Paymentco Inc." and by["M12345678"].extra["other_names"] == "PayCo"
    assert by["M12345678"].city == "Toronto" and by["M12345678"].status_date == "2019-05-15"
    assert by["M44444444"].active is False and "2025-03-31" in by["M44444444"].status_detail
    assert by["M55555555"].licence_type == "Foreign MSB"


def test_boc_helpers():
    e = boc.normalise_entry({"main_trade_name": "PayCo", "en_legal_name": "", "fr_legal_name": "Paiementco Inc.",
                             "head_office_country": "Canada", "status": "Under review", "created_on": "2025-01-02 10:00:00",
                             "registration_date": None, "has_violations": 0, "id": "e72f-1", "group_label": "applicant"})
    assert (e["legal"], e["trade"], e["country"], e["id"]) == ("Paiementco Inc.", "PayCo", "Canada", "e72f-1")
    assert boc.kind_of(e, "registered") == "applicant"
    assert boc.kind_of({"group": "registered", "status": "Registered"}, "applicant") == "registered"
    assert boc.kind_of({"group": "", "status": "Registration revoked"}, "registered") == "revoked"
    assert boc.kind_of({"group": "", "status": "Unregistered"}, "applicant") == "applicant"
    # real detail-page text (collapsed, as captured on the first live run)
    real = ("Registry: Payment service provider account details 1000042609 Ontario Inc. / Pay Away Registration "
            "information Registration status Registered Registration date June 23, 2026 Contact information Legal name "
            "1000042609 Ontario Inc. Trade names Pay Away, Pay Away Mailing address 247 Queen Street East Unit 108 Brampton "
            "Ontario L6W 0B8 Canada Head office address 247 Queen Street East Unit 108 Brampton Ontario L6W 0B8 Canada "
            "Head office city Brampton Head office country Canada Phone number +16472940512 Email address "
            "freedomfinanceontario@gmail.com")
    f = boc.parse_detail_text(real)
    assert f["Head office city"] == "Brampton" and f["Phone number"] == "+16472940512"
    email, site = boc.contacts_from_page(real, ["https://www.bankofcanadamuseum.ca", "https://www.facebook.com/x"])
    assert email == "freedomfinanceontario@gmail.com" and site == ""          # no museum link


@pytest.fixture
def mock_boc_server():
    import functools, http.server, threading
    root = Path(__file__).parent / "mock_boc"
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_boc_end_to_end_with_browser_and_linking(tmp_path, monkeypatch, mock_boc_server):
    pytest.importorskip("playwright")
    base = mock_boc_server
    monkeypatch.setattr(boc, "PAGES", {"registered": base + "/registry.html", "applicant": base + "/applicants.html"})
    monkeypatch.setattr(boc, "ENFORCEMENT_PAGE", base + "/enforcement.html")
    cfg = {"sources": {"boc": {"details_per_run": 10, "detail_delay_seconds": 0}}}
    ctx = cli.make_context(cfg, tmp_path)
    recs = boc.BoCSource().fetch(ctx)
    by = {r.name: r for r in recs}
    assert set(by) == {"Paymentco Inc.", "Wise Payments Canada Inc.", "Applicant One Ltd.", "Bad Pay Corp"}
    assert by["Wise Payments Canada Inc."].licence_type == "PSP (RPAA registered)"     # French-only legal name used
    assert by["Applicant One Ltd."].licence_type == "PSP applicant (RPAA)"             # status from the feed
    assert by["Bad Pay Corp"].licence_type == "PSP refused (RPAA)" and not by["Bad Pay Corp"].active
    p = by["Paymentco Inc."]
    assert (p.email, p.website, p.city, p.extra["phone"]) == ("info@paymentco.ca", "https://www.paymentco.ca",
                                                             "Toronto", "1-416-555-0100")
    assert by["Wise Payments Canada Inc."].website == ""                              # not the museum link
    assert by["Applicant One Ltd."].status_date == "2025-09-01" and by["Applicant One Ltd."].email == "hello@appone.com"
    assert "violations: 1" in by["Wise Payments Canada Inc."].status_detail
    sample = (tmp_path / "debug" / "boc_sample.txt").read_text()
    assert "from data" in sample and "firms by status" in sample
    # second run uses the cache: no detail pages opened
    recs2 = boc.BoCSource().fetch(ctx)
    assert "0 fetched this run" in (tmp_path / "debug" / "boc_sample.txt").read_text()
    assert {r.email for r in recs2} == {r.email for r in recs}

    # linking MSBs to PSP lists
    msbs, _ = fintrac.parse_fintrac(FINTRAC_CSV.encode())
    rows = [r.to_dict() for r in msbs] + [r.to_dict() for r in recs]
    counts = canada.link(rows)
    st = {r["name"]: r for r in rows if r["source"] == "fintrac"}
    assert st["Paymentco Inc."]["psp_status"] == "PSP registered" and st["Paymentco Inc."]["email"] == "info@paymentco.ca"
    assert st["Wise Payments Canada Inc."]["psp_status"] == "PSP registered"
    assert st["Corner FX Ltd."]["psp_status"] == canada.NONE
    assert counts["PSP registered"] == 2
    # an MSB that only applied
    rows2 = [{"source": "fintrac", "name": "Applicant One Ltd", "extra": {}}] + [r.to_dict() for r in recs]
    canada.link(rows2)
    assert rows2[0]["psp_status"] == "PSP applied" and rows2[0]["email"] == "hello@appone.com"


def test_builtin_source_fingerprint_rebaselines(tmp_path, monkeypatch):
    from regtracker.model import Record
    from regtracker.sources import ALL_SOURCES
    from regtracker.sources.base import Source
    state = {"v": "a", "recs": [Record(f"k{i}", "test", "CA", "X", f"N{i}", "MSB", True) for i in range(100)]}

    class T(Source):
        name, label = "test", "T"
        @property
        def fingerprint(self): return state["v"]
        def fetch(self, ctx): return state["recs"]

    monkeypatch.setitem(ALL_SOURCES, "test", T())
    cfgp = tmp_path / "c.yaml"; cfgp.write_text("sources:\n  test: {enabled: true}\n")
    args = ["--config", str(cfgp), "--data", str(tmp_path / "d"), "run", "--no-sheets", "--no-email"]
    assert cli.main(args) == 0
    state["v"] = "b"
    state["recs"] = [Record(f"new{i}", "test", "CA", "X", f"N{i}", "MSB", True) for i in range(100)]  # new keys
    assert cli.main(args) == 0
    assert not (tmp_path / "d" / "changes.csv").exists()          # quiet re-baseline, no 200 false alerts


def test_canada_link_operating_names():
    rows = [{"source": "fintrac", "name": "ARCA Logistics Solutions Inc.", "extra": {"other_names": "ARCA Group, ARCA Pay, Inc., ARCA Armoured"}},
            {"source": "boc", "name": "Something Else Ltd", "licence_type": "PSP (RPAA registered)", "email": "a@arca.ca",
             "website": "", "extra": {"trade_name": "ARCA Pay"}}]
    canada.link(rows)
    assert rows[0]["psp_status"] == "PSP registered" and rows[0]["email"] == "a@arca.ca"
