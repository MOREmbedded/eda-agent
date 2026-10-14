# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Find the circuit loops whose physical size matters (offline).

A loop here is a current path through parts and nets that a layout has to
keep small or keep clean: a switching converter's hot loop, its output and
bootstrap loops, a MOSFET gate drive, a decoupling cap and the pins it
serves, a crystal and its load caps, a regulator or op-amp feedback path,
and a current-sense Kelvin connection. Trace geometry comes later (the
analyzers measure a loop on a placed or routed board); this module only
says which parts and pins form each loop, in order, and why.

Every loop records what found it, most trusted first:

* ``role``: the plan tagged the parts or nets (Part.role cin_hf/cboot/...,
  Net.role switch/feedback);
* ``pin_name``: pin functions such as SW, BOOT, FB, VIN, IN-, OUT, G/S/D,
  SENSE+;
* ``structure``: the netlist shape alone (a two-pin cap across a rail and
  ground, an inductor on a node shared with an IC and a diode).

Built from a DesignPlan, or from a LayoutBoard where pads carry numbers and
the caller may pass a pin-function map read from the libraries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from eda_agent.core.net_naming import is_ground_net, is_power_net
from eda_agent.design.motifs import _kind_from_refdes
from eda_agent.design.value_parser import try_parse_value

PinKey = tuple[str, str]          # (refdes, pin number or name)


@dataclass
class PartInfo:
    refdes: str
    kind: str                     # R, C, L, D, Q, U, Y ... from the refdes
    value: str = ""
    role: str = ""
    description: str = ""


@dataclass
class NetInfo:
    name: str
    pins: list[PinKey] = field(default_factory=list)
    role: str = ""
    is_power: bool = False
    is_ground: bool = False


@dataclass
class CircuitGraph:
    parts: dict[str, PartInfo]
    nets: dict[str, NetInfo]
    pin_net: dict[PinKey, str]
    #: Pin function names, upper case, where known: (refdes, pin) -> "SW".
    pin_function: dict[PinKey, str] = field(default_factory=dict)
    #: Pad positions in mils, when built from a placed board.
    pin_xy: dict[PinKey, tuple[float, float]] = field(default_factory=dict)

    # --- queries -------------------------------------------------------
    def pins_of(self, refdes: str) -> list[PinKey]:
        return [k for k in self.pin_net if k[0] == refdes]

    def nets_of(self, refdes: str) -> list[str]:
        return [self.pin_net[k] for k in self.pins_of(refdes)]

    def function(self, key: PinKey) -> str:
        return self.pin_function.get(key, "")

    def is_ground(self, net: str) -> bool:
        n = self.nets.get(net)
        return bool(n and (n.is_ground or is_ground_net(net)))

    def is_rail(self, net: str) -> bool:
        n = self.nets.get(net)
        return bool(n and not self.is_ground(net)
                    and (n.is_power or is_power_net(net)))

    def parts_on(self, net: str) -> list[str]:
        n = self.nets.get(net)
        return sorted({p for p, _ in n.pins}) if n else []

    def two_pin(self, refdes: str) -> Optional[tuple[PinKey, PinKey]]:
        pins = self.pins_of(refdes)
        return (pins[0], pins[1]) if len(pins) == 2 else None

    def is_ic(self, refdes: str) -> bool:
        part = self.parts[refdes]
        return part.kind in ("U", "IC") or len(self.pins_of(refdes)) >= 5

    def pin_on(self, refdes: str, net: str) -> Optional[PinKey]:
        for k in self.pins_of(refdes):
            if self.pin_net[k] == net:
                return k
        return None

    def through(self, refdes: Optional[str], entry_net: Optional[str]
                ) -> list[PinKey]:
        """A two-pin part's pins in the order a path crosses it: the pin on
        ``entry_net`` first. Loop geometry is drawn through members in
        order, so a part listed backwards folds the polygon over itself."""
        if not refdes:
            return []
        pins = self.pins_of(refdes)
        if len(pins) == 2 and entry_net and self.pin_net[pins[1]] == entry_net:
            return [pins[1], pins[0]]
        return pins


