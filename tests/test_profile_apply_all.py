# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""One call that puts everything a profile declares onto what is open.

The two things worth guarding are both about not losing information. A
section the profile does not define is skipped rather than failed, and a
section that cannot run reports its own refusal without taking the others
with it: the design rules need a board open and the ERC matrix needs a
project, so a caller with only one of them should still get the other's
answer.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from eda_agent import bridge as bridge_module
from eda_agent.tools import pcb as pcb_tools
from eda_agent.tools import profile_apply

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
}


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class _Bridge:
    """Answers both managers' reads; records every write."""

    def __init__(self, *, rules=(), erc=(), fail=()):
        self.rules = list(rules)
        self.erc = list(erc)
        self.fail = set(fail)
        self.sent = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.sent.append((command, dict(params or {})))
        if command in self.fail:
            raise RuntimeError(f"{command}: no document open")
        if command == "pcb.get_design_rules":
            return {"rules": self.rules, "count": len(self.rules)}
        if command == "project.get_erc_levels":
            return {"project": "C:/p/B.PrjPcb", "count": len(self.erc),
                    "type_count": len(self.erc), "beyond_table": 0,
                    "levels": self.erc}
        if command == "application.save_document":
            return {"saved": True}
        if command == "project.set_erc_levels":
            items = [i for i in str(params.get("levels", "")).split("~~") if i]
            return {"project": "C:/p/B.PrjPcb", "set": len(items),
                    "failed": 0, "results": []}
        return {"success": True}

    @property
    def writes(self):
        return [s for s in self.sent
                if s[0] not in ("pcb.get_design_rules",
                                "project.get_erc_levels")]


def _tools(monkeypatch, bridge):
    monkeypatch.setattr(bridge_module, "get_bridge", lambda: bridge)
    monkeypatch.setattr(pcb_tools, "get_bridge", lambda: bridge)
    mcp = _Mcp()
    profile_apply.register_profile_apply_tools(mcp)
    return mcp.tools


def _profile(tmp_path, *, rules=True, erc=True, documents=False):
    if rules:
        (tmp_path / "fab_profiles").mkdir(exist_ok=True)
        (tmp_path / "fab_profiles" / "housefab.json").write_text(
            json.dumps(_FAB), encoding="utf-8")
        (tmp_path / "design_rules.json").write_text(
            json.dumps({"fab_profile": "housefab"}), encoding="utf-8")
    if erc:
        (tmp_path / "erc_levels.json").write_text(
            json.dumps({"levels": {"Nets with multiple names": "no_report"}}),
            encoding="utf-8")
    if documents:
        (tmp_path / "document_settings.json").write_text(
            json.dumps({"schematic_units": "mm"}), encoding="utf-8")
    return str(tmp_path)


_MATRIX = [{"kind": 1, "description": "Nets with multiple names",
            "level": "warning"}]
_MATCHED = [{"kind": 1, "description": "Nets with multiple names",
             "level": "no_report"}]


# --- what the profile declares ------------------------------------------

def test_a_section_the_profile_does_not_define_is_skipped_not_failed(
        monkeypatch, tmp_path):
    """A profile declaring only an ERC policy is a complete profile."""
    bridge = _Bridge(erc=_MATRIX)
    got = asyncio.run(_tools(monkeypatch, bridge)["profile_diff_all"](
        profile_dir=_profile(tmp_path, rules=False)))
    assert "skipped" in got["sections"]["design_rules"]
    assert "design_rules.json" in got["sections"]["design_rules"]["skipped"]
    assert got["compared"] == ["erc_levels"]
    assert "pcb.get_design_rules" not in [c for c, _ in bridge.sent]


def test_both_sections_are_compared_when_both_are_declared(
        monkeypatch, tmp_path):
    bridge = _Bridge(erc=_MATRIX)
    got = asyncio.run(_tools(monkeypatch, bridge)["profile_diff_all"](
        profile_dir=_profile(tmp_path)))
    assert sorted(got["compared"]) == ["design_rules", "erc_levels"]
    assert got["in_step"] is False
    assert bridge.writes == []


def test_everything_in_step_reads_in_step(monkeypatch, tmp_path):
    bridge = _Bridge(erc=_MATCHED, rules=[])
    directory = _profile(tmp_path, rules=False)
    got = asyncio.run(_tools(monkeypatch, bridge)["profile_diff_all"](
        profile_dir=directory))
    assert got["in_step"] is True and got["not_in_step"] == []


# --- one section failing must not hide the others ------------------------

def test_a_section_that_cannot_run_does_not_stop_the_others(
        monkeypatch, tmp_path):
    """The design rules need a board open and the ERC matrix needs a
    project; a caller with one of them should still get the other."""
    bridge = _Bridge(erc=_MATRIX, fail={"pcb.get_design_rules"})
    got = asyncio.run(_tools(monkeypatch, bridge)["profile_diff_all"](
        profile_dir=_profile(tmp_path)))
    assert got["sections"]["design_rules"]["ok"] is False
    assert "no document open" in got["sections"]["design_rules"]["reason"]
    assert got["compared"] == ["erc_levels"]
    assert got["sections"]["erc_levels"]["change"]


