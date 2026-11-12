# SPDX-License-Identifier: Apache-2.0
"""Which schematic pin drives which SPICE model node.

SPICE reads a subcircuit's terminals by POSITION: ``XU1 a b c LT3080``
connects net ``a`` to the first node of ``.SUBCKT LT3080``, whatever the
symbol calls that pin. Taking the symbol's pin-number order instead is
the classic way to build a deck that runs cleanly and answers wrongly, so
this module matches by NAME and refuses to guess:

1. a saved override for the model (and, if given, the symbol) wins;
2. otherwise every model node must match symbol pins by name, exactly
   or through a small, explicit alias table (``VIN``/``IN``,
   ``GND``/``VSS``, ``~SHDN``/``SHDN``, ...);
3. only when no node name matched anything and the counts are equal is
   pin-number order offered, and then it is marked ``needs_review``.

Several symbol pins with one name (two GND pins, four drain pins on a
power FET) all map to that node; if they sit on different nets the deck
builder reports it.

Pure: no Altium, no files.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .model_library import is_unused_node

#: Override entries that leave a model port unconnected.
_NO_PIN = frozenset({"", "-", "NC"})

#: Groups of names that mean the same terminal on an IC. Deliberately
#: short: a wrong alias is worse than an unmatched pin, because an
#: unmatched pin is reported and a wrong alias is not.
_ALIAS_GROUPS = (
    {"GND", "VSS", "AGND", "DGND", "GROUND", "0", "COM", "VNEG0"},
    {"IN", "VIN", "VI", "INPUT"},
    {"OUT", "VOUT", "VO", "OUTPUT"},
    {"EN", "ENABLE", "SHDN", "SD", "ON", "RUN", "ENUVLO", "ONOFF"},
    {"FB", "VFB", "ADJ", "FEEDBACK"},
    {"SW", "LX", "PH", "SWITCH"},
    {"BST", "BOOST", "BOOT", "CB", "CBOOT"},
    {"PG", "PGOOD", "PWRGD", "PGD", "POK"},
    {"SS", "TR", "SSTR", "TRSS"},
    {"RT", "FREQ", "RTSYNC", "RT/SYNC"},
    {"VCC", "VDD", "V+", "VS+", "VPOS", "VP", "VCCA"},
    {"VEE", "V-", "VS-", "VNEG", "VN"},
    {"IN+", "INP", "+IN", "NONINV", "NI"},
    {"IN-", "INN", "-IN", "INV"},
    {"SET", "ISET"},
)

#: Terminal order SPICE gives the semiconductor primitives, with the names
#: their symbols use. D's cathode is often lettered C, so the D table
#: knows C as cathode; Q's table, where C is the collector, is separate.
STANDARD_ORDERS: dict[str, list[tuple[str, set[str]]]] = {
    "D": [("A", {"A", "ANODE", "+", "P"}),
          ("K", {"K", "C", "CATHODE", "KA", "-", "N"})],
    "Q": [("C", {"C", "COLLECTOR"}), ("B", {"B", "BASE"}),
          ("E", {"E", "EMITTER"})],
    "M": [("D", {"D", "DRAIN"}), ("G", {"G", "GATE"}),
          ("S", {"S", "SOURCE"})],
    "J": [("D", {"D", "DRAIN"}), ("G", {"G", "GATE"}),
          ("S", {"S", "SOURCE"})],
}


def canonical(name: str) -> str:
    """Upper case, overline markers and punctuation dropped, sign kept.

    Altium writes an overline as backslashes (``S\\H\\D\\N\\``), LTspice
    as a leading ``~`` or trailing ``#``/``_N``/``B``-less forms; both
    mean the same pin for matching purposes. A '+' or '-' is kept because
    ``IN+`` and ``IN-`` are different pins.
    """
    s = str(name or "").upper().replace("\\", "").replace("~", "").strip()
    s = s.lstrip("/!")
    s = re.sub(r"(#|_N|_B|\(N\))$", "", s)
    sign = ""
    if s.endswith("+") or s.startswith("+"):
        sign = "+"
    elif s.endswith("-") or s.startswith("-"):
        sign = "-"
    core = re.sub(r"[^A-Z0-9]", "", s)
    return (core + sign) if sign else (core or s)


def _alias_table() -> dict[str, str]:
    table: dict[str, str] = {}
    for group in _ALIAS_GROUPS:
        label = "ALIAS:" + sorted(group)[0]
        for g in group:
            table[canonical(g)] = label
    return table


def _alias_key(name: str) -> str:
    c = canonical(name)
    return _ALIASES.get(c, c)


_ALIASES: dict[str, str] = {}


def _pin_sort_key(pin: dict[str, Any]):
    raw = str(pin.get("pin_number", "")).strip()
    try:
        return (0, int(raw), "")
    except ValueError:
        return (1, 0, raw)


def propose(model_nodes: list[str], symbol_pins: list[dict[str, Any]],
            override: Optional[list[str]] = None) -> dict[str, Any]:
    """Map each model node (in order) to the symbol pin numbers it joins.

    ``model_nodes`` are the subcircuit's node names in header order (or an
    LTspice symbol's pin names in SpiceOrder). ``symbol_pins`` carry
    ``pin_number`` and ``pin_name``. ``override`` is a saved list of pin
    numbers, one entry per node; ``"3+9"`` joins two pins to one node.

    Returns ``ok``, ``method`` (override | names | pin_number_order),
    ``nodes`` [{index, node, pins, how}], ``unmatched_nodes``,
    ``unused_pins`` and ``needs_review``.
    """
    pins = sorted(symbol_pins, key=_pin_sort_key)
    numbers = {str(p.get("pin_number", "")).strip() for p in pins}

    if override:
        if len(override) != len(model_nodes):
            return {"ok": False, "method": "override",
                    "reason": f"saved pin map has {len(override)} entries, the model "
                              f"has {len(model_nodes)} nodes",
                    "nodes": [], "unmatched_nodes": list(model_nodes),
                    "unused_pins": [], "needs_review": True}
        out, used, missing = [], set(), []
        for i, (node, entry) in enumerate(zip(model_nodes, override)):
            if str(entry).strip().upper() in _NO_PIN:
                out.append({"index": i + 1, "node": node, "pins": [], "how": "unused"})
                continue
            group = [x.strip() for x in str(entry).split("+") if x.strip()]
            bad = [x for x in group if x not in numbers]
            missing += bad
            used.update(group)
            out.append({"index": i + 1, "node": node, "pins": group, "how": "override"})
        unused = [str(p.get("pin_number")) for p in pins
                  if str(p.get("pin_number", "")).strip() not in used]
        if missing:
            return {"ok": False, "method": "override",
                    "reason": f"saved pin map names pins the symbol lacks: {missing}",
                    "nodes": out, "unmatched_nodes": [], "unused_pins": unused,
                    "needs_review": True}
        return {"ok": True, "method": "override", "nodes": out,
                "unmatched_nodes": [], "unused_pins": unused, "needs_review": False}

    by_exact: dict[str, list[str]] = {}
    by_alias: dict[str, list[str]] = {}
    for p in pins:
        num = str(p.get("pin_number", "")).strip()
        name = str(p.get("pin_name", "") or "")
        if not name:
            continue
        by_exact.setdefault(canonical(name), []).append(num)
        by_alias.setdefault(_alias_key(name), []).append(num)

    out, unmatched, used = [], [], set()
    for i, node in enumerate(model_nodes):
        if is_unused_node(node):
            # A port the vendor's symbol leaves unconnected (SpiceOrder gap).
            out.append({"index": i + 1, "node": node, "pins": [], "how": "unused"})
            continue
        hit = by_exact.get(canonical(node))
        how = "exact"
        if not hit:
            hit = by_alias.get(_alias_key(node))
            how = "alias"
        if hit:
            out.append({"index": i + 1, "node": node, "pins": list(hit), "how": how})
            used.update(hit)
        else:
            out.append({"index": i + 1, "node": node, "pins": [], "how": "unmatched"})
            unmatched.append(node)

    # Two different nodes claiming the same pin means an alias or a
    # repeated name collapsed distinct terminals: not trustworthy.
    claimed: dict[str, list[str]] = {}
    for n in out:
        for p in n["pins"]:
            claimed.setdefault(p, []).append(n["node"])
    clashes = {p: ns for p, ns in claimed.items() if len(ns) > 1}

    unused = [str(p.get("pin_number")) for p in pins
              if str(p.get("pin_number", "")).strip() not in used]

    if not unmatched and not clashes:
        return {"ok": True, "method": "names", "nodes": out,
                "unmatched_nodes": [], "unused_pins": unused,
                "needs_review": any(n["how"] == "alias" for n in out)}

    real = [n for n in model_nodes if not is_unused_node(n)]
    matched_any = len(unmatched) < len(real)
    if not matched_any and len(real) == len(pins):
        next_pin = iter(pins)
        order = []
        for i, node in enumerate(model_nodes):
            if is_unused_node(node):
                order.append({"index": i + 1, "node": node, "pins": [], "how": "unused"})
            else:
                p = next(next_pin)
                order.append({"index": i + 1, "node": node,
                              "pins": [str(p.get("pin_number", "")).strip()],
                              "how": "pin_number_order"})
        return {"ok": True, "method": "pin_number_order", "nodes": order,
                "unmatched_nodes": [], "unused_pins": [], "needs_review": True,
                "warning": "no model node name matched a symbol pin name; pins were "
                           "taken in pin-number order. Check against the model's "
                           "header comment and save an override if it is wrong."}

    reason = []
    if unmatched:
        reason.append(f"model node(s) with no matching symbol pin: {unmatched}")
    if clashes:
        reason.append(f"symbol pin(s) claimed by more than one node: {clashes}")
    return {"ok": False, "method": "names", "nodes": out,
            "unmatched_nodes": unmatched, "unused_pins": unused,
            "clashes": clashes, "needs_review": True,
            "reason": "; ".join(reason) + ". Save an override with the pin numbers "
                      "in model node order."}


def standard_order(prefix: str, symbol_pins: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Node order for D/Q/M/J from pin names, or None when the names do
    not say (the caller then keeps the pin-number assumption and flags it)."""
    table = STANDARD_ORDERS.get(prefix.upper())
    if not table:
        return None
    out = []
    used: set[str] = set()
    for terminal, names in table:
        wanted = {canonical(n) for n in names}
        hit = [str(p.get("pin_number", "")).strip() for p in symbol_pins
               if canonical(str(p.get("pin_name", ""))) in wanted
               and str(p.get("pin_number", "")).strip() not in used]
        if not hit:
            return None
        used.update(hit)
        out.append({"index": len(out) + 1, "node": terminal, "pins": hit, "how": "standard"})
    return {"ok": True, "method": "standard", "nodes": out, "unmatched_nodes": [],
            "unused_pins": [str(p.get("pin_number")) for p in symbol_pins
                            if str(p.get("pin_number", "")).strip() not in used],
            "needs_review": False}


_ALIASES.update(_alias_table())


def override_key(model: str, lib_ref: str = "") -> str:
    return f"{model.strip().upper()}|{lib_ref.strip().upper()}"


def lookup_override(store: dict[str, Any], model: str, lib_ref: str = "") -> Optional[list[str]]:
    """A saved pin map: for this model on this symbol, else for the model."""
    for key in (override_key(model, lib_ref), override_key(model)):
        hit = store.get(key)
        if isinstance(hit, dict) and isinstance(hit.get("pins"), list):
            return [str(x) for x in hit["pins"]]
    return None
