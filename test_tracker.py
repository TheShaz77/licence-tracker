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
