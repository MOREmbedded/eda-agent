# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Multi-layer Manhattan / 45-degree A* router over a routing grid.

Net order: class priority first (power / ground, then high-current /
switch, differential / clock, analog, control, plain signal -- the
classes :mod:`eda_agent.design.net_classes` assigns), then short
nets before long inside each class, then name. Each routed net is
rasterized back into the obstacle map before the next starts.

Multi-pin nets grow a tree by sequential closest-pair: route the closest
terminal pair, then repeatedly route the terminal nearest the tree into
ANY cell of the tree (steiner-lite -- taps can land mid-segment, which
is connective copper in Altium). Corner cutting leaves every cell a tap
lands on in place, and a net is reported routed only if its emitted
copper joins all of its pads (:func:`net_islands`).

Output tracks are ``{x1, y1, x2, y2, width, layer, net_name}`` and vias
``{x, y, net, size, hole_size}`` -- integer mils, key-for-key the
parameters of the ``pcb_place_tracks`` / ``pcb_place_via`` MCP tools.

Determinism: no randomness; every iteration order is sorted and heap
ties break on insertion order, so identical input gives identical
output regardless of input list order.
"""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass
from typing import Any

from eda_agent import compute
from eda_agent.route.model import (
    DEFAULT_GRID_PITCH_MILS,
    RouteRules,
    RoutingProblem,
    Terminal,
    dist_point_rect,
    dist_point_seg,
    dist_seg_rect,
    dist_seg_seg,
    rect_extents,
    to_rect_frame,
)

# Lower routes earlier. Unknown classes route with plain signals.
_CLASS_PRIORITY = {
    "power": 0,
    "ground": 0,
    "high_current": 1,
    "switch": 1,
    "differential": 2,
    "clock": 2,
    "analog": 3,
    "control": 4,
    "signal": 5,
}

# FOUR DIRECTIONS IN THE SEARCH, 45s AFTERWARDS. Adding the diagonals to
# the A* move set was tried and measured: it reroutes earlier nets
# through cells the orthogonal routes left free, walls off a later net,
# and cost the blinker benchmark a net it had always routed (100% to
# 83.3%). Nets are routed in order and each becomes an obstacle for the
# next, so a locally prettier path is not a better board. The corners are
# chamfered after the fact instead, against the same obstacle map, which
# cannot change what routes at all.
_DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_DIAGONAL_DIRS = ((1, 1), (1, -1), (-1, 1), (-1, -1))
_DIR_NONE = 8  # start of path / just emerged from a via: next move is free

#: How deep a corner may be cut, in grid cells. The chamfer never eats
#: more than half of the shorter leg, so a short jog keeps its shape.
_MAX_CHAMFER_CELLS = 4
_EPS = 1e-6

#: Layer name a violation record gives a via or a through-hole pad.
_MULTI_LAYER = "MultiLayer"


@dataclass
class RouterOptions:
    """A* cost knobs. Step cost is 1.0 per grid cell; the penalties are
    in the same unit (multiples of one grid pitch)."""

    bend_penalty: float = 1.0
    via_cost: float = 10.0
    #: A via inside a surface-mount pad wicks solder off the joint and
    #: has to be filled and capped, which is a different and dearer
    #: process. Off by default; the cases that genuinely need it are BGA
    #: fanout with no room to escape and a thermal pad being stitched to
    #: a plane.
    allow_via_in_pad: bool = False
    # Per-connection expansion budget so a walled-in net fails fast
    # instead of flooding a big grid forever.
    max_expansions: int = 200_000
    # Permit diagonal grid steps. Corner cutting is blocked unless both
    # adjacent orthogonal cells are passable, yielding manufacturable 45° runs.
    allow_diagonal: bool = False


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def route_geometry(geometry: dict[str, Any],
                   rules: dict[str, Any] | RouteRules | None = None,
                   net_classes: dict[str, str] | None = None,
                   grid_pitch_mils: int = DEFAULT_GRID_PITCH_MILS,
                   options: RouterOptions | None = None) -> dict[str, Any]:
    """Route every netted pad group in the board geometry dict (mils).

    Tool-shaped result: ``{"ok": False, "reason": ...}`` on malformed
    input, else ``{"ok": True, "summary": ..., "order": ..., "nets":
    ..., "tracks": ..., "vias": ..., "validation": ...}``. Per-net
    routing failure is honest data (status ``failed``), not a tool
    error.
    """
    try:
        problem = RoutingProblem.from_geometry(
            geometry, rules, net_classes=net_classes,
            grid_pitch_mils=grid_pitch_mils)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "reason": str(exc)}
    return route_problem(problem, options)


def route_problem(problem: RoutingProblem,
                  options: RouterOptions | None = None) -> dict[str, Any]:
    """Route a prebuilt :class:`RoutingProblem` (mils). Same payload as
    :func:`route_geometry`. Mutates the problem's obstacle map."""
    opt = options or RouterOptions()
    # Which implementation runs the search, decided once per run so the
    # report cannot name one and the nets use the other. Raises here,
    # before any net is touched, when Rust was asked for and is missing.
    implementation = compute.active()
    order = _net_order(problem)

    nets_out: dict[str, dict[str, Any]] = {}
    all_tracks: list[dict[str, Any]] = []
    all_vias: list[dict[str, Any]] = []
    routed = failed = skipped = 0

    for net in sorted(problem.terminals):
        if len(problem.terminals[net]) < 2:
            skipped += 1
            nets_out[net] = {
                "status": "skipped",
                "reason": "fewer than 2 terminals",
                "class": problem.class_of(net),
                "tracks": [], "vias": [],
            }
    for net in order:
        res = _route_net(problem, net, opt)
        nets_out[net] = res
        if res["status"] == "routed":
            routed += 1
            all_tracks.extend(res["tracks"])
            all_vias.extend(res["vias"])
            problem.add_route_obstacles(net, res["tracks"], res["vias"])
        else:
            failed += 1

    attempted = routed + failed
    # "Nothing attempted" used to fall straight through to completion 1.0,
    # so a run whose nets had all been filtered away reported perfect
    # completion while routing nothing. Distinguish the two ways of having
    # no work: a geometry with no netted pads at all really is vacuously
    # complete, but a geometry that HAD netted pads and still produced no
    # routable net means we routed none of what was there -- 0.0.
    if attempted:
        completion = routed / attempted
    elif not nets_out and problem.pad_nets_seen:
        completion = 0.0
    else:
        completion = 1.0  # nothing to route, or everything present skipped
    # Tally of WHY nets failed. "expansion_budget_exhausted" in here means
    # the router hit a knob, not the board -- a caller that treats every
    # failure as a geometry verdict would otherwise redesign around a
    # limit it could have raised.
    failed_hints: dict[str, int] = {}
    for res in nets_out.values():
        if res.get("status") == "failed":
            h = str(res.get("hint", "no_route"))
            failed_hints[h] = failed_hints.get(h, 0) + 1
    summary = {
        "nets_total": len(nets_out),
        "routed": routed,
        "failed": failed,
        "skipped": skipped,
        "attempted": attempted,
        "completion": completion,
        "failed_hints": failed_hints,
        "grid": problem.grid_info(),
        # python | rust: which A* produced this (eda_agent.compute).
        "compute": implementation,
        "track_count": len(all_tracks),
        "via_count": len(all_vias),
        "total_length_mils": sum(
            abs(t["x2"] - t["x1"]) + abs(t["y2"] - t["y1"])
            for t in all_tracks),
    }
    solution = {
        "ok": True,
        "summary": summary,
        "order": order,
        "nets": {k: nets_out[k] for k in sorted(nets_out)},
        "tracks": all_tracks,
        "vias": all_vias,
    }
    solution["validation"] = validate_solution(problem, solution)
    return solution