@dataclass
class Loop:
    """One loop: ordered (part, pin) stops, the nets it crosses, and why."""

    kind: str
    members: list[PinKey]
    nets: list[str]
    source: str
    judged_on: str
    note: str = ""
    parts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"kind": self.kind,
                "parts": self.parts or sorted({r for r, _ in self.members}),
                "members": [f"{r}.{p}" for r, p in self.members],
                "nets": self.nets, "source": self.source,
                "judged_on": self.judged_on, "note": self.note}


# ---------------------------------------------------------------------------
# Building the graph
# ---------------------------------------------------------------------------

def _is_number(pin: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]?\d+", pin.strip()))


def graph_from_plan(plan, pin_names: Optional[dict[str, dict[str, str]]] = None
                    ) -> CircuitGraph:
    """CircuitGraph from a DesignPlan. A pin given by NAME in the plan is its
    own function; ``pin_names`` ({refdes: {pin_number: name}}) supplies the
    rest, e.g. from lib_get_pin_list."""
    parts = {p.refdes: PartInfo(p.refdes, _kind_from_refdes(p.refdes),
                                p.value or "", p.role or "",
                                p.lib_ref or "")
             for p in plan.parts}
    nets: dict[str, NetInfo] = {}
    pin_net: dict[PinKey, str] = {}
    pin_function: dict[PinKey, str] = {}
    for n in plan.nets:
        info = NetInfo(n.name, [], n.role or "", n.is_power, n.is_ground)
        for pr in n.pins:
            key = (pr.refdes, str(pr.pin))
            info.pins.append(key)
            pin_net[key] = n.name
            if not _is_number(str(pr.pin)):
                pin_function[key] = str(pr.pin).upper()
        nets[n.name] = info
    for refdes, mapping in (pin_names or {}).items():
        for number, name in mapping.items():
            key = (refdes, str(number))
            if key in pin_net and name:
                pin_function[key] = str(name).upper()
    return CircuitGraph(parts, nets, pin_net, pin_function)


def graph_from_board(board, pin_names: Optional[dict[str, dict[str, str]]] = None
                     ) -> CircuitGraph:
    """CircuitGraph from a LayoutBoard (pads carry numbers, not functions)."""
    parts = {c.ref: PartInfo(c.ref, _kind_from_refdes(c.ref), c.comment or "",
                             "", c.footprint or "")
             for c in board.components}
    nets: dict[str, NetInfo] = {}
    pin_net: dict[PinKey, str] = {}
    for pad in board.pads:
        if not pad.comp or not pad.net:
            continue
        key = (pad.comp, str(pad.name))
        pin_net[key] = pad.net
        nets.setdefault(pad.net, NetInfo(pad.net)).pins.append(key)
    pin_function: dict[PinKey, str] = {}
    for refdes, mapping in (pin_names or {}).items():
        for number, name in mapping.items():
            key = (refdes, str(number))
            if key in pin_net and name:
                pin_function[key] = str(name).upper()
    pin_xy = {(p.comp, str(p.name)): (p.x, p.y) for p in board.pads if p.comp}
    return CircuitGraph(parts, nets, pin_net, pin_function, pin_xy)


# ---------------------------------------------------------------------------
# Pin-function vocabulary
# ---------------------------------------------------------------------------

