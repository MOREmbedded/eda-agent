# SPDX-License-Identifier: Apache-2.0
"""sdk-probe refuses to read across a script version gap.

The probe block is regenerated per deploy. A property list built from
today's symbol table, sent to yesterday's running script, can name a
member the old block still reads with a spelling the engine does not
resolve - which raises "Undeclared identifier" and halts the script
debugger, where Try/Except cannot reach it and Altium will not close
the document until the script is stopped.

That has happened twice for real, so the gap is refused rather than
narrated.
"""

from __future__ import annotations

import pytest

from eda_agent import bridge as bridge_module
from eda_agent import cli
from eda_agent.diag import doctor


@pytest.fixture
def no_bridge(monkeypatch):
    """Nothing should reach Altium in these tests.

    cmd_sdk_probe imports get_bridge from .bridge inside the function,
    so the name to replace lives on that module, not on cli.
    """
    def explode(*_a, **_k):
        raise AssertionError("the guard let a command through")
    monkeypatch.setattr(bridge_module, "get_bridge", explode)


def test_a_version_gap_is_refused(monkeypatch, capsys, no_bridge):
    monkeypatch.setattr(doctor, "_ping_altium",
                        lambda: {"script_version": "2026.10.27.4"})
    monkeypatch.setattr(doctor, "bundled_script_version",
                        lambda: "2026.10.27.6")

    assert cli.cmd_sdk_probe(properties="AliasAsText") == 1
    err = capsys.readouterr().err
    assert "2026.10.27.4" in err and "2026.10.27.6" in err
    assert "StartMCPServer" in err


def test_a_silent_loop_is_named_as_such(monkeypatch, capsys, no_bridge):
    monkeypatch.setattr(doctor, "_ping_altium", lambda: None)
    assert cli.cmd_sdk_probe(properties="AliasAsText") == 1
    assert "polling loop is not running" in capsys.readouterr().err


def test_matching_versions_are_not_refused(monkeypatch, capsys):
    """The guard must not become the thing that blocks every probe."""
    monkeypatch.setattr(doctor, "_ping_altium",
                        lambda: {"script_version": "2026.10.27.6"})
    monkeypatch.setattr(doctor, "bundled_script_version",
                        lambda: "2026.10.27.6")

    sent = {}

    class Bridge:
        async def send_command_async(self, command, params, timeout=0.0):
            sent["command"] = command
            sent["params"] = params
            return {"designator": "R1", "interface": "ISch_Component",
                    "results": [{"name": "AliasAsText", "exposed": True,
                                 "status": "exposed", "value": "x"}]}

    monkeypatch.setattr(bridge_module, "get_bridge", lambda: Bridge())
    assert cli.cmd_sdk_probe(properties="AliasAsText") == 0
    assert sent["command"] == "generic.probe_sdk_properties"
    assert sent["params"]["properties"] == "AliasAsText"


# --- aiming the probe at something other than a component ----------------

def _bridge(monkeypatch, sent):
    class Bridge:
        async def send_command_async(self, command, params, timeout=0.0):
            sent["command"] = command
            sent["params"] = params
            return {"designator": "", "interface": "ISch_Pin",
                    "results": [{"name": "Owner", "exposed": True,
                                 "status": "exposed", "value": "U1"}]}

    monkeypatch.setattr(bridge_module, "get_bridge", lambda: Bridge())
    monkeypatch.setattr(doctor, "_ping_altium",
                        lambda: {"script_version": "x"})
    monkeypatch.setattr(doctor, "bundled_script_version", lambda: "x")


def test_the_object_kind_and_index_reach_the_handler(monkeypatch):
    """The planner handled all 37 schematic types all along; only the CLI
    and the Pascal were wired to components."""
    sent: dict = {}
    _bridge(monkeypatch, sent)
    assert cli.cmd_sdk_probe(properties="Owner", object_type="pin",
                             index=3) == 0
    assert sent["params"]["object_type"] == "pin"
    assert sent["params"]["index"] == "3"


def test_an_unknown_object_kind_is_refused_by_name(monkeypatch, capsys,
                                                   no_bridge):
    monkeypatch.setattr(doctor, "_ping_altium",
                        lambda: {"script_version": "x"})
    monkeypatch.setattr(doctor, "bundled_script_version", lambda: "x")
    assert cli.cmd_sdk_probe(object_type="sprocket") == 1
    assert "sprocket" in capsys.readouterr().err


def test_the_probe_iterates_the_kind_it_was_asked_for():
    """It filtered on eSchComponent whatever it was asked for, so the ten
    names declared on ISch_GraphicalObject itself could only ever be
    measured through a component."""
    import re
    from pathlib import Path

    from tests.pascal_source import load

    generic = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "Generic.pas"
    body = re.sub(r"\{[^}]*\}", "", load(generic)["Gen_ProbeSdkProperties"])
    assert "Kind := ObjectTypeFromString(TypeStr);" in body
    assert "Iter.AddFilter_ObjectSet(MkSet(Kind));" in body
    assert "MkSet(eSchComponent)" not in body


def test_a_designator_only_selects_a_component():
    """Every other kind has none, and a designator passed with one of them
    would silently match nothing."""
    import re
    from pathlib import Path

    from tests.pascal_source import load

    generic = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "Generic.pas"
    body = re.sub(r"\{[^}]*\}", "", load(generic)["Gen_ProbeSdkProperties"])
    assert "If (Designator <> '') And (Kind = eSchComponent) Then" in body


def test_a_non_component_is_probed_against_the_shared_base_block():
    """GetSchComponentGenerated is the component chain; the other kinds
    have only the base one, and asking the component chain for them would
    report every name missing."""
    import re
    from pathlib import Path

    from tests.pascal_source import load

    generic = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "Generic.pas"
    body = re.sub(r"\{[^}]*\}", "", load(generic)["Gen_ProbeSdkProperties"])
    component = body.index("GetSchComponentGenerated(Target, PropName, Found)")
    shared = body.index("GetSchGraphicalGenerated(Target, PropName, Found)")
    assert body.index("If Kind = eSchComponent Then") < component < shared
    assert "If Not Found Then" in body