def validate_solution(problem: RoutingProblem,
                      solution: dict[str, Any]) -> dict[str, Any]:
    """Geometric post-check of a routing solution against the problem.

    Verifies, for every pair of DIFFERENT nets, that no same-layer
    track/track, track/pad, track/via or via/via spacing falls below
    half-width + half-width + clearance (mils) -- which also catches
    same-layer crossings (distance 0). Each violation names the layer
    of both objects (``layer``, ``layer_b``; ``MultiLayer`` for a via
    or a through-hole pad).

    Also verifies that every net the solution reports routed is one
    piece of copper: its pads, its new tracks and vias, and the copper
    it already had. Clearance alone passes a net whose track stops
    short of the rest of it. A net in more than one island is listed in
    ``unconnected`` with its pad centres grouped by island.

    Returns ``{"ok": bool, "violations": [...], "unconnected": [...],
    "checked": n}``; ``ok`` is False if either list is non-empty.
    """
    clearance = problem.rules.clearance_mils
    tracks = solution.get("tracks") or []
    vias = solution.get("vias") or []
    layer_idx = {name.lower(): i for i, name in enumerate(problem.layers)}
    violations: list[dict[str, Any]] = []
    checked = 0

    def _report(kind: str, net_a: str, net_b: Any,
                dist: float, need: float, where: tuple,
                layer: str, layer_b: str) -> None:
        violations.append({
            "kind": kind, "net_a": net_a,
            "net_b": net_b if net_b is not None else "",
            "distance_mils": round(dist, 3),
            "required_mils": round(need, 3),
            "at": [int(round(w)) for w in where],
            "layer": layer,
            "layer_b": layer_b,
        })

    # BOXES FIRST. Every pair below is still visited and counted, in the
    # same order, but a pair whose boxes are further apart than the
    # distance that would be a violation is skipped before the exact
    # distance. The exact distance is never less than the gap between
    # the boxes, so a skipped pair could not have been reported: the
    # result is identical, and on a 1090-pad board this check went from
    # most of the run time to a fraction of it.
    tbox = [_seg_box(t["x1"], t["y1"], t["x2"], t["y2"]) for t in tracks]
    gbox = [_geom_box(g) for g in problem.geoms]
    vbox = [(v["x"], v["y"], v["x"], v["y"]) for v in vias]
    tlayer = [str(t["layer"]).lower() for t in tracks]

    # Track vs track (same layer, different nets).
    for i, a in enumerate(tracks):
        for j in range(i + 1, len(tracks)):
            b = tracks[j]
            if a["net_name"] == b["net_name"]:
                continue
            if tlayer[i] != tlayer[j]:
                continue
            checked += 1
            need = a["width"] / 2.0 + b["width"] / 2.0 + clearance
            if _box_gap(tbox[i], tbox[j]) > need:
                continue
            d = dist_seg_seg(a["x1"], a["y1"], a["x2"], a["y2"],
                             b["x1"], b["y1"], b["x2"], b["y2"])
            if d < need - _EPS:
                _report("track_track", a["net_name"], b["net_name"],
                        d, need, (a["x1"], a["y1"]),
                        str(a["layer"]), str(b["layer"]))

    # Track vs static copper (pads / existing tracks / existing vias).
    for ai, a in enumerate(tracks):
        ali = layer_idx[tlayer[ai]]
        ahw = a["width"] / 2.0
        abox = tbox[ai]
        for gi, g in enumerate(problem.geoms):
            if g["net"] == a["net_name"]:
                continue
            if g["layer"] is not None and g["layer"] != ali:
                continue
            checked += 1
            if _box_gap(abox, gbox[gi]) > ahw + clearance + _geom_reach(g):
                continue
            if g["kind"] == "rect":
                need = ahw + clearance
                d = dist_seg_rect(a["x1"], a["y1"], a["x2"], a["y2"],
                                  g["cx"], g["cy"], g["hw"], g["hh"],
                                  g.get("rot", 0.0))
                where = (g["cx"], g["cy"])
            elif g["kind"] == "seg":
                need = ahw + g["hw"] + clearance
                d = dist_seg_seg(a["x1"], a["y1"], a["x2"], a["y2"],
                                 g["x1"], g["y1"], g["x2"], g["y2"])
                where = (g["x1"], g["y1"])
            else:  # circle
                need = ahw + g["r"] + clearance
                d = dist_point_seg(g["x"], g["y"],
                                   a["x1"], a["y1"], a["x2"], a["y2"])
                where = (g["x"], g["y"])
            if d < need - _EPS:
                _report(f"track_{g['kind']}", a["net_name"], g["net"],
                        d, need, where,
                        str(a["layer"]), _geom_layer_name(problem, g))

    # Vias span every layer: check against all tracks, pads, and vias.
    for i, v in enumerate(vias):
        vr = v["size"] / 2.0
        for ai, a in enumerate(tracks):
            if a["net_name"] == v["net"]:
                continue
            checked += 1
            need = vr + a["width"] / 2.0 + clearance
            if _box_gap(vbox[i], tbox[ai]) > need:
                continue
            d = dist_point_seg(v["x"], v["y"],
                               a["x1"], a["y1"], a["x2"], a["y2"])
            if d < need - _EPS:
                _report("via_track", v["net"], a["net_name"],
                        d, need, (v["x"], v["y"]),
                        _MULTI_LAYER, str(a["layer"]))
        for gi, g in enumerate(problem.geoms):
            if g["net"] == v["net"]:
                continue
            checked += 1
            if _box_gap(vbox[i], gbox[gi]) > vr + clearance + _geom_reach(g):
                continue
            if g["kind"] == "rect":
                need = vr + clearance
                d = dist_point_rect_edge(v["x"], v["y"],
                                         g["cx"], g["cy"], g["hw"], g["hh"],
                                         g.get("rot", 0.0))
            elif g["kind"] == "seg":
                need = vr + g["hw"] + clearance
                d = dist_point_seg(v["x"], v["y"],
                                   g["x1"], g["y1"], g["x2"], g["y2"])
            else:
                need = vr + g["r"] + clearance
                d = math.hypot(v["x"] - g["x"], v["y"] - g["y"])
            if d < need - _EPS:
                _report(f"via_{g['kind']}", v["net"], g["net"],
                        d, need, (v["x"], v["y"]),
                        _MULTI_LAYER, _geom_layer_name(problem, g))
        for w in vias[i + 1:]:
            if w["net"] == v["net"]:
                continue
            checked += 1
            need = vr + w["size"] / 2.0 + clearance
            d = math.hypot(v["x"] - w["x"], v["y"] - w["y"])
            if d < need - _EPS:
                _report("via_via", v["net"], w["net"],
                        d, need, (v["x"], v["y"]),
                        _MULTI_LAYER, _MULTI_LAYER)

    # Connectivity, per net the solution says it routed. A solution with
    # no per-net status is checked for every net that has new copper.
    nets_info = solution.get("nets")
    if isinstance(nets_info, dict):
        check_nets = sorted(
            n for n, r in nets_info.items()
            if isinstance(r, dict) and r.get("status") == "routed")
    else:
        check_nets = sorted({str(t["net_name"]) for t in tracks}
                            | {str(v["net"]) for v in vias})
    static_by_net: dict[Any, list[dict[str, Any]]] = {}
    for g in problem.geoms:
        static_by_net.setdefault(g["net"], []).append(g)
    tracks_by_net: dict[str, list[dict[str, Any]]] = {}
    for t in tracks:
        tracks_by_net.setdefault(str(t["net_name"]), []).append(t)
    vias_by_net: dict[str, list[dict[str, Any]]] = {}
    for v in vias:
        vias_by_net.setdefault(str(v["net"]), []).append(v)
    unconnected: list[dict[str, Any]] = []
    for net in check_nets:
        groups = _islands(problem, tracks_by_net.get(net, []),
                          vias_by_net.get(net, []),
                          static_by_net.get(net, []))
        if len(groups) > 1:
            unconnected.append({
                "net": net,
                "islands": len(groups),
                "pads": [[list(p) for p in grp] for grp in groups],
            })

    return {"ok": not violations and not unconnected,
            "violations": violations,
            "unconnected": unconnected,
            "checked": checked}


