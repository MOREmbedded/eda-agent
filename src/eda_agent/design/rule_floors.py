# SPDX-License-Identifier: Apache-2.0
"""Check a board's design rules against a fab's floors and IPC class floors.

``fab_rules`` gives every published limit of one build at one fab, and,
with an IPC class, the stricter of the fab minimum and the class floor
from the profile's licensed values. This module holds those floors up
against the rules a board already carries and says, rule by rule, which
ones would let something through that the fab (or the class) does not
allow.

It is a floor check, not a rule generator:

- a board rule stricter than the floor is right and is left alone. A fab
  minimum is the least the fab can make, not the value to design at, so
  nothing is ever loosened toward it;
- a rule below the floor is raised to the floor, and only that value
  (plus a preferred value that would otherwise sit under the new minimum);
- only board-wide rules (scope ``All``) are raised by default. A scoped
  rule below the floor is very often a deliberate exception (a waiver
  for one connector, a neck-down in a BGA), so it is reported and only
  raised when the caller names it;
- a rule DRC does not check is reported, not raised;
- nothing is created or deleted. A floor with no rule of its kind on the
  board, or with no writer (annular ring, aspect ratio), is reported with
  its value and source so it can be set by hand.

Pure Python: the caller passes ``pcb.get_design_rules`` output in and
sends the returned ``pcb.set_rule_properties`` calls out. All lengths are
MILS; the calls use the millimetre forms where the tool has one, so a
floor of 0.09 mm is written as 0.09 mm and not as a rounded mil.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable, Optional

from .rule_manager import board_rule_state

MM_PER_MIL = 0.0254

#: One Altium rule kind the check can read, recognised by how its
#: descriptor starts (``rule_kind`` is an enum ordinal that differs between
#: builds, the descriptor does not). ``value`` is the descriptor key that
#: holds the minimum; ``writer`` is the ``pcb_set_rule_properties`` keyword
#: that sets it (None: there is no writer, report only).
KINDS: list[dict[str, Any]] = [
    {"kind": "clearance", "prefixes": ("Clearance Constraint",),
     "label": "Clearance", "checks": [
         {"floor": "min_gap", "value": "gap", "writer": "gap_mm"}]},
    {"kind": "width", "prefixes": ("Width Constraint",),
     "label": "Width", "checks": [
         {"floor": "min_track", "value": "min", "writer": "min_width_mm",
          "preferred": ("preferred", "favored_width_mm"),
          "max": "max"}]},
    {"kind": "hole_size", "prefixes": ("Hole Size Constraint",),
     "label": "Hole Size", "checks": [
         {"floor": "min_drill", "value": "min", "writer": "min_hole_size_mils",
          "max": "max"}]},
    {"kind": "routing_via", "prefixes": ("Routing Via",),
     "label": "Routing Via", "checks": [
         {"floor": "min_drill", "value": "minholewidth",
          "writer": "min_via_hole_mils",
          "preferred": ("preferredholewidth", "preferred_via_hole_mils"),
          "max": "maxholewidth"},
         {"floor": "via_diameter", "value": "minwidth",
          "writer": "min_via_size_mils",
          "preferred": ("preferredwidth", "preferred_via_size_mils"),
          "max": "maxwidth"}]},
    {"kind": "hole_to_hole", "prefixes": ("Hole To Hole Clearance",),
     "label": "Hole To Hole Clearance", "checks": [
         {"floor": "min_hole_to_hole", "value": "gap", "writer": "gap_mm"}]},
    {"kind": "board_outline", "prefixes": ("Board Outline Clearance",
                                           "Board Clearance Constraint"),
     "label": "Board Outline Clearance", "checks": [
         {"floor": "copper_to_edge", "value": "gap", "writer": "gap_mm"}]},
    {"kind": "mask_sliver", "prefixes": ("Minimum Solder Mask Sliver",),
     "label": "Minimum Solder Mask Sliver", "checks": [
         {"floor": "min_mask_sliver", "value": "gap", "writer": "gap_mm"}]},
    {"kind": "silk_to_mask", "prefixes": ("Silk To Solder Mask",),
     "label": "Silk To Solder Mask Clearance", "checks": [
         {"floor": "silk_to_pad", "value": "clearance", "writer": "gap_mm"}]},
    {"kind": "annular_ring", "prefixes": ("Minimum Annular Ring",),
     "label": "Minimum Annular Ring", "checks": [
         {"floor": "min_annular_ring", "value": "minimum", "writer": None}]},
]

#: Floors no board rule can hold, carried to the reply with their value and
#: source so they are set or checked by hand.
REPORT_ONLY = ("pth_annular_ring", "hole_to_copper_inner",
               "inner_via_hole_to_copper", "max_aspect_ratio",
               "min_silk_width", "min_silk_height", "min_bga_pitch",
               "min_laser_drill")

_ALL = re.compile(r"^\s*\(?\s*all\s*\)?\s*$", re.IGNORECASE)


def floors_from_fab_rules(got: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``{floor key: {"mils", "source"}}`` from a ``fab_rules`` reply.

    The governing value is the class floor where one applied and was
    stricter (``design_mils``), else the fab's own figure. Routing-via
    diameter is derived (minimum drill plus twice the via annular ring)
    and says so.
    """
    rules = got.get("rules") or {}
    out: dict[str, dict[str, Any]] = {}
    for key, r in rules.items():
        mils = r.get("design_mils", r.get("mils"))
        if not isinstance(mils, (int, float)):
            continue
        governs = r.get("governs", "fab")
        if governs == "ipc":
            floor = r["ipc_floor"]
            source = {"governs": "ipc", "cite": floor.get("clause"),
                      "fab_mils": r.get("mils")}
        else:
            source = {"governs": "fab", "quote": r.get("quote"), "url": r.get("url")}
            if r.get("ipc_floor"):
                source["ipc_mils"] = r["ipc_floor"].get("mils")
        out[key] = {"mils": float(mils), "label": r.get("label", key),
                    "kind": r.get("kind", "min"), "source": source}
    drill, ring = out.get("min_drill"), out.get("min_annular_ring")
    if drill and ring:
        out["via_diameter"] = {
            "mils": round(drill["mils"] + 2 * ring["mils"], 4),
            "label": "Minimum via diameter", "kind": "min",
            "source": {"governs": "derived",
                       "formula": "min_drill + 2 x min_annular_ring",
                       "from": [drill["source"], ring["source"]]}}
    return out


