# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""app_ping names the Main.pas it compared, and where Altium loaded from.

version_match compares the running script's SCRIPT_VERSION with the
Main.pas this server can see. With Altium running the script project from
a working tree somewhere else, that comparison was against the wrong file
and version_match true meant nothing. The reply now carries the path it
read, the Altium_API.PrjScr paths the running script reports, and a
warning when none of those is a folder this server compares with.
version_match keeps its meaning.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path, PureWindowsPath

from eda_agent import cli
from eda_agent.tools import application as app_mod
from eda_agent.tools.registry import ToolRegistry

_PAS = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "Application.pas"

BUNDLED = Path("D:/site/eda_agent/scripts")
INSTALLED = Path("D:/profile/EDA Agent/scripts")
TREE = str(PureWindowsPath(r"D:\checkout\eda-agent\scripts\altium\Altium_API.PrjScr"))


def _ping(monkeypatch, reply):
    class _Bridge:
        def is_altium_running(self):
            return True

        def ping_with_version(self):
            return reply

    monkeypatch.setattr(app_mod, "get_bridge", lambda: _Bridge())
    monkeypatch.setattr(app_mod, "get_bundled_scripts_path", lambda: BUNDLED)
    monkeypatch.setattr(app_mod, "_bundled_script_version", lambda: "2026.10.09.1")
    monkeypatch.setattr(cli, "get_default_scripts_dest", lambda: INSTALLED)
    registry = ToolRegistry()
    app_mod.register_application_tools(registry)
    tools = {t.name: t.fn for t in asyncio.run(registry.list_tools())}
    return asyncio.run(tools["app_ping"]())


def _reply(projects):
    out = {"pong": True, "script_version": "2026.10.09.1", "altium_version": "26.10.1.6"}
    if projects is not None:
        out["script_projects"] = projects
    return out


def test_the_compared_file_is_named(monkeypatch):
    out = _ping(monkeypatch, _reply([]))
    assert out["bundled_script_path"] == str(BUNDLED / "Main.pas")


def test_a_project_loaded_from_elsewhere_is_warned_about(monkeypatch):
    out = _ping(monkeypatch, _reply([TREE]))
    assert out["version_match"] is True, "the comparison itself is unchanged"
    assert out["script_location_match"] is False
    assert out["script_projects_open"] == [TREE]
    assert any(TREE in w and str(BUNDLED / "Main.pas") in w for w in out["warnings"])
    assert TREE in out["message"]


def test_the_bundled_and_installed_folders_both_match(monkeypatch):
    for folder in (BUNDLED, INSTALLED):
        path = str(PureWindowsPath(folder) / "Altium_API.PrjScr").upper()
        out = _ping(monkeypatch, _reply([path]))
        assert out["script_location_match"] is True, folder
        assert out["warnings"] == []


def test_a_script_too_old_to_say_is_unknown_not_a_mismatch(monkeypatch):
    out = _ping(monkeypatch, _reply(None))
    assert out["script_location_match"] is None
    assert out["script_projects_open"] is None
    assert out["warnings"] == []


def test_two_open_script_projects_are_named(monkeypatch):
    mine = str(PureWindowsPath(BUNDLED) / "Altium_API.PrjScr")
    out = _ping(monkeypatch, _reply([mine, TREE]))
    assert out["script_location_match"] is True
    assert any("2 Altium_API.PrjScr projects are open" in w for w in out["warnings"])


def test_the_script_reports_its_open_script_projects():
    text = _PAS.read_text(encoding="utf-8", errors="replace")
    body = text[text.index("Function App_Ping"):text.index("\nEnd;", text.index("Function App_Ping"))]
    assert '"script_projects":[' in body
    # Read inside a Try, so a failure costs the field and not the ping.
    loop = body.index("DM_ProjectCount")
    assert body.rfind("Try", 0, loop) > body.rfind("End;", 0, loop)
    assert re.search(r"DM_ProjectFullPath", body)
