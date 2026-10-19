# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Measure the circuit loops on a placed board (offline).

design/loops.py says which parts and pins form each loop; this draws each
loop through its member pads, in path order, on a LayoutBoard and measures
it. Placement-time geometry: pad centre to pad centre, so it judges the
placement before anything is routed. The routed copper path and its return
come in a later pass.

Area loops (hot, output, decoupling, crystal) report the enclosed area and an
inductance estimate; path loops (bootstrap, feedback, gate drive, Kelvin)
report their length. Without a datasheet limit nothing passes or fails:
loops are ranked worst-first within each kind, which is what a reviewer
acts on.

Inductance estimate: the loop as a circle of equal area,
    L = mu0 * R * (ln(8R / a) - 2)                       (Grover 1946)
with a, the equivalent radius of a flat trace, 0.2235 * (w + t)
(Grover, rectangular cross-section). An estimate for ranking and for the
SPICE parasitics, not a field solution.
"""

from __future__ import annotations

import math
from typing import Optional

from eda_agent.design.loops import Loop, find_loops, graph_from_board
from eda_agent.layout.geom import ring_area
from eda_agent.layout.model import LayoutBoard

AREA_KINDS = ("hot_loop", "output_loop", "decoupling", "crystal")
PATH_KINDS = ("bootstrap", "feedback", "gate_drive", "kelvin")

MU0 = 4e-7 * math.pi
MIL = 25.4e-6                      # metres per mil
DEFAULT_TRACE_W_MILS = 10.0
COPPER_T_MILS = 1.4                # 1 oz


def loop_inductance_nh(area_sq_mils: float,
                       trace_w_mils: float = DEFAULT_TRACE_W_MILS) -> float:
    """Circular-loop estimate of a loop's inductance, in nH (see module doc)."""
    if area_sq_mils <= 0:
        return 0.0
    r = math.sqrt(area_sq_mils / math.pi) * MIL
    a = 0.2235 * (trace_w_mils + COPPER_T_MILS) * MIL
    if 8 * r / a <= math.e ** 2:
        return 0.0
    return MU0 * r * (math.log(8 * r / a) - 2) * 1e9


def _pad_xy(board: LayoutBoard) -> dict[tuple[str, str], tuple[float, float]]:
    return {(p.comp, str(p.name)): (p.x, p.y) for p in board.pads if p.comp}


def routed_points(board: LayoutBoard, loop: Loop, pads: dict, planes: set,
                  copper: dict) -> tuple[list, int, int]:
    """The loop's outline following real copper where it is routed.

    Between consecutive stops on one part the path crosses the part body
    (straight). Between stops on one net it follows that net's routed
    copper; on a plane or pour net, or where the net is not routed, it is a
    straight line, the plane return running under the path. Returns the
    points and how many net hops were routed out of how many there were."""
    from eda_agent.layout.copper_path import NetCopper

    pts: list = []
    hops = routed = 0
    members = [m for m in loop.members if m in pads]
    for i, m in enumerate(members):
        a = pads[m]
        if not pts:
            pts.append((a.x, a.y))
        if i + 1 == len(members):
            break
        b = pads[members[i + 1]]
        seg = [(b.x, b.y)]
        if a.comp != b.comp and a.net and a.net == b.net:
            hops += 1
            if a.net not in planes:
                net_copper = copper.get(a.net)
                if net_copper is None:
                    net_copper = copper[a.net] = NetCopper(board, a.net)
                path = net_copper.path(a, b)
                if path:
                    routed += 1
                    seg = path[1:]
            else:
                routed += 1
        for p in seg:
            if pts[-1] != p:
                pts.append(p)
    return pts, routed, hops


def measure_loop(loop: Loop, xy: dict, routed: Optional[tuple] = None) -> dict:
    """Geometry of one loop: the member pads in order, area or length."""
    pts = [xy[m] for m in loop.members if m in xy]
    missing = [f"{r}.{p}" for r, p in loop.members if (r, p) not in xy]
    dedup = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
    length = sum(math.dist(dedup[i], dedup[i + 1]) for i in range(len(dedup) - 1))
    out = loop.as_dict()
    out["missing_pads"] = missing
    if loop.kind in AREA_KINDS:
        perimeter = length + (math.dist(dedup[-1], dedup[0]) if len(dedup) > 2
                              else 0.0)
        area = ring_area(dedup) if len(dedup) >= 3 else 0.0
        out.update(area_sq_mils=round(area, 1), perimeter_mils=round(perimeter, 1),
                   inductance_nh_est=round(loop_inductance_nh(area), 2),
                   metric="area_sq_mils")
    else:
        out.update(length_mils=round(length, 1), metric="length_mils")
    if routed is not None:
        rpts, done, hops = routed
        out["routed_hops"] = f"{done}/{hops}"
        if hops and done == hops:
            if loop.kind in AREA_KINDS and len(rpts) >= 3:
                rarea = ring_area(rpts)
                out.update(routed_area_sq_mils=round(rarea, 1),
                           routed_inductance_nh_est=round(
                               loop_inductance_nh(rarea), 2))
            else:
                out["routed_length_mils"] = round(sum(
                    math.dist(rpts[i], rpts[i + 1])
                    for i in range(len(rpts) - 1)), 1)
    return out


def analyze_loops(board: LayoutBoard,
                  pin_names: Optional[dict[str, dict[str, str]]] = None,
                  kinds=None) -> list[dict]:
    """Find the loops on ``board`` and measure each, worst first per kind."""
    from eda_agent.layout.copper_path import plane_nets

    graph = graph_from_board(board, pin_names)
    xy = _pad_xy(board)
    pads = {(p.comp, str(p.name)): p for p in board.pads if p.comp}
    planes = plane_nets(board)
    copper: dict = {}
    has_copper = bool(board.tracks or board.arcs)
    measured = []
    for loop in find_loops(graph, kinds):
        routed = routed_points(board, loop, pads, planes, copper)             if has_copper else None
        measured.append(measure_loop(loop, xy, routed))
    order = {k: i for i, k in enumerate(
        ["hot_loop", "gate_drive", "bootstrap", "output_loop", "kelvin",
         "crystal", "feedback", "decoupling"])}
    measured.sort(key=lambda m: (order.get(m["kind"], 99),
                                 -m.get(m["metric"], 0.0)))
    return measured


def loop_audit(board: LayoutBoard, limit: int = 50,
               pin_names: Optional[dict[str, dict[str, str]]] = None) -> dict:
    """Layout audit check: every recognised loop, measured and ranked.

    Advisory: ``pass`` stays True because no limit is known without the
    part's datasheet; the ranking says which loop to tighten first."""
    measured = analyze_loops(board, pin_names)
    by_kind: dict[str, int] = {}
    for m in measured:
        by_kind[m["kind"]] = by_kind.get(m["kind"], 0) + 1
    worst = {}
    for m in measured:
        worst.setdefault(m["kind"], m)
    return {
        "pass": True,
        "advisory": True,
        "counts": {"loops": len(measured), **{f"{k}s": v
                                              for k, v in by_kind.items()}},
        "worst_by_kind": {k: {"parts": v["parts"], v["metric"]: v[v["metric"]]}
                          for k, v in worst.items()},
        "loops": measured[:limit],
        "note": ("Placement-time measure, pad centre to pad centre. Loops are "
                 "found from board structure alone unless pin_names are given, "
                 "so pin-function loops (bootstrap, gate drive, feedback) need "
                 "them."),
    }


__all__ = ["analyze_loops", "loop_audit", "measure_loop", "loop_inductance_nh"]