def _seg_box(x1: float, y1: float, x2: float, y2: float
             ) -> tuple[float, float, float, float]:
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def _geom_box(g: dict[str, Any]) -> tuple[float, float, float, float]:
    """Box around the part of a static obstacle its distance is measured
    from: the rect itself, a segment's centreline, a circle's centre."""
    if g["kind"] == "rect":
        ex, ey = rect_extents(g["hw"], g["hh"], g.get("rot", 0.0))
        return (g["cx"] - ex, g["cy"] - ey, g["cx"] + ex, g["cy"] + ey)
    if g["kind"] == "seg":
        return _seg_box(g["x1"], g["y1"], g["x2"], g["y2"])
    return (g["x"], g["y"], g["x"], g["y"])


def _geom_reach(g: dict[str, Any]) -> float:
    """What a static obstacle adds to the required distance beyond its
    measured core: a segment's half-width, a circle's radius."""
    if g["kind"] == "seg":
        return g["hw"]
    if g["kind"] == "rect":
        return 0.0
    return g["r"]


def _box_gap(a: tuple[float, float, float, float],
             b: tuple[float, float, float, float]) -> float:
    """Largest axis gap between two boxes; 0 or less when they touch.
    Never more than the true distance between anything inside them."""
    return max(b[0] - a[2], a[0] - b[2], b[1] - a[3], a[1] - b[3])


def _geom_layer_name(problem: RoutingProblem, g: dict[str, Any]) -> str:
    """Layer name of a static obstacle, for a violation record."""
    if g["layer"] is not None:
        return problem.layers[g["layer"]]
    # Copper on every layer: a through-hole pad or a via. The only track
    # the model puts on every layer is one drawn on the keep-out layer.
    if g["kind"] == "seg":
        return "KeepOutLayer"
    return _MULTI_LAYER


def net_islands(problem: RoutingProblem, net: str,
                tracks: list[dict[str, Any]],
                vias: list[dict[str, Any]]) -> list[list[tuple[int, int]]]:
    """Group a net's pads by the copper that actually joins them (mils).

    The copper is the net's pads (a through-hole pad is on every layer),
    the ``tracks`` and ``vias`` given (router output shape), and any
    copper of the net already on the board. Two pieces join when they
    share a layer and touch: tracks when their centrelines come within
    the sum of their half widths, a track and a pad when the centreline
    reaches the pad, and a via on every layer within its radius.

    Returns one list of pad centres per island that holds a pad, each
    sorted, the islands sorted. A joined net gives exactly one.
    """
    static = [g for g in problem.geoms if g["net"] == net]
    return _islands(problem, tracks, vias, static)


