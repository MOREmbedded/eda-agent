"""Engineering tools adapted from the team's Altium AI Automation v3.1.0.

The calculations are offline. Live IPC comparison reads a footprint through
eda-agent's existing, tested Altium bridge.
"""
from __future__ import annotations

import os
import re
from functools import cache
from pathlib import Path
from typing import Any

from ..engineering import a365, design_intent, eco_preview, footprint, ipc7351, libcheck, libvalidate, ltspice, mapping, naming, pins, rules, symbol
from ..engineering.config import A365Policy, LTspicePolicy
from ..engineering.errors import AltiumAIError
from ..units import MM_PER_MIL


def _result(fn, *args, **kwargs) -> dict[str, Any]:
    try:
        value = fn(*args, **kwargs)
        return {"ok": True, **value} if isinstance(value, dict) else {"ok": True, "result": value}
    except (AltiumAIError, KeyError, TypeError, ValueError, OSError) as exc:
        result: dict[str, Any] = {"ok": False, "reason": str(exc)}
        if isinstance(exc, AltiumAIError) and exc.details:
            result["details"] = exc.details
        return result


def _audit_footprint(footprint: dict, min_pad_gap_um: int, silk_clear_um: int) -> dict:
    return libcheck.summarize(libcheck.audit_footprint(
        footprint, min_pad_gap_um=min_pad_gap_um, silk_clear_um=silk_clear_um))


def _audit_symbol(symbol: dict, grid_mils: int) -> dict:
    return libcheck.summarize(libcheck.audit_symbol(symbol, grid=grid_mils))


def _check_name(kind: str, name: str) -> dict:
    return {"name": naming.NamingPolicy().validate(kind, name)}


def _render_name(kind: str, tokens: dict) -> dict:
    return {"name": naming.NamingPolicy().render(kind, **tokens)}


def _compare_ipc(footprint: dict, spec: dict, tolerance_um: int) -> dict:
    return libcheck.cross_check_ipc(footprint, spec, tol_um=tolerance_um)


def _validate_intent(intent: dict) -> dict:
    return design_intent.validate_intent(intent)


def _parse_pins(csv_text: str, expected_count: int | None) -> dict:
    parsed = pins.parse_pin_table(csv_text)
    return {"pins": pins.pins_to_dicts(parsed),
            "warnings": pins.validate_pins(parsed, expected_count)}


def _generate_symbol(csv_text: str, lib_ref: str, description: str,
                     designator_prefix: str) -> dict:
    parsed = pins.parse_pin_table(csv_text)
    warnings = pins.validate_pins(parsed)
    generated = symbol.generate_symbol(parsed, lib_ref, description, designator_prefix)
    warnings.extend(symbol.check_symbol(generated))
    return {"symbol": generated, "warnings": warnings}


def _validate_ruleset(ruleset: dict) -> dict:
    return rules.validate_ruleset(ruleset)


def _evaluate_rules(ruleset: dict, data: dict) -> dict:
    rules.validate_ruleset(ruleset)
    return rules.evaluate(ruleset, data)


def _validate_library(symbols: dict, footprints: dict, ruleset: dict | None,
                      use_naming: bool) -> dict:
    policy = naming.NamingPolicy() if use_naming else None
    return libvalidate.validate(symbols, footprints, naming=policy, ruleset=ruleset)


def _write_library_validation_report(result: dict, out_dir: str, title: str) -> dict:
    return libvalidate.write_reports(result, Path(out_dir), title)


def _symbol_from_live_details(details: dict) -> dict:
    designator = details.get("designator")
    footprints = [str(m["model_name"]) for m in (details.get("models") or [])
                 if str(m.get("model_type", "")).upper() == "PCBLIB" and m.get("model_name")]
    pins = [{"number": str(p.get("designator", "")), "name": str(p.get("name", "")),
            "x": p.get("x", 0), "y": p.get("y", 0), "length": p.get("length", 0),
            "hidden": bool(p.get("hidden"))}
           for p in (details.get("pins") or [])]
    return {"lib_ref": str(details.get("name", "")),
           "designator": str((designator or {}).get("text", "")) if isinstance(designator, dict) else "",
           "description": str(details.get("description", "")),
           "pin_count": int(details.get("pin_count") or len(pins)),
           "footprints": footprints, "pins": pins,
           "parameters": details.get("parameters") or {}}


