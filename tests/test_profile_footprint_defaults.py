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


def test_a_broken_profile_does_not_break_pad_authoring(tmp_path, monkeypatch):
    """One stray comma in a profile used to crash every pad call with a
    JSONDecodeError raised from json.loads, several frames from anything
    the caller could act on."""
    d = tmp_path / "profile"
    d.mkdir()
    (d / "footprint_defaults.json").write_text("{oops}", encoding="utf-8")
    monkeypatch.setenv("EDA_AGENT_PROFILE_DIR", str(d))

    tools, bridge = _tools(monkeypatch)
    asyncio.run(tools["lib_add_footprint_pad"](designator="1", x=0, y=0))
    _cmd, params = bridge.calls[0]
    assert params["corner_radius"] == 25


@pytest.mark.parametrize("bad", ["one", True, 150, -5, None])
def test_an_unusable_corner_radius_falls_back_on_both_pad_paths(tmp_path, monkeypatch, bad):
    """A bad setting used to behave two different wrong ways: the singular
    tool forwarded it to Altium verbatim (corner_radius=one), and the bulk
    payload raised TypeError in round(). Both now fall back to 25, and
    profile-validate is what reports the typo."""
    import json
    d = tmp_path / "profile"
    d.mkdir()
    (d / "footprint_defaults.json").write_text(
        json.dumps({"pad_corner_radius_pct": bad}), encoding="utf-8")
    monkeypatch.setenv("EDA_AGENT_PROFILE_DIR", str(d))

    tools, bridge = _tools(monkeypatch)
    asyncio.run(tools["lib_add_footprint_pad"](designator="1", x=0, y=0))
    assert bridge.calls[0][1]["corner_radius"] == 25

    asyncio.run(tools["lib_add_footprint_pads"](pads=[{"designator": "1", "x": 0, "y": 0}]))
    assert "corner_radius=25;" in bridge.calls[1][1]["pads"]


def test_a_fractional_corner_radius_is_rounded_to_a_whole_percent(tmp_path, monkeypatch):
    _activate_profile(tmp_path, monkeypatch, footprint_defaults={"pad_corner_radius_pct": 1.4})
    tools, bridge = _tools(monkeypatch)
    asyncio.run(tools["lib_add_footprint_pad"](designator="1", x=0, y=0))
    assert bridge.calls[0][1]["corner_radius"] == 1


def test_a_bulk_batch_that_sets_every_radius_never_reads_the_profile(tmp_path, monkeypatch):
    """The default is resolved lazily, once per batch at most: a caller that
    specifies every pad (lib_create_standard_footprint does) should not pay
    for a filesystem lookup at all."""
    from eda_agent import profile as profile_mod

    d = tmp_path / "profile"
    d.mkdir()
    (d / "footprint_defaults.json").write_text('{"pad_corner_radius_pct": 1}', encoding="utf-8")
    monkeypatch.setenv("EDA_AGENT_PROFILE_DIR", str(d))

    reads = []
    original = profile_mod._read_json_lenient
    monkeypatch.setattr(profile_mod, "_read_json_lenient",
                        lambda path: (reads.append(path), original(path))[1])

    tools, _bridge = _tools(monkeypatch)
    pads = [{"designator": str(i), "x": i * 10, "y": 0, "corner_radius": 1}
            for i in range(1, 9)]
    asyncio.run(tools["lib_add_footprint_pads"](pads=pads))
    assert reads == []

    pads_without = [{"designator": str(i), "x": i * 10, "y": 0} for i in range(1, 9)]
    asyncio.run(tools["lib_add_footprint_pads"](pads=pads_without))
    assert len(reads) == 1, "resolved once for the whole batch, not once per pad"


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
