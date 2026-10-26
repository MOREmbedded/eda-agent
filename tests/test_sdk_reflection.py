# SPDX-License-Identifier: Apache-2.0
"""Altium's SDK assembly, read as data, and what it says about coverage.

The generic property layer is four hand-written If-chains. Until now
the only way to state how much of Altium they reach was to read the
documentation and compare by eye, which produced guesses that moved
every time someone counted. Altium.SDK.Interfaces.dll declares every
interface and every property the scripting engine exposes, so the
question has an answer.

These tests run WITHOUT Altium installed. The model is exercised
against a small synthetic assembly dump, and the chain parsing against
the real Pascal sources, which are in the repository. The one test that
needs a reflected SDK skips when there is none, because CI has no
Altium and a machine without one must still be able to run the suite.
"""

from __future__ import annotations

import json

import pytest

from eda_agent.sdk import load_sdk, sdk_json_path
from eda_agent.sdk.reflect import SdkModel, SdkUnavailable
from eda_agent.sdk import coverage as cov


# --- a synthetic assembly, shaped exactly like the real dump ---------------

FAKE = {
    "schema": 1,
    "source": r"C:\fake\Altium.SDK.Interfaces.dll",
    "assembly": "Altium.SDK.Interfaces, Version=1.0.0.0",
    "file_version": "1.0.0.0",
    "skipped_methods": 2,
    "interfaces": {
        "SCH.ISch_BasicContainer": {
            "name": "ISch_BasicContainer",
            "namespace": "SCH",
            "inherits": [],
            "properties": {
                "OwnerDocument": {"name": "OwnerDocument", "get": "ISch_Document"},
                "Selection": {"name": "Selection", "get": "Boolean",
                              "set": "Boolean"},
            },
            "methods": [],
        },
        "SCH.ISch_GraphicalObject": {
            "name": "ISch_GraphicalObject",
            "namespace": "SCH",
            "inherits": ["SCH.ISch_BasicContainer"],
            "properties": {
                "Location": {"name": "Location", "get": "TLocation",
                             "set": "TLocation"},
                "Color": {"name": "Color", "get": "Int32", "set": "Int32"},
                # Declared here and overridden below, to pin precedence.
                "Orientation": {"name": "Orientation", "get": "Int32"},
            },
            "methods": [{"name": "MoveByXY", "ret": "Void",
                         "params": [{"name": "dx", "type": "Int32"}]}],
        },
        "SCH.ISch_Component": {
            "name": "ISch_Component",
            "namespace": "SCH",
            "inherits": ["SCH.ISch_GraphicalObject", "SCH.ISch_BasicContainer"],
            "properties": {
                "LibReference": {"name": "LibReference", "get": "String",
                                 "set": "String"},
                "AliasAt": {"name": "AliasAt", "get": "String",
                            "get_index": ["Int32"], "set": "String",
                            "set_index": ["Int32"]},
                "AliasCount": {"name": "AliasCount", "get": "Int32"},
                "PartCount": {"name": "PartCount", "set": "Int32"},
                "Orientation": {"name": "Orientation", "get": "Int32",
                                "set": "Int32"},
            },
            "methods": [],
        },
        # A cycle. The assembly is generated and should never contain one,
        # but a model that hangs on bad input is worse than one that does not.
        "SCH.ISch_Loop": {
            "name": "ISch_Loop", "namespace": "SCH",
            "inherits": ["SCH.ISch_LoopBack"], "properties": {}, "methods": [],
        },
        "SCH.ISch_LoopBack": {
            "name": "ISch_LoopBack", "namespace": "SCH",
            "inherits": ["SCH.ISch_Loop"], "properties": {}, "methods": [],
        },
    },
}


@pytest.fixture
def model() -> SdkModel:
    return SdkModel(json.loads(json.dumps(FAKE)))


# --- the model ------------------------------------------------------------

def test_an_interface_is_found_by_short_or_full_name(model):
    assert model.get("ISch_Component") is model.get("SCH.ISch_Component")
    assert model.get("ISch_Nothing") is None
    assert "ISch_Component" in model


def test_a_property_knows_which_directions_it_allows(model):
    props = model.get("ISch_Component").properties
    assert props["LibReference"].readable and props["LibReference"].writable
    # Read-only and write-only both occur, and conflating them would
    # generate a setter for a property Altium does not let you set.
    assert props["AliasCount"].readable and not props["AliasCount"].writable
    assert props["PartCount"].writable and not props["PartCount"].readable


def test_an_indexed_property_is_marked_as_one(model):
    props = model.get("ISch_Component").properties
    assert props["AliasAt"].indexed
    assert props["AliasAt"].get_index == ("Int32",)
    assert not props["LibReference"].indexed


def test_inheritance_is_resolved_transitively(model):
    names = [i.name for i in model.ancestors("ISch_Component")]
    assert names == ["ISch_GraphicalObject", "ISch_BasicContainer"]


def test_effective_properties_include_every_ancestor(model):
    eff = model.effective_properties("ISch_Component")
    assert "LibReference" in eff        # its own
    assert "Location" in eff            # ISch_GraphicalObject
    assert "OwnerDocument" in eff       # ISch_BasicContainer


def test_a_nearer_declaration_wins_over_an_inherited_one(model):
    """What the compiler resolves to is what the model must report.

    ISch_GraphicalObject declares Orientation read-only; ISch_Component
    redeclares it writable. A merge in the wrong order would report a
    component orientation as unsettable, and the generator would then
    not emit the setter that Altium does accept.
    """
    eff = model.effective_properties("ISch_Component")
    assert eff["Orientation"].writable


