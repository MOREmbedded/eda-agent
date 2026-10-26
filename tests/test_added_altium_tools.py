# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Tools added for Altium.

Symbol parts, display modes and aliases, model and part STEP export,
impedance profiles, variant entries, net colours, the rule Altium applies,
rule scopes, net clean-up, drill pairs, declared classes and rooms,
sheet zones, xSignals, shared packages, part text positions, net naming
objects, the library rule check, project setup, and the extra board and
pin properties. What each sends, what each refuses before Altium sees
it, and the Pascal shapes that keep a refused or missing call from
passing for a success.
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
    assert ("If (PropName = 'IsCounterHole') Or (PropName = 'IsBGA') Or\n"
            "       (PropName = 'PackageSpecificHash') Then Exit;") in body
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


def test_xsignals_send_pins_and_refuse_bad_ones(monkeypatch):
    _, sent = _sent(pcb, "register_pcb_tools", monkeypatch, "pcb_create_xsignals",
                    pins=["U4.4", " LED1.3 "], class_name="LED_DATA")
    assert sent == [("pcb.create_xsignals",
                     {"pins": "U4.4\tLED1.3", "class_name": "LED_DATA"})]
    for pins in (["U4.4"], ["U4.4", "LED1"], ["U4.4", ".3"], []):
        result, sent = _sent(pcb, "register_pcb_tools", monkeypatch,
                             "pcb_create_xsignals", pins=pins)
        assert sent == [] and result["ok"] is False, pins


def test_xsignal_listing_names_pads_and_counts_copper():
    body = _body("PCB.pas", "XSignalJson")
    # Tracks, arcs and vias are counted, not listed: a routed xSignal
    # otherwise lists every segment.
    assert body.index("Inc(Tracks);") < body.index("GetPinPairItemDisplayName")
    assert body.index("Inc(Vias);") < body.index("GetPinPairItemDisplayName")
    assert "JsonRaw('path'," in body
    create = _body("PCB.pas", "PCB_CreateXSignals")
    # New xSignals are found by name against the list taken before.
    assert create.index("BeforeNames := BeforeNames + PP.Name") < create.index("Mgr.CreateFromPins(Pins)")
    assert "If Pos(#9 + PP.Name + #9, BeforeNames) > 0 Then Continue;" in create
    assert "'NO_PIN'" in create


def test_same_package_groups_are_confirmed_member_by_member(monkeypatch):
    _, sent = _sent(pcb, "register_pcb_tools", monkeypatch,
                    "pcb_find_same_package", designator=" R1 ")
    assert sent == [("pcb.find_same_package", {"designator": "R1"})]
    body = _body("PCB.pas", "PCB_FindSamePackage")
    # A shared hash is a candidate; IsPackageEqual decides.
    assert "Same := First.IsPackageEqual(Comp);" in body
    assert "If Size < 2 Then Continue;" in body
    assert "parts_without_hash" in body


@pytest.mark.parametrize("given, number", [
    ("manual", 0), ("top_left", 1), ("bottom-center", 6), ("Bottom Right", 9),
    ("4", 4), (5, 5)])
def test_text_anchors_take_names_or_numbers(given, number):
    assert pcb._text_anchor(given) == number


def test_text_position_sends_and_refuses(monkeypatch):
    _, sent = _sent(pcb, "register_pcb_tools", monkeypatch, "pcb_auto_position_text",
                    designators=["LED2", "R4"], name_position="top_center")
    assert sent == [("pcb.auto_position_text",
                     {"designators": "LED2\tR4", "name_position": "4"})]
    _, sent = _sent(pcb, "register_pcb_tools", monkeypatch, "pcb_auto_position_text")
    assert sent == [("pcb.auto_position_text", {})]
    for kwargs in ({"name_position": "middle"}, {"comment_position": "10"},
                   {"designators": []}):
        result, sent = _sent(pcb, "register_pcb_tools", monkeypatch,
                             "pcb_auto_position_text", **kwargs)
        assert sent == [] and result["ok"] is False, kwargs


def test_text_position_reports_whether_the_text_is_shown():
    # Measured: Altium records the anchor on hidden text but does not
    # move it, so the reply has to say which texts are hidden.
    body = _body("PCB.pas", "CompTextPosJson")
    assert "JsonBool('name_shown', NameOn)" in body
    assert "JsonBool('comment_shown', CommentOn)" in body
    run = _body("PCB.pas", "PCB_AutoPositionText")
    assert "Comp.ChangeNameAutoposition(NamePos);" in run
    assert "Comp.AutoPosition_NameComment;" in run


def test_part_text_properties_write_through_the_moving_call():
    body = _body("PCBGeneric.pas", "PCBSetExtra")
    assert "Comp.ChangeNameAutoposition(N)" in body
    assert "Comp.ChangeCommentAutoposition(N)" in body
    assert "Comp.LockStrings := B" in body
    get = _body("PCBGeneric.pas", "PCBGetExtra")
    assert "Result := Comp.PackageSpecificHash;" in get