def _islands(problem: RoutingProblem,
             tracks: list[dict[str, Any]],
             vias: list[dict[str, Any]],
             static: list[dict[str, Any]]) -> list[list[tuple[int, int]]]:
    """Union-find over one net's copper; see :func:`net_islands`.

    Each item is ``(kind, layer or None for every layer, bbox, shape)``
    with the bbox covering the copper itself, so a sweep along x only
    measures pairs whose copper could touch.
    """
    layer_idx = {name.lower(): i for i, name in enumerate(problem.layers)}
    items: list[tuple[str, int | None, tuple[float, float, float, float],
                      tuple[float, ...]]] = []
    pad_items: list[int] = []

    def _add_seg(li: int | None, x1: float, y1: float, x2: float,
                 y2: float, hw: float) -> None:
        items.append(("seg", li, (min(x1, x2) - hw, min(y1, y2) - hw,
                                  max(x1, x2) + hw, max(y1, y2) + hw),
                      (x1, y1, x2, y2, hw)))

    def _add_circle(x: float, y: float, r: float) -> None:
        items.append(("circle", None, (x - r, y - r, x + r, y + r),
                      (x, y, r)))

    for g in static:
        if g["kind"] == "rect":
            cx, cy, hw, hh = g["cx"], g["cy"], g["hw"], g["hh"]
            rot = g.get("rot", 0.0)
            ex, ey = rect_extents(hw, hh, rot)
            pad_items.append(len(items))
            items.append(("rect", g["layer"],
                          (cx - ex, cy - ey, cx + ex, cy + ey),
                          (cx, cy, hw, hh, rot)))
        elif g["kind"] == "seg":
            _add_seg(g["layer"], g["x1"], g["y1"], g["x2"], g["y2"],
                     g["hw"])
        else:
            _add_circle(g["x"], g["y"], g["r"])
    for t in tracks:
        li = layer_idx.get(str(t["layer"]).lower())
        if li is None:
            continue  # not routing copper this problem knows about
        _add_seg(li, float(t["x1"]), float(t["y1"]), float(t["x2"]),
                 float(t["y2"]), float(t["width"]) / 2.0)
    for v in vias:
        _add_circle(float(v["x"]), float(v["y"]), float(v["size"]) / 2.0)

    parent = list(range(len(items)))

    def _find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    order = sorted(range(len(items)), key=lambda k: items[k][2][0])
    for pos, i in enumerate(order):
        ki, li_a, box_a, sa = items[i]
        for q in range(pos + 1, len(order)):
            j = order[q]
            kj, li_b, box_b, sb = items[j]
            if box_b[0] > box_a[2] + _EPS:
                break  # sorted on x: nothing further along can touch
            if box_b[1] > box_a[3] + _EPS or box_a[1] > box_b[3] + _EPS:
                continue
            if li_a is not None and li_b is not None and li_a != li_b:
                continue
            ra, rb = _find(i), _find(j)
            if ra == rb:
                continue
            if _copper_touches(ki, sa, kj, sb):
                parent[ra] = rb

    groups: dict[int, list[tuple[int, int]]] = {}
    for k in pad_items:
        cx, cy = items[k][3][0], items[k][3][1]
        groups.setdefault(_find(k), []).append(
            (int(round(cx)), int(round(cy))))
    return sorted(sorted(grp) for grp in groups.values())


def _copper_touches(ka: str, a: tuple[float, ...],
                    kb: str, b: tuple[float, ...]) -> bool:
    """True if two same-layer copper shapes touch (mils). Kinds are
    ``seg`` (x1, y1, x2, y2, hw), ``rect`` (cx, cy, hw, hh[, rot]) and
    ``circle`` (x, y, r)."""
    if ka > kb:  # canonical order: circle < rect < seg
        ka, a, kb, b = kb, b, ka, a
    if ka == "seg":
        return dist_seg_seg(*a[:4], *b[:4]) <= a[4] + b[4] + _EPS
    if ka == "rect" and kb == "seg":
        return dist_seg_rect(*b[:4], *a) <= b[4] + _EPS
    if ka == "rect":  # rect / rect
        if not (a[4:] and a[4]) and not (b[4:] and b[4]):
            dx = max(0.0, abs(a[0] - b[0]) - a[2] - b[2])
            dy = max(0.0, abs(a[1] - b[1]) - a[3] - b[3])
            return math.hypot(dx, dy) <= _EPS
        # Turned pads: convex shapes touch when an edge of one reaches
        # the other, or one sits wholly inside the other.
        if (dist_point_rect(a[0], a[1], *b) <= _EPS
                or dist_point_rect(b[0], b[1], *a) <= _EPS):
            return True
        pts = _rect_corners(*a)
        return any(dist_seg_rect(*pts[i], *pts[(i + 1) % 4], *b) <= _EPS
                   for i in range(4))
    if kb == "seg":  # circle / seg
        return dist_point_seg(a[0], a[1], *b[:4]) <= a[2] + b[4] + _EPS
    if kb == "rect":  # circle / rect
        return dist_point_rect(a[0], a[1], *b) <= a[2] + _EPS
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= a[2] + b[2] + _EPS


def _rect_corners(cx: float, cy: float, hw: float, hh: float,
                  rot: float = 0.0) -> list[tuple[float, float]]:
    """A rect's four corners in order, turned ``rot`` degrees about its
    centre."""
    a = math.radians(rot)
    c, s = math.cos(a), math.sin(a)
    return [(cx + dx * c - dy * s, cy + dx * s + dy * c)
            for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]


def dist_point_rect_edge(px: float, py: float, cx: float, cy: float,
                         hw: float, hh: float, rot: float = 0.0) -> float:
    """Distance from a point to a rect's boundary, 0 inside (mils)."""
    px, py = to_rect_frame(px, py, cx, cy, rot)
    dx = max(0.0, abs(px - cx) - hw)
    dy = max(0.0, abs(py - cy) - hh)
    return math.hypot(dx, dy)


# ---------------------------------------------------------------------------
# Net ordering
# ---------------------------------------------------------------------------