def _mils_to_um(value: float) -> int:
    return round(value * MM_PER_MIL * 1000)


def _live_footprint_geometry(raw: dict, overlay_layer: str, courtyard_layer: str) -> dict:
    """Adapt library.get_footprint_pads' response (mils) into libvalidate's
    micrometre shape, isolating silkscreen/courtyard tracks by the layer
    names the caller supplies (Altium has no per-footprint "this is the
    courtyard layer" API; see scripts/altium/Library.pas:Lib_GetFootprintPads).
    courtyard is present only when courtyard_layer matched at least one
    track, so audit_footprint's has_full_geometry stays an honest signal
    rather than a bbox computed from zero tracks."""
    name = str(raw.get("name", ""))
    pads = [
        {"name": str(p["name"]),
         "x": _mils_to_um(p["x"]), "y": _mils_to_um(p["y"]),
         "w": _mils_to_um(p["size_x"]), "h": _mils_to_um(p["size_y"])}
        for p in (raw.get("pads") or [])
    ]
    silkscreen: list[dict] = []
    court_tracks: list[dict] = []
    for prim in (raw.get("primitives") or []):
        if prim.get("kind") != "track" or "x1" not in prim:
            continue
        layer = str(prim.get("layer", ""))
        seg = {"layer": layer,
              "x1": _mils_to_um(prim["x1"]), "y1": _mils_to_um(prim["y1"]),
              "x2": _mils_to_um(prim["x2"]), "y2": _mils_to_um(prim["y2"])}
        if overlay_layer and layer == overlay_layer:
            silkscreen.append(seg)
        if courtyard_layer and layer == courtyard_layer:
            court_tracks.append(seg)
    footprint: dict[str, Any] = {"name": name, "pads": pads, "silkscreen": silkscreen,
                                 "has_3d": bool(raw.get("has_3d"))}
    if court_tracks:
        xs = [v for t in court_tracks for v in (t["x1"], t["x2"])]
        ys = [v for t in court_tracks for v in (t["y1"], t["y2"])]
        footprint["courtyard"] = {"x1": min(xs), "y1": min(ys), "x2": max(xs), "y2": max(ys)}
    if pads:
        footprint["pin1_marker"] = {"present_if_silk": bool(silkscreen)} if silkscreen else None
    return footprint


async def _fetch_live_library(schlib_path: str, pcblib_path: str,
                              max_components: int, max_footprints: int,
                              overlay_layer: str = "", courtyard_layer: str = "") -> tuple[dict, dict, dict]:
    from ..bridge import get_bridge
    bridge = get_bridge()
    meta: dict[str, Any] = {"components_truncated": False, "footprints_truncated": False}

    comp_list = await bridge.send_command_async(
        "library.get_components", {"library_path": schlib_path})
    names = [str(c["name"]) for c in (comp_list.get("components") or [])]
    if len(names) > max_components:
        meta["components_truncated"] = True
        names = names[:max_components]
    symbols = {}
    for name in names:
        details = await bridge.send_command_async(
            "library.get_component_details",
            {"component_name": name, "library_path": schlib_path})
        sym = _symbol_from_live_details(details)
        symbols[sym["lib_ref"] or name] = sym

    fp_list = await bridge.send_command_async(
        "library.get_footprints", {"library_path": pcblib_path})
    fp_names = [str(f["name"]) for f in (fp_list.get("footprints") or [])]
    if len(fp_names) > max_footprints:
        meta["footprints_truncated"] = True
        fp_names = fp_names[:max_footprints]
    footprints = {}
    for name in fp_names:
        raw = await bridge.send_command_async(
            "library.get_footprint_pads",
            {"footprint_name": name, "library_path": pcblib_path})
        fp = _live_footprint_geometry(raw, overlay_layer, courtyard_layer)
        footprints[fp["name"] or name] = fp

    return symbols, footprints, meta