_F = {
    "sw": re.compile(r"^(SW\d*|LX\d*|PH|PHASE|SWN?)$"),
    "boot": re.compile(r"^(BOOT|BST|CB|BOOTSTRAP|CBOOT)$"),
    "vin": re.compile(r"^(VIN\d*|PVIN\d*|IN|VCC|VDD|VS)$"),
    "pgnd": re.compile(r"^(PGND\d*|GND\d*|AGND|SGND|VSS|EP|PAD)$"),
    "fb": re.compile(r"^(FB|VFB|FBK|ADJ|VSENSE|FB\+?)$"),
    "gate": re.compile(r"^(G|GATE)$"),
    "source": re.compile(r"^(S|SOURCE)$"),
    "drain": re.compile(r"^(D|DRAIN)$"),
    "sense_p": re.compile(r"^(SENSE\+|SNS\+|CS\+|ISNS\+|IN\+|ISENP|CSP)$"),
    "sense_n": re.compile(r"^(SENSE-|SNS-|CS-|ISNS-|IN-|ISENN|CSN)$"),
    "osc": re.compile(r"^(X(TAL)?(IN|OUT|1|2|I|O)|OSC(IN|OUT|1|2)?|XI|XO|"
                      r"OSC_(IN|OUT)|XTAL\d?)$"),
}


def _fn(graph: CircuitGraph, key: PinKey, what: str) -> bool:
    return bool(_F[what].match(graph.function(key).replace("_", "")
                               if what != "osc" else graph.function(key)))


def _pin_with(graph: CircuitGraph, refdes: str, what: str) -> Optional[PinKey]:
    for k in graph.pins_of(refdes):
        if _fn(graph, k, what):
            return k
    return None


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def _decoupling(graph: CircuitGraph) -> list[Loop]:
    """A two-pin cap across a rail and ground, with each IC it serves."""
    loops: list[Loop] = []
    for ref, part in graph.parts.items():
        if part.kind != "C":
            continue
        pins = graph.two_pin(ref)
        if not pins:
            continue
        nets = [graph.pin_net[k] for k in pins]
        gnd = [n for n in nets if graph.is_ground(n)]
        rail = [n for n in nets if graph.is_rail(n)]
        if len(gnd) != 1 or len(rail) != 1:
            continue
        gnd_net, rail_net = gnd[0], rail[0]
        cap_rail = pins[0] if graph.pin_net[pins[0]] == rail_net else pins[1]
        cap_gnd = pins[1] if cap_rail is pins[0] else pins[0]
        served = []
        for ic in graph.parts_on(rail_net):
            if ic == ref or not graph.is_ic(ic):
                continue
            ic_rail = graph.pin_on(ic, rail_net)
            ic_gnd = graph.pin_on(ic, gnd_net)
            if ic_rail and ic_gnd:
                served.append((ic, ic_rail, ic_gnd))
        # On a placed board a cap serves the IC whose rail pin is nearest;
        # pairing it with every IC on a shared rail counted one cap many
        # times (517 loops on a 282-part board). A plan has no positions,
        # so there every IC on the rail is a candidate.
        if graph.pin_xy and cap_rail in graph.pin_xy and served:
            cx, cy = graph.pin_xy[cap_rail]
            served = [min(served, key=lambda s: (
                (graph.pin_xy.get(s[1], (1e12, 1e12))[0] - cx) ** 2
                + (graph.pin_xy.get(s[1], (1e12, 1e12))[1] - cy) ** 2))]
        for ic, ic_rail, ic_gnd in served:
            source = "role" if part.role in ("cin_hf", "decap", "cdec") \
                else "structure"
            loops.append(Loop(
                "decoupling", [cap_rail, ic_rail, ic_gnd, cap_gnd],
                [rail_net, gnd_net], source, "area, distance to the pins",
                note=f"{ref} decouples {ic} on {rail_net}",
                parts=sorted({ref, ic})))
    return loops


