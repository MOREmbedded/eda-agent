# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A SPICE netlist from a DesignPlan, with vendor models only.

Resistors, capacitors and inductors become SPICE primitives from their
value. Everything else (ICs, transistors, diodes) needs the manufacturer's
model file: ``models`` maps a refdes to ``{"file": path, "subckt": name}``
(or ``"model"`` for a D/Q/M primitive model), with ``"pins"`` listing the
plan pins in the model's node order. A part with no model is reported and
the deck is not built; nothing is approximated.

Rails get a DC source each. The voltage comes from ``supplies`` or from the
net name (+12V, 3V3, VCC_1V8); a rail whose voltage cannot be read is
reported.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from eda_agent.core.net_naming import is_ground_net, is_power_net
from eda_agent.design.value_parser import parse_value

PRIMITIVE_PREFIX = {"R": "R", "C": "C", "L": "L"}
#: SPICE element letters for a vendor primitive model, by refdes prefix.
MODEL_ELEMENT = {"D": "D", "Q": "Q", "M": "M"}

_SUBCKT = re.compile(r"^\s*\.subckt\s+(\S+)\s+(.*)$", re.I | re.M)


def rail_voltage(name: str) -> Optional[float]:
    """The voltage a rail's name states: +12V, 12V, 3V3, 5V0, V3P3, VCC_1V8,
    -15V. None when the name states none (VCC, VIN)."""
    n = name.upper()
    sign = -1.0 if (n.startswith("-") or "NEG" in n
                    or re.search(r"_-\d", n)) else 1.0
    m = re.search(r"(\d+)V(\d+)", n)
    if m:
        return sign * float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"V(\d+)P(\d+)", n)
    if m:
        return sign * float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"(\d+(?:\.\d+)?)V(?![A-Z0-9])", n)
    if m:
        return sign * float(m.group(1))
    return None


def subckt_nodes(model_file: str, name: str) -> Optional[list[str]]:
    """The node list of ``.SUBCKT name`` in a vendor file, continuation
    lines included."""
    text = Path(model_file).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _SUBCKT.match(line)
        if m and m.group(1).lower() == name.lower():
            nodes = m.group(2).split()
            j = i + 1
            while j < len(lines) and lines[j].lstrip().startswith("+"):
                nodes += lines[j].lstrip()[1:].split()
                j += 1
            return [n for n in nodes
                    if "=" not in n and n.lower() not in ("params:",)]
    return None


def _node(net: str, ground: bool) -> str:
    return "0" if ground else re.sub(r"[^A-Za-z0-9_]", "_", net)