async def _fetch_live_sch_view(project_path: str | None, limit: int) -> tuple[dict, bool]:
    from ..bridge import get_bridge
    bridge = get_bridge()
    params: dict[str, Any] = {"limit": str(limit)}
    if project_path:
        params["project_path"] = project_path
    bom = await bridge.send_command_async("project.get_bom", params)
    comps = bom.get("components") or []
    sch = {
        "components": {str(c["designator"]): str(c.get("footprint") or "") for c in comps},
        "pins": {f"{c['designator']}.{p['pin']}": str(p.get("net") or "")
                for c in comps for p in (c.get("pins") or [])},
    }
    return sch, bool(bom.get("truncated"))


async def _fetch_live_pcb_view(pad_page_limit: int = 5000, max_pages: int = 20) -> dict:
    from ..bridge import get_bridge
    bridge = get_bridge()
    comp_result = await bridge.send_command_async("pcb.get_components", {})
    comps = comp_result.get("components") or []
    pads: list[dict] = []
    offset = 0
    for _ in range(max_pages):
        page = await bridge.send_command_async(
            "pcb.get_pad_properties",
            {"offset": str(offset), "limit": str(pad_page_limit)})
        pads.extend(page.get("pads") or [])
        if not page.get("truncated"):
            break
        offset = page.get("next_offset", offset + pad_page_limit)
    else:
        raise ValueError(
            f"board has more than {max_pages * pad_page_limit} pads; "
            "raise pad_page_limit or max_pages")
    return {
        "components": {str(c["designator"]): str(c.get("footprint") or "") for c in comps},
        "pins": {f"{p['component']}.{p['name']}": str(p.get("net") or "")
                for p in pads if p.get("component")},
    }


def _live_pads_to_um(raw: dict) -> dict:
    if not raw.get("success", True):
        raise ValueError(raw.get("error") or "Altium did not return footprint geometry")
    data = raw.get("data", raw)
    if not isinstance(data, dict) or not isinstance(data.get("pads"), list):
        raise ValueError("Altium response has no pads")
    return {"name": data.get("name", ""), "pads": [
        {"name": str(p["name"]), **{k: round(float(p[f"{k}_mm"]) * 1000)
                                   for k in ("x", "y", "w", "h")}}
        for p in data["pads"]]}


