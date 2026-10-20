# SPDX-License-Identifier: Apache-2.0
"""Two-pad parts that would connect shorter at another right-angle rotation.

An advisory on a placed board, not a placer: every two-pad part on the top
side is tried at 0, 90, 180 and 270 degrees about its own origin, every
other part held where it is, and the rotation that scores lowest is
reported when it beats the current one by ``min_improvement_mils``.

Each pad scores one term:

* a routable net (``plane_net_pads`` pads or fewer): the airline length of
  the whole net, a minimum spanning tree over its pad centres, with this
  pad at its candidate position. A pad that only slides along a chain
  (a bulk cap on a power path) costs almost nothing, a real detour costs
  its length;
* a plane net (more pads than that, a ground or a supply): the distance to
  the nearest other pad on that net, because a decoupling cap's return is
  local and closeness to the IC's ground or thermal pad is what matters.

Geometry only. It knows nothing of which nets are critical, so a pull-up or
a strap resistor can be told to turn for no electrical gain, and a
datasheet's recommended layout outranks it. Bottom-side parts are skipped:
whether a rotation value turns a mirrored part clockwise or anticlockwise
in board coordinates has not been checked against Altium.
"""

from __future__ import annotations

import math
from typing import Optional

from .model import TOP, LayoutBoard, Pad

#: Nets with more pads than this are scored as planes (nearest pad).
PLANE_NET_PADS = 40

#: Smallest gain, in mils of score, worth reporting.
MIN_IMPROVEMENT_MILS = 25.0

ROTATIONS = (0.0, 90.0, 180.0, 270.0)


def mst_length(points: list[tuple[float, float]]) -> float:
    """Minimum spanning tree length over ``points`` (Prim, O(n^2))."""
    n = len(points)
    if n < 2:
        return 0.0
    in_tree = [False] * n
    best = [math.inf] * n
    best[0] = 0.0
    total = 0.0
    for _ in range(n):
        u = min((i for i in range(n) if not in_tree[i]), key=best.__getitem__)
        in_tree[u] = True
        total += best[u]
        ux, uy = points[u]
        for v in range(n):
            if not in_tree[v]:
                d = math.hypot(points[v][0] - ux, points[v][1] - uy)
                if d < best[v]:
                    best[v] = d
    return total


def _turn(x: float, y: float, ox: float, oy: float, deg: float) -> tuple[float, float]:
    """(x, y) turned anticlockwise by ``deg`` about (ox, oy)."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    dx, dy = x - ox, y - oy
    return (ox + dx * c - dy * s, oy + dx * s + dy * c)


def _orthogonal(deg: float) -> bool:
    r = deg % 90.0
    return r < 0.5 or r > 89.5


def suggest_rotations(board: LayoutBoard, designators: Optional[list[str]] = None,
                      min_improvement_mils: float = MIN_IMPROVEMENT_MILS,
                      plane_net_pads: int = PLANE_NET_PADS) -> dict:
    """Score each two-pad top-side part at the four right-angle rotations.

    Returns ``{"checked", "skipped", "suggestions"}``: ``suggestions`` worst
    first, each with the current and suggested rotation, the gain in mils,
    ``bbox_changes`` (a quarter turn swaps the body's width and height, so
    clearance wants re-checking) and the per-pad terms at both rotations.
    """
    wanted = {d.strip() for d in designators or [] if d.strip()}
    pads_of: dict[str, list[Pad]] = {}
    net_pads: dict[str, list[Pad]] = {}
    for p in board.pads:
        if p.comp:
            pads_of.setdefault(p.comp, []).append(p)
        if p.net:
            net_pads.setdefault(p.net, []).append(p)

    checked = 0
    skipped: list[dict] = []
    suggestions: list[dict] = []
    for comp in board.components:
        if wanted and comp.ref not in wanted:
            continue
        pads = pads_of.get(comp.ref, [])
        if len(pads) != 2:
            continue
        if comp.side != TOP:
            skipped.append({"designator": comp.ref, "reason": "bottom side"})
            continue
        if not _orthogonal(comp.rotation):
            skipped.append({"designator": comp.ref,
                            "reason": f"rotation {comp.rotation:g} is not a right angle"})
            continue

        current = round(comp.rotation) % 360

        def score(rotation: float) -> Optional[tuple[float, list[dict]]]:
            total, terms = 0.0, []
            for pad in pads:
                others = [(q.x, q.y) for q in net_pads.get(pad.net, [])
                          if q.comp != comp.ref] if pad.net else []
                if not others:
                    continue
                px, py = _turn(pad.x, pad.y, comp.x, comp.y, rotation - current)
                if len(net_pads[pad.net]) > plane_net_pads:
                    value = min(math.hypot(px - x, py - y) for x, y in others)
                    metric = "nearest_return_mils"
                else:
                    value = mst_length(others + [(px, py)])
                    metric = "net_airline_mils"
                total += value
                terms.append({"pad": pad.name, "net": pad.net, metric: round(value, 1)})
            return (total, terms) if terms else None

        now = score(current)
        if now is None:
            continue
        checked += 1
        best_rot, best = current, now
        for r in ROTATIONS:
            s = score(r)
            if s is not None and s[0] < best[0] - 1e-6:
                best_rot, best = int(r), s
        gain = now[0] - best[0]
        if best_rot != current and gain >= min_improvement_mils:
            suggestions.append({
                "designator": comp.ref,
                "current_rotation": current,
                "suggested_rotation": best_rot,
                "improvement_mils": round(gain, 1),
                "bbox_changes": (best_rot - current) % 180 != 0,
                "current_pads": now[1],
                "suggested_pads": best[1],
            })
    suggestions.sort(key=lambda s: -s["improvement_mils"])
    return {"checked": checked, "skipped": skipped, "suggestions": suggestions}


def rotation_audit(board: LayoutBoard, limit: int = 200) -> dict:
    """Layout audit check: two-pad parts that would connect shorter turned.

    Advisory: ``pass`` stays True, since a shorter airline is not always the
    better layout."""
    found = suggest_rotations(board)
    return {
        "pass": True,
        "advisory": True,
        "counts": {"checked": found["checked"], "suggestions": len(found["suggestions"]),
                   "skipped": len(found["skipped"])},
        "suggestions": found["suggestions"][:limit],
        "skipped": found["skipped"][:limit],
        "note": ("Airline geometry only, pad centre to pad centre. Weigh each "
                 "suggestion: it matters for decoupling, snubber and filter parts, "
                 "rarely for pull-ups or straps. After a quarter turn "
                 "(bbox_changes) re-run placement_audit."),
    }


__all__ = ["mst_length", "rotation_audit", "suggest_rotations"]
