# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""plan, diff and apply over a workspace profile's ERC matrix.

Plan reads the profile and nothing else, so it works with Altium closed.
Diff adds one read of Project Options > Error Reporting. Apply writes only
the types that differ, which is what makes a second apply a no-op, and it
never touches a type the set does not name.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from eda_agent import bridge as bridge_module
from eda_agent.tools import erc as erc_tools


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

    def __init__(self, matrix=()):
        self.matrix = list(matrix)
        self.sent = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.sent.append((command, dict(params or {})))
        if command == "project.get_erc_levels":
            return {"project": "C:/p/Board.PrjPcb", "count": len(self.matrix),
                    "levels": self.matrix}
        if command == "project.set_erc_levels":
            items = [i for i in str(params.get("levels", "")).split("~~") if i]
            for item in items:
                kind, _, level = item.partition("=")
                for row in self.matrix:
                    if str(row["kind"]) == kind.strip():
                        row["level"] = level.strip()
            return {"project": "C:/p/Board.PrjPcb", "set": len(items),
                    "failed": 0,
                    "results": [{"kind_given": i.split("=")[0], "level":
                                 i.split("=")[1]} for i in items]}
        if command == "application.save_document":
            return {"saved": True}
        return {"success": True}

    @property
    def writes(self):
        return [s for s in self.sent if s[0] != "project.get_erc_levels"]


def _tools(monkeypatch, bridge):
    monkeypatch.setattr(bridge_module, "get_bridge", lambda: bridge)
    mcp = _Mcp()
    erc_tools.register_erc_tools(mcp)
    return mcp.tools


def _matrix(*rows):
    return [{"kind": kind, "description": description, "level": level}
            for kind, description, level in rows]


_PROJECT = (
    (0, "Duplicate Net Names", "warning"),
    (1, "Nets with multiple names", "warning"),
    (2, "Nets with no driving source", "error"),
    (3, "Floating power object", "error"),
)

_SET = {"levels": {"Nets with multiple names": "no_report",
                   "Nets with no driving source": "error"}}


