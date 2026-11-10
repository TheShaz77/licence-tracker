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