def _crystal(graph: CircuitGraph) -> list[Loop]:
    """Crystal, both load caps to ground, and the IC's two oscillator pins."""
    loops = []
    for ref, part in graph.parts.items():
        if part.kind != "Y":
            continue
        pins = graph.two_pin(ref)
        if not pins:
            continue
        a, b = (graph.pin_net[k] for k in pins)
        caps = []
        for net in (a, b):
            for c in graph.parts_on(net):
                cp = graph.two_pin(c) if graph.parts[c].kind == "C" else None
                if cp and any(graph.is_ground(graph.pin_net[k]) for k in cp):
                    caps.append(c)
                    break
        ics = [u for u in set(graph.parts_on(a)) & set(graph.parts_on(b))
               if u != ref and graph.is_ic(u)]
        if not ics:
            continue
        ic = ics[0]
        members = [graph.pin_on(ic, a), pins[0], pins[1], graph.pin_on(ic, b)]
        named = any(_fn(graph, m, "osc") for m in members if m)
        loops.append(Loop(
            "crystal", [m for m in members if m], [a, b],
            "pin_name" if named else "structure",
            "area, symmetry, distance to the oscillator pins",
            note=f"{ref} with load caps {', '.join(caps) or 'none found'} "
                 f"on {ic}",
            parts=sorted({ref, ic, *caps})))
    return loops


def _shunt_ohms(graph: CircuitGraph, ref: str) -> Optional[float]:
    v = try_parse_value(graph.parts[ref].value or "")
    return v if v is not None and 0 < v <= 1.0 else None


def _switching(graph: CircuitGraph) -> list[Loop]:
    """Buck and boost converters around each inductor on a switch node."""
    loops: list[Loop] = []
    for lref, part in graph.parts.items():
        if part.kind != "L":
            continue
        lp = graph.two_pin(lref)
        if not lp:
            continue
        n1, n2 = (graph.pin_net[k] for k in lp)
        sw, other, source = None, None, "structure"
        for cand, rest in ((n1, n2), (n2, n1)):
            net = graph.nets.get(cand)
            ic_sw = [k for k in (net.pins if net else [])
                     if graph.is_ic(k[0]) and _fn(graph, k, "sw")]
            if (net and net.role == "switch") or ic_sw:
                sw, other = cand, rest
                source = "role" if net and net.role == "switch" else "pin_name"
                break
        if sw is None:
            # Structure: the inductor node shared with an IC and a diode/FET.
            for cand, rest in ((n1, n2), (n2, n1)):
                on = graph.parts_on(cand)
                if any(graph.is_ic(p) for p in on if p != lref) and any(
                        graph.parts[p].kind in ("D", "Q") for p in on):
                    sw, other = cand, rest
                    break
        if sw is None:
            continue
        ic = next((p for p in graph.parts_on(sw)
                   if p != lref and graph.is_ic(p)), None)
        diode = next((p for p in graph.parts_on(sw)
                      if graph.parts[p].kind == "D"), None)
        fets = [p for p in graph.parts_on(sw) if graph.parts[p].kind == "Q"]

        ic_vin_net = None
        if ic:
            k = _pin_with(graph, ic, "vin")
            ic_vin_net = graph.pin_net[k] if k else None
        boost = bool(ic_vin_net and ic_vin_net == other)
        out_net = None
        if boost and diode:
            dnets = graph.nets_of(diode)
            out_net = next((n for n in dnets if n != sw), None)
        elif not boost:
            out_net = other

        def bypass(net):
            if not net:
                return None
            for c in graph.parts_on(net):
                cp = graph.two_pin(c) if graph.parts[c].kind == "C" else None
                if cp and any(graph.is_ground(graph.pin_net[k]) for k in cp):
                    return c
            return None

        gnd = next((n for n in graph.nets if graph.is_ground(n)), "")
        if boost:
            cout = bypass(out_net)
            members = [k for k in (
                graph.pin_on(ic, sw) if ic else None,
                *graph.through(diode, sw),
                *graph.through(cout, out_net),
                _pin_with(graph, ic, "pgnd") if ic else None) if k]
            loops.append(Loop(
                "hot_loop", members, [sw, out_net or "", gnd], source,
                "area, inductance",
                note=f"boost around {lref}: switch {ic or '/'.join(fets)}, "
                     f"diode {diode or 'synchronous'}, output cap "
                     f"{cout or 'not found'}",
                parts=sorted({p for p in (ic, diode, cout, *fets) if p})))
        else:
            cin_ref = next((p for p, i in graph.parts.items()
                            if i.role == "cin_hf"), None) or bypass(ic_vin_net)
            low = diode or (fets[1] if len(fets) > 1 else None)
            cin_pins = graph.through(cin_ref, ic_vin_net)
            members = [k for k in (
                cin_pins[0] if cin_pins else None,
                _pin_with(graph, ic, "vin") if ic else None,
                graph.pin_on(ic, sw) if ic else None,
                *graph.through(low, sw),
                _pin_with(graph, ic, "pgnd") if ic and not low else None,
                cin_pins[1] if len(cin_pins) > 1 else None)
                if k]
            loops.append(Loop(
                "hot_loop", members, [ic_vin_net or "", sw, gnd], source,
                "area, inductance",
                note=f"buck around {lref}: input cap {cin_ref or 'not found'},"
                     f" low side {low or 'inside ' + str(ic)}",
                parts=sorted({p for p in (cin_ref, ic, low, *fets) if p})))
            cout = bypass(out_net)
            if cout:
                loops.append(Loop(
                    "output_loop",
                    [*graph.through(lref, sw), *graph.through(cout, out_net)],
                    [sw, out_net or "", gnd], source, "area",
                    note=f"{lref} into {cout}", parts=[lref, cout]))
        # Bootstrap: a cap between a BOOT pin's net and the switch node.
        if ic:
            bk = _pin_with(graph, ic, "boot")
            if bk:
                bnet = graph.pin_net[bk]
                for c in graph.parts_on(bnet):
                    cp = graph.two_pin(c)
                    if graph.parts[c].kind == "C" and cp and sw in (
                            graph.pin_net[cp[0]], graph.pin_net[cp[1]]):
                        loops.append(Loop(
                            "bootstrap",
                            [bk, *graph.through(c, bnet), graph.pin_on(ic, sw)],
                            [bnet, sw], "pin_name", "length",
                            note=f"{c} between {ic} BOOT and SW",
                            parts=[c, ic]))
    return loops


