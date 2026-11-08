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
    args = ["--config", str(cfgp), "--data", str(tmp_path / "data"), "run", "--no-sheets", "--no-email", "--no-contacts"]
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
    rc = cli.main(["--config", str(cfgp), "--data", str(data), "run", "--no-sheets", "--no-email", "--no-contacts"])
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
    args = ["--config", str(cfgp), "--data", str(data), "run", "--no-sheets", "--no-email", "--no-contacts"]
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
    args = ["--config", str(cfgp), "--data", str(data), "run", "--no-sheets", "--no-email", "--no-contacts"]
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
    assert by["Bad Pay Corp"].licence_type == "PSP applicant (RPAA)"                  # listed as an applicant
    p = by["Paymentco Inc."]
    assert (p.email, p.website, p.city, p.extra["phone"]) == ("info@paymentco.ca", "https://www.paymentco.ca",
                                                             "Toronto", "1-416-555-0100")
    assert by["Wise Payments Canada Inc."].website == ""                              # not the museum link
    assert by["Applicant One Ltd."].status_date == "2025-09-01" and by["Applicant One Ltd."].email == "hello@appone.com"
    assert "violations: 1" in by["Wise Payments Canada Inc."].status_detail
    sample = (tmp_path / "debug" / "boc_sample.txt").read_text()
    assert "from matching data" in sample and "firms by status" in sample
    assert by["Applicant One Ltd."].reference == "aaaaaaaa-0010"                       # id from the detail link
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
    args = ["--config", str(cfgp), "--data", str(tmp_path / "d"), "run", "--no-sheets", "--no-email", "--no-contacts"]
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


def test_fca_email_and_phone_on_sheet():
    firm = {"Organisation Name": "Clear Junction Ltd", "Status": "Registered",
            "PSD / EMD Status": "Authorised Electronic Money Institution", "PSD / EMD Effective Date": "19/04/2018"}
    addr = {"Address Type": "Principal Place of Business", "Town": "London", "Phone Number": "+442038654565",
            "Website Address": "www.clearjunction.com", "Email Address": "info@clearjunction.com", "Postcode": "W1G 0PW"}
    r = fca.record_from_firm(900684, firm, addr).to_dict()
    assert r["email"] == "info@clearjunction.com" and r["website"] == "www.clearjunction.com"
    from regtracker.store import record_row
    from regtracker.model import SHEET_COLUMNS
    row = dict(zip([k for k, _ in SHEET_COLUMNS], record_row(r)))
    assert row["phone"] == "+442038654565" and row["email"] == "info@clearjunction.com"


# ---------------------------------------------------------------- contacts

def _serve(directory):
    import functools, http.server, threading
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    h.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_contact_helpers():
    from regtracker import contacts as c
    assert c.norm_site("www.acme.com/") == "https://www.acme.com"
    assert c.domain("https://www.Acme.com/x") == "acme.com"
    assert c.best_email(["jane@gmail.com", "sales@acme.com", "info@acme.com"], "acme.com") == "info@acme.com"
    html = 'Call <a href="tel:+44%2020%201234%205678">us</a> Fax: 1 Tel.: 0049 30 1234567 date 20240101'
    ph = c.phones_in(html)
    assert ph[0] == "+442012345678" and "0049 30 1234567" in ph
    assert c.emails_in('x <img src="a@2x.png"> info [at] firm.de') == ["info@firm.de"]


def test_crawl_site_reads_contact_page_and_respects_robots():
    from regtracker import contacts as c
    from regtracker.http import make_session
    srv = _serve(Path(__file__).parent / "mock_site")
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        f = c.Fetcher(make_session())
        assert not f.allowed(base + "/private/impressum")
        res = c.crawl_site(f, base, pause=0)
        assert res["email"] == "info@acmepay.com"
        assert res["phone"].replace(" ", "") == "+442079460958"
        assert res["pages"] == 2   # homepage + /contact/ ; /private/ blocked, partner link off-site
    finally:
        srv.shutdown()


def test_contacts_apply_fills_blanks_only(tmp_path):
    from regtracker import contacts as c
    recs = [{"key": "pra:1", "website": "", "email": "", "active": True},
            {"key": "eba:2", "website": "firm.eu", "email": "own@firm.eu", "active": True},
            {"key": "ecb:3", "website": "", "active": True}]
    c.save(tmp_path, {"pra:1": {"website": "https://bank.co.uk", "website_from": "FCA register",
                                "phone": "0207", "phone_from": "FCA register"},
                      "ecb:3": {"website": "https://bigbank.de", "website_from": "Wikidata"}})
    c.save(tmp_path, {"site:bank.co.uk": {"email": "info@bank.co.uk", "email_from": "firm website"},
                      "site:firm.eu": {"email": "x@firm.eu", "phone": "+33 1 23 45 67 89",
                                       "email_from": "firm website", "phone_from": "firm website"},
                      "site:bigbank.de": {"phone": "+49 69 1", "phone_from": "firm website"}}, c.WEB_STATE)
    n = c.apply(recs, c.load_all(tmp_path))
    assert recs[0]["website"] == "https://bank.co.uk" and recs[0]["email"] == "info@bank.co.uk"
    assert recs[0]["contact_source"] == "FCA register, firm website"
    assert recs[1]["email"] == "own@firm.eu" and recs[1]["phone"] == "+33 1 23 45 67 89"
    assert recs[2]["phone"] == "+49 69 1" and recs[2]["contact_source"] == "Wikidata, firm website"
    assert n == {"website": 2, "email": 1, "phone": 3}


def test_crawl_budget_and_cache(tmp_path):
    import logging
    from regtracker import contacts as c
    from regtracker.http import make_session
    from regtracker.sources.base import Context
    srv = _serve(Path(__file__).parent / "mock_site")
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        ctx = Context({}, make_session(), "2026-11-06", tmp_path, set(), logging.getLogger("t"))
        recs = [{"key": "a:1", "website": base, "active": True},
                {"key": "a:2", "website": base + "/", "active": True},       # same domain: read once
                {"key": "a:3", "website": base, "active": False},
                {"key": "a:4", "website": "", "active": True}]
        out = c.crawl(ctx, recs, 1, logging.getLogger("t"), workers=2)
        assert out["read"] == 1 and out["email"] == 1
        assert c.crawl(ctx, recs, 1, logging.getLogger("t"))["read"] == 0   # cached
        c.apply(recs, c.load_all(tmp_path))
        assert recs[1]["email"] == "info@acmepay.com"
    finally:
        srv.shutdown()