def _net_order(problem: RoutingProblem) -> list[str]:
    """Routable nets (>= 2 terminals) sorted: class priority, then HPWL
    short-to-long, then name."""
    keyed = []
    for net, terms in problem.terminals.items():
        if len(terms) < 2:
            continue
        xs = [t.x for t in terms]
        ys = [t.y for t in terms]
        hpwl = (max(xs) - min(xs)) + (max(ys) - min(ys))
        prio = _CLASS_PRIORITY.get(problem.class_of(net), 5)
        keyed.append((prio, hpwl, net))
    keyed.sort()
    return [net for _, _, net in keyed]


# ---------------------------------------------------------------------------
# Per-net routing
# ---------------------------------------------------------------------------


def _route_net(problem: RoutingProblem, net: str,
               opt: RouterOptions) -> dict[str, Any]:
    terms = sorted(problem.terminals[net],
                   key=lambda t: (t.x, t.y, t.layers))
    width = problem.width_for_net(net)
    cls = problem.class_of(net)

    def _failed(reason: str, diag: dict[str, Any] | None = None) -> dict[str, Any]:
        # Partial copper of a failed net is dropped: it would block
        # later nets without delivering connectivity.
        out = {
            "status": "failed", "reason": reason,
            "class": cls, "width": width, "tracks": [], "vias": [],
        }
        out.update(diag or {})
        return out

    def _fail(k: int, reason: str,
              diag: dict[str, Any] | None = None) -> dict[str, Any]:
        return _failed(f"connection {k + 1}/{len(terms) - 1}: {reason}", diag)

    # Seed pair: globally closest two terminals (Manhattan on centers).
    best = None
    for i in range(len(terms)):
        for j in range(i + 1, len(terms)):
            d = (abs(terms[i].x - terms[j].x)
                 + abs(terms[i].y - terms[j].y))
            cand = (d, i, j)
            if best is None or cand < best:
                best = cand
    _, i0, j0 = best  # type: ignore[misc]

    tree_cells: set[tuple[int, int, int]] = set()
    tree_xy: dict[tuple[int, int], set[int]] = {}
    paths: list[list[tuple[int, int, int]]] = []

    def _starts(t: Terminal) -> list[tuple[int, int, int]]:
        return [(li, t.cell[0], t.cell[1]) for li in t.layers]

    def _absorb(path: list[tuple[int, int, int]]) -> None:
        paths.append(path)
        for (li, ix, iy) in path:
            tree_cells.add((li, ix, iy))
            tree_xy.setdefault((ix, iy), set()).add(li)

    path, exhausted, used = _astar(problem, net, _starts(terms[i0]),
                                   set(_starts(terms[j0])), opt)
    if path is None:
        diag = _escape_diagnosis(problem, net, [terms[i0], terms[j0]],
                                 opt, exhausted, used)
        return _fail(0, f"no path between closest pair ({diag['hint']})",
                     diag)
    _absorb(path)

    connected = {i0, j0}
    k = 1
    while len(connected) < len(terms):
        # Closest unconnected terminal to the tree (cell Manhattan).
        pick = None
        for idx in range(len(terms)):
            if idx in connected:
                continue
            cx, cy = terms[idx].cell
            d = min(abs(cx - ix) + abs(cy - iy) for (ix, iy) in tree_xy)
            cand = (d, idx)
            if pick is None or cand < pick:
                pick = cand
        idx = pick[1]  # type: ignore[index]
        path, exhausted, used = _astar(
            problem, net, _starts(terms[idx]), tree_cells, opt)
        if path is None:
            diag = _escape_diagnosis(problem, net, [terms[idx]], opt,
                                     exhausted, used)
            return _fail(k, f"terminal at ({terms[idx].x},{terms[idx].y}) "
                            f"unreachable ({diag['hint']})", diag)
        _absorb(path)
        connected.add(idx)
        k += 1

    tracks, vias = _emit(problem, net, width, paths, terms, tree_xy)
    # The search joined grid cells; what gets placed is the emitted
    # copper, after corners are cut and stubs added. Measure that, so a
    # net is never reported routed while a track stops short of the rest.
    islands = net_islands(problem, net, tracks, vias)
    if len(islands) > 1:
        return _failed(f"the emitted copper leaves the pads in "
                       f"{len(islands)} separate islands")
    return {
        "status": "routed",
        "class": cls,
        "width": width,
        "tracks": tracks,
        "vias": vias,
        "length_mils": sum(
            abs(t["x2"] - t["x1"]) + abs(t["y2"] - t["y1"])
            for t in tracks),
    }


# ---------------------------------------------------------------------------
# Failure diagnosis
# ---------------------------------------------------------------------------


def _nearest_foreign_pad(problem: RoutingProblem, net: str,
                         t: Terminal) -> tuple[float, float] | None:
    """(center pitch, edge gap) to the nearest foreign pad, mils.

    Scans the retained pad rects rather than the grid: the grid has
    already quantized away the very distances the caller needs, so
    asking it whether a corridor exists would answer with the same
    resolution that is under suspicion.
    """
    own_hw = own_hh = 0.0
    for g in problem.geoms:
        # Terminal coordinates are rounded to whole mils, pad centers are
        # not, so match with a mil of slack rather than exactly.
        if (g["kind"] == "rect" and g["net"] == net
                and abs(g["cx"] - t.x) < 1.0 and abs(g["cy"] - t.y) < 1.0):
            own_hw, own_hh = g["hw"], g["hh"]
            break
    best: tuple[float, float] | None = None
    for g in problem.geoms:
        if g["kind"] != "rect" or g["net"] == net:
            continue
        pitch = math.hypot(g["cx"] - t.x, g["cy"] - t.y)
        # Edge-to-edge along the axis-aligned separation, which is what a
        # track actually has to fit through.
        gap = math.hypot(
            max(0.0, abs(g["cx"] - t.x) - g["hw"] - own_hw),
            max(0.0, abs(g["cy"] - t.y) - g["hh"] - own_hh))
        if best is None or pitch < best[0]:
            best = (pitch, gap)
    return best