def test_net_items_sends_and_refuses(monkeypatch):
    _, sent = _sent(project, "register_project_tools", monkeypatch,
                    "proj_get_net_items", net="GND", named_only=True)
    assert sent == [("project.get_net_items",
                     {"limit": "500", "net": "GND", "named_only": "true"})]
    result, sent = _sent(project, "register_project_tools", monkeypatch,
                         "proj_get_net_items", limit=0)
    assert sent == [] and result["ok"] is False


def test_net_items_are_in_mils_and_name_power_ports():
    body = _body("Project.pas", "NetNamingItemJson")
    assert "JsonFloat('x', X / 10000)" in body
    # A power port has no text of its own; the net name stands in.
    assert body.index("If Text = '' Then") < body.index("Item.DM_NetName")
    listing = _body("Project.pas", "Proj_GetNetItems")
    assert "nets_examined" in listing and "truncated" in listing


def test_project_setup_sends_only_what_was_asked(monkeypatch):
    _, sent = _sent(project, "register_project_tools", monkeypatch, "proj_project_setup")
    assert sent == [("project.project_setup", {})]
    _, sent = _sent(project, "register_project_tools", monkeypatch, "proj_project_setup",
                    move_document="C:/p/A.SchDoc", move_to_index=2,
                    add_search_path="C:/libs", include_subfolders=True,
                    allow_port_net_names=False)
    assert sent == [("project.project_setup", {
        "move_document": "C:/p/A.SchDoc", "move_to_index": "2",
        "add_search_path": "C:/libs", "include_subfolders": "true",
        "allow_port_net_names": "false"})]
    for kwargs in ({"move_document": "C:/p/A.SchDoc"}, {"move_to_index": 1},
                   {"move_document": "C:/p/A.SchDoc", "move_to_index": -1}):
        result, sent = _sent(project, "register_project_tools", monkeypatch,
                             "proj_project_setup", **kwargs)
        assert sent == [] and result["ok"] is False, kwargs


def test_project_setup_reads_every_change_back():
    body = _body("Project.pas", "Proj_ProjectSetup")
    for write, check in (
            ("Project.DM_MoveSourceDocument(MoveDoc, NewIdx);", "If NowIdx <> NewIdx Then"),
            ("Project.DM_AddSearchPath(AddPath", "If Not Found Then"),
            ("Project.DM_SetAllowPortNetNames(PortStr = 'true');",
             "If Project.DM_GetAllowPortNetNames <> (PortStr = 'true') Then"),
            ("Project.DM_SetAllowSheetEntryNetNames(EntryStr = 'true');",
             "If Project.DM_GetAllowSheetEntryNetNames <> (EntryStr = 'true') Then")):
        assert body.index(write) < body.index(check), write
    assert "'NO_DOCUMENT'" in body


def test_schlib_check_sends_and_refuses(monkeypatch, tmp_path):
    lib = tmp_path / "Parts.SchLib"
    lib.write_bytes(b"")
    _, sent = _sent(library, "register_library_tools", monkeypatch, "lib_check_schlib",
                    library_path=str(lib), checks=["missing_footprint", "duplicate_pins"])
    assert sent == [("library.check_schlib", {
        "checks": "missing_footprint\tduplicate_pins", "library_path": str(lib)})]
    for kwargs in ({"checks": ["missing_everything"]}, {"checks": []},
                   {"library_path": str(tmp_path / "None.SchLib")}):
        result, sent = _sent(library, "register_library_tools", monkeypatch,
                             "lib_check_schlib", **kwargs)
        assert sent == [] and result["ok"] is False, kwargs


def test_schlib_check_hides_the_report_and_frees_the_checker():
    body = _body("Library.pas", "Lib_CheckSchLib")
    assert "Checker.ShowReport := False;" in body
    assert body.index("Ran := Checker.Run;") < body.index("Sch_LibraryRuleChecker_Destroy(Checker)")
    assert body.index("Finally") < body.index("Sch_LibraryRuleChecker_Destroy")
    for check in library._SCHLIB_CHECKS:
        assert f"#9 + '{check}' + #9" in body, check


def test_schlib_check_report_is_read_per_symbol(tmp_path):
    lib = tmp_path / "Parts.SchLib"
    lib.write_bytes(b"")
    (tmp_path / "Parts.ERR").write_text(
        "Component Rule Check Report for : Parts.SchLib\n\n\n"
        "Name                Errors\n"
        "------------------------------------------------------------------\n"
        "Component_1         (No Footprint) (No Description) \n"
        "LM317 ADJ           (Duplicate Pins: 2) \n", encoding="utf-8")
    report, problems = library._schlib_check_report(lib, 0)
    assert report == tmp_path / "Parts.ERR"
    assert problems == [
        {"symbol": "Component_1", "problems": ["No Footprint", "No Description"]},
        {"symbol": "LM317 ADJ", "problems": ["Duplicate Pins: 2"]}]
    # A report older than the run is not this run's report.
    assert library._schlib_check_report(lib, 4102444800) == (None, [])
