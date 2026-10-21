# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Running ERC answers with what ERC found.

Gen_RunERC compiled, ran Sch:ERC and replied "ERC completed on project".
The autonomy gate reads ``violation_count`` off proj_run_erc, so it was
reading a key the reply never carried. The handler now returns the
violation list, and the tool falls back to reading it when an older
deployed script still answers with the message alone.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from eda_agent.tools import generic as generic_tools

from tests.pascal_source import load

_GENERIC = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "Generic.pas"


def test_the_handler_returns_the_violation_list():
    body = load(_GENERIC)["Gen_RunERC"]
    assert "RunProcess('Sch:ERC')" in body
    assert "Result := Gen_GetErcViolations(Params, RequestId)" in body
    assert "ERC completed" not in body


def test_the_reader_is_defined_before_the_runner():
    """DelphiScript has no forward declarations: calling a function
    defined further down is an undeclared identifier at run time."""
    text = _GENERIC.read_text(encoding="utf-8")
    assert (text.index("Function Gen_GetErcViolations(")
            < text.index("Function Gen_RunERC("))


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


class _Bridge:
    def __init__(self, run_reply):
        self.run_reply = run_reply
        self.sent = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.sent.append(command)
        if command == "generic.run_erc":
            return dict(self.run_reply)
        if command == "generic.get_erc_violations":
            return {"violation_count": 2, "violations": [{}, {}]}
        return {}


def _run(monkeypatch, bridge):
    monkeypatch.setattr(generic_tools, "get_bridge", lambda: bridge)
    mcp = _Mcp()
    generic_tools.register_generic_tools(mcp)
    return asyncio.run(mcp.tools["proj_run_erc"]())


def test_a_new_script_reply_is_returned_as_is(monkeypatch):
    bridge = _Bridge({"violation_count": 0, "violations": []})
    out = _run(monkeypatch, bridge)
    assert out["violation_count"] == 0
    assert bridge.sent == ["generic.run_erc"]


def test_an_old_script_reply_is_followed_by_the_list(monkeypatch):
    bridge = _Bridge({"success": True, "message": "ERC completed on project"})
    out = _run(monkeypatch, bridge)
    assert out["violation_count"] == 2
    assert bridge.sent == ["generic.run_erc", "generic.get_erc_violations"]
