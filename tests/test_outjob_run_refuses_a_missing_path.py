# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""An OutJob path that is not there is refused before Altium sees it.

MEASURED on AD 26.11: a run with outjob_path set to a file that does not
exist did not fail cleanly. Altium opened a NEW, empty OutJob at that
path and made it the active document, so the run wrote nothing and every
later tool refused to work on the document now in focus.
"""

from __future__ import annotations

import asyncio

from eda_agent.tools import project


def _tools(bridge, monkeypatch):
    monkeypatch.setattr(project, "get_bridge", lambda: bridge)
    tools = {}

    class _Mcp:
        def tool(self, *a, **k):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

    project.register_project_tools(_Mcp())
    return tools


class _Bridge:
    def __init__(self):
        self.sent = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.sent.append(command)
        return {}


def test_a_path_that_is_not_a_file_is_refused_and_nothing_is_sent(monkeypatch, tmp_path):
    bridge = _Bridge()
    got = asyncio.run(_tools(bridge, monkeypatch)["proj_run_outjob"](
        "Gerber", outjob_path=str(tmp_path / "nope" / "missing.OutJob")))
    assert got["success"] is False
    assert "is not a file" in got["reason"]
    assert not bridge.sent


def test_a_directory_is_not_a_file_either(monkeypatch, tmp_path):
    bridge = _Bridge()
    got = asyncio.run(_tools(bridge, monkeypatch)["proj_run_outjob"](
        "Gerber", outjob_path=str(tmp_path)))
    assert got["success"] is False and not bridge.sent


def test_a_real_outjob_still_runs(monkeypatch, tmp_path):
    outjob = tmp_path / "Fab.OutJob"
    outjob.write_text("[OutputGroup1]\nOutputMedium1=Gerber\n", encoding="utf-8")
    bridge = _Bridge()
    asyncio.run(_tools(bridge, monkeypatch)["proj_run_outjob"](
        "Gerber", outjob_path=str(outjob)))
    # The fab check reads which outputs the container writes first.
    assert bridge.sent[-1] == "project.run_outjob"
    assert set(bridge.sent[:-1]) <= {"project.get_outjob_containers"}


def test_no_path_is_still_allowed(monkeypatch):
    """Omitting the path means "the focused project's first OutJob"."""
    bridge = _Bridge()
    asyncio.run(_tools(bridge, monkeypatch)["proj_run_outjob"]("Gerber"))
    # The fab check reads which outputs the container writes first.
    assert bridge.sent[-1] == "project.run_outjob"
    assert set(bridge.sent[:-1]) <= {"project.get_outjob_containers"}
