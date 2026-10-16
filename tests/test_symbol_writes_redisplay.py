# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A symbol write is followed by switching the editor away and back.

MEASURED on AD 26.11: a polygon added to the SchLib symbol on screen was
not drawn, and a new symbol's shapes stayed off screen, until the editor
switched to another symbol and back, and only when the two switches were
separate commands. Saving or reopening was never needed.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from eda_agent.tools import library as lib_mod

LIBRARY_PAS = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "Library.pas"


class _Bridge:
    def __init__(self, write_reply, switched=True):
        self.calls = []
        self.write_reply = write_reply
        self.switched = switched

    async def send_command_async(self, command, params=None, **kwargs):
        self.calls.append((command, dict(params or {})))
        if command == "library.redisplay":
            return {"switched": self.switched}
        return dict(self.write_reply)


def _tools(monkeypatch, bridge):
    monkeypatch.setattr(lib_mod, "get_bridge", lambda: bridge)
    captured = {}

    class _Capture:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

        def __getattr__(self, name):
            return lambda *a, **k: (lambda fn: fn)

    lib_mod.register_library_tools(_Capture())
    return captured


def test_a_written_polygon_is_switched_away_then_back(monkeypatch):
    bridge = _Bridge({"success": True})
    tools = _tools(monkeypatch, bridge)
    out = asyncio.run(tools["lib_add_symbol_polygon"](vertices="0,0,400,200,0,400"))
    assert [c for c, _ in bridge.calls] == [
        "library.add_symbol_polygon", "library.redisplay", "library.redisplay"]
    assert [p.get("phase") for c, p in bridge.calls[1:]] == ["away", "back"]
    assert out["redisplayed"] is True


def test_nothing_is_switched_after_a_failed_write(monkeypatch):
    bridge = _Bridge({"success": False, "error": "NO_SCHLIB"})
    tools = _tools(monkeypatch, bridge)
    asyncio.run(tools["lib_add_symbol_rectangle"](x1=0, y1=0, x2=100, y2=100))
    assert [c for c, _ in bridge.calls] == ["library.add_symbol_rectangle"]


def test_no_way_back_is_asked_when_nothing_was_switched_away(monkeypatch):
    bridge = _Bridge({"success": True}, switched=False)
    tools = _tools(monkeypatch, bridge)
    out = asyncio.run(tools["lib_create_symbol"](name="X"))
    assert [p.get("phase") for c, p in bridge.calls if c == "library.redisplay"] == ["away"]
    assert out["redisplayed"] is False


def test_every_symbol_writer_redisplays():
    import inspect
    src = inspect.getsource(lib_mod)
    for name in ("lib_create_symbol", "lib_create_ic_symbol", "lib_create_passive_symbol",
                 "lib_add_pins", "lib_add_symbol_rectangle", "lib_add_symbol_text",
                 "lib_add_symbol_lines", "lib_add_symbol_arc", "lib_add_symbol_polygon"):
        m = re.search(rf"async def {name}\(.*?(?=\n    @mcp\.tool|\Z)", src, re.DOTALL)
        assert m and "_redisplay_symbol(" in m.group(0), name


def test_the_style_audit_can_save_and_reread_before_listing(monkeypatch):
    """MEASURED: the audit lists names from the saved file, so two symbols
    made this session were missing until the library was saved."""
    code = LIBRARY_PAS.read_text(encoding="ascii")
    start = code.index("Function Lib_AuditStyles(")
    body = code[start:start + code[start:].index("\nEnd;")]
    assert body.index("RefreshSchLibFromDisk(LibPath)") < body.index("CreateLibCompInfoReader(")
    assert """',"refreshed":' + BoolToJsonStr(Refreshed)""" in body
    bridge = _Bridge({"components": []})
    tools = _tools(monkeypatch, bridge)
    asyncio.run(tools["lib_audit_styles"](refresh=True))
    assert bridge.calls[0][1].get("refresh") == "true"


def test_the_pump_starts_holding_no_symbol():
    """MEASURED: after a relaunch the first symbol write read a member off
    the previous run's LastCreatedLibComponent and stopped the loop."""
    code = (LIBRARY_PAS.parent / "StatusForm.pas").read_text(encoding="ascii")
    start = code.index("Procedure StartMCPPump(")
    body = code[start:start + code[start:].index("\nEnd;")]
    running = body.index("Running := True;")
    for line in ("LastCreatedLibComponent := Nil;", "RedisplayLibComponent := Nil;"):
        assert body.index(line) < running, line


def test_the_way_back_only_assigns_the_held_symbol():
    """A member read on a symbol held across commands crashed the script
    after a reopen, so the held reference is only ever assigned back."""
    code = LIBRARY_PAS.read_text(encoding="ascii")
    start = code.index("Function Lib_Redisplay(")
    body = code[start:start + code[start:].index("\nEnd;")]
    body = re.sub(r"\{[^}]*\}", "", body)
    uses = re.findall(r"RedisplayLibComponent\.?\w*", body)
    assert all(u == "RedisplayLibComponent" for u in uses), uses
    assert "UpperCase(RedisplayLibPath) <> UpperCase(LibPath)" in body
    assert "'redisplay':" in code and "Lib_Redisplay(Params, RequestId)" in code
