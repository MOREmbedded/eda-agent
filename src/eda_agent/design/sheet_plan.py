# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Read a DesignPlan back off a live schematic sheet.

An edit to a drawn sheet is then a plan edit: the plan stays the
netlist truth, and the block is redrawn from a hand template
(design/sch_template.py) with the edited plan.
"""

from __future__ import annotations

import logging
import re
from pathlib import PureWindowsPath
from typing import Any

from pydantic import ValidationError

from eda_agent.design.plan import _NET_PATTERN, _REFDES_PATTERN, DesignPlan

logger = logging.getLogger(__name__)

#: Why the last bridge lookup failed, for the notes.
_last_bridge_failure = ""

# Standard schematic sheet drawing areas in mils (landscape). A4 keeps
# Altium's inner-area default (11500x7600); the rest are the ISO/US series.
# A planner picks a sheet via the `size` string; the dimensions follow.
_SHEET_DIMENSIONS: dict[str, tuple[int, int]] = {
    "A4": (11500, 7600),
    # A5 is 210 x 148 mm. Absent from this table it fell back to A4,
    # which is 81% MORE area than the sheet has: five of the KiCad demo
    # sheets are A5, and their layouts were being judged against a
    # frame they were not drawn on.
    "A5": (8270, 5830),
    "A3": (16540, 11690),
    "A2": (23390, 16540),
    "A1": (33110, 23390),
    "A0": (46810, 33110),
    "A": (11000, 8500),
    "B": (17000, 11000),
    "C": (22000, 17000),
    "D": (34000, 22000),
    "E": (44000, 34000),
    # KiCad writes the US series by these names, and they are the same
    # sheets as ANSI A / A / B above: Letter is 11 x 8.5 in, Legal is
    # 14 x 8.5, Ledger (Tabloid) is 17 x 11. Without them a US-locale
    # schematic silently falls back to A4, which is the same hole A5
    # was in.
    "USLETTER": (11000, 8500),
    "USLEGAL": (14000, 8500),
    "USLEDGER": (17000, 11000),
}



def _altium_path(path: str) -> PureWindowsPath:
    """A path Altium reported, read as the Windows path it always is.

    On a Linux host, WSL included, ``Path`` does not split on
    backslashes, so it takes a whole Windows path to be one file name.
    """
    return PureWindowsPath(path)


def plan_from_live_sheet(
    project_path: str,
    sheet_document: str = "",
    *,
    bridge: Any = None,
) -> dict[str, Any]:
    """Reconstruct a DesignPlan from a drawn schematic sheet.

    The components give the parts, the compiled netlist gives the nets,
    and the power-port glyphs say which of those nets are rails. The
    result is a plan you can edit and then redraw from a template.

    WHAT IS NOT ON THE SHEET, and so cannot come back:

    - ``role``. Every part's role tag is a planner assertion, not a
      drawn property; re-assert the roles from the part list and the
      netlist.
    - ``zone``. Functional blocks are not drawn either.
    - A SINGLE-PIN NET. ``Net.pins`` needs two, so a pin sitting on its
      own rail glyph or off-sheet label has nowhere to go. Those pins
      are listed in ``dropped_pins`` and the parts stay.
    - Anything on a sheet other than the one read. This is one sheet.

    The plan is validated before it is returned, so what comes back is
    either a valid plan or a refusal saying why.
    """
    out: dict[str, Any] = {
        "ok": True, "plan": None, "notes": [], "dropped_pins": [],
        "parts_read": 0, "nets_read": 0,
    }
    if not project_path or not _altium_path(project_path).name:
        out["ok"] = False
        out["notes"].append(
            f"project_path must name a .PrjPcb file; got {project_path!r}")
        return out
    bridge = bridge or _resolve_bridge()
    if bridge is None:
        out["ok"] = False
        reason = f" ({_last_bridge_failure})" if _last_bridge_failure else ""
        out["notes"].append(f"no Altium bridge; cannot read the sheet.{reason}")
        return out

    scope = f"doc:{sheet_document}" if sheet_document else "active_doc"
    sheet_name = (_altium_path(sheet_document).stem if sheet_document
                  else "main")
    # A sheet the retired layout engine drew is named
    # "<project stem>__<sheet>"; keep the plan-side name it had.
    stem = _altium_path(project_path).stem
    if sheet_name.startswith(f"{stem}__"):
        sheet_name = sheet_name[len(stem) + 2:]

    try:
        response = bridge.send_command("generic.query_objects", {
            "object_type": "eSchComponent", "scope": scope,
            "properties": ("Designator.Text,LibReference,SourceLibraryName,"
                           "Comment.Text"),
        })
    except Exception as exc:                    # noqa: BLE001
        out["ok"] = False
        out["notes"].append(f"reading the components failed: {exc}")
        return out

    parts: list[dict[str, Any]] = []
    on_sheet: set[str] = set()
    for row in (response or {}).get("objects", []):
        refdes = str(row.get("Designator.Text", "")).strip()
        lib_ref = str(row.get("LibReference", "")).strip()
        if not refdes or not lib_ref:
            # Both are required by the plan schema and neither can be
            # invented: a part with no designator has no identity and a
            # part with no lib_ref names no symbol.
            out["notes"].append(
                f"skipped a component with designator={refdes!r} "
                f"lib_ref={lib_ref!r}: the plan needs both")
            continue
        if not re.match(_REFDES_PATTERN, refdes):
            out["notes"].append(
                f"skipped {refdes!r}: not a designator the plan accepts")
            continue
        on_sheet.add(refdes)
        part: dict[str, Any] = {"refdes": refdes, "lib_ref": lib_ref,
                                "sheet": sheet_name}
        lib_path = str(row.get("SourceLibraryName", "")).strip()
        if lib_path:
            part["lib_path"] = lib_path
        value = str(row.get("Comment.Text", "")).strip()
        if value:
            part["value"] = value
        parts.append(part)
    out["parts_read"] = len(parts)
    if not parts:
        out["ok"] = False
        out["notes"].append(
            f"no components read from {scope}; a plan needs at least one")
        return out

    _enrich_parts(bridge, project_path, parts, out)
    rails = _rail_nets(bridge, scope, out)
    nets = _nets_on_sheet(bridge, project_path, on_sheet, rails, out)
    out["nets_read"] = len(nets)
    if not nets:
        out["ok"] = False
        out["notes"].append(
            "no net with two pins on this sheet; the plan schema has no "
            "way to express a part list with no connections")
        return out

    sheet: dict[str, Any] = {"name": sheet_name}
    size = _sheet_size(bridge, sheet_document, out)
    if size:
        sheet["size"] = size

    payload = {
        "spec": f"reconstructed from {sheet_document or 'the active sheet'}",
        "summary": (f"{len(parts)} parts and {len(nets)} nets read back off "
                    f"the sheet; roles and zones are not recorded on a "
                    f"schematic and are absent"),
        "sheets": [sheet],
        "parts": parts,
        "nets": nets,
    }
    try:
        DesignPlan.model_validate(payload)
    except ValidationError as exc:
        out["ok"] = False
        out["notes"].extend(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors())
        out["notes"].append(
            "the sheet does not reconstruct into a valid plan; the errors "
            "above name the parts or nets that could not be expressed")
        return out
    out["plan"] = payload
    out["notes"].append(
        "role and zone are NOT on a schematic and are absent from this "
        "plan; assert them before laying it out or the placer loses the "
        "motifs, the decoupling law and the placement priors")
    return out


def _same_altium_document(answered: str, asked: str) -> bool:
    """Whether two paths name the same Altium document.

    Two full paths have to match in full, since a sheet of the same name
    in another folder is another document. A bare file name can only be
    matched on the name. Case is ignored either way.
    """
    a, b = _altium_path(answered), _altium_path(asked)
    if a.anchor and b.anchor:
        return a == b
    return a.name.lower() == b.name.lower()


def _sheet_size(
    bridge: Any, sheet_document: str, out: dict[str, Any]
) -> str:
    """The paper the sheet is drawn on, or "" to leave the plan default.

    Worth a round trip because the placer sizes the drawing area from
    it: 19% of real sheets are bigger than A4, and laying one of those
    out as A4 confines every part to a band it cannot be separated in.

    READS THE ACTIVE DOCUMENT, WHATEVER IT IS ASKED. Gen_GetDocumentInfo
    resolves SchServer.GetCurrentSchDocument and honours no scope, so
    the answer is checked against the document that was wanted before it
    is believed. Reporting the size of a sheet nobody asked about is
    exactly the wrong-document failure this server warns about.
    """
    try:
        info = bridge.send_command("generic.get_document_info", {})
    except Exception as exc:                    # noqa: BLE001
        out["notes"].append(
            f"could not read the sheet size ({exc}); the plan carries the "
            f"default and a re-layout would use an A4 drawing area")
        return ""
    if not isinstance(info, dict):
        return ""
    answered = str(info.get("file_path", "") or "")
    if sheet_document and not _same_altium_document(answered, sheet_document):
        out["notes"].append(
            f"sheet size not read: the document info came back for "
            f"{answered!r}, not the sheet asked for; the plan carries the "
            f"default")
        return ""
    size = str(info.get("sheet_size", "") or "").strip()
    if not size:
        return ""
    # MEMBERSHIP, not "does it resolve": sheet_dimensions falls back to
    # A4 for anything it does not know, so comparing dimensions would
    # accept Custom and every OrCAD size as though they were A4.

    if size.upper() not in _SHEET_DIMENSIONS:
        out["notes"].append(
            f"sheet size {size!r} has no drawing area the layout knows; "
            f"the plan carries the default and a re-layout would use an "
            f"A4 area")
        return ""
    return size


def _enrich_parts(
    bridge: Any,
    project_path: str,
    parts: list[dict[str, Any]],
    out: dict[str, Any],
) -> None:
    """Fill in value, footprint and MPN from the compiled component data.

    THE VALUE IS NOT WHERE YOU FIRST LOOK. This engine stamps a part's
    value into a PARAMETER called Value, while a hand-drawn sheet
    conventionally carries it in the Comment, and the object query can
    only read the Comment. Reconstructing from the Comment alone gives a
    plan with no values at all for every sheet this engine drew, and
    laying that plan out again would strip them off the parts.

    Best effort. If the project will not answer, the parts keep whatever
    the sheet query gave them and a note says the values may be short.
    """
    if not parts:
        return
    try:
        response = bridge.send_command("project.get_component_info_batch", {
            "designators": "~~".join(p["refdes"] for p in parts),
            "project_path": project_path,
            "with_pin_nets": "false",
        })
    except Exception as exc:                    # noqa: BLE001
        out["notes"].append(
            f"could not read the compiled component data ({exc}); values "
            f"and footprints come from the sheet alone and may be missing")
        return
    by_refdes = {}
    for row in (response or {}).get("components", []):
        if isinstance(row, dict) and row.get("designator"):
            by_refdes[str(row["designator"]).strip()] = row
    for part in parts:
        row = by_refdes.get(part["refdes"])
        if not row:
            continue
        params = row.get("parameters") if isinstance(
            row.get("parameters"), dict) else {}
        for field, source in (
            ("value", params.get("Value") or row.get("comment")),
            ("footprint", row.get("footprint")),
            ("mpn", params.get("Manufacturer Part Number")),
            ("manufacturer", params.get("Manufacturer")),
        ):
            text = str(source or "").strip()
            if text:
                part[field] = text


def _rail_nets(
    bridge: Any, scope: str, out: dict[str, Any]
) -> dict[str, bool]:
    """Net name -> is_ground, from the sheet's power-port glyphs.

    A power port is drawn evidence that its net is a rail, and its style
    says which kind. Read from the sheet rather than from the net's name,
    which is a convention and not a fact.
    """
    from eda_agent.render.sch_svg import _GROUND_STYLES

    try:
        response = bridge.send_command("generic.query_objects", {
            "object_type": "ePowerObject", "scope": scope,
            "properties": "Text,Style",
        })
    except Exception as exc:                    # noqa: BLE001
        out["notes"].append(
            f"reading the power ports failed ({exc}); every net will be "
            f"laid out as a signal")
        return {}
    rails: dict[str, bool] = {}
    for row in (response or {}).get("objects", []):
        text = str(row.get("Text", "")).strip()
        if not text:
            continue
        try:
            style = int(row.get("Style", 0))
        except (TypeError, ValueError):
            continue
        # A rail with both a ground and a supply glyph on one sheet is
        # a drawing fault, not a thing to average: keep the ground,
        # which is the one that changes how it is drawn.
        rails[text] = rails.get(text, False) or style in _GROUND_STYLES
    return rails


def _nets_on_sheet(
    bridge: Any,
    project_path: str,
    on_sheet: set[str],
    rails: dict[str, bool],
    out: dict[str, Any],
) -> list[dict[str, Any]]:
    """The compiled netlist, restricted to the parts read off this sheet."""
    try:
        # FORCED RECOMPILE. This reads a sheet somebody has been working
        # on, which is the one case where the cached netlist is the old
        # one: a wire drawn since the last compile would be missing from
        # the plan, and nothing about the result would look wrong.
        response = bridge.send_command("project.get_nets", {
            "limit": "100000", "project_path": project_path,
            "force_recompile": "true"})
    except Exception as exc:                    # noqa: BLE001
        out["notes"].append(f"reading the netlist failed: {exc}")
        return []
    by_net: dict[str, list[dict[str, str]]] = {}
    for row in (response or {}).get("pins", []):
        comp = str(row.get("component", "")).strip()
        pin = str(row.get("pin", "")).strip()
        net = str(row.get("net", "")).strip()
        if not comp or not pin or not net:
            continue
        if comp not in on_sheet:
            continue
        # PinRef endpoints must be unique within a net or the plan is
        # refused wholesale. A pin listed twice for one net is a read
        # artefact, not a second connection, so it collapses here rather
        # than taking the whole reconstruction down with it.
        endpoint = {"refdes": comp, "pin": pin}
        if endpoint not in by_net.setdefault(net, []):
            by_net[net].append(endpoint)

    nets: list[dict[str, Any]] = []
    for name in sorted(by_net):
        pins = by_net[name]
        if len(pins) < 2:
            # Net.pins has a minimum of two. Say which pin went, because
            # a silently dropped connection reads as a wiring change.
            out["dropped_pins"].append(
                f"{pins[0]['refdes']}.{pins[0]['pin']} on {name}")
            continue
        if not re.match(_NET_PATTERN, name):
            out["dropped_pins"].extend(
                f"{p['refdes']}.{p['pin']} on {name}" for p in pins)
            out["notes"].append(
                f"net {name!r} is not a name the plan accepts; its "
                f"{len(pins)} pins are unconnected in this plan")
            continue
        net: dict[str, Any] = {"name": name, "pins": pins}
        if name in rails:
            net["is_ground" if rails[name] else "is_power"] = True
        nets.append(net)
    if out["dropped_pins"]:
        out["notes"].append(
            f"{len(out['dropped_pins'])} pin(s) sit on a net this plan "
            f"cannot express and are unconnected in it")
    return nets


def _resolve_bridge() -> Any:
    """Lazy-import the global bridge so this module doesn't pull bridge
    code at import time (keeps it cheap for unit tests that don't need
    Altium).

    On failure the reason is kept in ``_last_bridge_failure`` so callers
    can put the REAL cause (e.g. "Altium is not running") in the result
    notes instead of a generic "no bridge".
    """
    global _last_bridge_failure
    try:
        from eda_agent.bridge.altium_bridge import get_bridge
    except ImportError as exc:
        _last_bridge_failure = f"bridge module unavailable: {exc}"
        return None
    try:
        bridge = get_bridge()
        _last_bridge_failure = ""
        return bridge
    except Exception as exc:
        _last_bridge_failure = f"{type(exc).__name__}: {exc}"
        logger.warning("get_bridge failed: %s", exc)
        return None


