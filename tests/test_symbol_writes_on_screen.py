# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A primitive added to the symbol on screen goes in through the library.

MEASURED on AD 26.11: Component.AddSchObject on the symbol on screen put the
object in the data but not in the editor's view (not drawn, not found by a
query), and a library with one symbol has nothing to switch to and back.
SchLib.RegisterSchObjectInContainer drew polygons, rectangles, lines, arcs,
text and pins at once, but filed each on the part on screen: a part-2
polygon and a shared (0) power pin both landed on part 1 until the owner
was written back.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from eda_agent.tools import library as lib_mod

ALTIUM = Path(__file__).resolve().parents[1] / "scripts" / "altium"

WRITERS = ("Lib_AddPin", "Lib_AddSymbolRectangle", "Lib_AddSymbolLine",
           "Lib_AddSymbolArc", "Lib_AddSymbolPolygon", "Lib_AddPins",
           "Lib_AddSymbolText", "Lib_AddSymbolLines")


def _body(code: str, header: str, strip_comments: bool = True) -> str:
    start = code.index(header)
    body = code[start:start + code[start:].index("\nEnd;")]
    # The pattern would also eat JSON braces inside string literals.
    return re.sub(r"\{[^}]*\}", "", body) if strip_comments else body


class _Bridge:
    def __init__(self, write_reply):
        self.calls = []
        self.write_reply = write_reply

    async def send_command_async(self, command, params=None, **kwargs):
        self.calls.append((command, dict(params or {})))
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


def test_a_symbol_write_is_one_command(monkeypatch):
    """MEASURED: a primitive written to a symbol that was not on screen was
    there once the symbol was opened, so nothing follows the write."""
    bridge = _Bridge({"success": True, "shown": False})
    tools = _tools(monkeypatch, bridge)
    asyncio.run(tools["lib_add_symbol_polygon"](vertices="0,0,400,200,0,400"))
    assert [c for c, _ in bridge.calls] == ["library.add_symbol_polygon"]


def test_every_symbol_writer_adds_through_the_shared_helper():
    code = (ALTIUM / "Library.pas").read_text(encoding="ascii")
    for name in WRITERS:
        body = _body(code, f"Function {name}(")
        assert "AddLibSymbolPrim(SchLib, Component," in body, name
        assert "AddSchObject(" not in body, name
        assert '"shown":\' + BoolToJsonStr(ViaLib)' in _body(
            code, f"Function {name}(", strip_comments=False), name


def test_the_helper_uses_the_library_only_for_the_symbol_on_screen():
    code = (ALTIUM / "Library.pas").read_text(encoding="ascii")
    body = _body(code, "Function AddLibSymbolPrim(")
    assert "Shown.I_ObjectAddress = Component.I_ObjectAddress" in body
    lib_route = body.index("SchLib.RegisterSchObjectInContainer(Obj);")
    comp_route = body.index("Component.AddSchObject(Obj);")
    assert lib_route < body.index("Else") < comp_route


def test_the_helper_puts_the_callers_owner_part_back():
    code = (ALTIUM / "Library.pas").read_text(encoding="ascii")
    body = _body(code, "Function AddLibSymbolPrim(")
    saved = body.index("OwnerPart := Obj.OwnerPartId;")
    registered = body.index("SchLib.RegisterSchObjectInContainer(Obj);")
    restored = body.index("Obj.OwnerPartId := OwnerPart;")
    assert saved < registered < restored
    assert body.index("Obj.OwnerPartDisplayMode := OwnerMode;") > registered


def test_symbol_graphics_have_schematic_names_the_board_does_not_take():
    generic = (ALTIUM / "Generic.pas").read_text(encoding="ascii")
    utils = (ALTIUM / "Utils.pas").read_text(encoding="ascii")
    sch = dict(re.findall(r"N = '(\w+)'\s+Then Result := (e\w+)", generic))
    pcb = set(re.findall(r"N = '(\w+)'\s+Then Result := e\w+Object", utils))
    for name, const in (("schpolygon", "ePolygon"), ("scharc", "eArc"),
                        ("schellipticalarc", "eEllipticalArc"),
                        ("schellipse", "eEllipse"), ("schbezier", "eBezier")):
        assert sch.get(name) == const, name
        assert name not in pcb, name
        assert f"Id = {const} Then Result := 'sch_" in generic, const
    assert "polygon" in pcb and "polygon" not in sch


def test_the_style_audit_can_save_and_reread_before_listing(monkeypatch):
    """MEASURED: the audit lists names from the saved file, so two symbols
    made this session were missing until the library was saved."""
    code = (ALTIUM / "Library.pas").read_text(encoding="ascii")
    body = _body(code, "Function Lib_AuditStyles(", strip_comments=False)
    assert body.index("RefreshSchLibFromDisk(LibPath)") < body.index("CreateLibCompInfoReader(")
    assert """',"refreshed":' + BoolToJsonStr(Refreshed)""" in body
    bridge = _Bridge({"components": []})
    tools = _tools(monkeypatch, bridge)
    asyncio.run(tools["lib_audit_styles"](refresh=True))
    assert bridge.calls[0][1].get("refresh") == "true"


def test_the_pump_starts_holding_no_symbol():
    """MEASURED: after a relaunch the first symbol write read a member off
    the previous run's LastCreatedLibComponent and stopped the loop."""
    code = (ALTIUM / "StatusForm.pas").read_text(encoding="ascii")
    body = _body(code, "Procedure StartMCPPump(")
    assert body.index("LastCreatedLibComponent := Nil;") < body.index("Running := True;")
