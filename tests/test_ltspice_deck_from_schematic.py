# SPDX-License-Identifier: Apache-2.0
"""eng_ltspice_deck_from_schematic: the live-read side of deck building.

The emitter is covered in test_spice_netlist.py; what matters here is
the glue -- that the readiness buckets and the connectivity response are
read the way Altium actually returns them, that parts needing a vendor
model reach the emitter as such, and that nothing is ever run.
"""
from __future__ import annotations

import asyncio

import pytest

from eda_agent.tools import engineering as eng_module
from eda_agent.tools.registry import ToolRegistry


READINESS = {
    "ready": [{"designator": "R1", "spice_prefix": "R", "value": "10k"}],
    "needs_primitive": [
        {"designator": "R2", "suggested_prefix": "R", "suggested_value": "10k"},
        {"designator": "V1", "suggested_prefix": "V", "suggested_value": "5"},
    ],
    "needs_file": [],
}

CONNECTIVITY = {"components": [
    {"designator": "R1", "pins": [{"pin_number": "1", "net": "VIN"},
                                  {"pin_number": "2", "net": "VOUT"}]},
    {"designator": "R2", "pins": [{"pin_number": "1", "net": "VOUT"},
                                  {"pin_number": "2", "net": "GND"}]},
    {"designator": "V1", "pins": [{"pin_number": "1", "net": "VIN"},
                                  {"pin_number": "2", "net": "GND"}]},
]}


class _Bridge:
    def __init__(self, readiness=None, connectivity=None):
        self.calls = []
        self._readiness = READINESS if readiness is None else readiness
        self._connectivity = CONNECTIVITY if connectivity is None else connectivity

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        if command == "generic.get_simulation_readiness":
            return self._readiness
        if command == "project.get_connectivity_batch":
            return self._connectivity
        raise AssertionError(f"unexpected command {command}")


@pytest.fixture
def tool(monkeypatch):
    bridge = _Bridge()
    import eda_agent.bridge as bridge_pkg
    monkeypatch.setattr(bridge_pkg, "get_bridge", lambda: bridge, raising=False)
    registry = ToolRegistry()
    eng_module.register_engineering_tools(registry, altium=True)
    return registry.get("eng_ltspice_deck_from_schematic").fn, bridge


def test_a_divider_on_the_sheet_becomes_a_runnable_deck(tool):
    fn, bridge = tool
    result = asyncio.run(fn(analysis="op", title="divider"))
    assert result["ok"] is True
    assert result["deck"] == (
        "* divider\n"
        "R1 VIN VOUT 10k\n"
        "R2 VOUT 0 10k\n"
        "V1 VIN 0 5\n"
        ".op\n"
        ".end\n"
    )
    assert sorted(result["emitted"]) == ["R1", "R2", "V1"]


def test_it_reads_readiness_then_connectivity_and_runs_nothing(tool):
    fn, bridge = tool
    asyncio.run(fn(analysis="op"))
    assert [c for c, _ in bridge.calls] == [
        "generic.get_simulation_readiness", "project.get_connectivity_batch"]


def test_every_classified_component_is_asked_about(tool):
    """A part sitting in needs_primitive still has pins worth reading."""
    fn, bridge = tool
    asyncio.run(fn(analysis="op"))
    _cmd, params = bridge.calls[1]
    assert sorted(params["designators"].split("~~")) == ["R1", "R2", "V1"]


def test_the_result_points_at_the_tool_that_runs_it(tool):
    fn, _bridge = tool
    assert "eng_ltspice_run" in asyncio.run(fn(analysis="op"))["next_step"]


def test_an_empty_schematic_is_reported_not_emitted(monkeypatch):
    bridge = _Bridge(readiness={"ready": [], "needs_primitive": [], "needs_file": []})
    import eda_agent.bridge as bridge_pkg
    monkeypatch.setattr(bridge_pkg, "get_bridge", lambda: bridge, raising=False)
    registry = ToolRegistry()
    eng_module.register_engineering_tools(registry, altium=True)
    fn = registry.get("eng_ltspice_deck_from_schematic").fn

    result = asyncio.run(fn(analysis="op"))
    assert result["ok"] is False and "no components" in result["reason"]
    assert len(bridge.calls) == 1, "it did not go on to read connectivity"


def test_a_part_needing_a_vendor_model_refuses_by_default(monkeypatch):
    readiness = {
        **READINESS,
        "needs_file": [{"designator": "U1", "comment": "LT1763",
                        "suggested_prefix": "X"}],
    }
    connectivity = {"components": [
        *CONNECTIVITY["components"],
        {"designator": "U1", "pins": [{"pin_number": "1", "net": "VIN"},
                                      {"pin_number": "2", "net": "GND"}]},
    ]}
    bridge = _Bridge(readiness=readiness, connectivity=connectivity)
    import eda_agent.bridge as bridge_pkg
    monkeypatch.setattr(bridge_pkg, "get_bridge", lambda: bridge, raising=False)
    registry = ToolRegistry()
    eng_module.register_engineering_tools(registry, altium=True)
    fn = registry.get("eng_ltspice_deck_from_schematic").fn

    refused = asyncio.run(fn(analysis="op"))
    assert refused["ok"] is False
    assert "U1" in refused["reason"] and refused["deck"] == ""

    allowed = asyncio.run(fn(analysis="op", on_missing_model="placeholder"))
    assert allowed["ok"] is True
    assert "* U1 needs a manufacturer SPICE model" in allowed["deck"]


def test_a_bad_analysis_is_refused_before_anything_is_emitted(tool):
    fn, _bridge = tool
    result = asyncio.run(fn(analysis="transient 1m"))
    assert result["ok"] is False and "transient" in result["reason"]


def test_a_schematic_with_no_matching_ground_is_refused(monkeypatch):
    connectivity = {"components": [
        {"designator": "R1", "pins": [{"pin_number": "1", "net": "VIN"},
                                      {"pin_number": "2", "net": "VOUT"}]},
        {"designator": "R2", "pins": [{"pin_number": "1", "net": "VOUT"},
                                      {"pin_number": "2", "net": "AGND"}]},
        {"designator": "V1", "pins": [{"pin_number": "1", "net": "VIN"},
                                      {"pin_number": "2", "net": "AGND"}]},
    ]}
    bridge = _Bridge(connectivity=connectivity)
    import eda_agent.bridge as bridge_pkg
    monkeypatch.setattr(bridge_pkg, "get_bridge", lambda: bridge, raising=False)
    registry = ToolRegistry()
    eng_module.register_engineering_tools(registry, altium=True)
    fn = registry.get("eng_ltspice_deck_from_schematic").fn

    assert asyncio.run(fn(analysis="op"))["ok"] is False
    assert asyncio.run(fn(analysis="op", ground_net="AGND"))["ok"] is True


def test_extra_directives_are_passed_through(tool):
    fn, _bridge = tool
    result = asyncio.run(fn(analysis="op",
                            extra_directives=[".include vendor.lib"]))
    assert ".include vendor.lib" in result["deck"]


def test_a_bridge_failure_is_reported_rather_than_raised(monkeypatch):
    class _Failing:
        async def send_command_async(self, command, params=None, timeout=None):
            raise OSError("bridge went away")

    import eda_agent.bridge as bridge_pkg
    monkeypatch.setattr(bridge_pkg, "get_bridge", lambda: _Failing(), raising=False)
    registry = ToolRegistry()
    eng_module.register_engineering_tools(registry, altium=True)
    fn = registry.get("eng_ltspice_deck_from_schematic").fn

    result = asyncio.run(fn(analysis="op"))
    assert result["ok"] is False and "bridge went away" in result["reason"]
