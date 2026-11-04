# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""plan, diff and apply over a workspace profile's design rules.

The profile holds the synthesis TARGETS, so plan is pure Python and the
other two add one board read. Apply writes only what differs and never
deletes.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from eda_agent.tools import rules as rule_tools

_FAB = {
    "name": "housefab",
    "source": "a fixture, not a real fab page",
    "copper_layer_counts": [2],
    "min_track_mils": 6.0,
    "min_gap_mils": 7.0,
    "min_drill_mils": 12.0,
    "min_annular_ring_mils": 5.0,
    "min_hole_to_hole_mils": 10.0,
    "min_mask_sliver_mils": 3.0,
    "min_silk_width_mils": 5.0,
    "stackups": [{
        "name": "house-2L",
        "layers": [
            {"name": "Top", "kind": "copper", "thickness_mils": 1.4,
             "copper_oz": 1.0},
            {"name": "Core", "kind": "core", "thickness_mils": 59.0, "er": 4.3},
            {"name": "Bottom", "kind": "copper", "thickness_mils": 1.4,
             "copper_oz": 1.0}]}],
}

_TARGETS = {"fab_profile": _FAB}


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class _Bridge:
    """Answers the one read the tools make; records every write."""

    def __init__(self, board_rules=()):
        self.board_rules = list(board_rules)
        self.sent = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.sent.append((command, dict(params or {})))
        if command == "pcb.get_design_rules":
            return {"rules": self.board_rules, "count": len(self.board_rules)}
        return {"success": True}

    @property
    def writes(self):
        return [s for s in self.sent if s[0] != "pcb.get_design_rules"]


def _tools(monkeypatch, bridge):
    from eda_agent.tools import pcb as pcb_tools
    monkeypatch.setattr(pcb_tools, "get_bridge", lambda: bridge)
    mcp = _Mcp()
    rule_tools.register_rule_tools(mcp)
    return mcp.tools


def _profile(tmp_path, targets=None, fab=None):
    (tmp_path / "fab_profiles").mkdir()
    (tmp_path / "fab_profiles" / "housefab.json").write_text(
        json.dumps(fab if fab is not None else _FAB), encoding="utf-8")
    (tmp_path / "design_rules.json").write_text(
        json.dumps(targets if targets is not None
                   else {"fab_profile": "housefab"}), encoding="utf-8")
    return str(tmp_path)


def _rule(name, descriptor, **kw):
    return dict({"name": name, "descriptor": descriptor, "enabled": True,
                 "drc_enabled": True, "from_schematic": False}, **kw)


# --- plan -------------------------------------------------------------

def test_plan_reads_the_profile_and_touches_no_board(monkeypatch, tmp_path):
    bridge = _Bridge()
    got = asyncio.run(_tools(monkeypatch, bridge)["pcb_rules_plan"](
        profile_dir=_profile(tmp_path)))
    assert got.get("ok") is not False
    assert [r["name"] for r in got["rules"]][:2] == [
        "Clearance_Fab_Min", "Width_Fab_Min"]
    assert bridge.sent == [], "plan is pure Python"


def test_plan_takes_targets_inline_without_a_profile(monkeypatch):
    got = asyncio.run(_tools(monkeypatch, _Bridge())["pcb_rules_plan"](
        targets=_TARGETS))
    assert got["rules"] and got["notes"]


def test_a_missing_design_rules_file_is_refused_by_name(monkeypatch, tmp_path):
    got = asyncio.run(_tools(monkeypatch, _Bridge())["pcb_rules_plan"](
        profile_dir=str(tmp_path)))
    assert got["ok"] is False and "design_rules.json" in got["reason"]


def test_a_fab_profile_the_folder_lacks_is_refused_by_name(monkeypatch, tmp_path):
    directory = _profile(tmp_path, targets={"fab_profile": "nosuchfab"})
    got = asyncio.run(_tools(monkeypatch, _Bridge())["pcb_rules_plan"](
        profile_dir=directory))
    assert got["ok"] is False and "nosuchfab" in got["reason"]


def test_targets_without_a_fab_profile_say_what_is_needed(monkeypatch, tmp_path):
    directory = _profile(tmp_path, targets={"net_classes": {}})
    got = asyncio.run(_tools(monkeypatch, _Bridge())["pcb_rules_plan"](
        profile_dir=directory))
    assert got["ok"] is False and "fab_profile" in got["reason"]


# --- diff -------------------------------------------------------------

def test_an_empty_board_wants_every_rule_created(monkeypatch):
    got = asyncio.run(_tools(monkeypatch, _Bridge())["pcb_rules_diff"](
        targets=_TARGETS))
    assert got["in_step"] is False
    assert len(got["create"]) == got["planned_rule_count"]
    assert got["extra"] == []


def test_a_board_that_already_matches_reads_in_step(monkeypatch):
    bridge = _Bridge([
        _rule("Clearance_Fab_Min", "Clearance Constraint (Gap=7mil) (All),(All)"),
        _rule("Width_Fab_Min",
              "Width Constraint (Min=6mil) (Max=30mil) (Preferred=6mil) (All)"),
        _rule("Via_Fab_Min_Drill", "Hole Size Constraint (Min=12mil) (All)"),
    ])
    got = asyncio.run(_tools(monkeypatch, bridge)["pcb_rules_diff"](
        targets=_TARGETS))
    assert got["in_step"] is True, got["update"] or got["create"]
    assert sorted(got["same"]) == ["Clearance_Fab_Min", "Via_Fab_Min_Drill",
                                   "Width_Fab_Min"]


