# SPDX-License-Identifier: Apache-2.0
"""eng_eco_preview_live: builds eng_eco_preview's sch/pcb views from
eda-agent's existing reads, tested against synthetic payloads shaped
exactly like the documented bridge responses (project.get_bom,
pcb.get_components, pcb.get_pad_properties).

UNVERIFIED against live Altium (no open project was available while
porting this); see docs/ENGINEERING_TOOLS.md for the procedure to
verify before relying on it for anything that gates an actual ECO.
"""
from __future__ import annotations

import pytest

from eda_agent.tools.engineering import register_engineering_tools
from eda_agent.tools.registry import ToolRegistry


class _CannedBridge:
    """Answers each bridge command from a fixed map; a value may be a
    plain dict or a callable(params) -> dict for commands whose answer
    depends on the call (pagination)."""

    def __init__(self, responses: dict):
        self.responses = dict(responses)
        self.calls: list[tuple[str, dict]] = []

    async def send_command_async(self, command, params=None, **kwargs):
        self.calls.append((command, dict(params or {})))
        if command not in self.responses:
            raise AssertionError(f"no canned response for {command!r}")
        value = self.responses[command]
        return value(params or {}) if callable(value) else value


def _tool(name):
    registry = ToolRegistry()
    register_engineering_tools(registry, altium=True)
    return registry.get(name).fn


def _install(monkeypatch, tmp_path, bridge):
    from tests.conftest import install_bridge_fake
    install_bridge_fake(monkeypatch, tmp_path, bridge)


def _eco_responses(**overrides):
    base = {
        "project.get_bom": {
            "components": [{
                "designator": "R1", "comment": "10k", "footprint": "RESC1608X55N",
                "lib_ref": "RES", "pins": [
                    {"pin": "1", "name": "1", "net": "SIG"},
                    {"pin": "2", "name": "2", "net": "GND"}]}],
            "count": 1, "limit": 5000, "truncated": False,
        },
        "pcb.get_components": {
            "components": [{"designator": "R1", "x": 0, "y": 0, "rotation": 0,
                            "layer": "Top", "footprint": "RESC1608X55N"}],
            "count": 1,
        },
        "pcb.get_pad_properties": {
            "pads": [
                {"name": "1", "component": "R1", "x": 0, "y": 0, "net": "SIG"},
                {"name": "2", "component": "R1", "x": 10, "y": 0, "net": "GND"}],
            "count": 2, "returned": 2, "total_matching": 2,
            "total_pads_on_board": 2, "offset": 0, "limit": 5000,
            "truncated": False, "next_offset": 2,
        },
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_eco_preview_live_reports_in_sync_for_a_matching_board(monkeypatch, tmp_path):
    bridge = _CannedBridge(_eco_responses())
    _install(monkeypatch, tmp_path, bridge)

    result = await _tool("eng_eco_preview_live")()
    assert result["ok"] and result["in_sync"]


@pytest.mark.asyncio
async def test_eco_preview_live_catches_a_footprint_change(monkeypatch, tmp_path):
    responses = _eco_responses(**{"pcb.get_components": {
        "components": [{"designator": "R1", "x": 0, "y": 0, "rotation": 0,
                        "layer": "Top", "footprint": "RESC2012X55N"}],
        "count": 1}})
    bridge = _CannedBridge(responses)
    _install(monkeypatch, tmp_path, bridge)

    result = await _tool("eng_eco_preview_live")()
    assert result["ok"] and not result["in_sync"]
    assert result["footprint_changes"] == ["R1"]


@pytest.mark.asyncio
async def test_eco_preview_live_refuses_a_truncated_bom_rather_than_diff_partial_data(
        monkeypatch, tmp_path):
    responses = _eco_responses(**{"project.get_bom": {
        "components": [], "count": 5000, "limit": 5000, "truncated": True}})
    bridge = _CannedBridge(responses)
    _install(monkeypatch, tmp_path, bridge)

    result = await _tool("eng_eco_preview_live")(bom_limit=5000)
    assert result["ok"] is False
    assert "bom_limit" in result["reason"]
    # must refuse before ever reading the PCB side
    assert all(c != "pcb.get_components" for c, _ in bridge.calls)


@pytest.mark.asyncio
async def test_eco_preview_live_pages_through_pad_properties(monkeypatch, tmp_path):
    def _paged(params):
        offset = int(params["offset"])
        if offset == 0:
            return {"pads": [{"name": "1", "component": "R1", "x": 0, "y": 0, "net": "SIG"}],
                    "truncated": True, "next_offset": 1}
        return {"pads": [{"name": "2", "component": "R1", "x": 10, "y": 0, "net": "GND"}],
                "truncated": False}

    bridge = _CannedBridge(_eco_responses(**{"pcb.get_pad_properties": _paged}))
    _install(monkeypatch, tmp_path, bridge)

    result = await _tool("eng_eco_preview_live")()
    assert result["ok"] and result["in_sync"], (
        "both pages must be collected for the pcb side to match the sch side")
    pad_calls = [p for c, p in bridge.calls if c == "pcb.get_pad_properties"]
    assert [p["offset"] for p in pad_calls] == ["0", "1"]
