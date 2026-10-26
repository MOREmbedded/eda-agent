# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A custom sheet size and a fractional layer thickness both reach Altium.

sch_set_sheet_size style="Custom" named eSheetCustom, which does not exist:
the script halted with "Undeclared identifier" on AD 26.11 before anything
changed. A custom size is the UseCustomSheet flag, as Altium's own template
scripts set it, and SheetSizeX/Y only follow after UpdateDocumentProperties.

pcb_modify_layer took whole mils, so 1 oz copper (1.378 mil) could not be
set. Both verified live on AD 26.11.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from eda_agent.tools import pcb as pcb_tools

from tests.pascal_source import load

_ALTIUM = Path(__file__).resolve().parents[1] / "scripts" / "altium"


def _sheet():
    return load(_ALTIUM / "Generic.pas")["Gen_SetSheetSize"]


def test_a_custom_sheet_is_the_flag_not_a_style():
    body = _sheet()
    assert "eSheetCustom" not in body.replace(
        "There is no eSheetCustom", "")
    assert "SchDoc.UseCustomSheet := True;" in body
    assert "If StyleStr <> 'CUSTOM' Then SchDoc.UseCustomSheet := False;" in body


def test_the_frame_is_refreshed_before_it_is_read_back():
    body = _sheet()
    assert body.index("SchDoc.UpdateDocumentProperties;") < \
        body.index('"frame_x"')
    assert '"use_custom"' in body and '"custom_x"' in body


def test_layer_thicknesses_are_parsed_as_fractions():
    body = load(_ALTIUM / "PCB.pas")["PCB_ModifyLayer"]
    assert "MilsToCoordF(StrToFloatDef(ThickStr, 0))" in body
    assert "MilsToCoordF(StrToFloatDef(HeightStr, 0))" in body
    assert "StrToIntDef(ThickStr" not in body
    assert "StrToIntDef(HeightStr" not in body


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


def test_one_ounce_copper_reaches_the_script(monkeypatch):
    sent = []

    class _Bridge:
        async def send_command_async(self, command, params=None, timeout=None):
            sent.append(dict(params or {}))
            return {"success": True}

    monkeypatch.setattr(pcb_tools, "get_bridge", lambda: _Bridge())
    mcp = _Mcp()
    pcb_tools.register_pcb_tools(mcp)
    asyncio.run(mcp.tools["pcb_modify_layer"](
        layer="TopLayer", copper_thickness_mils=1.378,
        dielectric_height_mils=4.33))
    assert sent[0]["copper_thickness_mils"] == "1.378"
    assert sent[0]["dielectric_height_mils"] == "4.33"