def test_lb_slugs_and_wikidata(tmp_path):
    import logging
    from regtracker import contacts as c
    from regtracker.sources.base import Context
    assert c.lb_slugs("UAB Stanhope Financial")[0] == "uab-stanhope-financial"
    assert "curve-europe-uab" in c.lb_slugs("UAB „Curve Europe“")

    class Resp:
        def __init__(self, data): self.data = data; self.status_code = 200
        def raise_for_status(self): pass
        def json(self): return self.data
    class S:
        calls = []
        def get(self, url, params=None, **kw):
            self.calls.append(params["query"])
            return Resp({"results": {"bindings": [
                {"lei": {"value": "A" * 20}, "site": {"value": "http://www.bigbank.de/"}}]}})
    ctx = Context({}, S(), "2026-11-06", tmp_path, set(), logging.getLogger("t"))
    recs = [{"key": "ecb:1", "lei": "A" * 20}, {"key": "ecb:2", "lei": "B" * 20}, {"key": "x", "lei": ""}]
    cache = {}
    assert c.from_wikidata(ctx, recs, cache, logging.getLogger("t")) == 1
    assert cache["ecb:1"]["website"] == "http://www.bigbank.de" and cache["lei:" + "B" * 20]["website"] == ""
    assert c.from_wikidata(ctx, recs, cache, logging.getLogger("t")) == 0 and len(S.calls) == 1


# ---------------------------------------------------------------- United States

FINCEN_TSV = (
    "LEGAL NAME\tDBA NAME\tSTREET ADDRESS\tCITY\tSTATE\tZIP\tMSB ACTIVITIES\tSTATES OF MSB ACTIVITIES\t"
    "ALL STATES & TERRITORIES & FOREIGN FLAG**\tFOREIGN LOCATION\t# OF BRANCHES\tAUTH SIGN DATE\tRECEIVED DATE\n"
    "ACME PAY LLC\tACME\t1 MAIN ST\tMIAMI\tFL\t33101\t409 415\tFL NY TX\t\t\t0\t1/2/2022\t1/3/2022\n"
    "ACME PAY LLC\tACME\t1 MAIN ST\tMIAMI\tFL\t33101-1234\t409 415\tFL NY TX CA\t\t\t0\t11/2/2024\t11/4/2024\n"
    "CORNER CHECKS INC\t\t5 ELM\tAUSTIN\tTX\t73301\t408\tTX\t\t\t2\t3/1/2025\t3/2/2025\n"
    "GLOBAL REMIT CORP\tGR dba SendFast\t9 BAY\tNEW YORK\tNY\t10001\t409\t\tB\t\t10\t5/5/2025\t5/6/2025\n"
    "** A = All States and Territories, B = All States\t\t\t\t\t\t\t\t\t\t\t\t\n"
)


def test_fincen_parse_merges_filings():
    from regtracker.sources.fincen import parse_fincen
    recs, info = parse_fincen(FINCEN_TSV.encode())
    by = {r.name: r for r in recs}
    assert len(recs) == 3
    a = by["ACME PAY LLC"]
    assert a.licence_type == "MSB – money transmitter" and a.status_date == "2022-01-03"
    assert a.extra["latest_filing"] == "2024-11-04" and a.extra["filings"] == 2
    assert a.extra["us_states"] == "CA, FL, NY, TX" and "Dealer in foreign exchange" in a.services
    assert by["CORNER CHECKS INC"].licence_type == "MSB – other activities"
    assert by["GLOBAL REMIT CORP"].extra["us_states"] == "All states"


CA_HTML = """<html><body><h3><a href="/regulated_entity/aci">ACI Payments, Inc.</a></h3>
<p>License#: 2436<br>Institution Name: ACI Payments, Inc.<br>Officer Name: Keith T</p>
<p>Address:<br>6060 Coventry Drive<br>Elkhorn, NE 68022<br>Phone: 770-325-3193</p><p>DFPI Contact: J Lee</p>
<h3><a href="/regulated_entity/barri">Barri Money Services</a></h3>
<p>License#: 2643<br>Officer Name: X</p><p>Address:<br>1 Road<br>Los Angeles, CA 90015<br>Phone: 1</p>
</body></html>"""

TABLE_HTML = """<table><thead><tr><th>Name</th><th>Doing Business As</th><th>Address</th><th>City, State, Zip</th>
<th>Phone</th></tr></thead><tbody><tr><td>1Money USA, Inc.</td><td></td><td>575 5th Avenue</td>
<td>New York, NY 10017</td><td>Call 802-532-4080</td></tr>
<tr><td>Ace Payment Services, Inc.</td><td>Ace</td><td>300 E. Fwy</td><td>Irving, TX 75062</td><td>Call 972</td></tr>
</tbody></table>"""

LA_HTML = """<table><tr><th>EntityName</th><th>AddressLine1</th><th>AddressLine2</th><th>City</th><th>StateCode</th>
<th>PostalCode</th><th>CountryCode</th><th>AreaCode</th><th>Prefix</th><th>Number</th></tr>
<tr><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
<tr><td>Acme Pay LLC</td><td>1 Main</td><td></td><td>Miami</td><td>FL</td><td>33101</td><td>US</td>
<td>305</td><td>555</td><td>0100</td></tr></table>"""


def test_us_state_parsers_html():
    from regtracker.sources import usstates as u
    ca, _ = u.parse_ca_html("ca", CA_HTML.encode(), u.STATES["ca"])
    by = {r.reference: r for r in ca}
    assert by["2436"].name == "ACI Payments, Inc." and by["2436"].city == "Elkhorn"
    assert by["2436"].extra["phone"] == "770-325-3193" and by["2643"].name == "Barri Money Services"
    ind, info = u.parse_html_table("in", TABLE_HTML.encode(), u.STATES["in"])
    assert [r.name for r in ind] == ["1Money USA, Inc.", "Ace Payment Services, Inc."]
    assert ind[0].city == "New York" and ind[0].extra["phone"] == "802-532-4080"
    la, _ = u.parse_html_table("la", LA_HTML.encode(), u.STATES["la"])
    assert len(la) == 1 and la[0].city == "Miami" and la[0].extra["phone"] == "305-555-0100"
    assert all(r.active and r.jurisdiction == "US" for r in ca + ind + la)


def test_us_state_parsers_excel_and_zip():
    import io, zipfile
    import openpyxl
    from regtracker.sources import usstates as u
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Money Transmitter Licensees as of 9/30/2026"])
    ws.append([])
    ws.append(["License Number", "License Type", "Initial Approval", "Firm Name", "DBA Name", "Primary Address",
               "Phone Number", "Status"])
    ws.append(["FT2001", "FT2", "01/15/2020", "Acme Pay LLC", "", "1 Main St, Miami, FL 33101", "305", "Approved"])
    ws.append(["FT2002", "FT2", "02/01/2019", "Old Remit Inc", "", "2 Rd, Tampa, FL 33601", "", "Expired"])
    ws.append(["FT3001", "FT3", "03/01/2021", "Check Shop", "", "3 Rd, Orlando, FL 32801", "", "Approved"])
    buf = io.BytesIO()
    wb.save(buf)
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("Money_Transmitters.xlsx", buf.getvalue())
    recs, info = u.parse_fl_zip("fl", zbuf.getvalue(), u.STATES["fl"])
    by = {r.name: r for r in recs}
    assert set(by) == {"Acme Pay LLC", "Old Remit Inc"}            # Part III check casher left out
    assert by["Acme Pay LLC"].active and not by["Old Remit Inc"].active
    assert by["Acme Pay LLC"].status_date == "2020-01-15" and by["Acme Pay LLC"].city == "Miami"
    ma, _ = u.parse_table_file("ma", buf.getvalue(), u.STATES["ma"])
    assert len(ma) == 3