def deck_from_plan(plan, models: Optional[dict[str, dict]] = None,
                   supplies: Optional[dict[str, float]] = None,
                   pin_names: Optional[dict[str, dict[str, str]]] = None,
                   break_at: Optional[tuple[str, str]] = None,
                   title: str = "") -> dict:
    """Netlist text plus everything that stopped it being complete.

    ``break_at`` (refdes, pin) puts that one pin on its own node, the
    driver side of a loop-gain break; the reply names ``driver_node`` and
    ``load_node`` for :func:`eda_agent.sim.ngspice.loop_gain`."""
    models = models or {}
    supplies = {k.upper(): v for k, v in (supplies or {}).items()}
    pin_net: dict[tuple[str, str], str] = {}
    ground: set[str] = set()
    rails: dict[str, Optional[float]] = {}
    for n in plan.nets:
        for pr in n.pins:
            pin_net[(pr.refdes, str(pr.pin))] = n.name
        if n.is_ground or is_ground_net(n.name):
            ground.add(n.name)
        elif n.is_power or is_power_net(n.name):
            rails[n.name] = supplies.get(n.name.upper(), rail_voltage(n.name))

    driver_node = load_node = ""
    if break_at:
        key = (break_at[0], str(break_at[1]))
        if key not in pin_net:
            return {"ok": False,
                    "reason": f"{key[0]}.{key[1]} is not on any net"}
        load_node = _node(pin_net[key], pin_net[key] in ground)
        driver_node = "lg_drv"

    def node_of(refdes: str, pin: str) -> Optional[str]:
        if break_at and (refdes, pin) == (break_at[0], str(break_at[1])):
            return driver_node
        net = pin_net.get((refdes, pin))
        return _node(net, net in ground) if net else None

    def pins_of(refdes: str) -> list[str]:
        return [p for r, p in pin_net if r == refdes]

    lines = [f"* {title or plan.summary or 'design plan'}"]
    includes: list[str] = []
    missing, needs_pin_map, bad_values, problems = [], [], [], []
    names = {r: {str(k): str(v).upper() for k, v in m.items()}
             for r, m in (pin_names or {}).items()}

    for part in plan.parts:
        ref = part.refdes
        prefix = re.match(r"[A-Za-z]+", ref).group(0).upper()
        pins = pins_of(ref)
        if not pins:
            continue
        model = models.get(ref)
        if prefix in PRIMITIVE_PREFIX and len(pins) == 2 and not model:
            try:
                value = parse_value(part.value or "")
            except (ValueError, TypeError):
                bad_values.append({"refdes": ref, "value": part.value})
                continue
            a, b = node_of(ref, pins[0]), node_of(ref, pins[1])
            lines.append(f"{PRIMITIVE_PREFIX[prefix]}{ref} {a} {b} {value:g}")
            continue
        if not model:
            missing.append({"refdes": ref, "mpn": part.mpn or "",
                            "manufacturer": part.manufacturer or "",
                            "lib_ref": part.lib_ref})
            continue
        f = model.get("file", "")
        if not f or not Path(f).exists():
            problems.append(f"{ref}: model file {f!r} not found")
            continue
        if f not in includes:
            includes.append(f)
        order = [str(p) for p in model.get("pins") or []]
        if not order and model.get("subckt"):
            nodes = subckt_nodes(f, model["subckt"])
            if nodes is None:
                problems.append(f"{ref}: no .SUBCKT {model['subckt']} in {f}")
                continue
            by_name = {}
            for p in pins:
                by_name[p.upper()] = p
                if names.get(ref, {}).get(p):
                    by_name[names[ref][p]] = p
            order = [by_name.get(n.upper(), "") for n in nodes]
            if not all(order):
                needs_pin_map.append({"refdes": ref, "subckt_nodes": nodes,
                                      "plan_pins": pins})
                continue
        if not order:
            needs_pin_map.append({"refdes": ref, "plan_pins": pins})
            continue
        nodes_out = []
        for p in order:
            nd = node_of(ref, p)
            nodes_out.append(nd if nd else f"nc_{ref}_{p}")
        if model.get("subckt"):
            lines.append(f"X{ref} {' '.join(nodes_out)} {model['subckt']}")
        elif model.get("model") and prefix in MODEL_ELEMENT:
            lines.append(f"{MODEL_ELEMENT[prefix]}{ref} {' '.join(nodes_out)} "
                         f"{model['model']}")
        else:
            problems.append(f"{ref}: give subckt, or model for a D/Q/M part")

    unknown_supplies = sorted(n for n, v in rails.items() if v is None)
    for net, v in sorted(rails.items()):
        if v is not None:
            lines.append(f"Vsup_{_node(net, False)} {_node(net, False)} 0 DC {v:g}")

    netlist = "\n".join([lines[0], *[f'.include "{Path(i).as_posix()}"'
                                     for i in includes], *lines[1:], ".end"])
    ok = not (missing or needs_pin_map or bad_values or problems
              or unknown_supplies)
    out = {"ok": ok, "netlist": netlist, "rails": rails,
           "missing_models": missing, "needs_pin_map": needs_pin_map,
           "bad_values": bad_values, "unknown_supplies": unknown_supplies,
           "problems": problems}
    if break_at:
        out.update(driver_node=driver_node, load_node=load_node)
    if missing:
        out["model_policy"] = (
            "models come from the manufacturer (or SnapEDA, Ultra "
            "Librarian, Digi-Key); a part without one is reported, never "
            "approximated")
    return out
