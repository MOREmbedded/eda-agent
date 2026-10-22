# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Parts that would connect shorter turned by 90, 180 or 270 degrees.

A part placed with the right footprint at the right spot can still face
the wrong way: a resistor whose pad 1 points away from the pin it feeds
costs a crossing and a detour that no amount of routing recovers. This
scores every unlocked part with two or more pads at each quarter turn
about its own origin and suggests the turn that shortens its connections
most.

THE SCORE is the connection length a router has to cover, per net the part
touches: the minimum spanning tree over that net's pads for a signal net,
and for a power or ground net (or one with many pads, poured or planed in
practice) the distance from each of the part's pads to the nearest pad of
the same net elsewhere, which is where its return or supply really goes.

A TURN THAT COLLIDES IS NOT SUGGESTED: the part's courtyard is turned with
it and checked against every other part's, so a suggestion can be applied
as it is. Bottom-side parts are scored too: their pads are turned where
they are on the board, which needs no assumption about how a mirrored
part's rotation angle is counted. The suggestion says ``turn_by`` as seen
from the top; ``new_rotation`` is given for a top-side part only.

Advisory: it never fails a board.
"""
from __future__ import annotations

import math
from typing import Any

from ..core.net_naming import is_ground_net, is_power_net
from . import geom
from .audit import LIMIT, _overlaps, body_outline
from .model import TOP, LayoutBoard

#: Nets this large are poured or planed; their pads go to the nearest one.
_LARGE_NET = 24
_TURNS = (90.0, 180.0, 270.0)


def _turn(x: float, y: float, ox: float, oy: float, deg: float):
    a = math.radians(deg)
    dx, dy = x - ox, y - oy
    return (ox + dx * math.cos(a) - dy * math.sin(a),
            oy + dx * math.sin(a) + dy * math.cos(a))


def _mst(points: list[tuple[float, float]]) -> float:
    """Total length of the minimum spanning tree, Prim's on a full graph."""
    if len(points) < 2:
        return 0.0
    inside = [False] * len(points)
    best = [math.inf] * len(points)
    best[0] = 0.0
    total = 0.0
    for _ in range(len(points)):
        i = min((k for k in range(len(points)) if not inside[k]),
                key=lambda k: best[k])
        inside[i] = True
        total += best[i]
        xi, yi = points[i]
        for k in range(len(points)):
            if not inside[k]:
                d = math.hypot(points[k][0] - xi, points[k][1] - yi)
                if d < best[k]:
                    best[k] = d
    return total


def _plane_like(board: LayoutBoard, net: str, count: int) -> bool:
    planes = {lay.plane_net for lay in board.layers if lay.plane_net}
    return (net in planes or is_ground_net(net) or is_power_net(net)
            or count >= _LARGE_NET)


def rotation_audit(board: LayoutBoard, limit: int = LIMIT,
                   min_gain_mils: float = 25.0) -> dict:
    """Quarter turns that shorten a part's connections by ``min_gain_mils``
    or more, with the turns a neighbour's courtyard rules out."""
    net_pads: dict[str, list[int]] = {}
    for i, p in enumerate(board.pads):
        if p.net:
            net_pads.setdefault(p.net, []).append(i)
    outlines = {c.ref: body_outline(c) for c in board.components}
    suggestions: list[dict] = []
    blocked: list[dict] = []
    checked = 0
    for comp in board.components:
        mine = [i for i, p in enumerate(board.pads) if p.comp == comp.ref]
        if comp.locked or len(mine) < 2:
            continue
        nets = sorted({board.pads[i].net for i in mine if board.pads[i].net})
        if not nets:
            continue
        checked += 1

        def cost(deg: float) -> tuple[float, dict]:
            where = {i: _turn(board.pads[i].x, board.pads[i].y,
                              comp.x, comp.y, deg) for i in mine}
            per_net: dict[str, float] = {}
            for net in nets:
                members = net_pads[net]
                others = [(board.pads[j].x, board.pads[j].y)
                          for j in members if j not in where]
                own = [where[j] for j in members if j in where]
                if _plane_like(board, net, len(members)):
                    per_net[net] = sum(
                        min((math.hypot(x - ox, y - oy) for ox, oy in others),
                            default=0.0) for x, y in own)
                else:
                    per_net[net] = _mst(own + others)
            return sum(per_net.values()), per_net

        now, now_nets = cost(0.0)
        scored = sorted(((cost(d), d) for d in _TURNS), key=lambda t: t[0][0])
        for (total, per_net), deg in scored:
            gain = now - total
            if gain < min_gain_mils:
                break
            outline = outlines.get(comp.ref) or []
            hit = None
            if outline:
                turned = geom.polygon([_turn(x, y, comp.x, comp.y, deg)
                                       for x, y in outline])
                for other in board.components:
                    if other.ref == comp.ref or not outlines.get(other.ref):
                        continue
                    if _overlaps(turned, geom.polygon(outlines[other.ref])):
                        hit = other.ref
                        break
            entry: dict[str, Any] = {
                "ref": comp.ref, "side": comp.side, "turn_by": deg,
                "mils_saved": round(gain, 1),
                "length_now": round(now, 1), "length_after": round(total, 1),
                "nets": {n: [round(now_nets[n], 1), round(per_net[n], 1)]
                         for n in nets if abs(now_nets[n] - per_net[n]) > 0.05}}
            if comp.side == TOP:
                entry["new_rotation"] = (comp.rotation + deg) % 360.0
            if not outline:
                entry["courtyard_checked"] = False
            if hit:
                entry["collides_with"] = hit
                blocked.append(entry)
                continue
            suggestions.append(entry)
            break
    suggestions.sort(key=lambda e: -e["mils_saved"])
    return {
        "pass": True,
        "counts": {"parts_checked": checked, "suggested": len(suggestions),
                   "blocked_by_courtyard": len(blocked),
                   "mils_to_save": round(sum(e["mils_saved"]
                                             for e in suggestions), 1)},
        "suggestions": suggestions[:limit],
        "blocked": blocked[:limit],
        "truncated": len(suggestions) > limit,
    }


__all__ = ["rotation_audit"]