def _gate_drive(graph: CircuitGraph) -> list[Loop]:
    """A MOSFET gate driven from an IC pin, directly or through a resistor."""
    loops = []
    for qref, part in graph.parts.items():
        if part.kind != "Q":
            continue
        g = _pin_with(graph, qref, "gate")
        s = _pin_with(graph, qref, "source")
        if not g or not s:
            continue
        gnet = graph.pin_net[g]
        driver, rg = None, None
        for p in graph.parts_on(gnet):
            if p != qref and graph.is_ic(p):
                driver = graph.pin_on(p, gnet)
            elif graph.parts[p].kind == "R":
                rp = graph.two_pin(p)
                if rp:
                    far = graph.pin_net[rp[1] if graph.pin_net[rp[0]] == gnet
                                        else rp[0]]
                    for u in graph.parts_on(far):
                        if graph.is_ic(u):
                            rg, driver = p, graph.pin_on(u, far)
        if not driver:
            continue
        snet = graph.pin_net[s]
        ret = graph.pin_on(driver[0], snet)
        members = [driver, *(graph.pins_of(rg) if rg else []), g, s]
        if ret:
            members.append(ret)
        loops.append(Loop(
            "gate_drive", members, [gnet, snet], "pin_name", "area, length",
            note=f"{driver[0]} drives {qref}"
                 + (f" through {rg}" if rg else "")
                 + ("" if ret else "; the driver's return pin is not on "
                    "the source net"),
            parts=sorted({qref, driver[0], *( [rg] if rg else [])})))
    return loops


