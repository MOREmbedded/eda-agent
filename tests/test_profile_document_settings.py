# SPDX-License-Identifier: Apache-2.0
"""profile_apply_document_settings: the profile's schematic units and grid
reaching the existing sch_set_units / obj_set_grid bridge commands.

Every value this tool reads is hand-written in a profile file, so the
checks below are mostly about what happens when one of them is wrong:
an unknown unit name would otherwise fail in Pascal with nothing
pointing back at the typo, and a non-numeric grid reached int().
"""
from __future__ import annotations

import asyncio
import json

import pytest

from eda_agent.tools import profile as profile_tools


class _Bridge:
    def __init__(self):
        self.calls = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        return {"success": True}


@pytest.fixture
def apply_tool(monkeypatch):
    bridge = _Bridge()
    import eda_agent.bridge as bridge_pkg
    monkeypatch.setattr(bridge_pkg, "get_bridge", lambda: bridge, raising=False)

    captured = {}

    class DummyMcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    profile_tools.register_profile_tools(DummyMcp(), altium=True)
    return captured["profile_apply_document_settings"], bridge


def _profile(tmp_path, monkeypatch, settings):
    d = tmp_path / "profile"
    d.mkdir(exist_ok=True)
    (d / "document_settings.json").write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setenv("EDA_AGENT_PROFILE_DIR", str(d))
    return d


def test_units_and_grid_both_reach_the_bridge(tmp_path, monkeypatch, apply_tool):
    tool, bridge = apply_tool
    _profile(tmp_path, monkeypatch, {
        "schematic_units": "mm",
        "schematic_snap_grid_mil": 50,
        "schematic_visible_grid_mil": 100,
    })
    result = asyncio.run(tool())
    assert result["ok"] is True
    assert bridge.calls[0] == ("generic.set_sch_units", {"unit": "mm"})
    assert bridge.calls[1] == ("generic.set_grid",
                               {"snap_grid": "50", "visible_grid": "100"})


def test_units_alone_sends_no_grid_command(tmp_path, monkeypatch, apply_tool):
    tool, bridge = apply_tool
    _profile(tmp_path, monkeypatch, {"schematic_units": "mil"})
    assert asyncio.run(tool())["ok"] is True
    assert [c for c, _ in bridge.calls] == ["generic.set_sch_units"]


def test_a_zero_grid_is_left_alone(tmp_path, monkeypatch, apply_tool):
    """0 is obj_set_grid's own 'do not change', so sending it is pointless."""
    tool, bridge = apply_tool
    _profile(tmp_path, monkeypatch,
             {"schematic_snap_grid_mil": 0, "schematic_visible_grid_mil": 100})
    asyncio.run(tool())
    assert bridge.calls[0] == ("generic.set_grid", {"visible_grid": "100"})


def test_an_unknown_unit_name_is_refused_before_the_bridge(tmp_path, monkeypatch, apply_tool):
    tool, bridge = apply_tool
    _profile(tmp_path, monkeypatch, {"schematic_units": "millimeters"})
    result = asyncio.run(tool())
    assert result["ok"] is False
    assert "millimeters" in result["reason"]
    assert bridge.calls == []


def test_a_non_numeric_grid_is_refused_rather_than_raising(tmp_path, monkeypatch, apply_tool):
    tool, bridge = apply_tool
    _profile(tmp_path, monkeypatch, {"schematic_snap_grid_mil": "wide"})
    result = asyncio.run(tool())
    assert result["ok"] is False
    assert "schematic_snap_grid_mil" in result["reason"]
    assert bridge.calls == []


def test_a_bad_value_blocks_the_whole_call_not_just_its_own_field(tmp_path, monkeypatch, apply_tool):
    """Half-applying a document's settings is worse than applying none:
    the reader cannot tell which half took."""
    tool, bridge = apply_tool
    _profile(tmp_path, monkeypatch,
             {"schematic_units": "mm", "schematic_snap_grid_mil": "wide"})
    assert asyncio.run(tool())["ok"] is False
    assert bridge.calls == []


def test_no_profile_is_a_clean_refusal(tmp_path, monkeypatch, apply_tool):
    tool, bridge = apply_tool
    monkeypatch.setenv("EDA_AGENT_PROFILE_DIR", str(tmp_path / "nothing_here"))
    result = asyncio.run(tool())
    assert result["ok"] is False and "document_settings" in result["reason"]
    assert bridge.calls == []


def test_pcb_units_are_documented_but_not_applied(tmp_path, monkeypatch, apply_tool):
    """No bridge command exists to set a board's units; the profile may
    still carry the convention (docs/PROFILES.md)."""
    tool, bridge = apply_tool
    _profile(tmp_path, monkeypatch, {"pcb_units": "mm", "schematic_units": "mil"})
    asyncio.run(tool())
    assert [c for c, _ in bridge.calls] == ["generic.set_sch_units"]