def _escape_diagnosis(problem: RoutingProblem, net: str,
                      terms: list[Terminal], opt: RouterOptions,
                      exhausted: bool, expansions: int) -> dict[str, Any]:
    """Numbers behind a failed connection, and which knob they implicate.

    Five distinct causes used to surface as the same "no path": the A*
    budget ran out; the neighbouring pad pitch leaves no DRC-legal
    corridor at ANY resolution; a corridor exists but is narrower than
    one grid pitch; the pad's own grid node is walled in by foreign
    copper; or the reachable region genuinely has no target. Only the
    first two are knobs (max_expansions, grid_pitch_mils) and only the
    second is a real geometry verdict -- guessing between them costs a
    live board iteration each time, so spend the few hundred distance
    evaluations here instead.
    """
    width = problem.width_for_net(net)
    clearance = problem.rules.clearance_mils
    # Two flanking neighbours, so a track squeezing past needs its own
    # width plus one clearance to each side.
    corridor = width + 2.0 * clearance
    walled_in = [
        [t.x, t.y] for t in terms
        if not any(problem.passable(li, t.cell[0], t.cell[1], net)
                   for li in t.layers)
    ]
    pitch_mils = gap_mils = None
    for t in terms:
        near = _nearest_foreign_pad(problem, net, t)
        if near is None:
            continue
        if pitch_mils is None or near[0] < pitch_mils:
            pitch_mils, gap_mils = near

    diag: dict[str, Any] = {
        "grid_pitch_mils": problem.pitch,
        "walled_in_pads": walled_in,
        "expansions_used": expansions,
        "expansion_budget": opt.max_expansions,
        "corridor_needed_mils": round(corridor, 2),
    }
    if pitch_mils is not None:
        band = (gap_mils or 0.0) - corridor
        diag["limiting_pad_pitch_mils"] = round(pitch_mils, 2)
        diag["limiting_pad_gap_mils"] = round(gap_mils or 0.0, 2)
        diag["escape_band_mils"] = round(band, 2)
    else:
        band = None

    if exhausted:
        diag["hint"] = "expansion_budget_exhausted"
        diag["detail"] = (
            f"A* stopped after {opt.max_expansions} expansions with an "
            f"open frontier on a {problem.nx}x{problem.ny} x "
            f"{len(problem.layers)}-layer grid; this is a search-budget "
            f"verdict, NOT proof that no route exists. Raise "
            f"max_expansions or grid_pitch_mils and retry.")
    elif band is not None and band < 0:
        diag["hint"] = "no_corridor_at_any_pitch"
        diag["detail"] = (
            f"the limiting neighbour is {diag['limiting_pad_pitch_mils']} "
            f"mils away (edge gap {diag['limiting_pad_gap_mils']} mils); a "
            f"{width} mil track with {clearance} mil clearance needs "
            f"{round(corridor, 2)} mils to pass. No grid pitch fixes that "
            f"gap -- fan out with a via, narrow the track, or relax "
            f"clearance. Search used {expansions} expansions.")
    elif band is not None and problem.pitch > band:
        suggested = max(1, int(math.floor(band)))
        diag["hint"] = "grid_too_coarse"
        diag["suggested_grid_pitch_mils"] = suggested
        diag["detail"] = (
            f"a {round(band, 2)} mil escape band exists past the limiting "
            f"neighbour ({diag['limiting_pad_pitch_mils']} mils away), but "
            f"grid_pitch_mils={problem.pitch} cannot land a node inside "
            f"it. Retry at grid_pitch_mils<={suggested}. Search used "
            f"{expansions} expansions.")
    elif walled_in:
        diag["hint"] = "pad_node_blocked"
        diag["detail"] = (
            "the pad's own grid node is not passable for this net -- "
            "foreign copper already sits within clearance of it. Rip the "
            "offending net or route this one first.")
    else:
        diag["hint"] = "no_route"
        diag["detail"] = (
            "the reachable region was searched exhaustively and contains "
            "no target; the obstruction is elsewhere on the path, not at "
            "the pads.")
    return diag


# ---------------------------------------------------------------------------
# A* search
# ---------------------------------------------------------------------------


def _astar(problem: RoutingProblem, net: str,
           starts: list[tuple[int, int, int]],
           targets: set[tuple[int, int, int]],
           opt: RouterOptions,
           ) -> tuple[list[tuple[int, int, int]] | None, bool, int]:
    """Shortest Manhattan path from any start to any target cell.

    States are ``(layer, ix, iy, dir)`` so the bend penalty sees the
    arrival direction. Returns ``(path, budget_exhausted, expansions)``:
    the path as ``(layer, ix, iy)`` cells (consecutive same-cell entries
    mark a via) or None.

    ``budget_exhausted`` separates "searched everything reachable and
    there is no path" from "ran out of expansion budget". They are the
    same None to a caller that ignores it, and that conflation gets
    worse the finer the grid: halving the pitch quadruples the cells a
    flood has to chew through, so a budget that was ample at 25 mils
    turns real routes into "no path" at 5 mils -- silently, and in the
    direction that looks like a board problem rather than a knob. The
    expansion count makes the verdict auditable: a failure after a few
    dozen expansions is a pad trapped in its own neighbourhood, one
    after hundreds of thousands is a board-scale flood.
    """
    if not targets:
        return None, False, 0
    native = compute.native()
    if native is not None:
        return _astar_native(native, problem, net, starts, targets, opt)
    txs = [ix for (_l, ix, _y) in targets]
    tys = [iy for (_l, _x, iy) in targets]
    bx1, bx2 = min(txs), max(txs)
    by1, by2 = min(tys), max(tys)

    def _h(ix: int, iy: int) -> float:
        dx = max(0, bx1 - ix, ix - bx2)
        dy = max(0, by1 - iy, iy - by2)
        if opt.allow_diagonal:
            # Octile distance: admissible for unit orthogonal / sqrt(2)
            # diagonal movement.
            return max(dx, dy) + (math.sqrt(2.0) - 1.0) * min(dx, dy)
        return dx + dy

    counter = itertools.count()
    open_heap: list[tuple[float, float, int,
                          tuple[int, int, int, int]]] = []
    best: dict[tuple[int, int, int, int], float] = {}
    parent: dict[tuple[int, int, int, int],
                 tuple[int, int, int, int] | None] = {}

    for (li, ix, iy) in sorted(set(starts)):
        if not problem.passable(li, ix, iy, net):
            continue
        st = (li, ix, iy, _DIR_NONE)
        best[st] = 0.0
        parent[st] = None
        heapq.heappush(open_heap, (_h(ix, iy), 0.0, next(counter), st))

    n_layers = len(problem.layers)
    expansions = 0
    while open_heap and expansions < opt.max_expansions:
        f, g, _, st = heapq.heappop(open_heap)
        if g > best.get(st, math.inf) + _EPS:
            continue
        li, ix, iy, d = st
        if (li, ix, iy) in targets:
            return _reconstruct(parent, st), False, expansions
        expansions += 1

        directions = _DIRS + _DIAGONAL_DIRS if opt.allow_diagonal else _DIRS
        for di, (dx, dy) in enumerate(directions):
            jx, jy = ix + dx, iy + dy
            if not problem.passable(li, jx, jy, net):
                continue
            if dx and dy and (
                not problem.passable(li, ix + dx, iy, net)
                or not problem.passable(li, ix, iy + dy, net)
            ):
                continue
            ng = g + (math.sqrt(2.0) if dx and dy else 1.0)
            if d != _DIR_NONE and d != di:
                ng += opt.bend_penalty
            nst = (li, jx, jy, di)
            if ng < best.get(nst, math.inf) - _EPS:
                best[nst] = ng
                parent[nst] = st
                heapq.heappush(
                    open_heap, (ng + _h(jx, jy), ng, next(counter), nst))

        if n_layers > 1 and problem.via_ok(
                ix, iy, net, allow_in_pad=opt.allow_via_in_pad):
            for l2 in range(n_layers):
                if l2 == li:
                    continue
                ng = g + opt.via_cost
                nst = (l2, ix, iy, _DIR_NONE)
                if ng < best.get(nst, math.inf) - _EPS:
                    best[nst] = ng
                    parent[nst] = st
                    heapq.heappush(
                        open_heap, (ng + _h(ix, iy), ng, next(counter), nst))
    # Frontier still non-empty => we stopped on the budget, not on
    # having exhausted the reachable set.
    return (None,
            bool(open_heap) and expansions >= opt.max_expansions,
            expansions)


