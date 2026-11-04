# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Reconcile a board's design rules against the rules a profile asks for.

``rule_synthesis`` turns a fab profile into ``pcb_create_design_rule``
parameter dicts. Nothing then applied them twice safely: Altium's
``PCB_CreateDesignRule`` sets ``.Name`` and adds, with no check for a name
already there, so a second apply leaves two rules sharing a name and
Altium's priority order picks the winner. This module decides, per rule,
whether it has to be created, updated in place, or is already right, so an
apply is idempotent.

Reading a rule's values back is the awkward half. Only a clearance-family
rule reports a number of its own (``gap_mm``); every other kind is
readable solely as ``Rule.Descriptor``, the prose Altium writes for the
rules dialog. MEASURED against the 159 rules of a real six-layer board:

* units are mixed, ``mm`` on one rule and ``mil`` on the next;
* a value may be negative (``Horizontal Gap = -20mm``, how a clearance
  check is waived for one part);
* spacing around ``=`` is not consistent (``Expansion =0mm``);
* one paren may hold two values (``( Horizontal Gap = -20mm, Vertical
  Gap = -20mm )``);
* and Altium misspells ``Preferred`` as ``Prefered`` in some kinds, both
  spellings appearing on the same board, so a parser that knows only the
  correct one silently misses every differential-pair width rule.

``(Disabled)`` in the descriptor is ``DRCEnabled`` being false, NOT
``Enabled``: the two are different flags and the second stays true. The
bridge now reports ``drc_enabled`` directly, and this module falls back to
reading the word out of the descriptor for a reply from an older script.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

#: ``Preferred`` as Altium spells it, both ways, mapped to one key.
_CANON = {
    "prefered": "preferred",
    "preferedwidth": "preferredwidth",
    "prefered gap": "preferred gap",
}

#: mil per unit, for the suffixes a descriptor uses.
_UNITS = {"mm": 1000.0 / 25.4, "mil": 1.0, "mils": 1.0, "um": 1.0 / 25.4,
          "cm": 10000.0 / 25.4, "in": 1000.0, "inch": 1000.0}

#: ``Key = Value`` inside one paren group, the key being words and spaces.
_VALUE = re.compile(r"([A-Za-z][A-Za-z ]*?)\s*=\s*([^,()]*)")

#: A number with an optional unit suffix.
_NUMBER = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*$")

#: What the caller may not change: a rule the schematic owns is rewritten
#: from there on the next update, so writing it here is undone silently.
SKIP_REASON_SCHEMATIC = "defined on the schematic; change it there"


def canonical_key(name: str) -> str:
    """A descriptor's value key, lowercased and de-misspelt."""
    key = " ".join(name.split()).lower()
    return _CANON.get(key, key)


def to_mils(text: str) -> Optional[float]:
    """``"0.15mm"`` -> ``5.905...``; None when it is not a number.

    A bare number is already mils, which is what Altium writes for a
    unitless constraint such as ``Priority=3``.
    """
    m = _NUMBER.match(text or "")
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2).lower()
    if not unit:
        return value
    factor = _UNITS.get(unit)
    return None if factor is None else value * factor


def parse_descriptor(descriptor: str) -> dict[str, Any]:
    """The constraint values and the DRC flag inside Altium's own prose.

    Returns ``{"values": {key: mils}, "text": {key: as written},
    "drc_enabled": bool}``. A key whose value is not a number is kept in
    ``text`` only, so ``Allowed=No`` and ``Topology=Shortest`` survive
    without being guessed at.
    """
    text = descriptor or ""
    values: dict[str, float] = {}
    as_written: dict[str, str] = {}
    for raw_key, raw_value in _VALUE.findall(text):
        key = canonical_key(raw_key)
        if not key:
            continue
        written = raw_value.strip()
        as_written[key] = written
        mils = to_mils(written)
        if mils is not None:
            values[key] = mils
    return {"values": values, "text": as_written,
            "drc_enabled": "(Disabled)" not in text}


def board_rule_state(rule: dict[str, Any]) -> dict[str, Any]:
    """One rule as the reconciler compares it.

    ``drc_enabled`` is taken from the field when the script reports one
    and read out of the descriptor otherwise, so an older deployed script
    still gives the right answer.
    """
    parsed = parse_descriptor(rule.get("descriptor") or "")
    drc = rule.get("drc_enabled")
    if drc is None:
        drc = parsed["drc_enabled"]
    # A clearance-family rule reports its gap as a number of its own, and
    # Board Outline Clearance's descriptor always says Gap=0mil whatever
    # the rule holds, so the field wins wherever there is one.
    values = dict(parsed["values"])
    if rule.get("gap_mm") is not None:
        gap = to_mils(f"{rule['gap_mm']}mm")
        if gap is not None:
            values["gap"] = gap
    return {"name": rule.get("name"), "values": values,
            "text": parsed["text"], "drc_enabled": bool(drc),
            "enabled": rule.get("enabled"),
            "priority": rule.get("priority"),
            "from_schematic": bool(rule.get("from_schematic")),
            "descriptor": rule.get("descriptor") or ""}


#: Which descriptor keys carry the value a synthesized rule names, by the
#: ``rule_type`` that rule_synthesis emits. First key present wins.
_VALUE_KEYS: dict[str, tuple[str, ...]] = {
    "clearance": ("gap", "clearance", "minimum"),
    "width": ("min", "minwidth", "minimum", "conductor width"),
    "via_size": ("minholewidth", "min", "minimum"),
    "hole_size": ("minholewidth", "min", "minimum"),
    "diff_pair": ("min", "minimum"),
    "solder_mask": ("expansion",),
    "paste_mask": ("expansion",),
}

