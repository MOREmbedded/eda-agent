# SPDX-License-Identifier: Apache-2.0
"""IPC standards catalogue and rule database.

The repository ships the catalogue and rule DEFINITIONS only; values come
from the user's licensed copy, per profile. These tests hold that line (no
value can sit in the shipped data, no values file can land in src/), the
provenance rules for profile values, and the hand-off to the fab rule
builder's class floor file. All numbers below are made up for the test.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from eda_agent.design import standards as st

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "eda_agent"
_DATA = _SRC / "design" / "standards_data"

_LICENSED = {"kind": "licensed_copy", "document": "IPC-6012F", "clause": "3.x.y"}


def _write_values(tmp_path: Path, values: list[dict]) -> Path:
    path = tmp_path / st.VALUES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 1, "values": values}), encoding="utf-8")
    return tmp_path


# --- catalogue ---------------------------------------------------------------

def test_catalogue_loads_with_priorities():
    cat = st.load_catalogue()
    assert len(cat.standards) == 54
    assert sum(s.priority == 1 for s in cat.standards) == 11
    assert cat.checked_against, "the revision check must name its pages"


def test_catalogue_revisions_confirmed_where_checked():
    cat = st.load_catalogue()
    assert cat.get("IPC-2221").label == "IPC-2221C-2023"
    assert cat.get("IPC-2221").revision_confirmed
    assert cat.get("IPC-A-610").label == "IPC-A-610J-2024"
    # Not confirmed on a page: must say so, not pass as checked.
    assert not cat.get("IPC-7352").revision_confirmed


def test_catalogue_rejects_duplicates():
    data = json.loads((_DATA / "catalogue.json").read_text(encoding="utf-8"))
    data["standards"].append(dict(data["standards"][0]))
    with pytest.raises(ValidationError, match="duplicate"):
        st.Catalogue.model_validate(data)


# --- rule definitions: no values ship -----------------------------------------

def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


@pytest.mark.parametrize("name", ["catalogue.json", "rules.json"])
def test_shipped_data_carries_no_values(name):
    data = json.loads((_DATA / name).read_text(encoding="utf-8"))
    forbidden = {"value", "values", "mils", "um", "min", "max", "classes"}
    assert not forbidden & set(_walk_keys(data))


def test_rule_model_refuses_a_value():
    rule = st.load_rules().rules[0].model_dump()
    rule["value"] = 1.0
    with pytest.raises(ValidationError):
        st.Rule.model_validate(rule)


def test_no_values_or_floor_files_in_src():
    leaked = [p for p in _SRC.rglob("*.json")
              if p.name in ("values.json", "ipc_class_floors.json")]
    assert leaked == []


def test_rules_cite_catalogued_standards():
    book = st.load_rules()
    cat = st.load_catalogue()
    assert len(book.rules) == 47
    for r in book.rules:
        assert all(cat.get(s) for s in r.standards), r.rule_id


def test_fab_keys_match_the_fab_rule_builder():
    fab_rules = pytest.importorskip("eda_agent.design.fab_rules")
    for r in st.load_rules().rules:
        if r.fab_key:
            assert r.fab_key in fab_rules.RULE_KEYS, r.rule_id


def test_fab_floor_must_be_mils_or_ratio():
    rule = st.load_rules().get("ipc.hole.wall_copper.avg.min").model_dump()
    rule["fab_key"] = "min_track"
    with pytest.raises(ValidationError, match="mils or a ratio"):
        st.Rule.model_validate(rule)


# --- profile values -------------------------------------------------------------

def test_absent_values_file_is_empty_and_valid(tmp_path):
    assert st.load_values(tmp_path) == {"ok": True, "values": [], "path": None,
                                        "warnings": []}
    assert st.load_values(None)["ok"]


def test_valid_values_load_and_resolve(tmp_path):
    d = _write_values(tmp_path, [
        {"rule_id": "ipc.annular_ring.pth.external.min", "key": {"class": "3"},
         "value": 1.5, "provenance": _LICENSED}])
    got = st.load_values(d)
    assert got["ok"], got
    hit = st.resolve("ipc.annular_ring.pth.external.min", {"class": 3}, got["values"])
    assert hit["status"] == "configured" and hit["value"] == 1.5
    assert hit["cite"] == "IPC-6012F 3.x.y"
    miss = st.resolve("ipc.annular_ring.pth.external.min", {"class": 2}, got["values"])
    assert miss["status"] == "not_configured"
    assert "IPC-6012" in miss["reason"]


@pytest.mark.parametrize("bad, why", [
    ({"rule_id": "ipc.nope", "key": {}, "value": 1, "provenance": _LICENSED}, "unknown rule"),
    ({"rule_id": "ipc.annular_ring.pth.external.min", "key": {"class": "4"}, "value": 1,
      "provenance": _LICENSED}, "class"),
    ({"rule_id": "ipc.annular_ring.pth.external.min", "key": {}, "value": 1,
      "provenance": _LICENSED}, "needs key"),
    ({"rule_id": "ipc.annular_ring.pth.external.min", "key": {"class": "2", "layer": "internal"},
      "value": 1, "provenance": _LICENSED}, "keyed by"),
    ({"rule_id": "ipc.annular_ring.pth.external.min", "key": {"class": "2"}, "value": "wide",
      "provenance": _LICENSED}, "number"),
    ({"rule_id": "ipc.via_protection.type", "key": {"class": "2", "via_use": "bga"}, "value": 3,
      "provenance": _LICENSED}, "text"),
])
def test_bad_values_fail_the_file(tmp_path, bad, why):
    got = st.load_values(_write_values(tmp_path, [bad]))
    assert not got["ok"]
    assert any(why in e for e in got["errors"]), got


@pytest.mark.parametrize("prov", [
    {"kind": "licensed_copy", "document": "IPC-6012F"},
    {"kind": "secondary", "url": "https://example.com", "quote": "q"},
    {"kind": "derived"},
    {"kind": "user_override"},
])
def test_incomplete_provenance_is_refused(tmp_path, prov):
    got = st.load_values(_write_values(tmp_path, [
        {"rule_id": "ipc.annular_ring.pth.external.min", "key": {"class": "2"},
         "value": 1, "provenance": prov}]))
    assert not got["ok"]


def test_duplicate_values_are_refused(tmp_path):
    v = {"rule_id": "ipc.annular_ring.pth.external.min", "key": {"class": "2"},
         "value": 1, "provenance": _LICENSED}
    got = st.load_values(_write_values(tmp_path, [v, v]))
    assert not got["ok"] and any("two values" in e for e in got["errors"])


# --- class floors for the fab rule builder ---------------------------------------

def test_class_floors_shape(tmp_path):
    d = _write_values(tmp_path, [
        {"rule_id": "ipc.annular_ring.pth.external.min", "key": {"class": "3"},
         "value": 1.5, "provenance": _LICENSED, "note": "test"},
        {"rule_id": "ipc.hole.aspect_ratio.max",
         "key": {"class": "3", "producibility_level": "B"}, "value": 8,
         "provenance": _LICENSED},
        {"rule_id": "ipc.hole.wall_copper.avg.min", "key": {"class": "3"},
         "value": 20, "provenance": _LICENSED},
    ])
    doc = st.class_floors(st.load_values(d)["values"])
    assert doc["source"]["standard"] == "IPC-6012F"
    floors = doc["classes"]["3"]
    # Only class-keyed rules with a fab key take part.
    assert set(floors) == {"pth_annular_ring"}
    assert floors["pth_annular_ring"]["mils"] == 1.5
    assert floors["pth_annular_ring"]["clause"] == "IPC-6012F 3.x.y"
    assert "2" not in doc["classes"]


def test_write_class_floors(tmp_path):
    assert not st.write_class_floors(tmp_path)["ok"]
    assert not (tmp_path / st.CLASS_FLOORS_FILE).exists()
    _write_values(tmp_path, [
        {"rule_id": "ipc.annular_ring.via.external.min", "key": {"class": "2"},
         "value": 1.0, "provenance": _LICENSED}])
    got = st.write_class_floors(tmp_path)
    assert got["ok"] and got["classes"] == ["2"]
    data = json.loads((tmp_path / st.CLASS_FLOORS_FILE).read_text(encoding="utf-8"))
    assert data["classes"]["2"]["min_annular_ring"]["mils"] == 1.0


def test_class_floors_feed_the_fab_rule_builder(tmp_path):
    fab_rules = pytest.importorskip("eda_agent.design.fab_rules")
    _write_values(tmp_path, [
        {"rule_id": "ipc.annular_ring.via.external.min", "key": {"class": "2"},
         "value": 1.0, "provenance": _LICENSED}])
    st.write_class_floors(tmp_path)
    got = fab_rules.ipc_floors(2, tmp_path / st.CLASS_FLOORS_FILE)
    assert got["ok"] and got["floors"]["min_annular_ring"]["mils"] == 1.0


def test_coverage_lists_every_rule():
    rows = st.coverage([])
    assert len(rows) == len(st.load_rules().rules)
    assert all(r["values"] == 0 for r in rows)


# --- revisions -------------------------------------------------------------------

def test_current_revision_cites_cleanly(tmp_path):
    got = st.load_values(_write_values(tmp_path, [
        {"rule_id": "ipc.annular_ring.pth.external.min", "key": {"class": "2"},
         "value": 1, "provenance": _LICENSED}]))
    assert got["ok"] and got["warnings"] == []


@pytest.mark.parametrize("document, why", [
    ("IPC-6012E", "current revision is IPC-6012F"),
    ("IPC-6012", "current revision is IPC-6012F"),
    ("IPC-9999A", "not in the catalogue"),
])
def test_other_revisions_warn(tmp_path, document, why):
    prov = dict(_LICENSED, document=document)
    got = st.load_values(_write_values(tmp_path, [
        {"rule_id": "ipc.annular_ring.pth.external.min", "key": {"class": "2"},
         "value": 1, "provenance": prov}]))
    assert got["ok"]
    assert len(got["warnings"]) == 1 and why in got["warnings"][0]


def test_longest_catalogue_id_wins(monkeypatch):
    # A catalogue id that prefixes another (IPC-A-6 / IPC-A-600) must not
    # capture the longer one's documents.
    cat = st.load_catalogue().model_copy(deep=True)
    short = cat.get("IPC-A-600").model_copy(update={"id": "IPC-A-6", "revision": "Z"})
    cat.standards.append(short)
    monkeypatch.setattr(st, "load_catalogue", lambda: cat)
    rv = st.RuleValue.model_validate(
        {"rule_id": "ipc.annular_ring.breakout.allowed",
         "key": {"class": "2", "layer": "external"}, "value": "none",
         "provenance": dict(_LICENSED, document="IPC-A-600M")})
    assert st.revision_warnings([rv]) == []


# --- CLI ----------------------------------------------------------------------------

def _cli(monkeypatch, tmp_path, argv):
    from eda_agent import server
    if tmp_path is not None:
        monkeypatch.setenv("EDA_AGENT_PROFILE_DIR", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["eda-agent", *argv])
    return server.main()


def test_cli_std_list(monkeypatch, capsys):
    assert _cli(monkeypatch, None, ["std-list", "--priority", "1", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["standards"]) == 11


def test_cli_std_rules_counts_profile_values(monkeypatch, tmp_path, capsys):
    _write_values(tmp_path, [
        {"rule_id": "ipc.annular_ring.via.external.min", "key": {"class": "3"},
         "value": 1.0, "provenance": dict(_LICENSED, document="IPC-6012E")}])
    assert _cli(monkeypatch, tmp_path, ["std-rules", "--category", "annular_ring",
                                        "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    rows = {r["rule_id"]: r for r in data["rules"]}
    assert rows["ipc.annular_ring.via.external.min"]["values"] == 1
    assert all(r["category"] == "annular_ring" for r in data["rules"])
    assert data["warnings"], "an old revision must be reported"


def test_cli_std_rules_refuses_a_bad_values_file(monkeypatch, tmp_path):
    _write_values(tmp_path, [{"rule_id": "ipc.nope", "key": {}, "value": 1,
                              "provenance": _LICENSED}])
    assert _cli(monkeypatch, tmp_path, ["std-rules"]) == 1


def test_cli_std_floors_writes_the_file(monkeypatch, tmp_path):
    assert _cli(monkeypatch, tmp_path, ["std-floors"]) == 1  # nothing configured
    _write_values(tmp_path, [
        {"rule_id": "ipc.annular_ring.via.external.min", "key": {"class": "2"},
         "value": 1.0, "provenance": _LICENSED}])
    assert _cli(monkeypatch, tmp_path, ["std-floors"]) == 0
    assert (tmp_path / st.CLASS_FLOORS_FILE).is_file()