def test_us_state_parsers_pdf_text(monkeypatch):
    from regtracker.sources import usstates as u
    de_lines = ["Licensees and Existing Branches PDT: 8/20/2026 2:29PM", "Check Seller, Money Transmitter",
                "042465 1Money USA, Inc.", "575 5th Avenue Floor 14 Suite 14-107 New York, NY 10017",
                "Contact: Kristen H - (845) 999-0474", "Filing Status: Current - Licensed Expires Date: 12/31/2026",
                "011221 ACI Payments, Inc. D/B/A Speedpay", "6060 Coventry Drive Elkhorn, NE 68022",
                "Contact: M D - (703) 653-2515", "Filing Status: Current - Licensed Expires Date: 12/31/2026",
                "Branches / Locations", "123456 1 Branch Road Dover, DE 19901"]
    monkeypatch.setattr(u, "pdf_lines", lambda raw: de_lines)
    de, _ = u.parse_de_pdf("de", b"", u.STATES["de"])
    by = {r.reference: r for r in de}
    assert set(by) == {"042465", "011221"}
    assert by["011221"].name == "ACI Payments, Inc." and by["011221"].extra["dba"] == "Speedpay"
    assert by["042465"].city == "New York" and by["042465"].extra["phone"] == "(845) 999-0474"


def test_usa_link_and_licence_page():
    from regtracker import usa
    from regtracker.contacts import licence_states
    fin = {"key": "fincen:1", "source": "fincen", "name": "ACME PAY LLC", "licence_type": "MSB – money transmitter",
           "active": True, "website": "https://acme.com", "extra": {"dba": "ACME", "us_states": "CA, FL, NY, TX"}}
    fin2 = {"key": "fincen:2", "source": "fincen", "name": "OTHER CO", "licence_type": "MSB – other activities",
            "active": True, "extra": {"us_states": "TX"}}
    fl = {"key": "us_fl:1", "source": "us_fl", "name": "Acme Pay, LLC", "active": True,
          "extra": {"licence_state": "FL"}}
    ca = {"key": "us_ca:9", "source": "us_ca", "name": "Acme Pay LLC", "active": True, "extra": {"licence_state": "CA"}}
    lone = {"key": "us_ca:8", "source": "us_ca", "name": "Unknown Remit", "active": True, "extra": {"licence_state": "CA"}}
    web = {"site:acme.com": {"us_licence_states": ["CA", "TX", "NY"]}}
    out = usa.link([fin, fin2, fl, ca, lone], web)
    assert fin["us_licences"] == "CA, FL; per firm's website: TX, NY"
    assert fin2["us_licences"] == "None on the published lists (CA, FL)"
    assert fl["fincen_status"] == "Registered – money transmitter" and fl["us_states"] == "CA, FL, NY, TX"
    assert fl["us_licences"] == "CA, FL" and lone["fincen_status"].startswith("Not found")
    assert out["state_rows_on_fincen"] == 2
    states, nmls = licence_states("<p>Licensed in Alabama, West Virginia, Arkansas and New York. NMLS ID: 123456</p>")
    assert states == ["AL", "AR", "NY", "WV"] and nmls == "123456"


def test_us_end_to_end(tmp_path, monkeypatch):
    import csv as _csv
    import yaml
    from regtracker import cli
    from regtracker.sources import fincen, usstates
    recs, _ = fincen.parse_fincen(FINCEN_TSV.encode())
    monkeypatch.setattr(fincen.FinCENSource, "fetch", lambda self, ctx: recs)

    def state_fetch(self, ctx):
        if self.code != "ca":
            return []
        return usstates.parse_ca_html("ca", CA_HTML.replace("ACI Payments, Inc.", "Acme Pay LLC").encode(),
                                      usstates.STATES["ca"])[0]
    monkeypatch.setattr(usstates.USStateSource, "fetch", state_fetch)
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(yaml.safe_dump({"sources": {"fincen": {}, "us_states": {"enabled": True, "skip": ["de", "ak"]}}}))
    rc = cli.main(["--config", str(cfgp), "--data", str(tmp_path / "d"), "run", "--no-sheets", "--no-email",
                   "--no-contacts"])
    rows = list(_csv.DictReader(open(tmp_path / "d" / "licences.csv")))
    by = {(r["Source"], r["Firm name"]): r for r in rows}
    acme = by[("fincen", "ACME PAY LLC")]
    assert acme["State MT licences (US published lists)"] == "CA"
    assert acme["States of operation (FinCEN, US)"] == "CA, FL, NY, TX"
    assert by[("us_ca", "Acme Pay LLC")]["FinCEN MSB (US)"] == "Registered – money transmitter"
    assert rc == 0


def test_fca_replan_keeps_progress_and_crypto(tmp_path):
    from regtracker.sources import fca
    st = {"seed": {"ranges": [[900000, 999999], [500000, 899999], [1000000, 1100000], [100000, 499999]],
                   "range_index": 0, "cursor": 984154, "done": False, "checked": 84154},
          "tracked": {}, "max_frn_seen": 984153}
    cfg = {"seed_ranges": [[900000, 999999], [1000000, 1100000], [500000, 899999]]}
    assert fca.replan(st, cfg)
    sd = st["seed"]
    # the part scanned before agents were kept is re-scanned at the end
    assert sd["ranges"] == [[984154, 999999], [1000000, 1100000], [500000, 899999], [900000, 984153]]
    assert sd["cursor"] == 984154 and sd["classify_version"] == fca.CLASSIFY_VERSION
    assert fca.seed_progress(st).startswith("0.0% scanned")
    sd["ranges"], sd["scanned"] = [[984154, 999999], [1000000, 1100000], [500000, 899999]], [[900000, 984153]]
    assert fca.seed_progress(st).startswith("14.0% scanned")      # 84,154 of 600,001
    assert not fca.replan(st, cfg)                                  # unchanged config: no-op
    sd["range_index"], sd["cursor"] = 1, 1000500                    # later: move on, then reorder again
    fca.replan(st, {"seed_ranges": [[500000, 899999], [1000000, 1100000], [900000, 999999]]})
    assert st["seed"]["ranges"] == [[500000, 899999], [1000500, 1100000]]

    # crypto: by firm type, or on the FCA crypto list; RAISPs excluded
    crypto_only = {"Organisation Name": "Coin Ltd", "Status": "Registered", "Business Type": "MLD cryptoassets",
                   "MLRs Status": "MLRs Registered", "MLRs Status Effective Date": "18/08/2020"}
    r = fca.record_from_firm(838656, crypto_only)
    assert r.licence_type == fca.CRYPTO_TYPE and r.active and r.status_date == "18/08/2020"
    former = dict(crypto_only, **{"MLRs Status": "Formerly MLRs Registered"})
    assert not fca.record_from_firm(928607, former).active
    emi = {"Organisation Name": "Revolut Ltd", "Status": "Authorised", "Business Type": "Regulated",
           "PSD / EMD Status": "Authorised Electronic Money Institution", "MLRs Status": "MLRs Registered"}
    assert fca.record_from_firm(900562, emi).licence_type == "Authorised EMI"
    assert fca.record_from_firm(900562, emi, crypto_frns={900562}).licence_type == "Authorised EMI + cryptoasset firm"
    raisp = {"Organisation Name": "Open Data Ltd", "Status": "Registered",
             "PSD / EMD Status": "Registered Account Information Service Provider"}
    assert fca.record_from_firm(1, raisp) is None
    annex1 = {"Organisation Name": "Lender Ltd", "Status": "Registered", "Business Type": "Regulated",
              "MLRs Status": "MLRs Registered"}
    assert fca.record_from_firm(2, annex1) is None                  # MLR-registered but not crypto
    emd = {"Organisation Name": "Corner Shop Ltd", "Status": "Registered", "E-Money Agent Status": "Current",
           "E-Money Agent Effective Date": "2021-03-04T00:00:00"}
    r = fca.record_from_firm(3, emd)
    assert r.licence_type == "E-money agent" and r.active and r.status_date == "2021-03-04"
    psd = {"Organisation Name": "Remit Agent Ltd", "PSD Agent Status": "No Longer Registered"}
    r = fca.record_from_firm(4, psd)
    assert r.licence_type == "Payment agent (PSD)" and not r.active
    assert fca.record_from_firm(4, psd, agents=("emd",)) is None


