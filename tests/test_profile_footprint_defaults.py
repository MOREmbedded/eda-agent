# SPDX-License-Identifier: Apache-2.0
"""An active profile's footprint_defaults.json / footprint_policy.json
reach the pad-authoring and policy-audit tools (see profile.py,
tools/library.py). Same lightweight fake-bridge pattern as
test_footprint_writes_land_where_asked.py -- no disk I/O happens here,
so the heavier install_bridge_fake isolation isn't needed.
"""
from __future__ import annotations

import asyncio

import pytest

from eda_agent.tools import library as library_module


class _Bridge:
    def __init__(self):
        self.calls = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        return {"success": True}


def _tools(monkeypatch):
    bridge = _Bridge()
    monkeypatch.setattr(library_module, "get_bridge", lambda: bridge)
    captured = {}

    class DummyMcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    library_module.register_library_tools(DummyMcp())
    return captured, bridge


@pytest.fixture(autouse=True)
def _no_profile_by_default(monkeypatch):
    monkeypatch.delenv("EDA_AGENT_PROFILE_DIR", raising=False)


def _activate_profile(tmp_path, monkeypatch, footprint_defaults=None, footprint_policy=None):
    import json
    d = tmp_path / "profile"
    d.mkdir()
    if footprint_defaults is not None:
        (d / "footprint_defaults.json").write_text(json.dumps(footprint_defaults), encoding="utf-8")
    if footprint_policy is not None:
        (d / "footprint_policy.json").write_text(json.dumps(footprint_policy), encoding="utf-8")
    monkeypatch.setenv("EDA_AGENT_PROFILE_DIR", str(d))


def test_lib_add_footprint_pad_defaults_to_25_with_no_profile(monkeypatch):
    tools, bridge = _tools(monkeypatch)
    asyncio.run(tools["lib_add_footprint_pad"](designator="1", x=0, y=0))
    _cmd, params = bridge.calls[0]
    assert params["corner_radius"] == 25


def test_lib_add_footprint_pad_follows_the_active_profile(tmp_path, monkeypatch):
    _activate_profile(tmp_path, monkeypatch, footprint_defaults={"pad_corner_radius_pct": 1})
    tools, bridge = _tools(monkeypatch)
    asyncio.run(tools["lib_add_footprint_pad"](designator="1", x=0, y=0))
    _cmd, params = bridge.calls[0]
    assert params["corner_radius"] == 1


def test_lib_add_footprint_pad_explicit_argument_overrides_the_profile(tmp_path, monkeypatch):
    _activate_profile(tmp_path, monkeypatch, footprint_defaults={"pad_corner_radius_pct": 1})
    tools, bridge = _tools(monkeypatch)
    asyncio.run(tools["lib_add_footprint_pad"](designator="1", x=0, y=0, corner_radius=40))
    _cmd, params = bridge.calls[0]
    assert params["corner_radius"] == 40


def test_lib_create_standard_footprint_follows_the_active_profile(tmp_path, monkeypatch):
    _activate_profile(tmp_path, monkeypatch, footprint_defaults={"pad_corner_radius_pct": 1})
    tools, bridge = _tools(monkeypatch)
    asyncio.run(tools["lib_create_standard_footprint"](
        name="TEST_CHIP", family="chip", pin_count=2))
    pad_calls = [p for cmd, p in bridge.calls if cmd == "library.add_footprint_pads"]
    assert pad_calls, "expected at least one add_footprint_pads call"
    assert "corner_radius=1;" in pad_calls[0]["pads"]


def test_lib_audit_footprint_policies_uses_the_active_profile_when_no_policy_is_passed(tmp_path, monkeypatch):
    _activate_profile(tmp_path, monkeypatch, footprint_policy={"courtyard": True})
    tools, bridge = _tools(monkeypatch)

    captured = {}

    def fake_audit(footprints, policy=None):
        captured["policy"] = policy
        return {"findings": [], "summary": {}}

    monkeypatch.setattr(
        "eda_agent.design.footprint_policy.audit_footprint_library", fake_audit)
    monkeypatch.setattr(
        "eda_agent.design.footprint_policy.plan_footprint_fixes", lambda report: [])

    async def fake_read_library_geometry(bridge, library_path):
        return [], "x.PcbLib"
    monkeypatch.setattr(library_module, "_read_library_geometry", fake_read_library_geometry)

    asyncio.run(tools["lib_audit_footprint_policies"]())
    assert captured["policy"] == {"courtyard": True}


def test_lib_audit_footprint_policies_explicit_policy_still_wins(tmp_path, monkeypatch):
    _activate_profile(tmp_path, monkeypatch, footprint_policy={"courtyard": True})
    tools, bridge = _tools(monkeypatch)

    captured = {}

    def fake_audit(footprints, policy=None):
        captured["policy"] = policy
        return {"findings": [], "summary": {}}

    monkeypatch.setattr(
        "eda_agent.design.footprint_policy.audit_footprint_library", fake_audit)
    monkeypatch.setattr(
        "eda_agent.design.footprint_policy.plan_footprint_fixes", lambda report: [])

    async def fake_read_library_geometry(bridge, library_path):
        return [], "x.PcbLib"
    monkeypatch.setattr(library_module, "_read_library_geometry", fake_read_library_geometry)

    asyncio.run(tools["lib_audit_footprint_policies"](policy={"three_d_model": False}))
    assert captured["policy"] == {"three_d_model": False}
