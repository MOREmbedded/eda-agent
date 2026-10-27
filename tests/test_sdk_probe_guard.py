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
