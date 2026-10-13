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
        "eng_a365_workspace_infos", "eng_a365_library_components_params",
        "eng_a365_entity",
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