# ---------------------------------------------------------------- MiCA, South Africa, Swiss SRO

def test_mica_parse_real_esma_files():
    from regtracker.sources.mica import parse_casps, parse_emts
    fx = Path(__file__).parent / "fixtures"
    casps = parse_casps((fx / "CASPS.csv").read_bytes())
    assert len(casps) == 394                                       # same as casptracker.eu's merged count
    art60 = [r for r in casps if "Art. 60" in r.licence_type]
    assert len(art60) == 150 and sum(not r.active for r in casps) == 3
    bp = next(r for r in casps if r.name == "Bitpanda GmbH")
    assert bp.jurisdiction == "AT" and bp.regulator == "FMA" and bp.status_date == "2025-04-09"
    assert "DE" in bp.passports and bp.website == "https://www.bitpanda.com"
    emts = parse_emts((fx / "EMTWP.csv").read_bytes())
    assert len(emts) == 25 and all(r.licence_type == "MiCA e-money token issuer" for r in emts)


def test_south_africa_parsers():
    from regtracker.sources import southafrica as za
    tppp = ["Sponsoring Bank Entity Registration Registered Entity Name ATM Services Credit Card Debit Card",
            "1 Absa Bank Limited 2008/019068/07 Abellard Software Services (Pty) Ltd X XX",
            "2 Absa Bank Limited 1958/003362/07 Ackermans a division of pepkor trading (pty) ltdXXXX",
            "3 Absa Bank Limited 2019/463070/07 Addpay (Pty) LtdX XXXX",
            "40 Nedbank Limited 2019/463070/07 Addpay (Pty) Ltd X"]
    recs = {r.reference: r for r in za.parse_tppp(tppp, "u")}
    assert recs["1958/003362/07"].name == "Ackermans a division of pepkor trading (pty) ltd"
    assert recs["2019/463070/07"].name == "Addpay (Pty) Ltd"
    assert recs["2019/463070/07"].extra["sponsoring_banks"] == "Absa Bank Limited; Nedbank Limited"
    so = ["1 ACI Worldwide HRB 227459 ACI Worldwide (Germany) GMBH x x Visa Mastercard x x",
          "2 AddPay 2019/463070/07 AddPay (Pty) Ltd x x x x x Visa Mastercard x x x x",
          "3 Adumo Technologies 2000/029811/07 Lesaka Merchant Technologies (Pty) Ltd x x x x"]
    ops = {r.reference: r for r in za.parse_operators(so, "u", "za_so", "SO")}
    assert ops["HRB227459"].name == "ACI Worldwide (Germany) GMBH"
    assert ops["2000/029811/07"].extra["trading_name"] == "Adumo Technologies"
    banks = ["Locally Controlled Banks", "Name Address CEO Telephone Web Address", "Absa Group Limited",
             "P O Box 7735 Mr Kenny Fihla", "Absa Bank Limited 011 350 5347 https://www.absa.africa/",
             "Johannesburg Chief Executive Officer", "2000", "5 Neutron Road", "Techno Park Mr Graham Lee",
             "Capitec Bank Limited 021 809 5900 www.capitecbank.co.za", "Stellenbosch Chief Executive Officer", "7600",
             "P O Box 784921",
             "Access Bank (South Africa) Sandton Mr Sandile Shabalala 011 634 4300 www.southafrica.accessbankplc.com",
             "Limited 2196 Chief Executive Officer",
             "Albaraka Bank Limited Office Park Stalwart Simelane Street Mr Shabir Chohan 031 \u2010 364 9002 www.albaraka.co.za",
             "Durban Chief Executive Officer 082 490 5673", "4001",
             "Ubank Limited (under curatorship) 011 518 5000 www.ubank.co.za", "Carletonville"]
    b = {r.name: r for r in za.parse_banks(banks, "u", "Bank (locally controlled)")}
    assert set(b) == {"Absa Bank Limited", "Capitec Bank Limited", "Access Bank (South Africa) Limited",
                      "Albaraka Bank Limited", "Ubank Limited"}
    assert b["Absa Bank Limited"].website == "https://www.absa.africa/" and b["Absa Bank Limited"].extra["phone"] == "011 350 5347"
    assert b["Absa Bank Limited"].city == "Johannesburg" and b["Absa Bank Limited"].extra["ceo"] == "Mr Kenny Fihla"
    assert b["Capitec Bank Limited"].website == "https://www.capitecbank.co.za" and b["Capitec Bank Limited"].city == "Stellenbosch"
    assert b["Access Bank (South Africa) Limited"].city == "Sandton" and not b["Ubank Limited"].active
    so = ["SO PUBLIC REGISTER", "1ACI Worldwide HRB 227459 ACI Worldwide (Germany) GMBH x x Visa Mastercard x x",
          "15Callpay 2017/021680/07 Ahead Start (Pty) Ltd x x x x x Visa Mastercard x x"]
    ops = {r.reference: r for r in za.parse_operators(so, "u", "za_so", "SO")}
    assert ops["HRB227459"].name == "ACI Worldwide (Germany) GMBH" and ops["2017/021680/07"].extra["trading_name"] == "Callpay"
    pch = za.parse_pchso(["Entity Registration Entity Trading Name", "PCHs",
                          "1993/007766/07 PayInc South African Bankers Services Company (Pty) Ltd 1. Authenticated Collections (AC) PCH",
                          "2. Registered Mandate (RM) PCH", "1998/022242/07 Strate Strate (Pty) Ltd 1. Bonds PCH"], "u")
    p = {r.reference: r for r in pch}
    assert p["1993/007766/07"].name == "South African Bankers Services Company (Pty) Ltd"
    assert p["1993/007766/07"].extra["trading_name"] == "PayInc" and "Registered Mandate" in p["1993/007766/07"].services
    assert p["1998/022242/07"].name == "Strate (Pty) Ltd"
    casp = za.parse_casps(["No. FSP Name FSP Number", "1 Luno South Africa (Pty) Ltd 53279", "2 VALR (Pty) Ltd 53308"], "u")
    assert {r.reference: r.name for r in casp} == {"53279": "Luno South Africa (Pty) Ltd", "53308": "VALR (Pty) Ltd"}
    links = ["https://x/public-list-tppp-13-10-2026.pdf", "https://x/Public-list-TPPP-May-2026.pdf"]
    assert za.latest(links).endswith("13-10-2026.pdf")