def _profile(tmp_path, data=None, name=""):
    path = (tmp_path / "erc_levels.json" if not name
            else tmp_path / "erc_levels" / f"{name}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_SET if data is None else data), encoding="utf-8")
    return str(tmp_path)


# --- plan ----------------------------------------------------------------

def test_plan_reads_the_profile_and_touches_no_project(monkeypatch, tmp_path):
    bridge = _Bridge()
    got = asyncio.run(_tools(monkeypatch, bridge)["proj_erc_plan"](
        profile_dir=_profile(tmp_path)))
    assert got["ok"] is True and got["count"] == 2
    assert got["unlisted"] == "leave"
    assert bridge.sent == [], "plan is pure Python"


def test_plan_takes_levels_inline_without_a_profile(monkeypatch):
    got = asyncio.run(_tools(monkeypatch, _Bridge())["proj_erc_plan"](
        levels={"Duplicate Net Names": "error"}))
    assert got["levels"] == {"Duplicate Net Names": "error"}
    assert got["source"] == "levels"


def test_plan_merges_a_set_that_extends_another(monkeypatch, tmp_path):
    _profile(tmp_path, {"levels": {"A": "warning", "B": "error"}}, "company")
    _profile(tmp_path, {"extends": "company", "levels": {"B": "fatal"}}, "house")
    got = asyncio.run(_tools(monkeypatch, _Bridge())["proj_erc_plan"](
        profile_dir=str(tmp_path), set_name="house"))
    assert got["levels"] == {"A": "warning", "B": "fatal"}
    assert got["chain"] == ["company.json", "house.json"]


def test_a_missing_set_is_refused_and_lists_the_ones_there(monkeypatch, tmp_path):
    _profile(tmp_path, name="strict")
    got = asyncio.run(_tools(monkeypatch, _Bridge())["proj_erc_plan"](
        profile_dir=str(tmp_path), set_name="relaxed"))
    assert got["ok"] is False
    assert "relaxed" in got["reason"] and "strict" in got["reason"]


def test_a_profile_with_no_erc_file_says_what_one_is(monkeypatch, tmp_path):
    got = asyncio.run(_tools(monkeypatch, _Bridge())["proj_erc_plan"](
        profile_dir=str(tmp_path)))
    assert got["ok"] is False and "erc_levels.json" in got["reason"]


def test_no_profile_and_no_levels_says_how_to_point_at_one(monkeypatch):
    from eda_agent import profile as profile_module
    monkeypatch.setattr(profile_module, "get_active_profile", lambda *a: None)
    got = asyncio.run(_tools(monkeypatch, _Bridge())["proj_erc_plan"]())
    assert got["ok"] is False and "EDA_AGENT_PROFILE_DIR" in got["reason"]


# --- diff ----------------------------------------------------------------

def test_diff_reports_only_the_types_that_differ(monkeypatch, tmp_path):
    bridge = _Bridge(_matrix(*_PROJECT))
    got = asyncio.run(_tools(monkeypatch, bridge)["proj_erc_diff"](
        profile_dir=_profile(tmp_path)))
    assert [e["kind"] for e in got["change"]] == [1]
    assert [e["kind"] for e in got["same"]] == [2]
    assert got["in_step"] is False
    assert bridge.writes == []


def test_a_type_the_set_does_not_name_is_unmanaged(monkeypatch, tmp_path):
    bridge = _Bridge(_matrix(*_PROJECT))
    got = asyncio.run(_tools(monkeypatch, bridge)["proj_erc_diff"](
        profile_dir=_profile(tmp_path)))
    assert [s["kind"] for s in got["unmanaged"]] == [0, 3]
    assert got["unmanaged_count"] == 2


def test_a_project_already_at_the_policy_reads_in_step(monkeypatch, tmp_path):
    bridge = _Bridge(_matrix(
        (1, "Nets with multiple names", "no_report"),
        (2, "Nets with no driving source", "error")))
    got = asyncio.run(_tools(monkeypatch, bridge)["proj_erc_diff"](
        profile_dir=_profile(tmp_path)))
    assert got["in_step"] is True and got["change"] == []


def test_a_bridge_error_is_passed_back_not_compared_against(monkeypatch, tmp_path):
    class _Broken(_Bridge):
        async def send_command_async(self, command, params=None, timeout=None):
            return {"error": "no project is focused"}

    got = asyncio.run(_tools(monkeypatch, _Broken())["proj_erc_diff"](
        profile_dir=_profile(tmp_path)))
    assert got["ok"] is False and "no project" in got["reason"]


# --- apply ---------------------------------------------------------------

def test_a_dry_run_writes_nothing(monkeypatch, tmp_path):
    bridge = _Bridge(_matrix(*_PROJECT))
    got = asyncio.run(_tools(monkeypatch, bridge)["proj_erc_apply"](
        profile_dir=_profile(tmp_path)))
    assert got["dry_run"] is True and got["change"]
    assert got["written"] == 0 and bridge.writes == []


def test_apply_writes_the_differing_types_by_kind(monkeypatch, tmp_path):
    bridge = _Bridge(_matrix(*_PROJECT))
    got = asyncio.run(_tools(monkeypatch, bridge)["proj_erc_apply"](
        profile_dir=_profile(tmp_path), dry_run=False))
    assert got["ok"] is True and got["written"] == 1
    assert bridge.writes[0] == ("project.set_erc_levels",
                                {"levels": "1=no_report"})


def test_applying_twice_writes_nothing_the_second_time(monkeypatch, tmp_path):
    bridge = _Bridge(_matrix(*_PROJECT))
    tools = _tools(monkeypatch, bridge)
    asyncio.run(tools["proj_erc_apply"](profile_dir=_profile(tmp_path),
                                        dry_run=False))
    before = len(bridge.writes)
    again = asyncio.run(tools["proj_erc_apply"](profile_dir=_profile(tmp_path),
                                                dry_run=False))
    assert again["in_step"] is True and again["written"] == 0
    assert len(bridge.writes) == before


def test_the_project_is_saved_because_the_matrix_lives_in_it(monkeypatch, tmp_path):
    bridge = _Bridge(_matrix(*_PROJECT))
    got = asyncio.run(_tools(monkeypatch, bridge)["proj_erc_apply"](
        profile_dir=_profile(tmp_path), dry_run=False))
    assert got["saved"] is True
    assert ("application.save_document",
            {"file_path": "C:/p/Board.PrjPcb"}) in bridge.writes


def test_save_false_leaves_the_change_unsaved(monkeypatch, tmp_path):
    bridge = _Bridge(_matrix(*_PROJECT))
    got = asyncio.run(_tools(monkeypatch, bridge)["proj_erc_apply"](
        profile_dir=_profile(tmp_path), dry_run=False, save=False))
    assert got["saved"] is False
    assert not any(c == "application.save_document" for c, _ in bridge.writes)


def test_a_type_the_set_does_not_name_is_never_written(monkeypatch, tmp_path):
    bridge = _Bridge(_matrix(*_PROJECT))
    asyncio.run(_tools(monkeypatch, bridge)["proj_erc_apply"](
        profile_dir=_profile(tmp_path), dry_run=False))
    written = [w for c, w in bridge.writes if c == "project.set_erc_levels"]
    assert "0=" not in str(written) and "3=" not in str(written)


def test_unlisted_pins_the_rest_of_the_matrix(monkeypatch, tmp_path):
    bridge = _Bridge(_matrix(*_PROJECT))
    got = asyncio.run(_tools(monkeypatch, bridge)["proj_erc_apply"](
        profile_dir=_profile(tmp_path), dry_run=False, unlisted="no_report"))
    assert got["written"] == 3, got["change"]
    assert "0=no_report" in bridge.writes[0][1]["levels"]


def test_an_unknown_type_leaves_the_apply_not_ok(monkeypatch, tmp_path):
    bridge = _Bridge(_matrix(*_PROJECT))
    directory = _profile(tmp_path, {"levels": {"Net has no namne": "error"}})
    got = asyncio.run(_tools(monkeypatch, bridge)["proj_erc_apply"](
        profile_dir=directory, dry_run=False))
    assert got["ok"] is False and got["unknown"]
    assert bridge.writes == [], "nothing resolved, so nothing to write"


def test_a_long_matrix_is_written_in_more_than_one_call(monkeypatch, tmp_path):
    rows = [(i, f"Type {i}", "warning") for i in range(erc_tools.WRITE_CHUNK + 5)]
    bridge = _Bridge(_matrix(*rows))
    directory = _profile(tmp_path, {"levels": {}, "unlisted": "error"})
    got = asyncio.run(_tools(monkeypatch, bridge)["proj_erc_apply"](
        profile_dir=directory, dry_run=False))
    calls = [w for c, w in bridge.writes if c == "project.set_erc_levels"]
    assert len(calls) == 2
    assert got["written"] == erc_tools.WRITE_CHUNK + 5


def test_a_refusal_from_plan_is_passed_through_by_apply(monkeypatch, tmp_path):
    got = asyncio.run(_tools(monkeypatch, _Bridge())["proj_erc_apply"](
        profile_dir=str(tmp_path), dry_run=False))
    assert got["ok"] is False and "erc_levels.json" in got["reason"]


def test_levels_inline_and_empty_pins_the_whole_matrix(monkeypatch):
    """An empty dict is a caller, not a missing argument: with unlisted it
    is how somebody asks for the whole matrix at one level."""
    bridge = _Bridge(_matrix(*_PROJECT))
    got = asyncio.run(_tools(monkeypatch, bridge)["proj_erc_apply"](
        levels={}, unlisted="warning", dry_run=False))
    assert got["written"] == 2, got["change"]
    assert "2=warning" in bridge.writes[0][1]["levels"]
    assert "3=warning" in bridge.writes[0][1]["levels"]


def test_a_change_the_write_cannot_carry_is_reported_not_dropped(monkeypatch):
    bridge = _Bridge([{"kind": None, "description": "Odd", "level": "warning"}])
    got = asyncio.run(_tools(monkeypatch, bridge)["proj_erc_apply"](
        levels={"Odd": "error"}, dry_run=False))
    assert [u["description"] for u in got["unwritable"]] == ["Odd"]
    assert got["ok"] is False and bridge.writes == []


def test_a_refused_write_is_counted_as_failed_not_as_nothing(monkeypatch, tmp_path):
    """A refusal is a dict carrying an error and no counts, so adding up
    set and failed alone reported a whole batch that never landed as
    nothing having gone wrong."""
    class _Refusing(_Bridge):
        async def send_command_async(self, command, params=None, timeout=None):
            if command == "project.set_erc_levels":
                self.sent.append((command, dict(params or {})))
                return {"error": "the project is read-only"}
            return await _Bridge.send_command_async(self, command, params,
                                                    timeout)

    bridge = _Refusing(_matrix(*_PROJECT))
    got = asyncio.run(_tools(monkeypatch, bridge)["proj_erc_apply"](
        profile_dir=_profile(tmp_path), dry_run=False))
    assert got["ok"] is False
    assert got["written"] == 0 and got["failed"] == 1
    assert "read-only" in got["results"][0]["problem"]
    assert got["saved"] is False
