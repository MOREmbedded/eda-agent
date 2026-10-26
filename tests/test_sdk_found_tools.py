# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Tools built on calls found in Altium's SDK interface assembly.

lib_set_symbol_parts (ISch_Lib Add/Remove Part and Display Mode),
lib_export_3d_model (IPCB_Model.ExportToStep) and pcb_get_impedance_profiles
(IPCB_MasterLayerStack2 impedance profiles). What each sends, what each
refuses before Altium sees it, and the Pascal shapes that keep a refused or
missing call from passing for a success.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from eda_agent.tools import library, pcb

ALTIUM = Path(__file__).resolve().parents[1] / "scripts" / "altium"


def _body(unit: str, name: str) -> str:
    text = (ALTIUM / unit).read_text(encoding="utf-8-sig")
    start = text.index(f"Function {name}(")
    return text[start:text.index("\nEnd;\n", start)]


class _Bridge:
    def __init__(self):
        self.sent = []

    async def send_command_async(self, command, params, **kwargs):
        self.sent.append((command, params))
        return {"success": True}


def _tools(module, register, monkeypatch):
    bridge = _Bridge()
    monkeypatch.setattr(module, "get_bridge", lambda: bridge)
    captured = {}

    class _Mcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

        def __getattr__(self, name):
            return lambda *a, **k: (lambda fn: fn)

    getattr(module, register)(_Mcp())
    return captured, bridge


def test_symbol_parts_sends_only_what_was_asked(monkeypatch, tmp_path):
    lib = tmp_path / "Parts.SchLib"
    lib.write_bytes(b"")
    tools, bridge = _tools(library, "register_library_tools", monkeypatch)
    asyncio.run(tools["lib_set_symbol_parts"]("OPAMP", library_path=str(lib),
                                              part_count=4))
    assert bridge.sent == [("library.set_symbol_parts", {
        "name": "OPAMP", "part_count": "4", "library_path": str(lib)})]


@pytest.mark.parametrize("kwargs", [
    {"name": " ", "part_count": 2},
    {"name": "U", "part_count": 0},
    {"name": "U", "display_mode_count": 0},
    {"name": "U"},
    {"name": "U", "part_count": 2, "library_path": "C:/no/such.SchLib"},
])
def test_symbol_parts_refuses_before_altium(monkeypatch, kwargs):
    tools, bridge = _tools(library, "register_library_tools", monkeypatch)
    result = asyncio.run(tools["lib_set_symbol_parts"](**kwargs))
    assert bridge.sent == [] and result["ok"] is False


def test_symbol_parts_reads_back_every_step():
    body = _body("Library.pas", "Lib_SetSymbolParts")
    # Each add or remove is checked against the count before it, so a
    # command that does nothing stops the loop and says so.
    for call, check in (
            ("SetState_CurrentSchComponentAddPart", "If Comp.PartCount <= Was Then"),
            ("SetState_CurrentSchComponentRemovePart", "If Comp.PartCount >= Was Then"),
            ("SetState_CurrentSchComponentAddDisplayMode",
             "If Comp.DisplayModeCount <= Was Then"),
            ("SetState_CurrentSchComponentRemoveDisplayMode",
             "If Comp.DisplayModeCount >= Was Then")):
        assert body.index(call) < body.index(check), call
    assert body.count("Guard < 64") == 4
    assert "pins_removed" in body and "PinsBefore - PinsAfter" in body
    # A part is removed from the end: the last part is made current first.
    assert body.index("Comp.CurrentPartID := Was;") < body.index(
        "SetState_CurrentSchComponentRemovePart")


def test_model_export_sends_and_refuses(monkeypatch, tmp_path):
    lib = tmp_path / "Ics.PcbLib"
    lib.write_bytes(b"")
    out = tmp_path / "out" / "SOT23.step"
    tools, bridge = _tools(library, "register_library_tools", monkeypatch)
    asyncio.run(tools["lib_export_3d_model"]("SOT23", str(out), library_path=str(lib)))
    assert bridge.sent == [("library.export_model_step", {
        "footprint_name": "SOT23", "out_path": str(out), "body_index": "1",
        "library_path": str(lib)})]
    assert out.parent.is_dir()
    for kwargs in ({"footprint_name": "SOT23", "out_path": "rel.step"},
                   {"footprint_name": "", "out_path": str(out)},
                   {"footprint_name": "SOT23", "out_path": str(out), "body_index": 0},
                   {"footprint_name": "SOT23", "out_path": str(out),
                    "library_path": str(tmp_path / "Missing.PcbLib")}):
        bridge.sent.clear()
        assert asyncio.run(tools["lib_export_3d_model"](**kwargs))["ok"] is False
        assert bridge.sent == []


def test_model_export_reports_a_new_file_only():
    body = _body("Library.pas", "Lib_ExportModelStep")
    assert "Chosen.ExportToStep(OutPath);" in body
    assert "(AgeAfter <> -1) And (AgeAfter <> AgeBefore)" in body
    # Only STEP bodies (ModelType 1) are counted for body_index.
    assert "If Kind = 1 Then" in body
    assert "WRONG_DOCUMENT_FOCUSED" in body


def test_impedance_profiles_tool_sends_its_command(monkeypatch):
    tools, bridge = _tools(pcb, "register_pcb_tools", monkeypatch)
    asyncio.run(tools["pcb_get_impedance_profiles"]())
    assert bridge.sent == [("pcb.get_impedance_profiles", {})]


def test_impedance_profiles_guard_every_read():
    body = _body("PCB.pas", "PCB_GetImpedanceProfiles")
    # Every Profile./Trace. member read sits inside a Try, so a member
    # this build lacks costs one field, counted in read_errors.
    reads = [ln.strip() for ln in body.splitlines()
             if re.search(r"\b(Profile|Trace)\.\w+", ln) and ":=" in ln]
    assert reads and all(ln.startswith("Try ") for ln in reads), reads
    assert "read_errors" in body
    # Typed with the real interfaces: the declared type decides dispatch.
    assert "Profile : IPCB_ImpedanceProfile;" in body
    assert "Trace : IPCB_TraceImpedance;" in body


def test_step_export_api_handler_checks_for_a_new_file():
    body = _body("Project.pas", "Proj_ExportSTEPApi")
    assert "Exporter.ExportBoard3DModel(OutputPath, Format, Board, Proj);" in body
    assert "(AgeAfter <> -1) And (AgeAfter <> AgeBefore)" in body
    assert "Exporter : IPCB_Board3DModelExporter;" in body