def test_swiss_sro_matching_and_apply(tmp_path):
    from regtracker.sources import swiss_sro as s
    page = "Résultats\nBitcoin Suisse AG\nZug\nVQF Verein zur Qualitätssicherung\nOther Firm SA\nGenève\nARIF"
    assert s.match_results("Bitcoin Suisse AG", page, [])["sro"] == "VQF"
    assert s.match_results("Other Firm SA", page, [])["sro"] == "ARIF"
    payload = [{"results": [{"name": "Crypto Finance AG", "sro": "PolyReg", "city": "Zürich"}]}]
    assert s.match_results("Crypto Finance AG", "", payload)["sro"] == "PolyReg"
    assert s.match_results("Unknown AG", page, payload)["sro"] == ""
    recs = [{"name": "Bitcoin Suisse AG", "jurisdiction": "US", "extra": {"foreign_location": "SWITZERLAND"}},
            {"name": "Bitcoin Suisse AG", "jurisdiction": "US", "extra": {"foreign_location": "GERMANY"}}]
    assert s.apply(recs, {"bitcoinsuisse": {"sro": "VQF"}}) == 1
    assert recs[0]["ch_sro"] == "VQF" and "ch_sro" not in recs[1]


def test_disabled_sources_hidden(tmp_path):
    import logging
    from regtracker import cli
    from regtracker.store import save_snapshot
    data = tmp_path / "d"
    save_snapshot(data, "pra", {"pra:1": {"key": "pra:1", "source": "pra", "name": "Bank", "jurisdiction": "GB"}})
    save_snapshot(data, "eba", {"eba:1": {"key": "eba:1", "source": "eba", "name": "EMI", "jurisdiction": "FR"}})
    cfg = {"sources": {"pra": {"enabled": False}, "eba": {}}}
    recs = cli.assemble(cfg, data, cli.make_context(cfg, data))
    assert [r["key"] for r in recs] == ["eba:1"]


def test_us_fixes_de_layout_ct_link_and_manual_files(tmp_path, monkeypatch):
    import logging
    from regtracker.sources import usstates as u
    from regtracker.sources.base import Context, SourceNotReady
    # Delaware as it really extracts: status glued onto the address line, no Contact line
    lines = ["State of Delaware", "Licensees and Existing Branches PDT: 10/28/2026 11:43AM",
             "037280 Acme Remit LLC", "2828 JOHN F KENNEDY BOULEVARD SUITE 203 Jersey City, NJ 07306 Filing Status: "
             "Current - Licensed Expires Date: 12/31/2026",
             "042465 1Money USA, Inc.", "575 5th Avenue New York, NY 10017", "Contact: K H - (845) 999-0474",
             "Filing Status: Current - Licensed Expires Date: 12/31/2026", "Branches / Locations",
             "123456 1 Branch Road Dover, DE 19901"]
    monkeypatch.setattr(u, "pdf_lines", lambda raw: lines)
    de, _ = u.parse_de_pdf("de", b"", u.STATES["de"])
    by = {r.reference: r for r in de}
    assert set(by) == {"037280", "042465"} and by["037280"].city == "Jersey City"
    assert by["042465"].extra["phone"] == "(845) 999-0474"

    class Resp:
        def __init__(self, text="", code=200):
            self.text, self.status_code, self.content = text, code, text.encode()
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"{self.status_code} Client Error: Forbidden")
    page = '<a href="/-/media/dob/money_transmitters.xlsx?rev=ae0&amp;hash=1D">List</a>'
    ctx = Context({}, None, "2026-11-06", tmp_path, set(), logging.getLogger("t"))
    monkeypatch.setattr(u, "robots_allowed", lambda s, url: True)
    src = u.USStateSource("ct", u.STATES["ct"])
    monkeypatch.setattr(src, "_get", lambda c, url: Resp(page))
    assert src.resolve_url(ctx) == "https://portal.ct.gov/-/media/dob/money_transmitters.xlsx?rev=ae0&hash=1D"
    ma = u.USStateSource("ma", u.STATES["ma"])
    monkeypatch.setattr(ma, "_get", lambda c, url: (_ for _ in ()).throw(RuntimeError("403 Client Error: Forbidden")))
    try:
        ma.fetch(ctx)
        assert False, "should wait for a manual file"
    except SourceNotReady as e:
        assert "data/manual/" in str(e) and "us_ma.xlsx" in str(e)
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.append(["Licensee Name", "License Number", "City"])
    wb.active.append(["Acme Remit LLC", "MT1", "Boston"])
    (tmp_path / "manual").mkdir()
    wb.save(tmp_path / "manual" / "us_ma.xlsx")
    recs = ma.fetch(ctx)
    assert [r.name for r in recs] == ["Acme Remit LLC"] and recs[0].city == "Boston"


def test_fincen_manual_spreadsheet():
    import io
    import openpyxl
    from regtracker.sources.fincen import parse_fincen
    wb = openpyxl.Workbook()
    for line in FINCEN_TSV.splitlines():
        wb.active.append(line.split("\t"))
    buf = io.BytesIO()
    wb.save(buf)
    recs, _ = parse_fincen(buf.getvalue())
    assert len(recs) == 3


