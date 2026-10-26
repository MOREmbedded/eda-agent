# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Tools added for Altium.

Symbol parts, display modes and aliases, model and part STEP export,
impedance profiles, variant entries, net colours, the rule Altium applies,
rule scopes, net clean-up, drill pairs, declared classes and rooms,
sheet zones, and the extra board and pin properties. What each sends, what each
refuses before Altium sees it, and the Pascal shapes that keep a refused or
missing call from passing for a success.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from eda_agent.tools import generic, library, pcb, project

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


def _sent(module, register, monkeypatch, tool, **kwargs):
    tools, bridge = _tools(module, register, monkeypatch)
    result = asyncio.run(tools[tool](**kwargs))
    return result, bridge.sent


def test_variation_sends_and_refuses(monkeypatch):
    _, sent = _sent(project, "register_project_tools", monkeypatch,
                    "proj_set_component_variation", variant="Lite",
                    designator="R12", kind="not_fitted")
    assert sent == [("project.set_component_variation",
                     {"variant": "Lite", "designator": "R12", "kind": "not_fitted"})]
    for kwargs in ({"variant": "Lite", "designator": "R12", "kind": "maybe"},
                   {"variant": "", "designator": "R12", "kind": "fitted"},
                   {"variant": "Lite", "designator": "R12", "kind": "alternate"}):
        result, sent = _sent(project, "register_project_tools", monkeypatch,
                             "proj_set_component_variation", **kwargs)
        assert sent == [] and result["ok"] is False


def test_net_color_goes_to_altium_as_bgr(monkeypatch):
    _, sent = _sent(project, "register_project_tools", monkeypatch,
                    "proj_net_color", net="VBUS", color="#FF8000")
    assert sent == [("project.net_color", {"net": "VBUS", "color": str(0x0080FF)})]
    result, sent = _sent(project, "register_project_tools", monkeypatch,
                         "proj_net_color", net="VBUS", color="orange")
    assert sent == [] and result["ok"] is False


def test_rule_tools_need_their_objects(monkeypatch):
    _, sent = _sent(pcb, "register_pcb_tools", monkeypatch, "pcb_find_dominant_rule",
                    object1="U1.3", object2="net:GND", rule_name="Clearance")
    assert sent == [("pcb.find_dominant_rule",
                     {"object1": "U1.3", "object2": "net:GND", "rule_name": "Clearance"})]
    result, sent = _sent(pcb, "register_pcb_tools", monkeypatch,
                         "pcb_find_dominant_rule", object1="U1")
    assert sent == [] and result["ok"] is False
    _, sent = _sent(pcb, "register_pcb_tools", monkeypatch, "pcb_check_rule_scope",
                    rule_name="Width_VBUS", object1="net:VBUS")
    assert sent == [("pcb.rule_scope_check",
                     {"rule_name": "Width_VBUS", "object1": "net:VBUS"})]


def test_drill_pairs_bound_their_input(monkeypatch):
    _, sent = _sent(pcb, "register_pcb_tools", monkeypatch, "pcb_set_drill_pair",
                    index=1, backdrill=True)
    assert sent == [("pcb.set_drill_pair", {"index": "1", "backdrill": "true"})]
    result, sent = _sent(pcb, "register_pcb_tools", monkeypatch, "pcb_set_drill_pair",
                         index=1)
    assert sent == [] and result["ok"] is False


def test_aliases_and_sheet_options_send_what_was_asked(monkeypatch, tmp_path):
    lib = tmp_path / "Logic.SchLib"
    lib.write_bytes(b"")
    _, sent = _sent(library, "register_library_tools", monkeypatch,
                    "lib_set_symbol_aliases", name="74HC00", library_path=str(lib),
                    add=["74HCT00", " "], remove=["74LS00"])
    assert sent == [("library.set_symbol_aliases", {
        "name": "74HC00", "add": "74HCT00", "remove": "74LS00",
        "library_path": str(lib)})]
    _, sent = _sent(generic, "register_generic_tools", monkeypatch,
                    "sch_set_sheet_options", zones_x=6, reference_zones=False)
    assert sent == [("generic.sheet_options",
                     {"zones_x": "6", "reference_zones": "false"})]
    result, sent = _sent(generic, "register_generic_tools", monkeypatch,
                         "sch_set_sheet_options", zones_y=0)
    assert sent == [] and result["ok"] is False


def test_variation_is_read_back_after_the_write():
    body = _body("Project.pas", "Proj_SetComponentVariation")
    write = body.index("CompVar.DM_SetVariationKind(Kind);")
    assert body.index("DM_FindComponentVariationByUniqueId", write) > write
    assert "DM_RemoveComponentVariation(Found)" in body
    assert "applied" in body


def test_rule_answers_come_from_altium():
    body = _body("PCB.pas", "PCB_FindDominantRule")
    assert "Board.FindDominantRuleForObject(P1, Kind)" in body
    assert "Board.FindDominantRuleForObjectPair(P1, P2, Kind)" in body
    body = _body("PCB.pas", "PCB_RuleScopeCheck")
    assert "Rule.CheckUnaryScope(P1)" in body and "Rule.CheckBinaryScope(P1, P2)" in body


def test_the_fast_designator_lookup_is_checked_and_backed_up():
    body = _body("PCB.pas", "FindPCBComponentByDesignator")
    fast = body.index("Board.GetPcbComponentByRefDes(Desig)")
    assert body.index("If NameStr = Desig Then", fast) > fast
    assert "BoardIterator_Create" in body[fast:]


def test_extra_properties_fit_only_their_owner():
    text = (ALTIUM / "PCBGeneric.pas").read_text(encoding="utf-8-sig")
    assert "(ExtraPCBPropertyOwner(PropName) > 0);" in text
    body = _body("PCBGeneric.pas", "PCBSetExtra")
    assert "If (PropName = 'IsCounterHole') Or (PropName = 'IsBGA') Then Exit;" in body
    # Read-only first, then the type check has already passed, then values.
    assert body.index("ExtraPCBPropertyFits(Oid, PropName)") < body.index("Result := -1;")
    swaps = (ALTIUM / "Generic.pas").read_text(encoding="utf-8-sig")
    assert "SwapPin.SetState_SwapIdPart(Value)" in swaps
    assert "And (Obj.ObjectId <> ePin)) Then" in swaps


def test_net_color_none_is_altiums_unset_flag(monkeypatch):
    _, sent = _sent(project, "register_project_tools", monkeypatch,
                    "proj_net_color", net="VBUS", color="none")
    assert sent == [("project.net_color", {"net": "VBUS", "color": "536870912"})]
    body = _body("Project.pas", "Proj_NetColor")
    assert "(Wanted <> 536870912)" in body


def test_an_unset_tolerance_reads_and_writes_as_unset():
    get = _body("PCBGeneric.pas", "PCBGetExtra")
    assert "If Tol = UnsetTolerance(0) Then Result := 'unset'" in get
    put = _body("PCBGeneric.pas", "PCBSetExtra")
    assert "(LowerCase(Value) = 'unset')" in put
    assert put.index("'unset'") < put.index("If Not IsFloatStr(Value) Then Exit;")


def test_variant_creation_tries_the_api_first():
    body = _body("Project.pas", "Proj_CreateVariant")
    assert body.index("Project.DM_AddProjectVariant") < body.index("RunProcess('WorkspaceManager:VariantManagement')")
