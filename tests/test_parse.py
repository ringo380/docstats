"""Tests for smart search query parsing."""

from docstats.parse import parse_query, build_interpretations, ParseResult


def test_simple_last_name():
    r = parse_query("Chen")
    assert r.last_name == "chen"
    assert r.first_name == ""
    assert r.specialty == ""
    assert not r.is_org


def test_first_and_last():
    r = parse_query("sarah chen")
    assert r.first_name == "sarah"
    assert r.last_name == "chen"


def test_strips_dr_prefix():
    r = parse_query("dr sarah chen")
    assert r.first_name == "sarah"
    assert r.last_name == "chen"
    assert r.honorific == "dr"


def test_strips_dr_dot_prefix():
    r = parse_query("dr. sarah chen")
    assert r.first_name == "sarah"
    assert r.last_name == "chen"


def test_strips_doctor_prefix():
    r = parse_query("Doctor James Park")
    assert r.first_name == "james"
    assert r.last_name == "park"


def test_strips_md_credential():
    r = parse_query("chen md")
    assert r.last_name == "chen"
    assert "md" in r.credentials


def test_detects_specialty():
    r = parse_query("sarah chen cardiology")
    assert r.first_name == "sarah"
    assert r.last_name == "chen"
    assert r.specialty == "Cardiology"


def test_ambiguous_do_stays_as_name():
    """'do' is both a credential and a common surname — keep as name by default."""
    r = parse_query("dr. kim do orthopedics")
    assert r.first_name == "kim"
    assert r.last_name == "do"
    assert r.specialty == "Orthopedic Surgery"
    assert "do" not in r.credentials


def test_detects_org():
    r = parse_query("Kaiser Permanente")
    assert r.is_org
    assert r.org_name == "Kaiser Permanente"


def test_detects_org_by_hospital_keyword():
    r = parse_query("UCSF Medical Center")
    assert r.is_org


def test_phd_stripped():
    r = parse_query("sarah chen phd cardiology")
    assert r.first_name == "sarah"
    assert r.last_name == "chen"
    assert "phd" in r.credentials
    assert r.specialty == "Cardiology"


def test_build_interpretations_full():
    """With first, last, and specialty — 4 interpretations in order."""
    r = ParseResult(first_name="kim", last_name="do", specialty="Orthopedic Surgery",
                    honorific="dr", credentials=[], is_org=False, org_name="")
    interps = build_interpretations(r)
    assert len(interps) == 4
    assert interps[0] == {"first_name": "Kim", "last_name": "Do",
                          "taxonomy_description": "Orthopedic Surgery"}
    assert interps[1] == {"last_name": "Kim", "taxonomy_description": "Orthopedic Surgery"}
    assert interps[2] == {"first_name": "Kim", "last_name": "Do"}
    assert interps[3] == {"last_name": "Kim"}


def test_build_interpretations_last_only():
    r = ParseResult(first_name="", last_name="chen", specialty="",
                    honorific="", credentials=[], is_org=False, org_name="")
    interps = build_interpretations(r)
    assert interps == [{"last_name": "Chen"}]


def test_build_interpretations_org():
    r = ParseResult(first_name="", last_name="", specialty="",
                    honorific="", credentials=[], is_org=True, org_name="Kaiser Permanente")
    interps = build_interpretations(r)
    assert interps == [{"organization_name": "Kaiser Permanente",
                        "enumeration_type": "NPI-2"}]


def test_build_interpretations_last_and_specialty():
    r = ParseResult(first_name="", last_name="lopez", specialty="Cardiology",
                    honorific="", credentials=[], is_org=False, org_name="")
    interps = build_interpretations(r)
    assert interps[0] == {"last_name": "Lopez", "taxonomy_description": "Cardiology"}
    assert interps[1] == {"last_name": "Lopez"}