def test_za_fsca_link_manual_files_and_finma_fintech(tmp_path, monkeypatch):
    import logging
    from regtracker.sources import southafrica as za
    from regtracker.sources.base import Context, SourceNotReady
    from regtracker.sources.finma import parse_finma
    page = ('<div><h3>FSCA confirms list of currently approved crypto asset service providers</h3>'
            '<p>The list is <a href="/_api/cr3ad_regulatoryframes%28f928%29/cr3ad_document/$value">here</a>'
            ' and the <a href="/_api/cr3ad_newses%28ac3e%29/cr3ad_document/$value">press release</a></p></div>')
    assert za.fsca_list_link(page) == "https://www.fsca.co.za/_api/cr3ad_regulatoryframes%28f928%29/cr3ad_document/$value"
    assert za.fsca_list_link("<a href='/x.pdf'>Annual report</a>") == ""

    ctx = Context({}, None, "2026-11-08", tmp_path, set(), logging.getLogger("t"))
    src = za.ZABanksSource()
    monkeypatch.setattr(src, "fetch_web", lambda c: (_ for _ in ()).throw(SourceNotReady("x.pdf is disallowed by robots.txt")))
    try:
        src.fetch(ctx)
        assert False
    except SourceNotReady as e:
        assert "data/manual/" in str(e) and "za_banks_mutual.pdf" in str(e)
    (tmp_path / "manual").mkdir()
    (tmp_path / "manual" / "za_banks_mutual.pdf").write_bytes(b"%PDF fake")
    monkeypatch.setattr(za, "pdf_lines", lambda raw: ["Mutual Banks", "18-20 Hill Street Mr Jonathan Fincham",
                                                       "GBS Mutual Bank 046 622 7109 https://www.gbsbank.co.za/",
                                                       "Makhanda Managing Director"])
    recs = src.fetch(ctx)
    assert [(r.name, r.licence_type) for r in recs] == [("GBS Mutual Bank", "Mutual bank")]

    import openpyxl, io
    wb = openpyxl.Workbook()
    wb.active.append(["No", "FSP Name", "FSP Number"])
    wb.active.append([1, "Luno South Africa (Pty) Ltd", 53279])
    buf = io.BytesIO(); wb.save(buf)
    assert [r.reference for r in za.parse_casps(za.sheet_lines(buf.getvalue()), "u")] == ["53279"]

    csv_text = ("Name;City;UID;AuthorisationTypeEN\nSygnum Bank AG;Zürich;CHE-1;Bank\n"
                "Fintech AG;Zug;CHE-2;Persons under Article 1b of the Banking Act\n"
                "Foreign Fintech;Zug;CHE-3;Representation of foreign persons under Article 1b of the Banking Act\n")
    got = parse_finma(csv_text, "article 1b|fintech", "representati")
    assert [r.name for r in got] == ["Fintech AG"]


def test_mica_tab():
    from regtracker.sheets import mica_tab, build_tabs
    from regtracker.sources.mica import parse_casps, parse_emts
    fx = Path(__file__).parent / "fixtures"
    recs = [r.to_dict() for r in parse_casps((fx / "CASPS.csv").read_bytes()) + parse_emts((fx / "EMTWP.csv").read_bytes())]
    for r in recs:
        r["source"] = "mica"
    rows = mica_tab(recs)
    assert rows[0][0] == "Firm name" and len(rows) == 1 + 394            # CASPs only, EMT issuers left out
    bp = next(x for x in rows if x[0] == "Bitpanda GmbH")
    assert bp[2] == "Austria (AT)" and bp[4] == "Authorised CASP (Art. 63)" and bp[5] == "Active"
    assert sum(1 for x in rows[1:] if x[4].startswith("Notified")) == 150
    tabs = build_tabs(recs, [], {"finished": "", "sources_status": "", "changes": 0, "problems": []},
                      extra_tabs={"MiCA CASPs (EU-EEA)": rows})
    assert list(tabs)[:4] == ["Summary", "Changes", "MiCA CASPs (EU-EEA)", "Licences"]


def test_friendly_change_labels():
    from regtracker.diff import diff, friendly_labels
    prev = {"boc:1": {"key": "boc:1", "source": "boc", "name": "Pay Co", "licence_type": "PSP applicant (RPAA)", "active": True},
            "us_fl:9": {"key": "us_fl:9", "source": "us_fl", "name": "Remit", "licence_type": "Money transmitter licence (FL)",
                        "active": True}}
    curr = {"boc:1": dict(prev["boc:1"], licence_type="PSP (RPAA registered)"),
            "boc:2": {"key": "boc:2", "source": "boc", "name": "New PSP", "licence_type": "PSP applicant (RPAA)", "active": True},
            "us_fl:9": dict(prev["us_fl:9"], active=False),
            "us_fl:10": {"key": "us_fl:10", "source": "us_fl", "name": "Newco", "licence_type": "Money transmitter licence (FL)",
                         "active": True}}
    events, _ = diff(prev, curr, "2026-11-08 03:00")
    labels = sorted(e["event_label"] for e in friendly_labels(events))
    assert labels == ["Money transmitter licence ended (FL)", "Money transmitter licence granted (FL)",
                      "PSP application filed (Canada)", "PSP registered (Canada)"]


def test_opensanctions_msb_parse():
    import json
    from regtracker.sources.opensanctions_msb import parse
    lines = [
        {"id": "a1", "schema": "Address", "properties": {"full": ["1 MAIN ST, MIAMI, FL 33101"], "city": ["MIAMI"],
                                                          "state": ["FL"], "postalCode": ["33101"]}},
        {"id": "e1", "schema": "LegalEntity", "first_seen": "2024-11-04T00:00:00",
         "properties": {"name": ["ACME PAY LLC"], "alias": ["ACME"], "sector": ["409", "415"], "country": ["us"],
                        "addressEntity": ["a1"]}},
        {"id": "e2", "schema": "LegalEntity", "first_seen": "2022-01-03T00:00:00",
         "properties": {"name": ["ACME PAY LLC"], "sector": ["409"], "addressEntity": ["a1"]}},
        {"id": "e3", "schema": "LegalEntity", "first_seen": "2025-01-01T00:00:00",
         "properties": {"name": ["CORNER CHECKS INC"], "sector": ["408"], "address": ["5 ELM, AUSTIN, TX 73301"]}},
    ]
    recs, info = parse("\n".join(json.dumps(l) for l in lines).encode())
    by = {r.name: r for r in recs}
    assert len(recs) == 2
    a = by["ACME PAY LLC"]
    assert a.licence_type == "MSB – money transmitter" and a.city == "Miami" and a.status_date == "2022-01-03"
    assert "Dealer in foreign exchange" in a.services and a.extra["hq_state"] == "FL"
    assert by["CORNER CHECKS INC"].licence_type == "MSB – other activities"



def test_za_banks_wiki_parse():
    from regtracker.sources.za_wiki import parse_wikitext
    text = """This is a list of commercial banks in South Africa<ref>{{cite web|title=SARB}}</ref>
[[Absa Group Limited|Absa Bank]]
[[Capitec Bank]]
* [[Standard Bank]]
* [[FirstRand|FirstRand Bank]] ([[First National Bank (South Africa)|FNB]])
* [[TymeBank]]
* [[Albaraka Bank]]
* [[South African Reserve Bank]]
* [[Johannesburg]]
* [[GBS Mutual Bank]]
* Ditsobotla Primary Savings and Credit Co-operative Bank
[[File:Logo.png]]
== See also ==
* [[Banking in South Africa]]
"""
    names = [n for n, _ in parse_wikitext(text)]
    assert names == ["Absa Bank", "Capitec Bank", "Standard Bank", "FirstRand Bank", "TymeBank",
                     "Albaraka Bank", "GBS Mutual Bank", "Ditsobotla Primary Savings and Credit Co-operative Bank"]