def _kind_of(descriptor: str) -> Optional[dict[str, Any]]:
    text = (descriptor or "").lstrip()
    for k in KINDS:
        if any(text.startswith(p) for p in k["prefixes"]):
            return k
    return None


def _board_wide(rule: dict[str, Any]) -> bool:
    s1 = str(rule.get("scope_1") or "All")
    s2 = str(rule.get("scope_2") or "All")
    return bool(_ALL.match(s1)) and bool(_ALL.match(s2))


def _ceil(x: float, step: float) -> float:
    """``x`` rounded UP to ``step``: a written floor never lands under it.
    The 1e-6 step slack absorbs float noise (0.09 mm is not 0.0901)."""
    return round(math.ceil(x / step - 1e-6) * step, 6)


def _writer_value(writer: str, mils: float) -> float:
    """The value to send: millimetres to 0.1 um, or mils to 0.001 mil,
    rounded up. The fab data holds mils to three decimals, so 0.09 mm
    arrives as 3.543 mil (0.08999 mm) and goes back out as 0.09 mm."""
    if writer.endswith("_mm"):
        return _ceil(mils * MM_PER_MIL, 1e-4)
    return _ceil(mils, 1e-3)


def check_rules(floors: dict[str, dict[str, Any]],
                board_rules: Iterable[dict[str, Any]],
                *, names: Optional[Iterable[str]] = None,
                tolerance_mils: float = 0.02) -> dict[str, Any]:
    """Every board rule of a known kind against its floor.

    ``names`` lists scoped rules the caller has decided to raise as well.
    Returns ``rows`` (one per rule and checked value, with ``status``
    ok / below_floor / no_value, ``action`` none / raise / decide /
    not_checked / no_writer, and the reason), ``calls`` (the
    ``pcb.set_rule_properties`` parameter dicts that raise every rule
    whose action is ``raise``), ``no_rule_on_board`` (floors with no rule
    of their kind), ``report_only`` and ``in_step``.
    """
    chosen = {n.lower() for n in (names or [])}
    rows: list[dict[str, Any]] = []
    calls: dict[str, dict[str, Any]] = {}
    seen_kinds: set[str] = set()
    named_found: set[str] = set()

    for rule in board_rules:
        kind = _kind_of(rule.get("descriptor") or "")
        if kind is None:
            continue
        seen_kinds.add(kind["kind"])
        state = board_rule_state(rule)
        name = str(rule.get("name") or "")
        wide = _board_wide(rule)
        picked = name.lower() in chosen
        if picked:
            named_found.add(name.lower())
        for chk in kind["checks"]:
            floor = floors.get(chk["floor"])
            if floor is None:
                continue
            row: dict[str, Any] = {
                "rule": name, "kind": kind["label"], "floor_key": chk["floor"],
                "floor_mils": round(floor["mils"], 4), "source": floor["source"],
                "scope_1": rule.get("scope_1") or "", "scope_2": rule.get("scope_2") or "",
                "drc_enabled": state["drc_enabled"],
                "from_schematic": state["from_schematic"]}
            got = state["values"].get(chk["value"])
            row["board_mils"] = None if got is None else round(got, 4)
            if got is None:
                row.update(status="no_value", action="none",
                           reason="the rule's descriptor carries no value to compare")
                rows.append(row)
                continue
            if got >= floor["mils"] - tolerance_mils:
                row.update(status="ok", action="none")
                rows.append(row)
                continue
            row["status"] = "below_floor"
            if chk["writer"] is None:
                row.update(action="no_writer", reason=(
                    "no tool writes this rule kind; set it in Altium's rules "
                    "editor"))
            elif state["from_schematic"]:
                row.update(action="decide", reason=(
                    "defined on the schematic; change it there"))
            elif not state["drc_enabled"]:
                row.update(action="not_checked", reason=(
                    "DRC does not check this rule; left as it is"))
            elif not (wide or picked):
                row.update(action="decide", reason=(
                    "scoped rule below the floor; often a deliberate "
                    "exception. Name it to raise it"))
            else:
                fix = _raise(chk, state, floor["mils"], tolerance_mils)
                if "reason" in fix:
                    row.update(action="decide", reason=fix["reason"])
                else:
                    row["action"] = "raise"
                    row["write"] = fix["write"]
                    calls.setdefault(name, {"name": name}).update(fix["write"])
            rows.append(row)

    present = {c["floor"] for k in KINDS if k["kind"] in seen_kinds for c in k["checks"]}
    no_rule = []
    for k in KINDS:
        if k["kind"] in seen_kinds:
            continue
        for chk in k["checks"]:
            floor = floors.get(chk["floor"])
            if floor is not None and chk["floor"] not in present:
                no_rule.append({"kind": k["label"], "floor_key": chk["floor"],
                                "floor_mils": round(floor["mils"], 4),
                                "source": floor["source"]})
    report_only = [{"floor_key": key, "label": floors[key]["label"],
                    "kind": floors[key]["kind"],
                    "floor_mils" if floors[key]["kind"] != "max" else "limit":
                        round(floors[key]["mils"], 4),
                    "source": floors[key]["source"]}
                   for key in REPORT_ONLY if key in floors]
    unknown_names = sorted(chosen - named_found)
    below = [r for r in rows if r["status"] == "below_floor"]
    return {"rows": rows, "calls": list(calls.values()),
            "below_floor": len(below),
            "to_raise": sum(1 for r in below if r["action"] == "raise"),
            "to_decide": [r["rule"] for r in below if r["action"] == "decide"],
            "no_rule_on_board": no_rule, "report_only": report_only,
            "names_not_found": unknown_names,
            "in_step": not below}


def _raise(chk: dict[str, Any], state: dict[str, Any], floor: float,
           tolerance_mils: float) -> dict[str, Any]:
    """The writes that lift one value to ``floor``, or why they cannot."""
    values = state["values"]
    mx = values.get(chk["max"]) if chk.get("max") else None
    if mx is not None and mx < floor - tolerance_mils:
        return {"reason": (f"the rule's maximum ({round(mx, 4)} mil) is under "
                           f"the floor ({round(floor, 4)} mil); a raised minimum "
                           "would pass its maximum. Decide the range in Altium")}
    write = {chk["writer"]: _writer_value(chk["writer"], floor)}
    pref = chk.get("preferred")
    if pref:
        pv = values.get(pref[0])
        if pv is not None and pv < floor - tolerance_mils:
            write[pref[1]] = _writer_value(pref[1], floor)
    return {"write": write}


__all__ = ["KINDS", "REPORT_ONLY", "floors_from_fab_rules", "check_rules"]
