"""Check the imported engineering calculations and MCP surface together."""
from eda_agent.engineering import ipc7351
from eda_agent.tools.engineering import _live_pads_to_um, register_engineering_tools
from eda_agent.tools.registry import ToolRegistry


SOIC8 = {
    "kind": "gullwing2", "prefix": "SOIC", "density": "N", "pins": 8,
    "pitch": 1.27, "L": [5.8, 6.2], "T": [0.4, 1.27],
    "W": [0.31, 0.51], "body_l": [4.8, 5.0],
    "body_w": [3.8, 4.0], "height": [1.35, 1.75],
}


def _tools():
    registry = ToolRegistry()
    register_engineering_tools(registry, altium=True)
    return {name: registry.get(name).fn for name in (
        "eng_calculate_ipc", "eng_compare_ipc_footprint", "eng_audit_footprint",
        "eng_validate_ruleset", "eng_evaluate_rules", "eng_validate_design_intent",
        "eng_ltspice_check_netlist", "eng_ltspice_parse_log",
        "eng_check_pin_pad_map", "eng_compare_live_ipc_footprint",
        "eng_parse_pin_table", "eng_generate_symbol",
        "eng_generate_datasheet_footprint", "eng_parse_rul",
        "eng_parse_ipc_footprint_name", "eng_validate_library",
        "eng_write_library_validation_report", "eng_eco_preview",
    )}


def test_ipc_result_can_be_audited_and_compared():
    tools = _tools()
    result = tools["eng_calculate_ipc"](SOIC8)
    assert result["ok"] and result["ipc_name"] == "SOIC127P600X175-8N"
    footprint = {
        "name": result["ipc_name"],
        "pads": [
            {"name": p["name"], **{key: round(p[key] * 1000)
                                  for key in ("x", "y", "w", "h")}}
            for p in result["pads"]
        ],
        "courtyard": {k: round(v * 1000) for k, v in result["courtyard"].items()},
        "pin1_marker": {"x": 0},
    }
    assert tools["eng_audit_footprint"](footprint)["passed"]
    assert tools["eng_compare_ipc_footprint"](footprint, SOIC8)["agree"]
    footprint["pads"][0]["w"] += 100
    assert not tools["eng_compare_ipc_footprint"](footprint, SOIC8)["agree"]


def test_missing_ipc_goals_are_reported():
    spec = {**SOIC8, "pitch": 0.5}
    result = _tools()["eng_calculate_ipc"](spec)
    assert not result["ok"]
    assert "not configured" in result["reason"]


def test_rules_and_intent_return_findings():
    tools = _tools()
    ruleset = {"ruleset": "company", "rules": [
        {"id": "R1", "type": "component_parameter_required",
         "parameters": ["MPN"], "severity": "error"},
    ]}
    assert tools["eng_validate_ruleset"](ruleset)["ok"]
    report = tools["eng_evaluate_rules"](
        ruleset, {"components": [{"designator": "U1"}], "parameters": {}})
    assert report["counts"]["error"] == 1
    intent = {"schema": "altium-ai/design-intent/v1", "title": "demo",
              "components": [{"ref": "R1"}],
              "nets": [{"name": "SIG", "nodes": ["R1.1", "R1.2"]}]}
    assert tools["eng_validate_design_intent"](intent)["ok"]


def test_pin_map_and_ltspice_safety():
    tools = _tools()
    assert tools["eng_check_pin_pad_map"](
        [{"number": "1"}], [{"name": "1"}])["mapping"] == {"1": ["1"]}
    assert not tools["eng_ltspice_check_netlist"](
        "* demo\n.ferret https://example.test/model\n.end")["ok"]
    log = tools["eng_ltspice_parse_log"]("vmax: MAX(V(out))=0.5 FROM 0 TO 1\n")
    assert log["measurements"]["vmax"]["value"] == 0.5


def test_live_geometry_uses_millimetres_to_micrometres():
    geometry = _live_pads_to_um({"success": True, "pads": [
        {"name": "1", "x_mm": -2.54, "y_mm": 1.27,
         "w_mm": 0.65, "h_mm": 1.5}]})
    assert geometry["pads"][0] == {
        "name": "1", "x": -2540, "y": 1270, "w": 650, "h": 1500}


def test_a365_requires_configuration_and_rejects_mutations(monkeypatch):
    from eda_agent.engineering.a365 import assert_read_only
    from eda_agent.engineering.errors import PermissionDenied

    monkeypatch.delenv("EDA_AGENT_NEXAR_CLIENT_ID", raising=False)
    registry = ToolRegistry()
    register_engineering_tools(registry)
    assert not registry.get("eng_a365_workspaces").fn()["ok"]
    try:
        assert_read_only("mutation { desReleaseComponent(input: {}) { id } }")
    except PermissionDenied:
        pass
    else:
        raise AssertionError("Nexar mutation reached a read-only query path")


def test_pin_table_to_symbol_and_datasheet_footprint():
    tools = _tools()
    csv_text = "number,name,type,side\n1,IN,input,left\n2,OUT,output,right\n"
    parsed = tools["eng_parse_pin_table"](csv_text, expected_count=2)
    assert parsed["ok"] and len(parsed["pins"]) == 2
    sym = tools["eng_generate_symbol"](csv_text, "AMP2")
    assert sym["ok"] and len(sym["symbol"]["pins"]) == 2
    package = {
        "footprint_name": "AMP2", "body": {"length": 1.6, "width": 0.8},
        "pattern": {"type": "custom", "pads": [
            {"name": "1", "x": -0.8, "y": 0, "w": 0.4, "h": 0.7},
            {"name": "2", "x": 0.8, "y": 0, "w": 0.4, "h": 0.7},
        ]},
    }
    fp = tools["eng_generate_datasheet_footprint"](package)
    assert fp["ok"] and fp["pads"][0]["x"] == -800