def test_robots_rules_and_adla():
    from regtracker.http import rules_allow
    from regtracker.sources.southafrica import parse_adla
    sarb = ("# Group 1\nUser-agent: *\nDisallow: /content/dam/\nAllow: /content/dam/sarb/*.pdf\n"
            "Allow: /content/dam/sarb/*.xls*\nSitemap: https://www.resbank.co.za/en.sitemap.xml\n")
    base = "https://www.resbank.co.za"
    assert rules_allow(sarb, base + "/content/dam/sarb/what-we-do/x/public-list-tppp-13-10-2026.pdf")
    assert rules_allow(sarb, base + "/content/dam/sarb/a/List%20(1).xlsx")
    assert not rules_allow(sarb, base + "/content/dam/sarb/a/page.html")
    assert not rules_allow(sarb, base + "/content/dam/other/x.pdf")
    assert rules_allow(sarb, base + "/en/home/what-we-do")
    assert not rules_allow("User-agent: *\nDisallow: /", "https://msb.fincen.gov/retrieve.msb.list.php")
    assert rules_allow("User-agent: googlebot\nDisallow: /\n\nUser-agent: *\nAllow: /", "https://x.com/a")
    assert not rules_allow("User-agent: *\nDisallow: /*.pdf$", "https://x.com/a/b.pdf")
    assert rules_allow("User-agent: *\nDisallow: /*.pdf$", "https://x.com/a/b.pdf?x=1")

    lines = ["(A) Authorised Dealers in foreign exchange with limited authority ........................... 13",
             "(B) Authorised Dealers ............................................................ 14",
             "(A) Categories of Authorised Dealers in foreign exchange with limited authority ..... 16",
             "A.2 Authorised entities", "(A) Authorised Dealers in foreign exchange with limited authority",
             "The offices in South Africa of the under-mentioned entities are authorised to",
             "Name of entity - Authorised Dealer in foreign Category of", "exchange with limited authority appointment",
             "Border Forex (Pty) Limited Two", "Imali Express (Pty) Limited trading as Finex", "Two", "Exchange",
             "Access Forex (Pty) Limited Three (MTO)", "Kawena Exchange (Pty) Limited Three (VTSP)",
             "Terra Payment Services South Africa (RF) (Pty)", "Three (MTO)", "Limited", "13 of 94 20/2026",
             "Currency and Exchanges Manual for ADLAs Introduction", "Hello Paisa (Pty) Limited Four",
             "Southeast Exchange Company (South Africa) (Pty) Four", "Limited", "(B) Authorised Dealers",
             "ABSA Bank Limited"]
    recs = {r.name: r for r in parse_adla(lines, "u")}
    assert set(recs) == {"Border Forex (Pty) Limited", "Imali Express (Pty) Limited", "Access Forex (Pty) Limited",
                         "Kawena Exchange (Pty) Limited", "Terra Payment Services South Africa (RF) (Pty) Limited",
                         "Hello Paisa (Pty) Limited", "Southeast Exchange Company (South Africa) (Pty) Limited"}
    assert recs["Imali Express (Pty) Limited"].extra["trading_name"] == "Finex Exchange"
    assert recs["Kawena Exchange (Pty) Limited"].licence_type == "ADLA – Category 3 (value transfer service provider)"
    assert recs["Terra Payment Services South Africa (RF) (Pty) Limited"].licence_type == "ADLA – Category 3 (money transfer operator)"


def test_tppp_three_digit_rows():
    from regtracker.sources import southafrica as za
    recs = za.parse_tppp(["99 Absa Bank Limited 2008/019068/07 Abellard Software Services (Pty) Ltd X X X",
                          "100Absa Bank Limited 2012/010479/07 African Independent Brokers (Pty) Ltd X X",
                          "512FirstRand Bank Limited 2015/201375/07 African Payments Solutions (Pty) Ltd X"], "u")
    assert len(recs) == 3
    assert {r.extra["sponsoring_banks"] for r in recs} == {"Absa Bank Limited", "FirstRand Bank Limited"}


def test_telegram_filter_and_messages(monkeypatch, tmp_path):
    import logging
    from regtracker import notify
    ev = [
        {"event": "NEW_LICENCE", "event_label": "New licence", "jurisdiction": "GB", "name": "Acme <EMI> Ltd",
         "licence_type": "Authorised EMI", "regulator": "FCA", "register_url": "https://register.fca.org.uk/x"},
        {"event": "NEW_LICENCE", "event_label": "PSP application filed (Canada)", "jurisdiction": "CA", "name": "Pay Co",
         "licence_type": "PSP applicant (RPAA)", "regulator": "BoC", "register_url": ""},
        {"event": "TYPE_CHANGED", "event_label": "PSP registered (Canada)", "jurisdiction": "CA", "name": "Old PSP"},
        {"event": "NAME_CHANGED", "event_label": "Name changed", "jurisdiction": "GB", "name": "Renamed"},
        {"event": "LICENCE_LOST", "event_label": "Licence cancelled / revoked", "jurisdiction": "GB", "name": "Gone"},
    ]
    assert [e["name"] for e in notify.telegram_filter(ev, {})] == ["Acme <EMI> Ltd", "Pay Co", "Old PSP"]
    assert [e["name"] for e in notify.telegram_filter(ev, {"countries": ["gb"]})] == ["Acme <EMI> Ltd"]
    assert [e["name"] for e in notify.telegram_filter(ev, {"events": ["LICENCE_LOST"]})] == ["Gone"]
    msgs = notify.telegram_messages(notify.telegram_filter(ev, {}), "https://docs.google.com/x")
    assert len(msgs) == 1 and "🆕 3 new licences" in msgs[0] and "🇨🇦 <b>CA</b> (2)" in msgs[0]
    assert "Acme &lt;EMI&gt; Ltd</a> — Authorised EMI · FCA" in msgs[0] and "PSP application filed (Canada)" in msgs[0]
    many = [dict(ev[0], name=f"Firm {i}") for i in range(400)]
    msgs = notify.telegram_messages(many, "")
    assert all(len(m) <= 4096 for m in msgs) and "and 250 more" in msgs[-1]

    sent = []

    class R:
        status_code = 200
        text = ""
        def __init__(self, data=None): self.data = data or {}
        def json(self): return self.data
        def raise_for_status(self): pass
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("REGTRACKER_DATA", str(tmp_path))
    monkeypatch.setattr(notify.requests, "get", lambda url, timeout=0: R(
        {"result": [{"message": {"chat": {"id": 4242, "type": "private"}, "text": "hi"}}]}))
    monkeypatch.setattr(notify.requests, "post", lambda url, json=None, timeout=0: sent.append(json) or R())
    notify.send_telegram(ev, "", logging.getLogger("t"), {})
    assert sent and sent[0]["chat_id"] == "4242" and sent[0]["parse_mode"] == "HTML"
    assert (tmp_path / "state" / "telegram.json").exists()


