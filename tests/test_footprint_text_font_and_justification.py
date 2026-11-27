# SPDX-License-Identifier: Apache-2.0
"""lib_add_footprint_text takes a TrueType font and a justification.

QPA9903's layout notes on M8 came out in the default font instead of
Consolas, and its .Designator anchored at (0,0) on a corner instead of
being centred (2026-11-27).
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from eda_agent.tools import library

PAS = Path(__file__).resolve().parents[1] / "scripts" / "altium"


def _routine(file, name):
    text = (PAS / file).read_text(encoding="utf-8")
    start = re.search(rf"^Function {name}\b", text, re.M).start()
    end = re.search(r"^(Function|Procedure) ", text[start + 10:], re.M)
    return text[start:start + 10 + end.start()]


def test_the_handler_writes_the_font_and_a_justified_frame():
    body = _routine("Library.pas", "Lib_AddFootprintText")
    assert "Text.FontName := FontNameStr" in body
    assert "Text.MultilineTextAutoPosition := Just" in body
    assert "TextJustifyCode(JustStr)" in body
    assert "BoundingRectangle" not in body


def test_the_justification_helpers_live_where_both_callers_see_them():
    utils = (PAS / "Utils.pas").read_text(encoding="utf-8")
    pcb = (PAS / "PCB.pas").read_text(encoding="utf-8")
    for name in ("TextJustifyCode", "TextJustifyFracX", "TextJustifyFracY"):
        assert f"Function {name}(" in utils and f"Function {name}(" not in pcb


def test_the_tool_passes_font_and_justification(monkeypatch):
    sent = []

    class Bridge:
        async def send_command_async(self, command, params=None, timeout=None):
            sent.append(params)
            return {"success": True}

    monkeypatch.setattr(library, "get_bridge", lambda: Bridge())
    tools = {}

    class Mcp:
        def tool(self, *a, **k):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

        def __getattr__(self, name):
            return lambda *a, **k: (lambda fn: fn)

    library.register_library_tools(Mcp())
    asyncio.run(tools["lib_add_footprint_text"](
        text=".Designator", font_name="Consolas", justification="Center"))
    assert sent[-1]["font_name"] == "Consolas" and sent[-1]["justification"] == "center"