def test_rul_export_parser_preserves_fields():
    result = _tools()["eng_parse_rul"]("NAME=Clearance|KIND=Electrical|VALUE=0.2mm\n")
    assert result["records"] == [{"NAME": "Clearance", "KIND": "Electrical", "VALUE": "0.2mm"}]


def test_ipc_footprint_name_parses_back_to_package_dimensions():
    tools = _tools()
    parsed = tools["eng_parse_ipc_footprint_name"]("SOIC127P600X175-8N")
    assert parsed["ok"] and parsed["pins"] == 8 and parsed["pitch"] == 1.27
    chip = tools["eng_parse_ipc_footprint_name"]("RESC1608X55N")
    assert chip["ok"] and chip["body_l"] == 1.6
    unknown = tools["eng_parse_ipc_footprint_name"]("MY_FOOTPRINT")
    assert not unknown["ok"]


def _soic8_footprint():
    result = _tools()["eng_calculate_ipc"](SOIC8)
    return {
        "name": result["ipc_name"],
        "pads": [
            {"name": p["name"], **{key: round(p[key] * 1000)
                                   for key in ("x", "y", "w", "h")}}
            for p in result["pads"]
        ],
        "courtyard": {k: round(v * 1000) for k, v in result["courtyard"].items()},
        "pin1_marker": {"x": 0},
    }


def test_validate_library_passes_a_consistent_schlib_pcblib_pair():
    tools = _tools()
    fp = _soic8_footprint()
    symbols = {"AMP_8": {"lib_ref": "AMP_8", "designator": "U", "description": "", "pin_count": 8,
                         "footprints": [fp["name"]], "pins": [
                             {"number": str(i), "name": str(i), "x": 0, "y": i * 100, "length": 200, "hidden": False}
                             for i in range(1, 9)], "parameters": {}}}
    result = tools["eng_validate_library"](symbols, {fp["name"]: fp})
    assert result["ok"] and result["passed"]
    assert result["counts"]["error"] == 0


def test_validate_library_reports_missing_footprint_link_and_ipc_mismatch():
    tools = _tools()
    fp = _soic8_footprint()
    fp["pads"] = fp["pads"][:-1]  # drop a pad: now disagrees with the IPC name's pin count
    symbols = {"ORPHAN": {"lib_ref": "ORPHAN", "designator": "U", "description": "", "pin_count": 2,
                          "footprints": [], "pins": [], "parameters": {}}}
    result = tools["eng_validate_library"](symbols, {fp["name"]: fp})
    assert result["ok"] and not result["passed"]
    checks = {f["check"] for f in result["findings"]}
    assert "footprint_link" in checks
    assert "ipc_pins" in checks


def test_write_library_validation_report_produces_csv_and_html(tmp_path):
    tools = _tools()
    fp = _soic8_footprint()
    result = tools["eng_validate_library"]({}, {fp["name"]: fp})
    written = tools["eng_write_library_validation_report"](result, str(tmp_path), "Demo library")
    assert written["ok"]
    from pathlib import Path
    csv_text = Path(written["csv"]).read_text(encoding="utf-8")
    html_text = Path(written["html"]).read_text(encoding="utf-8")
    assert "severity" in csv_text
    assert "Demo library" in html_text


def test_eco_preview_reports_in_sync_boards_as_clean():
    tools = _tools()
    sch = {"components": {"R1": "RESC1608X55N"}, "pins": {"R1.1": "SIG", "R1.2": "GND"}}
    pcb = {"components": {"R1": "RESC1608X55N"}, "pins": {"R1.1": "SIG", "R1.2": "GND"}}
    result = tools["eng_eco_preview"](sch, pcb)
    assert result["ok"] and result["in_sync"]
    assert result["summary"]["components_to_add"] == 0


def test_eco_preview_catches_component_footprint_and_net_changes():
    tools = _tools()
    sch = {"components": {"R1": "RESC1608X55N", "R2": "RESC1608X55N"},
           "pins": {"R1.1": "SIG", "R1.2": "GND", "R2.1": "SIG", "R2.2": "GND"}}
    pcb = {"components": {"R1": "RESC2012X55N", "R3": "RESC1608X55N"},
          "pins": {"R1.1": "SIG", "R1.2": "VCC", "R3.1": "OLD"}}
    result = tools["eng_eco_preview"](sch, pcb)
    assert result["ok"] and not result["in_sync"]
    assert result["components_to_add"] == ["R2"]
    assert result["components_to_remove"] == ["R3"]
    assert result["footprint_changes"] == ["R1"]
    assert {"pin": "R1.2", "schematic": "GND", "pcb": "VCC"} in result["pin_net_changes"]


def test_ltspice_hides_its_window_only_on_windows():
    from eda_agent.engineering.ltspice import hidden_window_kwargs

    assert hidden_window_kwargs("linux") == {}
    assert hidden_window_kwargs("darwin") == {}
    win = hidden_window_kwargs("win32")
    assert win["startupinfo"].wShowWindow == 0
    assert win["creationflags"] != 0
