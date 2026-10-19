# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""sim_setup_analysis saves Altium's copy, changes the file, reloads it.

MEASURED on AD 26.10.1.6: with the project open, an edit on disk is not
used by the next run, and Altium later saves its own copy over it. The
order is the whole fix, so the test records it. Nothing touches Altium.
"""

from __future__ import annotations

import asyncio

import pytest

from eda_agent.sim.simcfg import new_simcfg, read_simcfg, write_simcfg


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def setup(monkeypatch, tmp_path):
    from eda_agent.tools import sim as sim_mod
    project = tmp_path / "Amp.PrjPcb"
    calls = []
    state = {"docs": [], "file_at_reload": None}

    class Bridge:
        async def send_command_async(self, command, params=None, timeout=None):
            calls.append((command, dict(params or {})))
            if command == "project.get_focused":
                return {"project_path": str(project)}
            if command == "project.get_documents":
                return list(state["docs"])
            if command == "generic.simcfg_document":
                if params["action"] == "reload":
                    state["file_at_reload"] = read_simcfg(params["file_path"])
                return {"success": True, "open": True, "done": True}
            if command == "project.add_document":
                return {"success": True}
            raise AssertionError(f"unexpected command {command}")

    monkeypatch.setattr(sim_mod, "get_bridge", lambda: Bridge())
    mcp = _Mcp()
    sim_mod.register_sim_tools(mcp)
    return mcp.tools["sim_setup_analysis"], calls, state, tmp_path


def test_an_open_settings_file_is_saved_changed_then_reloaded(setup):
    tool, calls, state, tmp = setup
    cfg_path = tmp / "project_sim_config.simcfg"
    write_simcfg(new_simcfg(), cfg_path)
    state["docs"] = [{"file_path": str(cfg_path), "document_kind": "SimCfg"}]

    out = asyncio.run(tool(transient={"stop": "3u", "step": "10n"}, outputs=["v(out)"]))
    assert out["success"] and out["reloaded"] and not out["created"]

    sim_calls = [p["action"] for c, p in calls if c == "generic.simcfg_document"]
    assert sim_calls == ["save", "reload"]
    assert not any(c == "project.add_document" for c, _ in calls)
    # By the time Altium is told to reload, the file already holds the change.
    settings = dict(state["file_at_reload"].settings())["project"]
    assert settings["TRAN_StopTime"] == "3u" and settings["TRAN_Variable0"] == "v(out)"


def test_a_project_without_one_gets_a_file_added_and_loaded(setup):
    tool, calls, state, tmp = setup
    out = asyncio.run(tool(ac={"start": 10, "stop": "1meg", "points": 10}))
    assert out["success"] and out["created"]
    path = tmp / "project_sim_config.simcfg"
    assert out["simcfg"] == str(path) and path.exists()
    commands = [c for c, _ in calls]
    assert commands.index("project.add_document") < commands.index("generic.simcfg_document")
    assert dict(read_simcfg(path).settings())["project"]["AC_StopFrequency"] == "1meg"


def test_bad_settings_never_reach_altium(setup):
    tool, calls, _state, _tmp = setup
    out = asyncio.run(tool(transient={"stop": "1u", "step": 0}))
    assert out["error"] == "BAD_SETTINGS" and calls == []


def test_a_failed_save_stops_before_the_file_is_touched(setup, monkeypatch):
    tool, calls, state, tmp = setup
    cfg_path = tmp / "project_sim_config.simcfg"
    write_simcfg(new_simcfg(), cfg_path)
    before = cfg_path.read_bytes()
    state["docs"] = [{"file_path": str(cfg_path), "document_kind": "SimCfg"}]
    from eda_agent.tools import sim as sim_mod

    class Refusing:
        async def send_command_async(self, command, params=None, timeout=None):
            calls.append((command, params))
            if command == "project.get_focused":
                return {"project_path": str(tmp / "Amp.PrjPcb")}
            if command == "project.get_documents":
                return state["docs"]
            return {"success": False, "error": "SIMCFG_SAVE_FAILED"}

    monkeypatch.setattr(sim_mod, "get_bridge", lambda: Refusing())
    out = asyncio.run(tool(transient={"stop": "3u", "step": "10n"}))
    assert out["error"] == "SIMCFG_NOT_SAVED"
    assert cfg_path.read_bytes() == before
