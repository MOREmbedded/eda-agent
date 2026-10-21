# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""obj_explain_pin answers for the sheet in front of you.

It walked the focused project only, so with a scratch sheet active and a
production project focused, it explained the production project's U1
(issue #94). A sheet can now be named, the active sheet is searched
before the project, the reply names the sheet's full path, and the
compiled net is only read from a project the sheet belongs to.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from eda_agent.tools import generic as generic_tools

from tests.pascal_source import load

_GENERIC = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "Generic.pas"


def test_the_active_sheet_is_searched_before_the_project():
    body = load(_GENERIC)["Gen_ExplainPin"]
    active = body.index("Paths.Add(CurDoc.DocumentName);")
    project = body.index("Paths.Add(CandPath);")
    assert active < project
    assert "ExtractJsonValue(Params, 'sheet_path')" in body
    assert "FindProjectByPath(Workspace, ProjectPathParam)" in body


def test_the_reply_names_the_sheet_and_where_it_was_found():
    body = load(_GENERIC)["Gen_ExplainPin"]
    assert "JsonStr('sheet_path', FoundPath)" in body
    assert "JsonStr('found_on', ProjectFile)" in body
    assert "'active_sheet_outside_project'" in body


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


class _Bridge:
    def __init__(self, found_on):
        self.found_on = found_on
        self.sent = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.sent.append((command, dict(params or {})))
        if command == "generic.explain_pin":
            reply = {"designator": "U1", "pin": "1"}
            if self.found_on is not None:
                reply["found_on"] = self.found_on
            return reply
        if command == "project.get_connectivity":
            return {"pins": [{"pin_number": "1", "net": "PROD_NET"}]}
        return {}


def _explain(monkeypatch, found_on, **kw):
    bridge = _Bridge(found_on)
    monkeypatch.setattr(generic_tools, "get_bridge", lambda: bridge)
    mcp = _Mcp()
    generic_tools.register_generic_tools(mcp)
    out = asyncio.run(mcp.tools["obj_explain_pin"]("U1", "1", **kw))
    return out, [c for c, _ in bridge.sent], bridge.sent


def test_a_sheet_outside_the_project_gets_no_borrowed_net(monkeypatch):
    out, cmds, _ = _explain(monkeypatch, "active_sheet_outside_project")
    assert "project.get_connectivity" not in cmds
    assert "compiled_net" not in out


def test_a_sheet_in_the_project_gets_its_net(monkeypatch):
    out, _, _ = _explain(monkeypatch, "active_sheet_in_project")
    assert out["compiled_net"] == "PROD_NET"


def test_the_named_project_reaches_both_reads(monkeypatch):
    _, _, sent = _explain(monkeypatch, "project", project_path=r"C:\p\A.PrjPcb")
    for command, params in sent:
        assert params.get("project_path") == r"C:\p\A.PrjPcb", command


def test_an_older_script_keeps_the_old_behaviour(monkeypatch):
    """A script without found_on searched the focused project only, the
    same project the netlist read uses."""
    out, _, _ = _explain(monkeypatch, None)
    assert out["compiled_net"] == "PROD_NET"
