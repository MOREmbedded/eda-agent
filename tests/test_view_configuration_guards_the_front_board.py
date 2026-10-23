# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""pcb_set_view_configuration acts only on the board it was asked about.

The toolbar combo belongs to whichever board is in front. Measured: a
switch meant for a scratch board landed on another session's board,
which that session had brought forward between the focus and the click.
"""
from __future__ import annotations

import asyncio

from eda_agent.tools import uiauto

_MINE = r"C:\scratch\Mine.PcbDoc"
_THEIRS = r"C:\other\Theirs.PcbDoc"


def _tool(monkeypatch, fronts, picked):
    """The tool with the bridge answering ``fronts`` in turn."""
    tools = {}

    class _Mcp:
        def tool(self, *a, **k):
            def reg(fn):
                tools[fn.__name__] = fn
                return fn
            return reg

        def __getattr__(self, n):
            return lambda *a, **k: (lambda fn: fn)

    answers = iter(fronts)

    class _Bridge:
        async def send_command_async(self, command, params=None, timeout=None):
            if command == "pcb.focus_board":
                return {"focused": True}
            return {"file_path": next(answers)}

    def _set(pid, name):
        picked.append(name)
        return {"ok": True, "value": name, "changed": True}

    monkeypatch.setattr("eda_agent.bridge.get_bridge", lambda: _Bridge())
    monkeypatch.setattr(uiauto, "_altium_pid", lambda reads_only=False: (1, None))
    monkeypatch.setattr(uiauto.menu, "set_view_configuration", _set)
    uiauto.register_uiauto_tools(_Mcp())
    return tools["pcb_set_view_configuration"]


def test_another_board_in_front_is_refused_untouched(monkeypatch):
    picked = []
    tool = _tool(monkeypatch, [_THEIRS], picked)
    out = asyncio.run(tool("Altium 3D Black", board_path=_MINE))
    assert out["ok"] is False and out["front_document"] == _THEIRS
    assert picked == [], "the combo was set on another board"


def test_a_board_brought_forward_mid_switch_is_reported(monkeypatch):
    picked = []
    tool = _tool(monkeypatch, [_MINE, _THEIRS], picked)
    out = asyncio.run(tool("Altium 3D Black", board_path=_MINE))
    assert picked == ["Altium 3D Black"]
    assert out["ok"] is False and _THEIRS in out["warning"]


def test_the_board_in_front_is_named_on_success(monkeypatch):
    picked = []
    tool = _tool(monkeypatch, [_MINE, _MINE.lower()], picked)
    out = asyncio.run(tool("Altium 3D Black", board_path=_MINE))
    assert out["ok"] is True and out["front_document"] == _MINE