def test_a_cycle_in_the_declarations_terminates(model):
    assert [i.name for i in model.ancestors("ISch_Loop")] == ["ISch_LoopBack"]


def test_an_unknown_interface_has_no_properties_rather_than_raising(model):
    assert model.effective_properties("ISch_Imaginary") == {}
    assert model.ancestors("ISch_Imaginary") == []


def test_losses_during_reflection_are_carried_not_hidden(model):
    """A consumer must be able to tell a complete picture from a partial one."""
    assert model.skipped_methods == 2


# --- absence --------------------------------------------------------------

def test_a_missing_dump_says_how_to_produce_one(tmp_path):
    with pytest.raises(SdkUnavailable) as caught:
        load_sdk(tmp_path / "nothing.json")
    message = str(caught.value)
    assert "altium_sdk_reflect.ps1" in message
    assert "reference/" in message


def test_the_env_override_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_SDK_JSON", str(tmp_path / "elsewhere.json"))
    assert sdk_json_path() == tmp_path / "elsewhere.json"
    # An explicit argument still wins, so a caller is never stuck with it.
    assert sdk_json_path(tmp_path / "given.json") == tmp_path / "given.json"


def test_a_utf8_bom_does_not_break_the_read(tmp_path):
    """Windows PowerShell's Out-File -Encoding utf8 writes a BOM.

    That is how the dump is produced on the machine that has Altium, so
    a reader that cannot take a BOM cannot read the real file.
    """
    path = tmp_path / "sdk.json"
    path.write_text(json.dumps(FAKE), encoding="utf-8-sig")
    assert len(load_sdk(path)) == len(FAKE["interfaces"])


# --- parsing our own Pascal ----------------------------------------------

def test_every_chain_is_found_and_is_not_empty():
    """Renaming a chain must fail here, not silently report 0% coverage."""
    for name in cov.CHAINS:
        handled = cov.handled_names(name)
        assert len(handled) > 20, f"{name} parsed as only {len(handled)} names"


def test_a_path_property_counts_as_reaching_its_head_member():
    """Designator.Text is our syntax over the SDK member Designator."""
    heads = cov.handled_heads("GetSchProperty")
    names = cov.handled_names("GetSchProperty")
    assert "Designator.Text" in names
    assert "Designator" in heads
    assert "Vertex.1.X" in names and "Vertex" in heads


def test_both_object_type_resolvers_are_parsed():
    sch = cov.supported_sch_types()
    pcb = cov.supported_pcb_types()
    assert sch["component"] == "eSchComponent"
    assert sch["port"] == "ePort"
    assert pcb["pad"] == "ePadObject"
    assert len(sch) > 30 and len(pcb) > 20


def test_the_interface_name_is_mechanical_except_where_it_is_not():
    assert cov.interface_for("eRectangle", "SCH") == "ISch_Rectangle"
    assert cov.interface_for("eSchComponent", "SCH") == "ISch_Component"
    assert cov.interface_for("ePadObject", "PCB") == "IPCB_Pad"
    assert cov.interface_for("eViaObject", "PCB") == "IPCB_Via"


def test_coverage_against_a_synthetic_sdk(model, monkeypatch):
    """The arithmetic, without needing a real assembly.

    ISch_Component has five own properties plus five inherited, and the
    chain reaches Location, Color, Selection, Orientation and
    LibReference among them.
    """
    monkeypatch.setattr(cov, "supported_sch_types",
                        lambda: {"component": "eSchComponent"})
    monkeypatch.setattr(cov, "supported_pcb_types", lambda: {})
    report = cov.build_report(model)

    assert len(report.types) == 1
    entry = report.types[0]
    assert entry.resolved and entry.interface == "ISch_Component"
    assert "LibReference" in entry.read_covered
    assert "AliasCount" in entry.missing_reads
    reached, declared = report.totals()
    assert 0 < reached < declared


def test_an_unmappable_type_is_reported_not_counted_as_a_gap(model, monkeypatch):
    """A name the assembly does not declare is a mapping bug, not a hole.

    Counting it as uncovered would inflate the gap with properties that
    do not exist, and hide the fact that the mapping needs fixing.
    """
    monkeypatch.setattr(cov, "supported_sch_types",
                        lambda: {"imaginary": "eImaginary"})
    monkeypatch.setattr(cov, "supported_pcb_types", lambda: {})
    report = cov.build_report(model)

    assert [t.type_name for t in report.unresolved()] == ["imaginary"]
    assert report.totals() == (0, 0)


# --- against the real thing, when this machine has one --------------------

def _real_sdk():
    try:
        return load_sdk()
    except SdkUnavailable:
        return None


@pytest.mark.skipif(_real_sdk() is None,
                    reason="no reflected Altium SDK on this machine")
def test_the_real_report_maps_every_supported_type():
    """Every object type we accept must name an interface that exists.

    An unresolved one means our name for the type is wrong or Altium
    renamed the interface, and either way the coverage figure for that
    type is missing rather than low.
    """
    report = cov.build_report()
    assert report.unresolved() == [], (
        "no SDK interface resolved for: "
        + ", ".join(f"{t.type_name} -> {t.interface}"
                    for t in report.unresolved()))

    reached, declared = report.totals()
    assert declared > 1500, f"only {declared} properties declared; dump looks partial"
    assert reached > 0
    text = cov.format_report(report)
    assert "declared properties reachable" in text