def _feedback(graph: CircuitGraph) -> list[Loop]:
    """Regulator FB dividers and op-amp feedback networks."""
    from eda_agent.design.symbol_gen import classify_amplifier_pins

    loops: list[Loop] = []
    for ic, part in graph.parts.items():
        if not graph.is_ic(ic):
            continue
        fb = _pin_with(graph, ic, "fb")
        if fb:
            fbnet = graph.pin_net[fb]
            top = [r for r in graph.parts_on(fbnet)
                   if graph.parts[r].kind == "R" and graph.two_pin(r)]
            net = graph.nets.get(fbnet)
            loops.append(Loop(
                "feedback", [fb, *[k for r in top for k in graph.through(r, fbnet)]],
                [fbnet], "role" if net and net.role == "feedback"
                else "pin_name", "length, distance from switching nodes",
                note=f"{ic} FB with divider {', '.join(top) or 'none found'}",
                parts=sorted({ic, *top})))
            continue
        pins = [{"designator": k[1], "name": graph.function(k)}
                for k in graph.pins_of(ic) if graph.function(k)]
        groups = classify_amplifier_pins(pins) if pins else None
        if not groups:
            continue
        for unit in groups["units"]:
            out = (ic, unit["out"]["designator"])
            neg = (ic, unit["in-"]["designator"])
            onet, nnet = graph.pin_net.get(out), graph.pin_net.get(neg)
            if not onet or not nnet:
                continue
            if onet == nnet:
                loops.append(Loop("feedback", [out, neg], [onet], "pin_name",
                                  "length", note=f"{ic} follower",
                                  parts=[ic]))
                continue
            net_parts = []
            for p in graph.parts_on(nnet):
                pp = graph.two_pin(p)
                if pp and graph.parts[p].kind in ("R", "C") and onet in (
                        graph.pin_net[pp[0]], graph.pin_net[pp[1]]):
                    net_parts.append(p)
            if net_parts:
                loops.append(Loop(
                    "feedback",
                    [out, *[k for p in net_parts for k in graph.through(p, onet)],
                     neg], [onet, nnet], "pin_name",
                    "length, distance from noisy nodes",
                    note=f"{ic} OUT to IN- through {', '.join(net_parts)}",
                    parts=sorted({ic, *net_parts})))
    return loops


def _kelvin(graph: CircuitGraph) -> list[Loop]:
    """A low-value shunt whose two nets reach an IC's sense pins."""
    loops = []
    for ref, part in graph.parts.items():
        if part.kind != "R" or _shunt_ohms(graph, ref) is None:
            continue
        sp = graph.two_pin(ref)
        if not sp:
            continue
        a, b = (graph.pin_net[k] for k in sp)
        for ic in set(graph.parts_on(a)) & set(graph.parts_on(b)):
            if not graph.is_ic(ic):
                continue
            ka, kb = graph.pin_on(ic, a), graph.pin_on(ic, b)
            named = (_fn(graph, ka, "sense_p") or _fn(graph, ka, "sense_n")
                     or _fn(graph, kb, "sense_p") or _fn(graph, kb, "sense_n"))
            loops.append(Loop(
                "kelvin", [ka, sp[0], sp[1], kb], [a, b],
                "pin_name" if named else "structure",
                "sense taps at the shunt pads, routed as a pair",
                note=f"{ref} ({part.value}) sensed by {ic}",
                parts=[ref, ic]))
    return loops


DETECTORS = {
    "hot_loop": _switching,          # also output_loop and bootstrap
    "gate_drive": _gate_drive,
    "decoupling": _decoupling,
    "crystal": _crystal,
    "feedback": _feedback,
    "kelvin": _kelvin,
}


def find_loops(graph: CircuitGraph,
               kinds: Optional[Iterable[str]] = None) -> list[Loop]:
    """Every loop the detectors recognise, hot loops first."""
    wanted = set(kinds) if kinds else None
    found: list[Loop] = []
    for name, detect in DETECTORS.items():
        for loop in detect(graph):
            if wanted is None or loop.kind in wanted:
                found.append(loop)
    order = ["hot_loop", "gate_drive", "bootstrap", "output_loop", "kelvin",
             "crystal", "feedback", "decoupling"]
    found.sort(key=lambda l: order.index(l.kind) if l.kind in order else 99)
    return found


__all__ = ["CircuitGraph", "Loop", "PartInfo", "NetInfo", "graph_from_plan",
           "graph_from_board", "find_loops", "DETECTORS"]
