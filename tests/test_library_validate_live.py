"""eng_validate_library_live: the live version of eng_validate_library,
tested against synthetic payloads shaped exactly like the documented
bridge responses (library.get_components, library.get_component_details,
library.get_footprints, library.get_pad_geometry).

UNVERIFIED against live Altium (no SchLib/PcbLib pair was available
while porting this) - this is the same status ltspice.py's output
parsers carry until verified against real output; see
docs/ENGINEERING_TOOLS.md for the procedure to verify before relying
on it.
"""
from __future__ import annotations

import pytest

from eda_agent.tools.engineering import register_engineering_tools
from eda_agent.tools.registry import ToolRegistry


class _CannedBridge:
    """Answers each bridge command from a fixed map; a value may be a
    plain dict or a callable(params) -> dict for commands whose answer
    depends on the call."""

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


_SYMBOL_DETAILS = {
    "name": "AMP2", "description": "", "pin_count": 2,
    "designator": {"text": "U?"},
    "pins": [
        {"designator": "1", "name": "IN", "x": 0, "y": 100,
         "orientation": 0, "length": 200, "hidden": False, "label_hidden": False},
        {"designator": "2", "name": "OUT", "x": 0, "y": 200,
         "orientation": 0, "length": 200, "hidden": False, "label_hidden": False},
    ],
    "parameters": {"Value": "AMP2"},
    "models": [{"model_name": "TESTFP", "model_type": "PCBLIB",
               "is_current": True, "datafile_links": []}],
}
_FOOTPRINT_PADS = {
    "name": "TESTFP", "pad_count": 2,
    "pads": [
        {"name": "1", "x_mm": -1.0, "y_mm": 0.0, "w_mm": 0.3, "h_mm": 0.3},
        {"name": "2", "x_mm": 1.0, "y_mm": 0.0, "w_mm": 0.3, "h_mm": 0.3},
    ],
}


def _library_responses(**overrides):
    base = {
        "library.get_components": {"count": 1, "components": [{"index": 0, "name": "AMP2"}]},
        "library.get_component_details": _SYMBOL_DETAILS,
        "library.get_footprints": {"count": 1, "footprints": [{"name": "TESTFP", "description": ""}]},
        "library.get_pad_geometry": _FOOTPRINT_PADS,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_validate_library_live_builds_symbols_and_footprints_from_the_bridge(
        monkeypatch, tmp_path):
    bridge = _CannedBridge(_library_responses())
    _install(monkeypatch, tmp_path, bridge)

    result = await _tool("eng_validate_library_live")("fake.SchLib", "fake.PcbLib")
    assert result["ok"]
    assert result["symbols"] == 1
    assert result["footprints"] == 1
    assert result["passed"] and result["findings"] == []
    assert not result["components_truncated"] and not result["footprints_truncated"]
    # one enumeration call each side, one detail call per component/footprint
    commands = [c for c, _ in bridge.calls]
    assert commands.count("library.get_component_details") == 1
    assert commands.count("library.get_pad_geometry") == 1


@pytest.mark.asyncio
async def test_validate_library_live_skips_courtyard_and_pin1_marker_checks(
        monkeypatch, tmp_path):
    """The real regression guard: pad-only live geometry has no courtyard
    or silkscreen, so those checks must not fire as false positives on
    every footprint. A footprint with >2 pads and no courtyard would
    normally get BOTH a courtyard error and a pin-1-marker warning
    (confirmed directly against libcheck.audit_footprint below); the live
    path must report neither."""
    from eda_agent.engineering.libcheck import audit_footprint

    fp_no_geometry = {"name": "X", "pads": [
        {"name": "1", "x": -1000, "y": 0, "w": 300, "h": 300},
        {"name": "2", "x": 0, "y": 0, "w": 300, "h": 300},
        {"name": "3", "x": 1000, "y": 0, "w": 300, "h": 300},
    ]}
    with_full = audit_footprint(fp_no_geometry)
    checks = {f["check"] for f in with_full}
    assert "courtyard" in checks and "pin1_marker" in checks

    without_full = audit_footprint(fp_no_geometry, has_full_geometry=False)
    checks2 = {f["check"] for f in without_full}
    assert "courtyard" not in checks2 and "pin1_marker" not in checks2

    responses = _library_responses(
        **{"library.get_pad_geometry": {
            "name": "TESTFP", "pad_count": 3,
            "pads": [{"name": "1", "x_mm": -1.0, "y_mm": 0, "w_mm": 0.3, "h_mm": 0.3},
                    {"name": "2", "x_mm": 0.0, "y_mm": 0, "w_mm": 0.3, "h_mm": 0.3},
                    {"name": "3", "x_mm": 1.0, "y_mm": 0, "w_mm": 0.3, "h_mm": 0.3}]}})
    bridge = _CannedBridge(responses)
    _install(monkeypatch, tmp_path, bridge)
    result = await _tool("eng_validate_library_live")("fake.SchLib", "fake.PcbLib")
    checks3 = {f["check"] for f in result["findings"]}
    assert "courtyard" not in checks3 and "pin1_marker" not in checks3


@pytest.mark.asyncio
async def test_validate_library_live_reports_truncation_rather_than_hiding_it(
        monkeypatch, tmp_path):
    responses = _library_responses(**{
        "library.get_components": {"count": 2, "components": [
            {"index": 0, "name": "AMP2"}, {"index": 1, "name": "AMP3"}]}})
    bridge = _CannedBridge(responses)
    _install(monkeypatch, tmp_path, bridge)

    result = await _tool("eng_validate_library_live")(
        "fake.SchLib", "fake.PcbLib", max_components=1)
    assert result["ok"]
    assert result["components_truncated"] is True
    assert result["symbols"] == 1


@pytest.mark.asyncio
async def test_validate_library_live_reports_a_bridge_failure_as_not_ok(monkeypatch, tmp_path):
    class _Boom:
        async def send_command_async(self, command, params=None, **kwargs):
            raise ValueError("library not found")
    _install(monkeypatch, tmp_path, _Boom())

    result = await _tool("eng_validate_library_live")("missing.SchLib", "missing.PcbLib")
    assert result == {"ok": False, "reason": "library not found"}