def _astar_native(native, problem: RoutingProblem, net: str,
                  starts: list[tuple[int, int, int]],
                  targets: set[tuple[int, int, int]],
                  opt: RouterOptions,
                  ) -> tuple[list[tuple[int, int, int]] | None, bool, int]:
    """:func:`_astar` in the compiled kernel (eda_agent.compute).

    Same contract, same answer: the kernel is a line-for-line port and
    tests/route/test_compute_parity.py holds it to that.
    """
    d = problem.dense_grid()
    path, exhausted, used = native.astar(
        problem.nx, problem.ny, len(problem.layers),
        d.blocked, d.via_blocked, d.pad_any, d.net_id(net),
        sorted(set(starts)), list(targets),
        bool(opt.allow_diagonal), float(opt.bend_penalty),
        float(opt.via_cost), bool(opt.allow_via_in_pad),
        int(opt.max_expansions))
    return path, exhausted, used


def _reconstruct(parent: dict, last: tuple[int, int, int, int]
                 ) -> list[tuple[int, int, int]]:
    cells: list[tuple[int, int, int]] = []
    st: tuple[int, int, int, int] | None = last
    while st is not None:
        cells.append((st[0], st[1], st[2]))
        st = parent[st]
    cells.reverse()
    # Direction-only state changes repeat the same cell; collapse them.
    out: list[tuple[int, int, int]] = []
    for c in cells:
        if not out or out[-1] != c:
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# Emission (grid path -> track / via dicts)
# ---------------------------------------------------------------------------


