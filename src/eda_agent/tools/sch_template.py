# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Draw a schematic sheet from a hand template (design/sch_template.py).

``sch_preview_template`` checks a template and draws it to a PNG without
touching the sheet. ``sch_draw_template`` draws it: parts, then the wires,
ports, labels and values, refusing before the first wire if the parts did
not land where the template expected or the wiring would short two nets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..bridge import get_bridge
from ..bridge.exceptions import AltiumError
from ..bridge.payload import payload_safe
from ..config import get_config
from ..design import sch_template as st

#: Keys a template part may carry.
PART_KEYS = frozenset({"designator", "lib_reference", "library_path", "x", "y",
                       "rotation", "value", "footprint"})


def _renders_dir() -> Path:
    d = get_config().workspace_dir / "renders"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _send(bridge, command: str, params: dict, **kw) -> dict:
    """A bridge reply, with a refusal turned into a reply instead of a raise,
    so one failed step reports where it stopped."""
    try:
        reply = await bridge.send_command_async(command, params, **kw)
    except AltiumError as exc:
        return {"success": False, "error": f"{command}: {exc}"}
    return reply if isinstance(reply, dict) else {"success": True, "reply": reply}


def _failed(reply: dict) -> bool:
    return reply.get("success") is False


async def _focus(bridge, path: str) -> dict:
    """Focus a document, opening it first if Altium has not loaded it."""
    reply = await _send(bridge, "application.set_active_document", {"file_path": path})
    if _failed(reply) and "not loaded" in str(reply.get("error", "")).lower():
        await _send(bridge, "application.run_process", {
            "process_name": "WorkspaceManager:OpenObject",
            "parameters": f"ObjectKind=Document|FileName={path}"})
        reply = await _send(bridge, "application.set_active_document", {"file_path": path})
    return reply


def _part_problems(template: Any) -> list[str]:
    if not isinstance(template, dict):
        return ["the template must be an object with parts and wires"]
    problems = []
    seen: set[str] = set()
    for i, p in enumerate(template.get("parts") or []):
        if not isinstance(p, dict):
            problems.append(f"parts[{i}] is not an object")
            continue
        extra = set(p) - PART_KEYS
        if extra:
            problems.append(f"parts[{i}]: unknown keys {sorted(extra)}; "
                            f"known: {sorted(PART_KEYS)}")
        ref = str(p.get("designator", "")).strip()
        if not ref or not str(p.get("lib_reference", "")).strip():
            problems.append(f"parts[{i}]: designator and lib_reference are required")
        elif ref in seen:
            problems.append(f"parts[{i}]: designator {ref} is used twice")
        seen.add(ref)
        if int(p.get("rotation", 0) or 0) % 90:
            problems.append(f"parts[{i}]: rotation must be 0, 90, 180 or 270")
    return problems


async def _symbol_pins(bridge, parts: list[dict]) -> tuple[dict, list[str]]:
    """Each part's pins on the sheet, read from its library symbol."""
    cache: dict[tuple[str, str], list[dict]] = {}
    problems: list[str] = []
    for p in parts:
        key = (str(p.get("library_path", "")).strip(), str(p["lib_reference"]).strip())
        if key in cache:
            continue
        lib, ref = key
        if lib:
            focus = await _focus(bridge, lib)
            if _failed(focus):
                problems.append(f"could not open {lib}: {focus.get('error')}")
                cache[key] = []
                continue
        reply = await _send(bridge, "library.get_pin_list", {"component_name": ref})
        pins = reply.get("pins") if isinstance(reply, dict) else None
        if not isinstance(pins, list):
            problems.append(f"no pins read for {ref}"
                            + ("" if lib else " (give library_path)") + f": {reply}")
            pins = []
        cache[key] = pins
    out = {}
    for p in parts:
        key = (str(p.get("library_path", "")).strip(), str(p["lib_reference"]).strip())
        out[str(p["designator"])] = st.place_symbol_pins(
            str(p["designator"]), cache[key], int(p.get("x", 0)), int(p.get("y", 0)),
            int(p.get("rotation", 0) or 0))
    return out, problems


def _with_plan(template: Any, plan_json: str, sheet: str
               ) -> tuple[Any, dict, list[dict], list[str]]:
    """The template with the plan's parts and nets merged in; stamps per
    designator; notes; and problems that stop the call."""
    if not plan_json.strip():
        return template, {}, [], []
    if not isinstance(template, dict):
        return template, {}, [], ["the template must be an object with parts and wires"]
    from pydantic import ValidationError

    from ..design.plan import DesignPlan
    try:
        plan = DesignPlan.model_validate_json(plan_json)
    except ValidationError as exc:
        return template, {}, [], [
            f"plan_json: {'.'.join(str(x) for x in e['loc'])}: {e['msg']}"
            for e in exc.errors()][:10]
    merged, stamps, notes = st.merge_plan(template, plan, sheet)
    errors = [f"{n['where']}: {n['detail']}" for n in notes if n["level"] == "error"]
    return merged, stamps, [n for n in notes if n["level"] != "error"], errors