def test_a_rule_the_profile_does_not_define_is_extra_not_deleted(monkeypatch):
    bridge = _Bridge([_rule("SomeoneElses",
                            "Clearance Constraint (Gap=8mil) (All),(All)")])
    got = asyncio.run(_tools(monkeypatch, bridge)["pcb_rules_diff"](
        targets=_TARGETS))
    assert [e["name"] for e in got["extra"]] == ["SomeoneElses"]
    assert bridge.writes == []


# --- apply ------------------------------------------------------------

def test_a_dry_run_writes_nothing(monkeypatch):
    bridge = _Bridge()
    got = asyncio.run(_tools(monkeypatch, bridge)["pcb_rules_apply"](
        targets=_TARGETS))
    assert got["dry_run"] is True and got["created"]
    assert bridge.writes == []


def test_apply_creates_what_is_missing(monkeypatch):
    bridge = _Bridge()
    got = asyncio.run(_tools(monkeypatch, bridge)["pcb_rules_apply"](
        targets=_TARGETS, dry_run=False))
    assert got["ok"] is True
    assert [c for c, _ in bridge.writes] == ["pcb.create_design_rule"] * len(
        got["created"])


def test_applying_to_a_board_already_in_step_writes_nothing(monkeypatch):
    """Altium's create has no duplicate-name check: a second apply
    without this left two rules sharing a name."""
    bridge = _Bridge([
        _rule("Clearance_Fab_Min", "Clearance Constraint (Gap=7mil) (All),(All)"),
        _rule("Width_Fab_Min",
              "Width Constraint (Min=6mil) (Max=30mil) (Preferred=6mil) (All)"),
        _rule("Via_Fab_Min_Drill", "Hole Size Constraint (Min=12mil) (All)"),
    ])
    got = asyncio.run(_tools(monkeypatch, bridge)["pcb_rules_apply"](
        targets=_TARGETS, dry_run=False))
    assert got["in_step"] is True
    assert bridge.writes == []


def test_a_value_that_differs_is_updated_in_place(monkeypatch):
    bridge = _Bridge([
        _rule("Clearance_Fab_Min", "Clearance Constraint (Gap=3mil) (All),(All)"),
        _rule("Width_Fab_Min",
              "Width Constraint (Min=6mil) (Max=30mil) (Preferred=6mil) (All)"),
        _rule("Via_Fab_Min_Drill", "Hole Size Constraint (Min=12mil) (All)"),
    ])
    got = asyncio.run(_tools(monkeypatch, bridge)["pcb_rules_apply"](
        targets=_TARGETS, dry_run=False))
    assert got["updated"] == ["Clearance_Fab_Min"]
    assert [c for c, _ in bridge.writes] == ["pcb.set_rule_properties"]
    assert bridge.writes[0][1]["gap_mils"] == "7"


def test_a_schematic_owned_rule_is_never_written(monkeypatch):
    bridge = _Bridge([
        _rule("Clearance_Fab_Min", "Clearance Constraint (Gap=3mil) (All),(All)",
              from_schematic=True),
        _rule("Width_Fab_Min",
              "Width Constraint (Min=6mil) (Max=30mil) (Preferred=6mil) (All)"),
        _rule("Via_Fab_Min_Drill", "Hole Size Constraint (Min=12mil) (All)"),
    ])
    got = asyncio.run(_tools(monkeypatch, bridge)["pcb_rules_apply"](
        targets=_TARGETS, dry_run=False))
    assert [s["name"] for s in got["skipped"]] == ["Clearance_Fab_Min"]
    assert bridge.writes == []


def test_a_kind_with_no_writer_is_reported_not_recreated(monkeypatch):
    """Deleting and recreating would lose the rule's priority and any
    scope the owner edited by hand."""
    bridge = _Bridge([
        _rule("Clearance_Fab_Min", "Clearance Constraint (Gap=7mil) (All),(All)"),
        _rule("Width_Fab_Min",
              "Width Constraint (Min=6mil) (Max=30mil) (Preferred=6mil) (All)"),
        _rule("Via_Fab_Min_Drill", "Hole Size Constraint (Min=99mil) (All)"),
    ])
    got = asyncio.run(_tools(monkeypatch, bridge)["pcb_rules_apply"](
        targets=_TARGETS, dry_run=False))
    assert [u["name"] for u in got["unwritable"]] == ["Via_Fab_Min_Drill"]
    assert bridge.writes == []
    assert "delete" not in str(bridge.sent)


def test_nothing_is_ever_deleted(monkeypatch):
    bridge = _Bridge([_rule("SomeoneElses",
                            "Clearance Constraint (Gap=8mil) (All),(All)")])
    asyncio.run(_tools(monkeypatch, bridge)["pcb_rules_apply"](
        targets=_TARGETS, dry_run=False))
    assert not any("delete" in c for c, _ in bridge.sent)


def test_a_refusal_from_plan_is_passed_through_by_apply(monkeypatch, tmp_path):
    got = asyncio.run(_tools(monkeypatch, _Bridge())["pcb_rules_apply"](
        profile_dir=str(tmp_path), dry_run=False))
    assert got["ok"] is False and "design_rules.json" in got["reason"]