def _ltspice_runner() -> ltspice.LTspiceRunner:
    configured = os.environ.get("EDA_AGENT_LTSPICE_EXE", "")
    candidates = [Path(configured)] if configured else [
        Path(r"C:\Program Files\ADI\LTspice\LTspice.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ADI" / "LTspice" / "LTspice.exe",
    ]
    exe = next((p for p in candidates if p.is_file()), None)
    if exe is None:
        raise FileNotFoundError("LTspice.exe not found; set EDA_AGENT_LTSPICE_EXE")
    root = Path(os.environ.get("EDA_AGENT_LTSPICE_RUNS") or Path.home() / "EDA Agent" / "ltspice")
    models = tuple(Path(p) for p in os.environ.get("EDA_AGENT_LTSPICE_MODEL_ROOTS", "").split(os.pathsep) if p)
    return ltspice.LTspiceRunner(LTspicePolicy(str(exe), root, model_roots=models))


def _run_ltspice(project: str, netlist: str, timeout_s: float) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", project):
        raise ValueError("project must be a short name without path separators")
    return _ltspice_runner().run(project, netlist_text=netlist, timeout_s=timeout_s)


def _trace_ltspice(project: str, run_id: str, trace_name: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", project):
        raise ValueError("project must be a short name without path separators")
    return _ltspice_runner().get_trace(project, run_id, trace_name)


def a365_config() -> A365Policy:
    client_id = os.environ.get("EDA_AGENT_NEXAR_CLIENT_ID", "")
    if not client_id:
        raise ValueError("set EDA_AGENT_NEXAR_CLIENT_ID before using Altium 365")
    workspaces = tuple(w.strip().rstrip("/") + "/" for w in
                       os.environ.get("EDA_AGENT_A365_WORKSPACES", "").split(",") if w.strip())
    return A365Policy(client_id=client_id, workspaces=workspaces,
                      token_store=Path.home() / "EDA Agent" / "a365_token.bin")


@cache
def _a365_client() -> a365.NexarClient:
    return a365.NexarClient(a365_config(),
                            client_secret=os.environ.get("EDA_AGENT_NEXAR_CLIENT_SECRET"))


#: Exact confirmation string Booster 3.2's own replace_existing guard requires
#: (altium_ai/service.py: "replace_existing deletes ALL parameters ... set
#: confirm_replace to 'REPLACE ALL PARAMETERS' to proceed"). Kept verbatim
#: rather than inventing a different phrase for the same guard.
_CONFIRM_REPLACE_PARAMETERS = "REPLACE ALL PARAMETERS"


def _a365_update_parameters(component_id: str, parameters: list[dict], release_note: str,
                           replace_existing: bool, confirm_replace: str) -> dict:
    if replace_existing and confirm_replace != _CONFIRM_REPLACE_PARAMETERS:
        raise ValueError(
            "replace_existing deletes ALL parameters of the component; set "
            f"confirm_replace to {_CONFIRM_REPLACE_PARAMETERS!r} to proceed")
    client = _a365_client()
    res = client.update_parameters(component_id, parameters, release_note, replace_existing)
    comp = client.component(res["new_component_id"])
    got = {p["name"]: p["value"] for p in ((comp.get("details") or {}).get("parameters") or [])}
    missing = [p["name"] for p in parameters if got.get(p["name"]) != str(p["value"])]
    if missing:
        raise ValueError(f"parameters not confirmed by read-back: {missing}")
    return {"new_component_id": res["new_component_id"], "parameters_verified": len(parameters)}


def _a365_publish_model(kind: str, workspace_url: str, folder_id: str, name: str,
                        file_path: str) -> dict:
    if kind not in ("symbol", "footprint"):
        raise ValueError("kind must be 'symbol' or 'footprint'")
    naming.NamingPolicy().validate(kind, name)
    return _a365_client().create_model(kind, workspace_url, folder_id, name, Path(file_path))


def register_engineering_tools(mcp, *, altium: bool = False) -> None:
    @mcp.tool()
    def eng_calculate_ipc(spec: dict) -> dict:
        """Calculate an IPC-7351B footprint from package min/max dimensions in mm.

        Requires kind, prefix, density, L, T, W, body_l, body_w, height,
        plus pins and pitch for non-chip packages. Missing solder-joint goals
        are refused; supply company goals via ALTIUM_AI_IPC_DIR.
        """
        return _result(ipc7351.build, spec)

    @mcp.tool()
    def eng_compare_ipc_footprint(footprint: dict, spec: dict,
                                  tolerance_um: int = 20) -> dict:
        """Compare footprint pads in integer micrometres with IPC calculation."""
        return _result(_compare_ipc, footprint, spec, tolerance_um)

    @mcp.tool()
    def eng_audit_footprint(footprint: dict, min_pad_gap_um: int = 150,
                            silk_clear_um: int = 100) -> dict:
        """Audit pads, numbering, courtyard, silkscreen, origin and pin-1 marker.

        Footprint coordinates and dimensions must be integer micrometres.
        """
        return _result(_audit_footprint, footprint, min_pad_gap_um, silk_clear_um)

    @mcp.tool()
    def eng_audit_symbol(symbol: dict, grid_mils: int = 100) -> dict:
        """Audit symbol pin numbers, grid alignment and hidden pins."""
        return _result(_audit_symbol, symbol, grid_mils)

    @mcp.tool()
    def eng_parse_pin_table(csv_text: str, expected_count: int | None = None) -> dict:
        """Parse and validate a datasheet pin CSV: number,name,type[,side]."""
        return _result(_parse_pins, csv_text, expected_count)

    @mcp.tool()
    def eng_generate_symbol(csv_text: str, lib_ref: str, description: str = "",
                            designator_prefix: str = "U") -> dict:
        """Generate schematic symbol geometry on a 100 mil grid from pins."""
        return _result(_generate_symbol, csv_text, lib_ref, description, designator_prefix)

    @mcp.tool()
    def eng_generate_datasheet_footprint(package: dict) -> dict:
        """Build footprint geometry from a datasheet land pattern in mm.

        The package needs footprint_name, body and a dual_row or custom
        pattern. The supplied dimensions are used as printed in the datasheet.
        """
        return _result(footprint.generate_footprint, package)

    @mcp.tool()
    def eng_validate_ruleset(ruleset: dict) -> dict:
        """Validate a JSON engineering ruleset and list required design data."""
        return _result(_validate_ruleset, ruleset)

    @mcp.tool()
    def eng_evaluate_rules(ruleset: dict, data: dict) -> dict:
        """Evaluate owner rules against supplied components, nets and PCB data."""
        return _result(_evaluate_rules, ruleset, data)

    @mcp.tool()
    def eng_parse_ipc_footprint_name(name: str) -> dict:
        """Parse an IPC-7351B footprint name back into package dimensions.

        Returns the kind/pitch/span/pins/density implied by the name, or
        ok: false if the name does not follow a recognised IPC convention.
        """
        parsed = libvalidate.parse_ipc_name(name)
        if parsed is None:
            return {"ok": False, "reason": f"{name!r} does not match a known IPC-7351B naming convention"}
        return {"ok": True, **parsed}

    @mcp.tool()
    def eng_validate_library(symbols: dict, footprints: dict,
                             ruleset: dict | None = None, use_naming: bool = True) -> dict:
        """Audit a whole SchLib/PcbLib pair: checks, IPC-name consistency, footprint links, pin/pad counts.

        Runs per-footprint and per-symbol checks, IPC-name consistency,
        footprint-link resolution, symbol pin vs. footprint pad counts, and
        (with a ruleset) required-parameter checks.

        symbols is lib_ref -> {designator, description, pin_count, footprints,
        pins, parameters}; footprints is name -> {description, pads, ...} as
        produced by footprint.py or by reading an existing PcbLib footprint.
        Phase 1: pass in already-assembled dicts. Reading a whole library
        live from Altium into this shape is Phase 2 work (no bulk library
        dump bridge command exists yet; see docs/ENGINEERING_TOOLS.md).
        """
        return _result(_validate_library, symbols, footprints, ruleset, use_naming)

    @mcp.tool()
    def eng_write_library_validation_report(result: dict, out_dir: str,
                                            title: str = "Library validation") -> dict:
        """Write a library validation result (from eng_validate_library) as CSV and HTML reports."""
        return _result(_write_library_validation_report, result, out_dir, title)

    @mcp.tool()
    def eng_parse_rul(export_text: str) -> dict:
        """Parse a PCB .RUL text export into field records (format varies by AD version)."""
        return _result(lambda: {"records": rules.parse_rul(export_text)})

    @mcp.tool()
    def eng_validate_design_intent(intent: dict) -> dict:
        """Validate requirements, parts and nets in a design-intent document."""
        return _result(_validate_intent, intent)

    @mcp.tool()
    def eng_diff_design_intent(before: dict, after: dict) -> dict:
        """Report components and nets added, removed or changed in design intent."""
        return _result(design_intent.diff_intent, before, after)

    @mcp.tool()
    def eng_eco_preview(sch: dict, pcb: dict) -> dict:
        """Compare a schematic view against a PCB view before running an ECO.

        sch and pcb are both {"components": {designator: footprint},
        "pins": {"designator.pin": net}}. Reports components to
        add/remove, footprint changes, net changes per pin, stray
        PCB-only connections, and whole nets to add/remove - the things
        "Design > Update PCB Document" itself would act on.

        This is a different, coarser check than eng's pad-level ECO
        verification (core.eco_verify.verify_eco, used after an ECO runs
        to catch pads an ECO silently left unbound): this one runs
        BEFORE an ECO and sees component/footprint/net-level changes
        verify_eco cannot. Building sch/pcb from a live schematic/PCB
        read is Phase 2 work; see docs/ENGINEERING_TOOLS.md.
        """
        return _result(eco_preview.diff, sch, pcb)

    @mcp.tool()
    def eng_check_pin_pad_map(pins: list[dict], pads: list[dict],
                              explicit: dict | None = None,
                              unconnected_pads: list[str] | None = None) -> dict:
        """Check that every symbol pin maps to valid footprint pads."""
        return _result(lambda: {"mapping": mapping.build_pin_pad_map(
            pins, pads, explicit, unconnected_pads)})

    @mcp.tool()
    def eng_check_name(kind: str, name: str) -> dict:
        """Check a footprint, symbol or component name against owner policy."""
        return _result(_check_name, kind, name)

    @mcp.tool()
    def eng_render_name(kind: str, tokens: dict) -> dict:
        """Render a footprint, symbol or component name from owner policy."""
        return _result(_render_name, kind, tokens)

    @mcp.tool()
    def eng_scan_delphiscript(source: str) -> dict:
        """Find process, registry and file operations in generated DelphiScript."""
        return _result(lambda: {"findings": libcheck.scan_script(source)})

    @mcp.tool()
    def eng_ltspice_check_netlist(netlist: str) -> dict:
        """Check a SPICE netlist for unsafe includes and file directives."""
        return _result(lambda: {"includes": ltspice.check_netlist(
            netlist, Path.home() / "EDA Agent" / "ltspice" / "check", ())})

    @mcp.tool()
    def eng_ltspice_parse_log(log_text: str) -> dict:
        """Parse LTspice measurements, warnings and errors from a log."""
        return _result(ltspice.parse_log, log_text)

    @mcp.tool()
    def eng_ltspice_parse_trace(raw_ascii: str, trace_name: str,
                                max_points: int = 2000) -> dict:
        """Extract and decimate a waveform from an ASCII LTspice RAW file."""
        def parse() -> dict:
            if not 1 <= max_points <= 100_000:
                raise ValueError("max_points must be between 1 and 100000")
            return ltspice.trace(ltspice.parse_raw_ascii(raw_ascii), trace_name, max_points)
        return _result(parse)

    @mcp.tool()
    def eng_ltspice_run(project: str, netlist: str, timeout_s: float = 300.0) -> dict:
        """Run a netlist in a sandboxed folder using an installed LTspice.

        Set EDA_AGENT_LTSPICE_EXE if LTspice is outside standard locations.
        """
        return _result(_run_ltspice, project, netlist, timeout_s)

    @mcp.tool()
    def eng_ltspice_trace(project: str, run_id: str, trace_name: str) -> dict:
        """Read a waveform from a previous LTspice batch run."""
        return _result(_trace_ltspice, project, run_id, trace_name)

    @mcp.tool()
    def eng_a365_workspaces() -> dict:
        """List Altium 365 workspaces visible to the configured Nexar account."""
        return _result(lambda: {"workspaces": _a365_client().workspaces()})

    @mcp.tool()
    def eng_a365_workspace_infos() -> dict:
        """Like eng_a365_workspaces, plus each workspace's id and description."""
        return _result(lambda: {"workspaces": _a365_client().workspace_infos()})

    @mcp.tool()
    def eng_a365_projects(workspace_url: str, name_contains: str = "",
                          limit: int = 200) -> dict:
        """List projects in an allowed Altium 365 workspace."""
        return _result(lambda: _a365_client().projects(workspace_url, name_contains, limit))

    @mcp.tool()
    def eng_a365_project(project_id: str) -> dict:
        """Read one Altium 365 project by Nexar ID."""
        return _result(lambda: _a365_client().project(project_id))

    @mcp.tool()
    def eng_a365_library_folders(workspace_url: str) -> dict:
        """List library folders in an allowed Altium 365 workspace."""
        return _result(lambda: {"folders": _a365_client().library_folders(workspace_url)})

    @mcp.tool()
    def eng_a365_library_components(workspace_url: str,
                                     name_contains: str = "") -> dict:
        """Search library component names in an allowed Altium 365 workspace."""
        return _result(lambda: {"components": _a365_client().library_components(
            workspace_url, name_contains)})

    @mcp.tool()
    def eng_a365_library_components_params(workspace_url: str) -> dict:
        """List library components in an allowed Altium 365 workspace, each with its parameters."""
        return _result(lambda: {"components": _a365_client().library_components_params(workspace_url)})

    @mcp.tool()
    def eng_a365_component(component_id: str) -> dict:
        """Read an Altium 365 managed component by Nexar ID."""
        return _result(lambda: _a365_client().component(component_id))

    @mcp.tool()
    def eng_a365_entity(query_field: str, entity_id: str, max_fields: int = 60) -> dict:
        """Schema-driven read of any Altium 365 des*ById entity.

        query_field must look like e.g. desFootprintById: selects the
        entity's scalar fields as reported by the live Nexar schema, so
        no field names are guessed. Use when there is no dedicated
        eng_a365_* reader for the entity kind you need.
        """
        return _result(lambda: _a365_client().entity(query_field, entity_id, max_fields))

    @mcp.tool()
    def eng_a365_query(query: str, variables: dict | None = None) -> dict:
        """Run a read-only Nexar GraphQL query with mutation checks."""
        return _result(lambda: _a365_client().execute(query, variables))

    @mcp.tool()
    def eng_a365_doctor() -> dict:
        """Check the connected Nexar schema for required Altium 365 fields."""
        return _result(lambda: _a365_client().doctor())

    @mcp.tool()
    def eng_a365_publish_model(kind: str, workspace_url: str, folder_id: str, name: str,
                              file_path: str, confirm: bool = False) -> dict:
        """Publish a symbol or footprint file to an Altium 365 managed library.

        kind is 'symbol' or 'footprint'; file_path is the local file to
        upload (via Nexar's File Service, then linked into folder_id). name
        is checked against the owner naming policy before publishing.

        Requires confirm=True: this writes a new model to the managed
        library.
        """
        if not confirm:
            return {"ok": False, "reason": "confirm=True is required: this publishes a "
                    "new model to the Altium 365 managed library"}
        return _result(_a365_publish_model, kind, workspace_url, folder_id, name, file_path)

    @mcp.tool()
    def eng_a365_release_component(input_obj: dict, confirm: bool = False) -> dict:
        """Release an Altium 365 managed component (desReleaseComponent).

        input_obj is the release mutation's input object (workspaceUrl,
        componentItemName, and the release fields Nexar's own examples
        show), passed through as-is rather than guessing an undocumented
        input type name (see a365.py's release_mutation/gql_literal).

        Requires confirm=True: this releases a component revision in the
        managed library.
        """
        if not confirm:
            return {"ok": False, "reason": "confirm=True is required: this releases a "
                    "component revision in the Altium 365 managed library"}
        return _result(lambda: _a365_client().release_component(input_obj))

    @mcp.tool()
    def eng_a365_update_parameters(component_id: str, parameters: list[dict], release_note: str,
                                   replace_existing: bool = False, confirm_replace: str = "",
                                   confirm: bool = False) -> dict:
        """Add or change parameters of an Altium 365 library component (new revision).

        parameters is a list of {"name", "value"}. replace_existing=True
        deletes ALL existing parameters first (documented Nexar behavior)
        and additionally requires confirm_replace set to the exact string
        'REPLACE ALL PARAMETERS' - ported from Altium Booster 3.2's own
        guard for this specific operation, kept verbatim rather than
        inventing a different phrase for the same protection.

        Verifies the write by reading the new component revision back and
        confirming every requested parameter matches, rather than trusting
        the mutation's own success response (same discipline as this
        project's eco_verify: a write that reports success is not the same
        as a write that happened).

        Requires confirm=True: this writes a new component revision to the
        managed library.
        """
        if not confirm:
            return {"ok": False, "reason": "confirm=True is required: this writes a new "
                    "component revision to the Altium 365 managed library"}
        return _result(_a365_update_parameters, component_id, parameters, release_note,
                      replace_existing, confirm_replace)

    if altium:
        @mcp.tool()
        async def eng_compare_live_ipc_footprint(footprint_name: str, spec: dict,
                                                  library_path: str = "",
                                                  tolerance_um: int = 20) -> dict:
            """Read a live PcbLib footprint and compare its pads to IPC geometry."""
            from ..bridge import get_bridge
            try:
                raw = await get_bridge().send_command_async(
                    "library.get_pad_geometry",
                    {"footprint_name": footprint_name, "library_path": library_path})
                return _result(_compare_ipc, _live_pads_to_um(raw), spec, tolerance_um)
            except (AltiumAIError, KeyError, TypeError, ValueError, OSError) as exc:
                return {"ok": False, "reason": str(exc)}

        @mcp.tool()
        async def eng_validate_library_live(schlib_path: str, pcblib_path: str,
                                            ruleset: dict | None = None, use_naming: bool = True,
                                            max_components: int = 200,
                                            max_footprints: int = 200,
                                            overlay_layer: str = "TopOverlay",
                                            courtyard_layer: str = "") -> dict:
            """Audit a live SchLib/PcbLib pair with eng_validate_library's checks.

            Reads every component (library.get_component_details) and every
            footprint (library.get_footprint_pads) through the existing
            library bridge commands - one round-trip per component plus
            one per footprint, so max_components/max_footprints cap the
            work on a large library; hitting a cap is reported in the
            result, not silently dropped.

            UNVERIFIED against live Altium (no SchLib/PcbLib was available
            to test against while porting this); verify against a known
            library pair before relying on it.

            Altium has no per-footprint "this is the courtyard/silkscreen
            layer" API (board-level LayerObject_V7.Kind is the closest, but
            is unset in most vendor libraries), so this isolates those
            tracks by LAYER NAME, which you supply: overlay_layer (default
            "TopOverlay", Altium's own default) for silkscreen, and
            courtyard_layer (default "" - unknown) for the courtyard. Set
            courtyard_layer to your library's actual courtyard layer (often
            a named Mechanical layer, e.g. "Mechanical13") to also get
            courtyard and pin-1-marker checks; left blank, those two checks
            are skipped rather than reporting a false "no courtyard" on
            every single footprint (see libcheck.audit_footprint's
            has_full_geometry). Pad spacing, numbering, origin, IPC-name
            consistency, symbol pin checks, footprint-link resolution,
            pin-vs-pad counts, and (with a ruleset) parameter checks always
            use real geometry regardless.
            """
            try:
                policy = naming.NamingPolicy() if use_naming else None
                symbols, footprints, meta = await _fetch_live_library(
                    schlib_path, pcblib_path, max_components, max_footprints,
                    overlay_layer, courtyard_layer)
                result = libvalidate.validate(
                    symbols, footprints, naming=policy, ruleset=ruleset,
                    has_full_footprint_geometry=bool(courtyard_layer))
                return {"ok": True, **result, **meta}
            except (AltiumAIError, KeyError, TypeError, ValueError, OSError) as exc:
                return {"ok": False, "reason": str(exc)}

        @mcp.tool()
        async def eng_eco_preview_live(project_path: str = "", bom_limit: int = 5000) -> dict:
            """Build eng_eco_preview's sch/pcb views from eda-agent's existing reads, then diff.

            sch comes from one project.get_bom call (components, footprints
            and pin-nets together); pcb comes from pcb.get_components plus a
            paginated sweep of pcb.get_pad_properties. A BOM truncated at
            bom_limit is refused rather than diffed against partial data -
            raise bom_limit instead of trusting a partial result.

            UNVERIFIED against live Altium; verify on a project with one
            added/removed component and one footprint swap before relying
            on it for anything that gates an actual ECO.
            """
            try:
                sch, truncated = await _fetch_live_sch_view(project_path or None, bom_limit)
                if truncated:
                    return {"ok": False, "reason":
                            f"BOM has more than {bom_limit} components; raise bom_limit "
                            "rather than diff against a partial component list"}
                pcb = await _fetch_live_pcb_view()
                return _result(eco_preview.diff, sch, pcb)
            except (AltiumAIError, KeyError, TypeError, ValueError, OSError) as exc:
                return {"ok": False, "reason": str(exc)}