def _chamfer(problem: RoutingProblem, net: str, li: int,
             cells: list[tuple[int, int]],
             keep: set[tuple[int, int]] | None = None,
             ) -> list[tuple[int, int]]:
    """Cut every right-angle corner in a same-layer run into two 45s.

    A right angle in signal copper is the first thing a reviewer picks
    up and the standard house rule on most boards. The search runs on a
    4-direction grid and cannot produce anything else, so the corners
    are cut here, after the fact, where doing it cannot change which
    nets route. Putting the diagonals in the search itself was tried and
    measured: it reroutes earlier nets and strands later ones.

    VERIFIED AGAINST THE SAME OBSTACLE MAP the search used. The diagonal
    crosses cells that neither leg occupies, so every one is checked,
    and so are the two cells beside each diagonal step, which the
    centreline passes between. A corner whose chamfer would clip a pad
    keeps its right angle, which is correct and visible rather than
    clever and shorted.

    NEVER CUTS A CELL IN ``keep``. A later branch of the same net is
    routed into any cell of the tree, so it can end on a corner or on a
    leg cell the chamfer would delete, and its track would then stop
    where there is no copper. The caller passes every cell something
    else joins at (branch ends, vias, pad cells); a cut that would
    remove one falls back to a shallower cut, or none.

    Cuts as deep as it can, never taking more than half of either leg,
    so a short jog keeps its shape and a long run gets a long chamfer.
    """
    if len(cells) < 3:
        return cells
    keep = keep or set()

    def _unit(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int]:
        return ((b[0] > a[0]) - (b[0] < a[0]),
                (b[1] > a[1]) - (b[1] < a[1]))

    out: list[tuple[int, int]] = [cells[0]]
    i = 1
    while i < len(cells) - 1:
        corner = cells[i]
        din = _unit(cells[i - 1], corner)
        dout = _unit(corner, cells[i + 1])
        # A right angle only: straight runs and reversals are left alone.
        if din == (0, 0) or dout == (0, 0) or (
                din[0] * dout[0] + din[1] * dout[1]) != 0:
            out.append(corner)
            i += 1
            continue

        # How far the legs run, in cells, either side of the corner.
        back = 0
        while back < len(out) and _unit(out[-1 - back], corner) == din:
            back += 1
        fwd = 0
        while (i + fwd + 1 < len(cells)
               and _unit(cells[i + fwd], cells[i + fwd + 1]) == dout):
            fwd += 1

        depth = min(_MAX_CHAMFER_CELLS, back // 2, fwd // 2)
        step = (din[0] + dout[0], din[1] + dout[1])
        applied = 0
        for d in range(depth, 0, -1):
            if _cut_is_clear(problem, net, li, corner, din, dout, d, keep):
                applied = d
                break

        if not applied:
            out.append(corner)
            i += 1
            continue

        # Drop the cells between the cut point and the corner; they were
        # already emitted on the way in.
        for _ in range(applied - 1):
            out.pop()
        a = out[-1]
        for k in range(1, applied + 1):
            out.append((a[0] + step[0] * k, a[1] + step[1] * k))
        # Resume past the outgoing cells the chamfer replaced.
        i += applied

    if out[-1] != cells[-1]:
        out.append(cells[-1])
    return out


def _cut_is_clear(problem: RoutingProblem, net: str, li: int,
                  corner: tuple[int, int], din: tuple[int, int],
                  dout: tuple[int, int], d: int,
                  keep: set[tuple[int, int]]) -> bool:
    """True if a chamfer of depth ``d`` at ``corner`` is safe to cut.

    The cut removes the corner and the ``d - 1`` cells either side of it
    on each leg, so none of those may be a cell something else joins
    at. The new diagonal must sit on passable cells, and so must the two
    cells beside each diagonal step: the centreline passes between them,
    and checking only the grid points it lands on would let it clip the
    corner of an obstacle in between.
    """
    if corner in keep:
        return False
    for k in range(1, d):
        if ((corner[0] - din[0] * k, corner[1] - din[1] * k) in keep
                or (corner[0] + dout[0] * k,
                    corner[1] + dout[1] * k) in keep):
            return False
    sx, sy = din[0] + dout[0], din[1] + dout[1]
    px, py = corner[0] - din[0] * d, corner[1] - din[1] * d
    for _ in range(d):
        for cx, cy in ((px + sx, py + sy), (px + sx, py), (px, py + sy)):
            if not problem.passable(li, cx, cy, net):
                return False
        px, py = px + sx, py + sy
    return True


def _emit(problem: RoutingProblem, net: str, width: int,
          paths: list[list[tuple[int, int, int]]],
          terms: list[Terminal],
          tree_xy: dict[tuple[int, int], set[int]],
          ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tracks: list[dict[str, Any]] = []
    vias: list[dict[str, Any]] = []
    via_at: set[tuple[int, int]] = set()

    # Cells the chamfer must leave in place, per layer: where one path
    # meets another. Each path is cut on its own, but a later branch was
    # routed into any cell of the tree, so its end can sit on a corner of
    # an earlier path; cutting that corner leaves the branch dangling.
    # Vias and pad cells join copper the same way.
    keep: list[set[tuple[int, int]]] = [set() for _ in problem.layers]
    for path in paths:
        for (li, ix, iy) in (path[0], path[-1]):
            keep[li].add((ix, iy))
        for (la, ix, iy), (lb, _x, _y) in zip(path, path[1:]):
            if la != lb:
                for layer_keep in keep:
                    layer_keep.add((ix, iy))
    for t in terms:
        for li in t.layers:
            keep[li].add(t.cell)

    def _track(x1: int, y1: int, x2: int, y2: int, layer: str) -> None:
        if (x1, y1) == (x2, y2):
            return
        tracks.append({
            "x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2),
            "width": int(width), "layer": layer, "net_name": net,
        })

    def _flush_cells(cells: list[tuple[int, int]], layer_idx: int) -> None:
        """Chamfer the run's corners, then emit it as track segments.

        Done on the CELLS rather than on the emitted segments: the
        chamfer has to check the obstacle map, and that is indexed by
        cell.
        """
        if len(cells) < 2:
            return
        points: list[tuple[int, int]] = []
        for ix, iy in _chamfer(problem, net, layer_idx, cells,
                               keep[layer_idx]):
            pt = problem.cell_center(ix, iy)
            if not points or points[-1] != pt:
                points.append(pt)
        _flush_run(points, problem.layers[layer_idx], _track)

    for path in paths:
        run_cells: list[tuple[int, int]] = []
        run_layer = path[0][0]
        for (li, ix, iy) in path:
            if li != run_layer:
                _flush_cells(run_cells, run_layer)
                pt = problem.cell_center(ix, iy)
                if pt not in via_at:
                    via_at.add(pt)
                    vias.append({
                        "x": pt[0], "y": pt[1], "net": net,
                        "size": int(problem.rules.via_size_mils),
                        "hole_size": int(problem.rules.via_drill_mils),
                    })
                run_cells = [(ix, iy)]
                run_layer = li
            else:
                if not run_cells or run_cells[-1] != (ix, iy):
                    run_cells.append((ix, iy))
        _flush_cells(run_cells, run_layer)

    # Stub from each exact pad center to its snapped grid point, on a
    # layer where the pad copper and the routed tree coincide.
    for t in terms:
        gx, gy = problem.cell_center(*t.cell)
        if (gx, gy) == (t.x, t.y):
            continue
        usable = sorted(set(t.layers) & tree_xy.get(t.cell, set()))
        li = usable[0] if usable else t.layers[0]
        layer = problem.layers[li]
        # Two-segment Manhattan dogleg when the offset is diagonal.
        if gx != t.x and gy != t.y:
            _track(t.x, t.y, gx, t.y, layer)
            _track(gx, t.y, gx, gy, layer)
        else:
            _track(t.x, t.y, gx, gy, layer)
    return tracks, vias


def _flush_run(run: list[tuple[int, int]], layer: str, track_fn) -> None:
    """Collapse a same-layer polyline of grid points into collinear
    track segments."""
    if len(run) < 2:
        return
    sx, sy = run[0]
    px, py = run[0]
    dirv: tuple[int, int] | None = None
    for (x, y) in run[1:]:
        d = ((x > px) - (x < px), (y > py) - (y < py))
        if dirv is None:
            dirv = d
        elif d != dirv:
            track_fn(sx, sy, px, py, layer)
            sx, sy = px, py
            dirv = d
        px, py = x, y
    track_fn(sx, sy, px, py, layer)


__all__ = [
    "RouterOptions",
    "net_islands",
    "route_geometry",
    "route_problem",
    "validate_solution",
]