#: The favored/preferred value, where a rule kind has one.
_FAVORED_KEYS: tuple[str, ...] = ("preferred", "preferredwidth",
                                  "preferredholewidth", "preferred gap")


def _planned_value(planned: dict[str, Any]) -> Optional[float]:
    value = planned.get("value")
    return float(value) if isinstance(value, (int, float)) else None


def _actual_value(state: dict[str, Any], rule_type: str) -> Optional[float]:
    for key in _VALUE_KEYS.get(rule_type, ("min", "gap", "minimum")):
        if key in state["values"]:
            return state["values"][key]
    return None


def _actual_favored(state: dict[str, Any]) -> Optional[float]:
    for key in _FAVORED_KEYS:
        if key in state["values"]:
            return state["values"][key]
    return None


def compare_rule(planned: dict[str, Any], state: dict[str, Any],
                 tolerance_mils: float = 0.02) -> list[dict[str, Any]]:
    """Every field of ``planned`` the board does not already match.

    ``tolerance_mils`` absorbs the round trip through Altium's millimetre
    descriptor: a rule written as 6 mil reads back as 0.152mm, which is
    5.9842... mil, and is not a difference anybody wants reported.
    """
    rule_type = str(planned.get("rule_type") or "")
    out: list[dict[str, Any]] = []

    want = _planned_value(planned)
    if want is not None:
        got = _actual_value(state, rule_type)
        if got is None:
            out.append({"field": "value", "want": want, "got": None,
                        "reason": "the descriptor carries no value for this kind"})
        elif abs(got - want) > tolerance_mils:
            out.append({"field": "value", "want": want, "got": round(got, 4)})

    favored = planned.get("favored_value")
    if isinstance(favored, (int, float)):
        got = _actual_favored(state)
        if got is not None and abs(got - float(favored)) > tolerance_mils:
            out.append({"field": "favored_value", "want": float(favored),
                        "got": round(got, 4)})

    if planned.get("drc_enabled") is not None:
        want_drc = bool(planned["drc_enabled"])
        if state["drc_enabled"] != want_drc:
            out.append({"field": "drc_enabled", "want": want_drc,
                        "got": state["drc_enabled"]})
    return out


def reconcile(planned: Iterable[dict[str, Any]],
              on_board: Iterable[dict[str, Any]],
              tolerance_mils: float = 0.02) -> dict[str, Any]:
    """What an apply would have to do, matching by rule name.

    Returns ``create`` (not on the board), ``update`` (there, with the
    differing fields), ``same``, ``skipped`` (there but owned by the
    schematic, so never written) and ``extra`` (on the board and not in
    the profile). Nothing is ever deleted: ``extra`` is reported so the
    drift is visible and the decision stays with the owner.

    A board carrying two rules of one name, which is what an apply
    without this produced, is reported in ``duplicates``; the first is
    compared and the rest listed.
    """
    states: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for rule in on_board:
        state = board_rule_state(rule)
        key = (state["name"] or "").lower()
        if key in states:
            duplicates.append(state["name"])
            continue
        states[key] = state

    create: list[dict[str, Any]] = []
    update: list[dict[str, Any]] = []
    same: list[str] = []
    skipped: list[dict[str, Any]] = []
    matched: set[str] = set()

    for rule in planned:
        name = str(rule.get("name") or "")
        state = states.get(name.lower())
        if state is None:
            create.append(dict(rule))
            continue
        matched.add(name.lower())
        if state["from_schematic"]:
            skipped.append({"name": name, "reason": SKIP_REASON_SCHEMATIC})
            continue
        differences = compare_rule(rule, state, tolerance_mils)
        if differences:
            update.append({"name": name, "rule_type": rule.get("rule_type"),
                           "differences": differences, "planned": dict(rule)})
        else:
            same.append(name)

    extra = [{"name": s["name"], "from_schematic": s["from_schematic"],
              "drc_enabled": s["drc_enabled"], "descriptor": s["descriptor"]}
             for key, s in states.items() if key not in matched]

    return {"create": create, "update": update, "same": same,
            "skipped": skipped, "extra": extra, "duplicates": duplicates,
            "in_step": not create and not update}


#: ``pcb_set_rule_properties`` keyword for a planned rule's value, by
#: rule_type. A kind absent here has no in-place writer, so a rule of it
#: that differs has to be reported rather than updated.
_SET_VALUE_KW: dict[str, str] = {
    "clearance": "gap_mils",
    "width": "min_width_mils",
    "diff_pair": "dp_min_gap_mils",
    "solder_mask": "expansion_mils",
}

_SET_FAVORED_KW: dict[str, str] = {
    "width": "favored_width_mils",
    "diff_pair": "dp_preferred_gap_mils",
}


def update_call(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """``pcb_set_rule_properties`` arguments for one ``update`` entry.

    None when this rule kind has no in-place writer; the caller reports it
    instead of deleting and recreating, which would lose the rule's
    priority and any scope the owner edited by hand.
    """
    rule_type = str(entry.get("rule_type") or "")
    planned = entry.get("planned") or {}
    args: dict[str, Any] = {"name": entry.get("name")}
    fields = {d["field"] for d in entry.get("differences") or []}

    if "value" in fields:
        keyword = _SET_VALUE_KW.get(rule_type)
        if keyword is None:
            return None
        args[keyword] = planned.get("value")
    if "favored_value" in fields:
        keyword = _SET_FAVORED_KW.get(rule_type)
        if keyword is None:
            return None
        args[keyword] = planned.get("favored_value")
    if "drc_enabled" in fields:
        # Not a pcb_set_rule_properties field: the caller sends it through
        # pcb_set_rules_enabled, which is the tool that writes DRCEnabled.
        args["_drc_enabled"] = planned.get("drc_enabled")
    return args if len(args) > 1 else None