def test_telegram_connect_confirms_once(monkeypatch, tmp_path):
    import logging
    from regtracker import notify
    sent = []

    class R:
        status_code = 200
        def __init__(self, data=None): self.data = data or {}
        def json(self): return self.data
        def raise_for_status(self): pass
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("REGTRACKER_DATA", str(tmp_path))
    updates = {"result": [{"message": {"chat": {"id": 77, "type": "private"}, "text": "hi"}}]}
    monkeypatch.setattr(notify.requests, "get", lambda url, timeout=0: R(updates))
    monkeypatch.setattr(notify.requests, "post", lambda url, json=None, timeout=0: sent.append(json) or R())
    assert notify.telegram_connect(logging.getLogger("t")) == "77" and len(sent) == 1
    assert "connected" in sent[0]["text"]
    updates["result"] = []                         # a day later Telegram has forgotten the "hi"
    assert notify.telegram_connect(logging.getLogger("t")) == "77" and len(sent) == 1


def test_telegram_token_cleanup(monkeypatch):
    from regtracker import notify
    tok = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    for raw in (tok, f" {tok}\n", f'"{tok}"', f"bot{tok}", f"https://api.telegram.org/bot{tok}/getMe"):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", raw)
        assert notify.telegram_token() == tok


def test_alaska_roster_columns_and_editions(tmp_path, monkeypatch):
    import logging
    from regtracker.sources import usstates as u
    from regtracker.sources.base import Context, SourceNotReady

    def w(text, x, top):
        return {"text": text, "x0": x, "top": top}
    header = [w("Filing", 10, 100), w("Description", 40, 100), w("DBA", 190, 100), w("/", 210, 100), w("Trade", 215, 100),
              w("Name", 240, 100), w("Business/Physical", 385, 100), w("Address", 450, 100), w("Filing", 570, 100),
              w("Status", 600, 100), w("File/License", 690, 100), w("#", 740, 100)]
    page = header + [
        w("Money", 10, 90), w("Transmitter", 40, 90),
        w("1Money", 10, 110), w("USA,", 50, 110), w("Inc.", 80, 110), w("575", 385, 110), w("5th", 405, 110),
        w("Avenue", 425, 110), w("Approved", 570, 110), w("018134", 690, 110),
        w("New", 385, 120), w("York,", 410, 120), w("NY", 440, 120), w("10017", 460, 120),
        w("AllPaid,", 10, 135), w("Inc.", 50, 135), w("GovPayNet", 190, 135), w("7820", 385, 135), w("Innovation", 410, 135),
        w("Approved", 570, 135), w("012824", 690, 135), w("Indianapolis,", 385, 145), w("IN", 450, 145), w("46278", 470, 145),
        w("American", 10, 160), w("Express", 60, 160), w("Prepaid", 100, 160), w("Card", 140, 160), w("18850", 385, 160),
        w("North", 415, 160), w("Approved", 570, 160), w("10048", 690, 160),
        w("Management", 10, 170), w("Corporation", 70, 170), w("Phoenix,", 385, 170), w("AZ", 430, 170), w("85054", 450, 170),
        w("MSB", 10, 190), w("USA", 35, 190), w("Inc", 60, 190), w("Ozan", 183, 190), w("1", 380, 190), w("Main", 392, 190),
        w("Approved", 566, 190), w("011130", 686, 190), w("Foster", 379, 200), w("City,", 410, 200), w("CA", 440, 200),
        w("94404", 460, 200), w("Zero", 10, 215), w("Hash", 40, 215), w("LLC", 70, 215), w("9", 385, 215),
        w("Approved", 570, 215), w("011078", 690, 215), w("End", 10, 230), w("of", 30, 230), w("Listing", 45, 230),
        w("Page", 690, 300), w("1", 715, 300), w("of", 725, 300), w("12", 735, 300)]
    rows = u.ak_rows([page])
    assert [r["lic"] for r in rows] == ["018134", "012824", "10048", "011130", "011078"]
    assert rows[3]["name"] == "MSB USA Inc" and rows[3]["dba"] == "Ozan" and rows[3]["addr"][-1] == "Foster City, CA 94404"
    assert rows[4]["name"] == "Zero Hash LLC"
    assert rows[2]["name"] == "American Express Prepaid Card Management Corporation"
    assert rows[2]["addr"] == ["18850 North", "Phoenix, AZ 85054"] and rows[1]["dba"] == "GovPayNet"

    class R:
        def __init__(self, code, content=b""):
            self.status_code, self.content = code, content
    calls = []
    monkeypatch.setattr(u, "robots_allowed", lambda s, url: True)
    src = u.USStateSource("ak", u.STATES["ak"])

    class S:
        def get(self, url, timeout=0, headers=None):
            calls.append(url)
            return R(200, b"%PDF roster") if "20260319" in url else R(404)
    ctx = Context({}, S(), "2026-11-08", tmp_path, set(), logging.getLogger("t"))
    url, raw = src._dated_pdf(ctx, u.STATES["ak"])
    assert url.endswith("MSB%20Roster%2020260319.pdf") and raw.startswith(b"%PDF")
    assert len(calls) == 9                                     # 8 recent dates looked for, then the known edition
    calls.clear()
    src._dated_pdf(ctx, u.STATES["ak"])
    assert len(calls) == 1                                     # the same day: only the known edition

    class Refused:
        def get(self, url, timeout=0, headers=None):
            calls.append(url)
            return R(403)
    ctx2 = Context({}, Refused(), "2026-11-09", tmp_path / "b", set(), logging.getLogger("t"))
    try:
        src._dated_pdf(ctx2, u.STATES["ak"])
        assert False
    except SourceNotReady as e:
        assert getattr(e, "quiet", False) and "off" in str(e)
    calls.clear()
    try:
        src._dated_pdf(ctx2, u.STATES["ak"])
        assert False
    except SourceNotReady as e:
        assert getattr(e, "quiet", False)
    assert calls == []                                         # refused once: never asked again


def test_florida_link_from_download_page(tmp_path, monkeypatch):
    import logging
    from regtracker.sources import usstates as u
    from regtracker.sources.base import Context

    class R:
        def __init__(self, text): self.text, self.status_code = text, 200
    page = ('<a href="https://real.flofr.gov/Public/CC/Check_Cashers_monthly.zip">Check cashers</a>'
            '<a href="https://real.flofr.gov/Public/MSB/Money%20Transmitters%20Monthly.zip">Money Transmitters</a>'
            '<a href="https://real.flofr.gov/Public/MSB/Money%20Transmitters%20Weekly.zip">weekly</a>')
    src = u.USStateSource("fl", u.STATES["fl"])
    monkeypatch.setattr(src, "_get", lambda c, url: R(page))
    ctx = Context({}, None, "2026-11-08", tmp_path, set(), logging.getLogger("t"))
    assert src.resolve_url(ctx) == "https://real.flofr.gov/Public/MSB/Money%20Transmitters%20Monthly.zip"
    monkeypatch.setattr(src, "_get", lambda c, url: R("<p>no links</p>"))
    assert src.resolve_url(ctx) == u.STATES["fl"]["url"]