def test_a_failed_section_is_not_counted_as_in_step(monkeypatch, tmp_path):
    bridge = _Bridge(erc=_MATCHED, fail={"pcb.get_design_rules"})
    got = asyncio.run(_tools(monkeypatch, bridge)["profile_diff_all"](
        profile_dir=_profile(tmp_path)))
    assert got["compared"] == ["erc_levels"]
    assert got["in_step"] is True, (
        "in_step reports what was compared, and says separately what was not")
    assert got["sections"]["design_rules"]["ok"] is False


# --- applying ------------------------------------------------------------

def test_a_dry_run_writes_nothing(monkeypatch, tmp_path):
    bridge = _Bridge(erc=_MATRIX)
    got = asyncio.run(_tools(monkeypatch, bridge)["profile_apply_all"](
        profile_dir=_profile(tmp_path)))
    assert got["dry_run"] is True
    assert bridge.writes == []


def test_apply_writes_both_sections(monkeypatch, tmp_path):
    bridge = _Bridge(erc=_MATRIX)
    got = asyncio.run(_tools(monkeypatch, bridge)["profile_apply_all"](
        profile_dir=_profile(tmp_path), dry_run=False))
    commands = {c for c, _ in bridge.writes}
    assert "pcb.create_design_rule" in commands
    assert "project.set_erc_levels" in commands
    assert sorted(got["applied"]) == ["design_rules", "erc_levels"]
    assert got["failed"] == []


def test_a_partial_apply_names_what_was_written_and_what_was_not(
        monkeypatch, tmp_path):
    bridge = _Bridge(erc=_MATRIX, fail={"pcb.get_design_rules"})
    got = asyncio.run(_tools(monkeypatch, bridge)["profile_apply_all"](
        profile_dir=_profile(tmp_path), dry_run=False))
    assert got["applied"] == ["erc_levels"]
    assert got["failed"] == ["design_rules"]


def test_applying_twice_writes_nothing_the_second_time(monkeypatch, tmp_path):
    bridge = _Bridge(erc=_MATCHED)
    directory = _profile(tmp_path, rules=False)
    tools = _tools(monkeypatch, bridge)
    asyncio.run(tools["profile_apply_all"](profile_dir=directory,
                                           dry_run=False))
    before = len(bridge.writes)
    again = asyncio.run(tools["profile_apply_all"](profile_dir=directory,
                                                   dry_run=False))
    assert again["in_step"] is True
    assert len(bridge.writes) == before


# --- document settings ---------------------------------------------------

def test_document_settings_are_reported_as_apply_only(monkeypatch, tmp_path):
    """Nothing reads a schematic's units back, so there is no comparison to
    make; left out entirely it would look as though the profile's setting
    had been ignored."""
    bridge = _Bridge(erc=_MATRIX)
    got = asyncio.run(_tools(monkeypatch, bridge)["profile_diff_all"](
        profile_dir=_profile(tmp_path, documents=True)))
    assert got["sections"]["document_settings"]["apply_only"] is True
    assert "document_settings" not in got["compared"]


def test_document_settings_refuse_a_profile_that_is_not_the_active_one(
        monkeypatch, tmp_path):
    """profile_apply_document_settings reads the active profile and takes
    no directory, so applying it here would write a different profile's
    units and report them as this one's."""
    from eda_agent import profile as profile_module
    monkeypatch.setattr(profile_module, "get_active_profile", lambda *a: None)
    bridge = _Bridge(erc=_MATRIX)
    got = asyncio.run(_tools(monkeypatch, bridge)["profile_apply_all"](
        profile_dir=_profile(tmp_path, documents=True), dry_run=False))
    section = got["sections"]["document_settings"]
    assert section["ok"] is False and "active profile" in section["reason"]


# --- no profile at all ---------------------------------------------------

def test_no_profile_says_how_to_point_at_one(monkeypatch):
    from eda_agent import profile as profile_module
    monkeypatch.setattr(profile_module, "get_active_profile", lambda *a: None)
    got = asyncio.run(_tools(monkeypatch, _Bridge())["profile_diff_all"]())
    assert got["ok"] is False and "EDA_AGENT_PROFILE_DIR" in got["reason"]


def test_the_umbrella_calls_the_managers_own_tools(monkeypatch):
    """Collected from their registrars rather than re-implemented, so the
    umbrella cannot drift from the tool a caller would name directly."""
    tools = profile_apply.managed_tools()
    assert sorted(tools) == ["pcb_rules_apply", "pcb_rules_diff",
                             "pcb_rules_plan", "proj_erc_apply",
                             "proj_erc_diff", "proj_erc_plan"]
