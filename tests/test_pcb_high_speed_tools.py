# SPDX-License-Identifier: Apache-2.0
"""Coverage for multilayer and controlled-impedance PCB helpers."""

from __future__ import annotations

import math

import pytest

from eda_agent.tools.pcb import _offset_pair_polyline


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


class _Bridge:
    def __init__(self):
        self.calls = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        if command == "pcb.get_trace_lengths":
            return {"trace_lengths": [
                {"net": "DP", "length_mils": 1000},
                {"net": "DN", "length_mils": 1004},
            ]}
        if command == "pcb.get_vias":
            return {"vias": [{"net": "DP"}, {"net": "DN"}]}
        return {"success": True}


def _registered(monkeypatch):
    from eda_agent.tools import pcb

    bridge = _Bridge()
    monkeypatch.setattr(pcb, "get_bridge", lambda: bridge)
    mcp = _Mcp()
    pcb.register_pcb_tools(mcp)
    return mcp.tools, bridge


def test_offset_pair_keeps_center_spacing_at_corner():
    pos, neg = _offset_pair_polyline([(0, 0), (100, 0), (100, 100)], 5)
    assert math.dist(pos[0], neg[0]) == pytest.approx(10)
    assert math.dist(pos[-1], neg[-1]) == pytest.approx(10)
    # At the 90-degree bend the two miter vertices lie on opposite corner
    # bisectors; each outgoing/incoming segment remains offset by 5 mil.
    assert pos[1] == pytest.approx((95, 5))
    assert neg[1] == pytest.approx((105, -5))


@pytest.mark.asyncio
async def test_route_diff_pair_dry_run(monkeypatch):
    tools, bridge = _registered(monkeypatch)
    out = await tools["pcb_route_diff_pair"](
        "DP", "DN", [{"x": 0, "y": 0}, {"x": 100, "y": 0}],
        width_mils=6, gap_mils=6,
    )
    assert out["dry_run"] is True
    assert len(out["tracks"]) == 2
    assert not bridge.calls


@pytest.mark.asyncio
async def test_diff_pair_vias_support_inner_span(monkeypatch):
    tools, _ = _registered(monkeypatch)
    out = await tools["pcb_place_diff_pair_vias"](
        "DP", "DN", 100, 200, 0, 12,
        low_layer="MidLayer1", high_layer="MidLayer2",
    )
    assert out["vias"] == [
        {"net": "DP", "x": 106, "y": 200},
        {"net": "DN", "x": 94, "y": 200},
    ]


@pytest.mark.asyncio
async def test_audit_diff_pair_checks_skew_and_vias(monkeypatch):
    tools, _ = _registered(monkeypatch)
    out = await tools["pcb_audit_diff_pair"]("DP", "DN", max_skew_mils=5)
    assert out["passed"] is True
    assert out["skew_mils"] == 4


@pytest.mark.asyncio
async def test_apply_impedance_rules_builds_width_and_gap(monkeypatch):
    tools, bridge = _registered(monkeypatch)
    out = await tools["pcb_apply_impedance_rules"](
        "USB", "InDifferentialPairClass('USB')", 90,
        "microstrip_diff", 7, 4.1, spacing_mils=6,
    )
    assert out["dry_run"] is True
    assert [r["rule_type"] for r in out["rules"]] == [
        "width", "differential_pairs",
    ]
    assert not bridge.calls


@pytest.mark.asyncio
async def test_multilayer_stackup_is_dry_run_by_default(monkeypatch):
    tools, bridge = _registered(monkeypatch)
    out = await tools["pcb_configure_multilayer_stackup"]([
        {"action": "add", "layer": "MidLayer1"},
        {"action": "modify", "layer": "MidLayer1",
         "dielectric_type": "core", "dielectric_constant": 4.1},
    ])
    assert out["success"] is True
    assert out["dry_run"] is True
    assert not bridge.calls
