# SPDX-License-Identifier: Apache-2.0
"""pcb_check_fab_output: the live-read half of fab verification.

The reading and comparison are covered in test_gerber_check.py. What
matters here is the glue: that board data is read from the commands
that actually exist, that a failure on any of them degrades to "that
check did not run" rather than to a false pass, and that the tool
reports instead of throwing.
"""
from __future__ import annotations

import asyncio
import warnings

import pytest

from eda_agent.bridge.exceptions import AltiumError
from eda_agent.tools import pcb as pcb_module
from eda_agent.tools.registry import ToolRegistry

pytest.importorskip("gerbonara", reason="needs the [gerber] extra")

OUTLINE = """%FSLAX46Y46*%
%MOMM*%
%ADD10C,0.200000*%
D10*
X0Y0D02*
X20000000Y0D01*
X20000000Y15000000D01*
X0Y15000000D01*
X0Y0D01*
M02*
"""
COPPER = ("%FSLAX46Y46*%\n%MOMM*%\n%ADD10C,0.250000*%\nD10*\n"
          "X1000000Y1000000D03*\nM02*\n")
DRILL = "M48\nMETRIC,TZ\nT1C0.300\n%\nT1\nX5.0Y5.0\nX10.0Y5.0\nM30\n"

BOARD_REPLIES = {
    "pcb.get_layer_stackup": {"layers": [
        {"name": "Top Layer", "type": "Signal"},
        {"name": "Bottom Layer", "type": "Signal"},
        {"name": "Dielectric 1", "type": "Core"},
    ]},
    "pcb.get_board_outline": {"width_mm": 20.0, "height_mm": 15.0},
    "pcb.get_vias": {"vias": [{"x": 1, "y": 1}]},
    "pcb.get_pad_properties": {"pads": [
        {"name": "1", "hole_size": 0.3},
        {"name": "2", "hole_size": 0.0},
    ]},
}


class _Bridge:
    def __init__(self, replies=None, fail_on=()):
        self.calls = []
        self._replies = BOARD_REPLIES if replies is None else replies
        self._fail_on = set(fail_on)

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append(command)
        if command in self._fail_on:
            raise AltiumError(f"{command} failed")
        return self._replies.get(command, {})


@pytest.fixture
def package(tmp_path):
    warnings.simplefilter("ignore")
    for name in ("GTL", "GBL", "GTO", "GTS"):
        (tmp_path / f"Board.{name}").write_text(COPPER, encoding="utf-8")
    (tmp_path / "Board.GKO").write_text(OUTLINE, encoding="utf-8")
    (tmp_path / "Board.TXT").write_text(DRILL, encoding="utf-8")
    return tmp_path


def _tool(monkeypatch, bridge):
    monkeypatch.setattr(pcb_module, "get_bridge", lambda: bridge)
    registry = ToolRegistry()
    pcb_module.register_pcb_tools(registry)
    return registry.get("pcb_check_fab_output").fn


def test_a_package_matching_the_board_passes(package, monkeypatch):
    bridge = _Bridge()
    result = asyncio.run(_tool(monkeypatch, bridge)(str(package)))
    assert result["ok"] and result["passed"], result.get("findings")
    assert result["board"]["copper_layers"] == 2, "the core ply is not copper"
    assert result["board"]["outline_mm"] == {"width": 20.0, "height": 15.0}
    assert result["board"]["hole_count"] == 2, "one via plus one drilled pad"


def test_every_board_read_goes_to_a_command_that_exists(package, monkeypatch):
    bridge = _Bridge()
    asyncio.run(_tool(monkeypatch, bridge)(str(package)))
    assert set(bridge.calls) == {
        "pcb.get_layer_stackup", "pcb.get_board_outline",
        "pcb.get_vias", "pcb.get_pad_properties"}


def test_a_stackup_mismatch_is_reported(package, monkeypatch):
    replies = {**BOARD_REPLIES, "pcb.get_layer_stackup": {"layers": [
        {"name": f"L{i}", "type": "Signal"} for i in range(4)]}}
    result = asyncio.run(_tool(monkeypatch, _Bridge(replies))(str(package)))
    assert result["passed"] is False
    assert any(f["check"] == "copper_layers" for f in result["findings"])


def test_a_failed_board_read_skips_that_check_rather_than_passing_it(
        package, monkeypatch):
    """A check that could not run must never look like a check that passed."""
    bridge = _Bridge(fail_on={"pcb.get_layer_stackup"})
    result = asyncio.run(_tool(monkeypatch, bridge)(str(package)))
    assert result["ok"] is True
    assert "copper_layers" not in result["checks_run"]
    assert "outline" in result["checks_run"], "the other reads still ran"


def test_every_board_read_failing_is_said_out_loud(package, monkeypatch):
    bridge = _Bridge(fail_on=set(BOARD_REPLIES))
    result = asyncio.run(_tool(monkeypatch, bridge)(str(package)))
    assert result["board"] == {}
    assert "no board data" in result["note"]


def test_fab_minimums_are_only_checked_when_given(package, monkeypatch):
    plain = asyncio.run(_tool(monkeypatch, _Bridge())(str(package)))
    assert "min_drill" not in plain["checks_run"]

    strict = asyncio.run(_tool(monkeypatch, _Bridge())(
        str(package), min_drill_mm=0.5))
    assert "min_drill" in strict["checks_run"]
    assert any(f["check"] == "min_drill" for f in strict["findings"])


def test_a_missing_directory_is_refused_without_touching_altium(monkeypatch):
    bridge = _Bridge()
    result = asyncio.run(_tool(monkeypatch, bridge)("no/such/folder"))
    assert result["ok"] is False and "not a directory" in result["reason"]
    assert bridge.calls == [], "it did not read the board for a package it cannot open"


def test_the_tool_reports_rather_than_raising(package, monkeypatch):
    """Altium tools refuse or report; they do not throw at the caller."""
    bridge = _Bridge(fail_on=set(BOARD_REPLIES))
    result = asyncio.run(_tool(monkeypatch, bridge)(str(package)))
    assert isinstance(result, dict) and result["ok"] is True
