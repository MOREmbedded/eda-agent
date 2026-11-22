# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Tests for the routing MCP tools (offline, synthetic geometry, mils).

The tools are called directly through a capturing fake MCP; the bridge
is never touched except in the explicit fetch-flag test, where it is
monkeypatched. Boards are tiny so the suite stays fast.
"""

from __future__ import annotations

import asyncio
import copy

import pytest

import eda_agent.tools.route as route_tools


# ---------------------------------------------------------------------------
# Harness + synthetic geometry
# ---------------------------------------------------------------------------


class _CapturingMCP:
    """Minimal stand-in for FastMCP, records registered tools."""

    def __init__(self) -> None:
        self.tools: dict[str, callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


@pytest.fixture()
def tools():
    fake = _CapturingMCP()
    route_tools.register_route_tools(fake)
    return fake.tools


def _run(coro):
    return asyncio.run(coro)


RULES = {
    "clearance_mils": 10,
    "track_width_mils": {"default": 10, "power": 20},
    "via_size_mils": 50,
    "via_drill_mils": 28,
    "layers": ["TopLayer", "BottomLayer"],
}


def _pad(x, y, net="", layer="TopLayer", size=40):
    return {"x": x, "y": y, "x_size": size, "y_size": size,
            "shape": "Rectangular", "layer": layer, "net": net,
            "rotation": 0}


def _geom(pads, bbox=(0, 0, 1000, 600), tracks=None, vias=None):
    return {
        "bbox": {"x1": bbox[0], "y1": bbox[1],
                 "x2": bbox[2], "y2": bbox[3]},
        "pads": pads,
        "tracks": tracks or [],
        "vias": vias or [],
    }


def _two_net_geom():
    return _geom([
        _pad(100, 100, "NET1"), _pad(900, 100, "NET1"),
        _pad(100, 500, "NET2"), _pad(900, 500, "NET2"),
    ])


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_exposes_route_tools(tools):
    assert set(tools) == {
        "route_plan",
        "route_plan_repairs",
        "pcb_autoroute",
        "pcb_autoroute_apply",
        "pcb_route_brief",
        "pcb_route_round",
        "pcb_route_simulate",
        "pcb_route_review",
        "pcb_autoplace",
        "pcb_autoplace_apply",
        "route_build_offline_package",
        "route_audit_plan",
    }


# ---------------------------------------------------------------------------
# route_plan
# ---------------------------------------------------------------------------


def test_route_plan_two_pad_net(tools):
    geom = _geom([_pad(100, 300, "SIG"), _pad(900, 300, "SIG")])
    res = _run(tools["route_plan"](geometry=geom, rules=RULES))
    assert res["ok"] is True
    assert res["summary"]["routed"] == 1
    assert res["summary"]["failed"] == 0
    assert res["summary"]["completion"] == 1.0
    assert res["nets"]["SIG"]["status"] == "routed"
    assert res["validation"]["ok"] is True
    for t in res["tracks"]:
        assert set(t) == {"x1", "y1", "x2", "y2", "width", "layer",
                          "net_name"}
        for k in ("x1", "y1", "x2", "y2", "width"):
            assert isinstance(t[k], int)
    for v in res["vias"]:
        assert set(v) == {"x", "y", "net", "size", "hole_size"}


def test_route_plan_per_class_width(tools):
    geom = _geom([_pad(100, 300, "VCC"), _pad(900, 300, "VCC")])
    res = _run(tools["route_plan"](
        geometry=geom, rules=RULES, net_classes={"VCC": "power"}))
    assert res["ok"] is True
    assert res["nets"]["VCC"]["class"] == "power"
    assert res["nets"]["VCC"]["width"] == 20
    assert all(t["width"] == 20 for t in res["tracks"])


def test_route_plan_nets_filter_routes_only_requested(tools):
    res = _run(tools["route_plan"](
        geometry=_two_net_geom(), rules=RULES, nets=["NET1"]))
    assert res["ok"] is True
    assert set(res["nets"]) == {"NET1"}
    assert res["nets"]["NET1"]["status"] == "routed"
    assert res["requested_nets"] == ["NET1"]
    assert res["unknown_nets"] == []
    assert all(t["net_name"] == "NET1" for t in res["tracks"])


def test_route_plan_nets_filter_reports_unknown(tools):
    res = _run(tools["route_plan"](
        geometry=_two_net_geom(), rules=RULES, nets=["NET1", "NOPE"]))
    assert res["ok"] is True
    assert res["unknown_nets"] == ["NOPE"]
    assert set(res["nets"]) == {"NET1"}


def test_route_plan_nets_filter_all_unknown(tools):
    # Routing none of the requested nets must NOT read as a clean run.
    # This used to return ok:True with completion 1.0, because the router
    # divided by the nets it attempted (zero) -- which is how seven real
    # nets came back as "unknown" alongside "100% complete".
    res = _run(tools["route_plan"](
        geometry=_two_net_geom(), rules=RULES, nets=["NOPE"]))
    assert res["ok"] is False
    assert res["summary"]["nets_total"] == 0
    assert res["summary"]["completion"] == 0.0
    assert res["summary"]["requested_count"] == 1
    assert res["summary"]["unknown_count"] == 1
    assert res["unknown_nets"] == ["NOPE"]
    assert (res["unknown_net_reasons"]["NOPE"]["reason"]
            == "absent_from_geometry")
    assert res["tracks"] == []


def test_route_plan_partial_unknown_scales_completion_to_requested(tools):
    # One of two requested nets is routable: completion is 1/2, not 1/1.
    res = _run(tools["route_plan"](
        geometry=_two_net_geom(), rules=RULES, nets=["NET1", "NOPE"]))
    assert res["ok"] is True
    assert res["summary"]["routed"] == 1
    assert res["summary"]["requested_count"] == 2
    assert res["summary"]["completion"] == 0.5


def test_route_plan_unknown_net_blames_the_routing_layers(tools):
    # Pads exist and carry the net, but sit on a layer outside
    # rules.layers. That is a reachability problem, not a missing net,
    # and reporting it as "unknown" is what produced the wrong call.
    geom = _geom([
        _pad(100, 100, "INNER", layer="MidLayer1"),
        _pad(900, 100, "INNER", layer="MidLayer1"),
    ])
    res = _run(tools["route_plan"](
        geometry=geom, rules=RULES, nets=["INNER"]))
    assert res["ok"] is False
    assert res["unknown_nets"] == ["INNER"]
    reason = res["unknown_net_reasons"]["INNER"]
    assert reason["reason"] == "pads_off_routing_layers"
    assert reason["pads_dropped"] == 2
    assert res["geometry_summary"]["pads_dropped_off_routing_layers"] == 2
    assert res["geometry_summary"]["nets_with_pads"] == 1
    assert res["geometry_summary"]["nets_with_terminals"] == 0


def test_route_plan_flags_geometry_short_of_declared_counts(tools):
    # The payload says it holds 4 pads and delivers 2. Whatever dropped
    # them, the routing input is incomplete, so the run must not present
    # its unknown_nets as fact.
    geom = _two_net_geom()
    geom["counts"] = {"pads": 4, "tracks": 0, "vias": 0}
    geom["pads"] = geom["pads"][:2]  # NET2's pads never arrived
    res = _run(tools["route_plan"](
        geometry=geom, rules=RULES, nets=["NET1", "NET2"]))
    assert res["ok"] is False
    assert res["geometry_summary"]["truncated"] is True
    assert res["geometry_summary"]["count_mismatches"]["pads"] == {
        "declared": 4, "received": 2}
    assert "incomplete" in res["reason"]
    assert res["unknown_nets"] == ["NET2"]


def test_route_plan_intact_geometry_is_not_flagged_truncated(tools):
    geom = _two_net_geom()
    geom["counts"] = {"pads": 4, "tracks": 0, "vias": 0}
    res = _run(tools["route_plan"](geometry=geom, rules=RULES))
    assert res["ok"] is True
    assert res["geometry_summary"]["truncated"] is False
    assert res["geometry_summary"]["count_mismatches"] == {}
    assert res["geometry_summary"]["pads_seen"] == 4
    assert res["geometry_summary"]["pads_used_as_terminals"] == 4


def test_route_plan_geometry_summary_without_counts_block(tools):
    # Older payloads carry no `counts`; absence is not evidence of loss.
    res = _run(tools["route_plan"](geometry=_two_net_geom(), rules=RULES))
    assert res["ok"] is True
    assert res["geometry_summary"]["truncated"] is False
    assert res["geometry_summary"]["declared_counts"] == {}


def test_route_plan_mechanism_failure_flips_ok_even_when_partial(tools):
    # One requested net routes, the other was dropped by the model
    # because its pads sit off rules.layers. ok used to stay True unless
    # EVERY requested net vanished, so six of seven silently-dropped
    # nets still read as a clean run.
    geom = _geom([
        _pad(100, 100, "NET1"), _pad(900, 100, "NET1"),
        _pad(100, 500, "INNER", layer="MidLayer1"),
        _pad(900, 500, "INNER", layer="MidLayer1"),
    ])
    res = _run(tools["route_plan"](
        geometry=geom, rules=RULES, nets=["NET1", "INNER"]))
    assert res["ok"] is False
    assert res["unknown_nets"] == ["INNER"]
    assert res["unknown_nets_mechanism_failures"] == ["INNER"]
    assert res["unknown_net_reasons"]["INNER"]["mechanism_failure"] is True
    assert "mechanism failure" in res["reason"]
    assert res["nets"]["NET1"]["status"] == "routed"


def test_route_plan_absent_net_is_the_only_benign_unknown(tools):
    res = _run(tools["route_plan"](
        geometry=_two_net_geom(), rules=RULES, nets=["NET1", "NOPE"]))
    assert res["ok"] is True
    reason = res["unknown_net_reasons"]["NOPE"]
    assert reason["reason"] == "absent_from_geometry"
    assert reason["mechanism_failure"] is False
    assert "unknown_nets_mechanism_failures" not in res


def test_route_plan_pad_less_payload_is_not_a_missing_net(tools):
    # Zero pads with nets requested is never a netlist verdict: the pad
    # list never arrived. Saying "no such net" here is the exact
    # confidently-wrong answer unknown_net_reasons exists to prevent.
    geom = {"bbox": {"x1": 0, "y1": 0, "x2": 1000, "y2": 600},
            "pads": [], "tracks": [], "vias": []}
    res = _run(tools["route_plan"](
        geometry=geom, rules=RULES, nets=["OCTOSPI_IO0"]))
    assert res["ok"] is False
    assert res["unknown_nets"] == ["OCTOSPI_IO0"]
    reason = res["unknown_net_reasons"]["OCTOSPI_IO0"]
    assert reason["reason"] == "geometry_has_no_pads"
    assert reason["mechanism_failure"] is True
    assert "fetch or payload failure" in reason["detail"]


def test_route_plan_grid_pitch_never_changes_which_nets_are_known(tools):
    # The reported defect: the same board and the same net list came back
    # with the nets in unknown_nets at 5 and 10 mils but recognised at 20.
    # grid_pitch_mils resizes the obstacle map and nothing else.
    geom = _geom([
        _pad(100, 100, "NET1"), _pad(900, 100, "NET1"),
        _pad(100, 500, "NET2"), _pad(900, 500, "NET2"),
    ], tracks=[{"x1": 400, "y1": 300, "x2": 600, "y2": 300, "width": 10,
                "layer": "TopLayer", "net": "NET1"}],
        vias=[{"x": 500, "y": 300, "size": 50, "net": "NET1"}])
    for pitch in (5, 10, 20, 25):
        res = _run(tools["route_plan"](
            geometry=geom, rules=RULES, nets=["NET1", "NET2"],
            grid_pitch_mils=pitch))
        assert res["unknown_nets"] == [], f"pitch {pitch} lost a net"
        assert res["summary"]["nets_total"] == 2
        assert res["geometry_summary"]["grid_pitch_mils"] == pitch


def test_route_plan_grid_too_fine_fails_by_name_not_by_losing_nets(tools):
    geom = _geom([_pad(100, 100, "NET1"), _pad(900, 100, "NET1")],
                 bbox=(0, 0, 4000, 4000))
    res = _run(tools["route_plan"](
        geometry=geom, rules={**RULES, "layers": ["A", "B", "C"]},
        nets=["NET1"], grid_pitch_mils=1))
    assert res["ok"] is False
    assert res["reason_code"] == "grid_too_fine"
    assert res["grid_nodes"] > res["grid_node_limit"]
    assert res["suggested_grid_pitch_mils"] > 1
    # The nets were never looked at, so none of them may be called unknown.
    assert res["unknown_nets"] == []
    assert res["requested_nets"] == ["NET1"]


def test_route_plan_failed_net_carries_an_actionable_hint(tools):
    # 0.8 mm BGA field: the 10 mil track + 2 x 10 mil clearance cannot fit
    # through a 15 mil pad gap at any grid pitch, and saying so beats
    # "no path between closest pair".
    pads = [_pad(400 + i * 31, 400 + j * 31, size=16,
                 net="S" if (i, j) in ((2, 2), (4, 4)) else f"F{i}{j}")
            for i in range(7) for j in range(7)]
    res = _run(tools["route_plan"](
        geometry=_geom(pads, bbox=(300, 300, 900, 900)), rules=RULES,
        nets=["S"], grid_pitch_mils=5))
    net = res["nets"]["S"]
    assert net["status"] == "failed"
    assert net["hint"] == "no_corridor_at_any_pitch"
    assert net["limiting_pad_pitch_mils"] == pytest.approx(31.0)
    assert net["corridor_needed_mils"] == pytest.approx(30.0)
    assert res["summary"]["failed_hints"] == {"no_corridor_at_any_pitch": 1}
    assert res["summary"]["grid"]["grid_pitch_mils"] == 5


def test_route_plan_nets_must_be_name_list(tools):
    geom = _two_net_geom()
    for bad in (42, "NET1", [1, 2], [""]):
        res = _run(tools["route_plan"](geometry=geom, rules=RULES, nets=bad))
        assert res["ok"] is False
        assert "nets" in res["reason"]


def test_route_plan_no_geometry(tools):
    res = _run(tools["route_plan"]())
    assert res["ok"] is False
    assert "geometry" in res["reason"]


def test_route_plan_geometry_not_dict(tools):
    res = _run(tools["route_plan"](geometry=[1, 2, 3]))
    assert res["ok"] is False


def test_route_plan_bad_rules(tools):
    geom = _geom([_pad(100, 300, "SIG"), _pad(900, 300, "SIG")])
    res = _run(tools["route_plan"](
        geometry=geom, rules={"clearance_mils": -1}))
    assert res["ok"] is False
    assert "clearance" in res["reason"]


def test_route_plan_bad_grid_pitch(tools):
    geom = _geom([_pad(100, 300, "SIG"), _pad(900, 300, "SIG")])
    res = _run(tools["route_plan"](
        geometry=geom, rules=RULES, grid_pitch_mils=0))
    assert res["ok"] is False


def test_route_plan_deterministic(tools):
    geom = _geom([
        _pad(100, 100, "A"), _pad(900, 500, "A"),
        _pad(100, 500, "B"), _pad(900, 100, "B"),
    ])
    res1 = _run(tools["route_plan"](geometry=copy.deepcopy(geom),
                                    rules=RULES))
    res2 = _run(tools["route_plan"](geometry=copy.deepcopy(geom),
                                    rules=RULES))
    assert res1 == res2


def test_route_plan_fetch_flag_uses_bridge(tools, monkeypatch):
    geom = _geom([_pad(100, 300, "SIG"), _pad(900, 300, "SIG")])
    calls: list[str] = []

    class _FakeBridge:
        async def send_command_async(self, command, params=None,
                                     timeout=None):
            calls.append(command)
            return geom

    monkeypatch.setattr(route_tools, "get_bridge", lambda: _FakeBridge())
    res = _run(tools["route_plan"](rules=RULES, fetch_geometry=True))
    assert calls == ["generic.get_pcb_geometry"]
    assert res["ok"] is True
    assert res["summary"]["routed"] == 1


def test_route_plan_passed_geometry_skips_bridge(tools, monkeypatch):
    def _no_bridge():
        raise AssertionError("bridge must not be touched")

    monkeypatch.setattr(route_tools, "get_bridge", _no_bridge)
    geom = _geom([_pad(100, 300, "SIG"), _pad(900, 300, "SIG")])
    res = _run(tools["route_plan"](geometry=geom, rules=RULES))
    assert res["ok"] is True


def test_route_plan_fetch_returns_garbage(tools, monkeypatch):
    class _FakeBridge:
        async def send_command_async(self, command, params=None,
                                     timeout=None):
            return None

    monkeypatch.setattr(route_tools, "get_bridge", lambda: _FakeBridge())
    res = _run(tools["route_plan"](fetch_geometry=True))
    assert res["ok"] is False


# ---------------------------------------------------------------------------
# route_plan_repairs
# ---------------------------------------------------------------------------


def _prim(type_="", net="", x=None, y=None):
    prim = {"detail": f"{type_} detail", "type": type_, "net": net,
            "layer": "Top Layer"}
    if x is not None:
        prim["x_mils"] = x
    if y is not None:
        prim["y_mils"] = y
    return prim


def _clearance_violation(net1, net2):
    return {
        "name": "Clearance Constraint Violation",
        "description": f"Clearance Constraint: Between Track on Top Layer "
                       f"and Track on Top Layer Net {net1} and Net {net2}",
        "rule": "Clearance",
        "x_mils": 100, "y_mils": 100, "layer": "Top Layer",
        "primitive1": _prim("Track", net1, 100, 100),
        "primitive2": _prim("Track", net2, 105, 100),
    }


def _unrouted_violation(net):
    return {
        "name": "Un-Routed Net Constraint Violation",
        "description": f"Un-Routed Net Constraint: Net {net}",
        "rule": "UnRoutedNet",
        "x_mils": 0, "y_mils": 0, "layer": "Top Layer",
        "primitive1": _prim("Pad", net, 200, 200),
        "primitive2": _prim("Pad", net, 400, 200),
    }


def test_route_plan_repairs_clearance(tools):
    payload = {"violation_count": 1,
               "violations": [_clearance_violation("SIG1", "SIG2")]}
    res = _run(tools["route_plan_repairs"](payload))
    assert res["ok"] is True
    assert res["counts"]["net_clearance"] == 1
    rips = [a for a in res["actions"] if a["action"] == "rip_and_reroute"]
    assert len(rips) == 1
    assert rips[0]["net"] in ("SIG1", "SIG2")
    assert res["ripped_nets"] == [rips[0]["net"]]


def test_route_plan_repairs_unrouted(tools):
    res = _run(tools["route_plan_repairs"]([_unrouted_violation("N1")]))
    assert res["ok"] is True
    assert res["counts"]["unrouted"] == 1
    assert any(a["action"] == "rip_and_reroute" and a["net"] == "N1"
               for a in res["actions"])


def test_route_plan_repairs_empty(tools):
    res = _run(tools["route_plan_repairs"]([]))
    assert res["ok"] is True
    assert res["actions"] == []


def test_route_plan_repairs_malformed(tools):
    res = _run(tools["route_plan_repairs"]("garbage"))
    assert res["ok"] is False


def test_route_plan_repairs_bad_max_rounds(tools):
    res = _run(tools["route_plan_repairs"]([], max_rounds=-1))
    assert res["ok"] is False
    assert "max_rounds" in res["reason"]


def test_route_plan_repairs_zero_rounds_escalates(tools):
    payload = [_clearance_violation("SIG1", "SIG2")]
    res = _run(tools["route_plan_repairs"](payload, max_rounds=0))
    assert res["ok"] is True
    assert res["actions"]
    assert res["actions"][0]["action"] == "escalate"
