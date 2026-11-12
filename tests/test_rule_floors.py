# SPDX-License-Identifier: Apache-2.0
"""The fab / IPC class floor check over a board's design rules.

A floor check: rules stricter than the floor stay, board-wide rules below
it are raised, scoped ones are left to the user, nothing is created,
deleted or loosened.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from eda_agent.design.rule_floors import check_rules, floors_from_fab_rules
from eda_agent.tools import rules as rule_tools


def _src(q="fixture"):
    return {"governs": "fab", "quote": q, "url": "https://example.invalid"}


FLOORS = {
    "min_gap": {"mils": 4.0, "label": "gap", "kind": "min", "source": _src()},
    "min_track": {"mils": 4.0, "label": "track", "kind": "min", "source": _src()},
    "min_drill": {"mils": 8.0, "label": "drill", "kind": "min", "source": _src()},
    "via_diameter": {"mils": 16.0, "label": "via", "kind": "min", "source": _src()},
    "min_hole_to_hole": {"mils": 10.0, "label": "h2h", "kind": "min", "source": _src()},
    "copper_to_edge": {"mils": 12.0, "label": "edge", "kind": "min", "source": _src()},
    "min_mask_sliver": {"mils": 4.0, "label": "sliver", "kind": "min", "source": _src()},
    "silk_to_pad": {"mils": 6.0, "label": "silk", "kind": "min", "source": _src()},
    "min_annular_ring": {"mils": 4.0, "label": "ring", "kind": "min", "source": _src()},
    "max_aspect_ratio": {"mils": 10.0, "label": "AR", "kind": "max", "source": _src()},
}


def _rule(name, descriptor, s1="All", s2="", **kw):
    r = {"name": name, "descriptor": descriptor, "scope_1": s1, "scope_2": s2,
         "drc_enabled": True, "enabled": True, "priority": 1,
         "from_schematic": False}
    r.update(kw)
    return r


def _row(out, rule, floor_key=None):
    return next(r for r in out["rows"] if r["rule"] == rule
                and (floor_key is None or r["floor_key"] == floor_key))


def test_stricter_rule_is_left_alone():
    out = check_rules(FLOORS, [
        _rule("Clearance", "Clearance Constraint (Gap=10mil) (All),(All)",
              s2="All", gap_mm=0.254)])
    row = _row(out, "Clearance")
    assert row["status"] == "ok" and row["action"] == "none"
    assert out["calls"] == [] and out["in_step"]


def test_board_wide_rule_below_floor_is_raised_in_mm():
    out = check_rules(FLOORS, [
        _rule("Clearance", "Clearance Constraint (Gap=3mil) (All),(All)",
              s2="All", gap_mm=0.0762)])
    row = _row(out, "Clearance")
    assert row["status"] == "below_floor" and row["action"] == "raise"
    assert out["calls"] == [{"name": "Clearance", "gap_mm": 0.1016}]
    assert not out["in_step"] and out["to_raise"] == 1


def test_scoped_rule_below_floor_needs_a_decision_unless_named():
    rules = [_rule("Waiver_J1", "Clearance Constraint (Gap=0mm) (InComponent('J1')),(All)",
                   s1="InComponent('J1')", s2="All", gap_mm=0.0)]
    out = check_rules(FLOORS, rules)
    assert _row(out, "Waiver_J1")["action"] == "decide"
    assert out["to_decide"] == ["Waiver_J1"] and out["calls"] == []
    named = check_rules(FLOORS, rules, names=["waiver_j1"])
    assert named["calls"] == [{"name": "Waiver_J1", "gap_mm": 0.1016}]


def test_drc_off_and_schematic_rules_are_not_written():
    out = check_rules(FLOORS, [
        _rule("Off", "Clearance Constraint (Gap=1mil) (Disabled)(All),(All)",
              s2="All", drc_enabled=False),
        _rule("Sch", "Clearance Constraint (Gap=1mil) (All),(All)",
              s2="All", from_schematic=True)])
    assert _row(out, "Off")["action"] == "not_checked"
    assert _row(out, "Sch")["action"] == "decide"
    assert out["calls"] == []


def test_width_raises_preferred_with_minimum_but_not_past_max():
    out = check_rules(FLOORS, [
        _rule("Width", "Width Constraint (Min=3mil) (Max=20mil) (Preferred=3mil) (All)")])
    assert out["calls"] == [{"name": "Width", "min_width_mm": 0.1016,
                             "favored_width_mm": 0.1016}]
    keep = check_rules(FLOORS, [
        _rule("Width", "Width Constraint (Min=3mil) (Max=20mil) (Preferred=10mil) (All)")])
    assert keep["calls"] == [{"name": "Width", "min_width_mm": 0.1016}]
    tight = check_rules(FLOORS, [
        _rule("Width", "Width Constraint (Min=2mil) (Max=3mil) (Preferred=3mil) (All)")])
    assert _row(tight, "Width")["action"] == "decide"
    assert tight["calls"] == []


def test_routing_via_checks_hole_and_diameter_in_one_call():
    out = check_rules(FLOORS, [_rule(
        "RoutingVias", "Routing Via (MinHoleWidth=6mil) (MaxHoleWidth=30mil) "
        "(PreferredHoleWidth=12mil) (MinWidth=12mil) (MaxWidth=50mil) "
        "(PreferedWidth=12mil) (All)")])
    assert out["calls"] == [{"name": "RoutingVias", "min_via_hole_mils": 8.0,
                             "min_via_size_mils": 16.0,
                             "preferred_via_size_mils": 16.0}]


def test_board_outline_uses_gap_mm_not_the_descriptor():
    out = check_rules(FLOORS, [_rule(
        "BoardOutlineClearance", "Board Outline Clearance (Gap=0mil) (All)",
        gap_mm=0.5)])                                   # 19.7 mil
    assert _row(out, "BoardOutlineClearance")["status"] == "ok"


def test_annular_ring_has_no_writer():
    out = check_rules(FLOORS, [_rule(
        "MinimumAnnularRing", "Minimum Annular Ring (Minimum=2mil) (All)")])
    assert _row(out, "MinimumAnnularRing")["action"] == "no_writer"
    assert out["calls"] == []


def test_kinds_missing_from_the_board_and_report_only_floors():
    out = check_rules(FLOORS, [
        _rule("Clearance", "Clearance Constraint (Gap=10mil) (All),(All)", s2="All")])
    missing = {m["floor_key"] for m in out["no_rule_on_board"]}
    assert "min_hole_to_hole" in missing and "min_gap" not in missing
    ar = next(r for r in out["report_only"] if r["floor_key"] == "max_aspect_ratio")
    assert ar["limit"] == 10.0


def test_unknown_kinds_and_unknown_names_are_reported_not_touched():
    out = check_rules(FLOORS, [_rule("Fanout", "Fanout Control Rule ( ((CompPinCount < 5)) )")],
                      names=["Nope"])
    assert out["rows"] == [] and out["names_not_found"] == ["nope"]


def test_floors_from_fab_rules_prefers_ipc_when_it_governs():
    got = {"rules": {
        "min_drill": {"label": "d", "kind": "min", "mils": 8.0, "quote": "q", "url": "u"},
        "min_annular_ring": {"label": "r", "kind": "min", "mils": 4.0, "quote": "q",
                             "url": "u", "ipc_floor": {"mils": 5.0, "clause": "IPC-6012F x"},
                             "governs": "ipc", "design_mils": 5.0},
        "board_thickness": {"label": "t", "kind": "info", "value": "0.4-4.5 mm"}}}
    f = floors_from_fab_rules(got)
    assert f["min_annular_ring"]["mils"] == 5.0
    assert f["min_annular_ring"]["source"]["cite"] == "IPC-6012F x"
    assert f["via_diameter"]["mils"] == 18.0
    assert f["via_diameter"]["source"]["governs"] == "derived"
    assert "board_thickness" not in f


# --- the MCP tool -----------------------------------------------------------

class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class _Bridge:
    def __init__(self, rules):
        self.rules = rules
        self.sent = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.sent.append((command, dict(params or {})))
        if command == "pcb.get_design_rules":
            return {"rules": self.rules, "count": len(self.rules), "board_path": ""}
        return {"success": True}

    @property
    def writes(self):
        return [s for s in self.sent if s[0] != "pcb.get_design_rules"]


def _tool(monkeypatch, bridge):
    from eda_agent.tools import pcb as pcb_tools
    monkeypatch.setattr(pcb_tools, "get_bridge", lambda: bridge)
    mcp = _Mcp()
    rule_tools.register_rule_tools(mcp)
    return mcp.tools["pcb_rules_fab_floors"]


_BOARD = [
    {"name": "Clearance", "descriptor": "Clearance Constraint (Gap=2mil) (All),(All)",
     "scope_1": "All", "scope_2": "All", "gap_mm": 0.0508, "drc_enabled": True},
    {"name": "Width", "descriptor": "Width Constraint (Min=10mil) (Max=100mil) "
     "(Preferred=10mil) (All)", "scope_1": "All", "drc_enabled": True},
]


def test_tool_reports_by_default_and_writes_nothing(monkeypatch):
    bridge = _Bridge(_BOARD)
    tool = _tool(monkeypatch, bridge)
    out = asyncio.run(tool(fab="jlcpcb", copper_layers=4))
    assert out["ok"] and out["apply"] is False
    assert bridge.writes == []
    clr = _row(out, "Clearance")
    assert clr["action"] == "raise" and clr["floor_mils"] == pytest.approx(3.543)
    assert clr["source"]["governs"] == "fab" and clr["source"]["quote"]
    assert _row(out, "Width")["status"] == "ok"           # 10 mil >= 3.5 mil: kept


def test_tool_apply_raises_only_what_is_below(monkeypatch):
    bridge = _Bridge(_BOARD)
    tool = _tool(monkeypatch, bridge)
    out = asyncio.run(tool(fab="jlcpcb", copper_layers=4, apply=True))
    assert out["ok"] and out["written"] == ["Clearance"]
    assert bridge.writes == [("pcb.set_rule_properties",
                              {"name": "Clearance", "gap_mm": "0.09"})]


def test_tool_refuses_ipc_class_without_profile_floors(monkeypatch, tmp_path):
    from eda_agent.design import fab_rules as fr
    monkeypatch.setattr(fr, "_ipc_catalogue", lambda: tmp_path / "none.json")
    bridge = _Bridge(_BOARD)
    tool = _tool(monkeypatch, bridge)
    out = asyncio.run(tool(fab="jlcpcb", copper_layers=4, ipc_class=3, apply=True))
    assert out["ok"] is False and "refused rather than guessed" in out["reason"]
    assert bridge.sent == []


def test_tool_applies_ipc_class_floor_when_stricter(monkeypatch, tmp_path):
    from eda_agent.design import fab_rules as fr
    floors = tmp_path / "ipc_class_floors.json"
    floors.write_text(json.dumps({"source": {"standard": "IPC-6012F"}, "classes": {
        "3": {"min_hole_to_hole": {"mils": 12.0, "clause": "IPC-6012F test"}}}}))
    monkeypatch.setattr(fr, "_ipc_catalogue", lambda: floors)
    board = _BOARD + [{"name": "HoleToHoleClearance",
                       "descriptor": "Hole To Hole Clearance (Gap=10mil) (All),(All)",
                       "scope_1": "All", "scope_2": "All", "gap_mm": 0.254,
                       "drc_enabled": True}]
    out = asyncio.run(_tool(monkeypatch, _Bridge(board))(
        fab="jlcpcb", copper_layers=4, ipc_class=3))
    row = _row(out, "HoleToHoleClearance")
    assert row["floor_mils"] == 12.0 and row["source"]["governs"] == "ipc"
    assert row["action"] == "raise" and out["ipc"]["class"] == 3


def test_component_clearance_is_not_read_as_copper_clearance():
    out = check_rules(FLOORS, [_rule(
        "ComponentClearance", "Component Clearance Constraint ( Horizontal Gap = "
        "-20mm, Vertical Gap = -20mm ) (InComponent('J1')),(All)",
        s1="InComponent('J1')", s2="All", gap_mm=-20.0)])
    assert out["rows"] == []