def _render(d: st.Drawing, png_path: str, title: str) -> dict[str, Any]:
    path = png_path or str(_renders_dir() / "template_preview.png")
    try:
        return {"preview_png": st.render_png(d, path, title=title)}
    except ImportError:
        return {"preview_error": "matplotlib is not installed (pip install eda-agent[render])"}
    except Exception as exc:  # a failed picture must not hide the report
        return {"preview_error": f"{type(exc).__name__}: {exc}"}


def register_sch_template_tools(mcp):
    """Register the hand-template schematic tools."""

    @mcp.tool()
    async def sch_preview_template(
        template: dict[str, Any],
        png_path: str = "",
        grid: int = 100,
        plan_json: str = "",
        sheet: str = "",
    ) -> dict[str, Any]:
        """Check a schematic template and draw it to a PNG, without
        touching any sheet. Read the PNG to look at it.

        A template is the sheet planned by hand, every coordinate on the
        grid (mils):

            {"parts": [{"designator": "U1", "lib_reference": "TPS62130",
                        "library_path": "C:/.../Power.SchLib",
                        "x": 3000, "y": 4000, "rotation": 0,
                        "value": "TPS62130"}],
             "wires": [{"net": "VIN",
                        "path": [[2000, 4600], "U1.PVIN"]}],
             "ports": [{"net": "+12V", "at": [2000, 4600], "style": "bar"}],
             "labels": [{"net": "PG", "at": [4200, 3700]}],
             "nets": {"VIN": ["U1.PVIN", "C1.1"]}}

        A path point is [x, y] or a pin anchor "U1.11" / "U1.EN" (the pin's
        wire end, worked out from the symbol, so you never compute it).
        Two points off-axis get one corner, horizontal first ("corner":
        "vh" for vertical first). Altium adds the junction dots itself
        where a wire ends on another; do not plan any. Ports default to
        style "bar" pointing up, or "gnd_power" pointing down for a net
        containing GND. ``nets`` is the netlist the sheet must produce,
        checked pin by pin.

        Args:
            template: as above.
            png_path: where to write the picture; default the workspace's
                renders folder.
            grid: the grid in mils every vertex should sit on.
            plan_json: the DesignPlan, if there is one. Parts then need
                only designator and position (symbol, library and value
                come from the plan) and the plan's nets are checked pin
                by pin. A part the plan lacks is an error.
            sheet: the plan sheet this template draws, to list the plan
                parts it leaves out.

        Returns:
            ``ok``, ``errors`` (shorts, overlapping wires, bad anchors,
            netlist differences, a port touching nothing), ``warnings``
            (crossings, a wire through a body, dangling ends, off-grid
            points, stacked labels), ``netlist``, ``unconnected_pins``,
            ``auto_junctions`` and ``preview_png``.
        """
        template, _, plan_notes, problems = _with_plan(template, plan_json, sheet)
        problems = problems or _part_problems(template)
        if problems:
            return {"ok": False, "errors": problems}
        bridge = get_bridge()
        pins, problems = await _symbol_pins(bridge, template.get("parts") or [])
        if problems:
            return {"ok": False, "errors": problems}
        try:
            d = st.build(template, pins, grid=grid)
        except st.TemplateError as exc:
            return {"ok": False, "errors": [str(exc)]}
        out = {**d.report(), **_render(d, png_path, "template preview")}
        if plan_notes:
            out["plan_notes"] = plan_notes
        return out

    @mcp.tool()
    async def sch_draw_template(
        template: dict[str, Any],
        document_path: str,
        png_path: str = "",
        grid: int = 100,
        plan_json: str = "",
        sheet: str = "",
    ) -> dict[str, Any]:
        """Draw a schematic sheet from a hand template: THE way to draw
        or redraw a block. Same template as sch_preview_template; run that
        first and look at the PNG.

        Order: the template is checked against the library symbols, the
        parts are placed, their pins are read back off the sheet, the
        wiring is checked again against those real pins (and every other
        part already on the sheet), and only then are the wires, power
        ports, net labels and values (the Comment) drawn. Any error stops
        it before the first wire. A designator already on the sheet is
        refused: redraw on a fresh sheet rather than patching. Does not
        save.

        Args:
            template: parts, wires, ports, labels and optional nets.
            document_path: the .SchDoc to draw on (it is focused first).
            png_path: where to write a picture of what was drawn.
            grid: grid in mils for the off-grid check.
            plan_json: the DesignPlan, as for sch_preview_template. Its
                Value, Manufacturer, Manufacturer Part Number and
                Footprint are stamped on every drawn part.
            sheet: the plan sheet this template draws.

        Returns:
            The same report as sch_preview_template, plus ``placed``,
            ``wires``, ``ports``, ``labels``, ``values`` counts and
            ``stage`` (where it stopped, or "done").
        """
        template, plan_stamps, plan_notes, problems = _with_plan(
            template, plan_json, sheet)
        problems = problems or _part_problems(template)
        if problems:
            return {"ok": False, "stage": "template", "errors": problems}
        parts = template.get("parts") or []
        bridge = get_bridge()

        predicted, problems = await _symbol_pins(bridge, parts)
        if problems:
            return {"ok": False, "stage": "symbols", "errors": problems}
        try:
            preview = st.build(template, predicted, grid=grid)
        except st.TemplateError as exc:
            return {"ok": False, "stage": "template", "errors": [str(exc)]}
        if not preview.ok:
            return {**preview.report(), "stage": "template",
                    **_render(preview, png_path, "template (not drawn)")}

        focus = await _focus(bridge, document_path)
        if _failed(focus):
            return {"ok": False, "stage": "focus",
                    "errors": [f"could not focus {document_path}: {focus.get('error')}"]}
        before = await _send(bridge, "generic.get_sch_doc_pins", {"sheet_path": document_path})
        if _failed(before):
            return {"ok": False, "stage": "sheet", "errors": [before.get("error")]}
        on_sheet = {str(r.get("refdes", "")) for r in (before or {}).get("pins", [])}
        clash = sorted(on_sheet & {str(p["designator"]) for p in parts})
        if clash:
            return {"ok": False, "stage": "sheet", "errors": [
                f"already on the sheet: {', '.join(clash)}; draw on a fresh sheet "
                "or delete them first"]}

        ops = []
        for p in parts:
            fields = [f"library_path={payload_safe(p.get('library_path', ''))}",
                      f"lib_reference={payload_safe(p['lib_reference'])}",
                      f"x={int(p.get('x', 0))}", f"y={int(p.get('y', 0))}",
                      f"rotation={int(p.get('rotation', 0) or 0)}",
                      f"designator={payload_safe(p['designator'])}"]
            if p.get("footprint"):
                fields.append(f"footprint={payload_safe(p['footprint'])}")
            ops.append(";".join(fields))
        placed = await _send(bridge, "generic.place_sch_components_from_library",
                             {"placements": "~~".join(ops)})
        if _failed(placed):
            return {"ok": False, "stage": "placement", "errors": [placed.get("error")]}

        after = await _send(bridge, "generic.get_sch_doc_pins", {"sheet_path": document_path})
        real = st.pins_from_sheet((after or {}).get("pins", []))
        moved = []
        for ref, plist in predicted.items():
            got = {(q.number, q.x, q.y) for q in real.get(ref, [])}
            want = {(q.number, q.x, q.y) for q in plist}
            if want - got:
                moved.append(ref)
        if moved:
            return {"ok": False, "stage": "placement", "placed": placed, "errors": [
                f"{', '.join(moved)} did not land where the template put "
                "their pins; nothing was wired. Check the parts on the sheet."]}

        d = st.build(template, real, grid=grid)
        if not d.ok:
            return {**d.report(), "stage": "wiring", "placed": placed,
                    **_render(d, png_path, "placed, not wired")}

        counts: dict[str, Any] = {"placed": placed}
        if d.segments:
            counts["wires"] = await _send(bridge,
                "generic.place_wires", {"wires": "~~".join(
                    f"x1={s.x1};y1={s.y1};x2={s.x2};y2={s.y2}" for s in d.segments)})
        if d.ports:
            counts["ports"] = await _send(bridge,
                "generic.place_power_ports", {"ports": "~~".join(
                    f"text={payload_safe(o['net'])};x={o['x']};y={o['y']};"
                    f"style={o['style']};orientation={o['orientation']}"
                    for o in d.ports)})
        if d.labels:
            counts["labels"] = await _send(bridge,
                "generic.place_net_labels", {"labels": "~~".join(
                    f"text={payload_safe(o['net'])};x={o['x']};y={o['y']};"
                    f"orientation={o['orientation']}" for o in d.labels)})
        stamps = []
        for p in parts:
            ref = str(p["designator"])
            fields = dict(plan_stamps.get(ref, {}))
            if str(p.get("value", "")).strip():
                fields["Comment"] = str(p["value"])
            if fields:
                stamps.append(";".join([f"designator={payload_safe(ref)}"] + [
                    f"{k}={payload_safe(v)}" for k, v in fields.items()]))
        if stamps:
            counts["values"] = await _send(
                bridge, "generic.set_sch_components_parameters",
                {"stamps": "~~".join(stamps), "sheet_path": document_path},
                timeout=60.0)
        failed = [k for k, v in counts.items()
                  if isinstance(v, dict) and (_failed(v) or v.get("failed"))]
        out = {**d.report(), **counts, "stage": "done",
               **_render(d, png_path, "drawn")}
        if failed:
            out["ok"] = False
            out["errors"] = out["errors"] + [
                f"{k}: not all placed ({counts[k]})" for k in failed]
        if plan_notes:
            out["plan_notes"] = plan_notes
        return out
