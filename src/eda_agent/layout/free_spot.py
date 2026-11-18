# SPDX-License-Identifier: Apache-2.0
"""Free spots on a board: where a via or a part fits, and what is close.

A courtyard search answers "which parts are in the way" and nothing
else, so a via it called free could land on a track, a keepout or a
polygon cutout drawn to keep that area clear. Here every object of a
LayoutBoard that can be in the way is an obstacle on the layers it
occupies:

* pads (their copper on each layer) and every drilled hole, on all layers;
* vias, on the layers they span;
* tracks and arcs, keepout tracks and arcs (on the Keep-Out layer: every
  layer);
* solid regions, fills, poured copper (unless ``ignore_pours``), polygon
  cutouts and keepout regions;
* the board edge and board cutouts, at ``edge_clearance``;
* component courtyards, when asked (``courtyards``), on their own side.

The candidate is a circle (a via, ``diameter``) or a rectangle (a part,
``width`` x ``height``) set on a grid, nearest the ``near`` point first,
and it is free when every obstacle on the chosen layers is at least
``clearance`` away (a courtyard: not overlapped). Copper of ``net`` does
not block, so a ground via may sit in the ground pour.

Pure geometry on ``geom``'s exact shapes, mils throughout; the tool that
reads the board and converts units is ``pcb_find_free_spot``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from . import geom
from .model import BOTTOM, TOP, LayoutBoard

KEEPOUT_LAYER = "KeepOutLayer"
ALL = "*"

#: Obstacle kinds that are copper, so copper of the candidate's own net
#: does not block.
COPPER_KINDS = frozenset({"pad", "via", "track", "arc", "region", "fill", "pour"})

#: Polygons with more vertices than this are indexed (geom.PolyIndex).
_BIG_POLY = 48


@dataclass
class Obstacle:
    kind: str           # pad | hole | via | track | arc | region | fill | pour
    #                     | polygon_cutout | keepout | courtyard
    layer: str          # a copper layer, or "*" for every layer
    shape: geom.Shape
    net: str = ""
    ref: str = ""       # what it is on the board: U1.3, R5, polygon:GND
    index: Optional[geom.PolyIndex] = None
    box: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        self.box = self.shape.bbox  # a pour's runs over every vertex: once

    def near(self, box: tuple[float, float, float, float], gap: float) -> bool:
        x0, y0, x1, y1 = self.box
        return not (x1 + gap < box[0] or box[2] + gap < x0
                    or y1 + gap < box[1] or box[3] + gap < y0)

    def core_distance(self, other: geom.Shape, cutoff: float) -> float:
        if self.index is not None:
            return self.index.core_distance(other, cutoff)
        return geom.core_distance(self.shape, other)

    def gap(self, other: geom.Shape, cutoff: float = math.inf) -> float:
        return self.core_distance(other, cutoff + other.r + self.shape.r) - other.r - self.shape.r

    def describe(self, gap: float) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "layer": "all" if self.layer == ALL else self.layer}
        if self.ref:
            out["ref"] = self.ref
        if self.net:
            out["net"] = self.net
        out["gap"] = round(gap, 3)
        return out


def resolve_layers(board: LayoutBoard, layers: Optional[Iterable[str]],
                   default: str) -> list[str]:
    """Copper layer names for ``layers``; "top", "bottom" and "all" are
    understood, case and spaces do not matter. Raises ValueError on a
    name the board does not have."""
    copper = board.copper_layers() or ["TopLayer", "BottomLayer"]
    by_key = {c.lower().replace(" ", ""): c for c in copper}
    asked = list(layers or [default])
    out: list[str] = []
    for name in asked:
        key = str(name).lower().replace(" ", "")
        if key in ("all", "*", "through", "multilayer"):
            pick = copper
        elif key in ("top", "toplayer"):
            pick = [by_key.get("toplayer", copper[0])]
        elif key in ("bottom", "bottomlayer"):
            pick = [by_key.get("bottomlayer", copper[-1])]
        elif key in by_key:
            pick = [by_key[key]]
        else:
            raise ValueError(f"no copper layer {name!r}; the board has {', '.join(copper)}")
        out.extend(p for p in pick if p not in out)
    return out


def _poly_obstacle(kind, layer, pts, holes, net, ref) -> Optional[Obstacle]:
    if len(pts) < 3:
        return None
    shape = geom.polygon(pts, holes)
    big = len(shape.pts) + sum(len(h) for h in shape.holes) > _BIG_POLY
    return Obstacle(kind, layer, shape, net, ref, geom.PolyIndex(shape) if big else None)


def collect_obstacles(board: LayoutBoard, layers: list[str], *, net: str = "",
                      ignore_pours: bool = False, courtyards: bool = False) -> list[Obstacle]:
    """Everything on ``layers`` (or on every layer) that a new object must
    keep clear of. The board edge is not here: see ``edge_gap``."""
    want = set(layers)
    out: list[Obstacle] = []

    def add(ob: Optional[Obstacle]) -> None:
        if ob is None:
            return
        if net and ob.net == net and ob.kind in COPPER_KINDS:
            return
        out.append(ob)

    for p in board.pads:
        ref = p.key
        for c in p.copper:
            if c.layer in want:
                shape = p.shape_on(c.layer)
                if shape is not None:
                    add(Obstacle("pad", c.layer, shape, p.net, ref))
        if p.hole > 0:
            slot = p.hole_type == "slot" and p.hole_width > p.hole
            hole = (geom.pad_shape(p.x, p.y, p.hole_width, p.hole, "round",
                                   p.rotation + p.hole_rotation) if slot
                    else geom.circle(p.x, p.y, p.hole))
            add(Obstacle("hole", ALL, hole, p.net, ref))

    for v in board.vias:
        span = board.layers_between(v.low_layer, v.high_layer) if v.low_layer and v.high_layer \
            else board.copper_layers()
        for layer in span:
            if layer in want:
                add(Obstacle("via", layer, v.shape_on(layer), v.net,
                             f"via@{v.x:.2f},{v.y:.2f}"))
        if v.hole > 0:
            add(Obstacle("hole", ALL, geom.circle(v.x, v.y, v.hole), v.net,
                         f"via@{v.x:.2f},{v.y:.2f}"))

    for t in board.tracks:
        if t.layer == KEEPOUT_LAYER:
            add(Obstacle("keepout", ALL, t.shape(), "", t.comp))
        elif t.layer in want:
            add(Obstacle("keepout" if t.keepout else "track", t.layer, t.shape(),
                         "" if t.keepout else t.net, t.comp))

    for a in board.arcs:
        if a.layer == KEEPOUT_LAYER:
            layer, kind = ALL, "keepout"
        elif a.layer in want:
            layer, kind = a.layer, ("keepout" if a.keepout else "arc")
        else:
            continue
        for s in a.shapes():
            add(Obstacle(kind, layer, s, "" if kind == "keepout" else a.net, a.comp))

    for r in board.regions:
        if r.kind == "pour_boundary":
            continue  # the outline a polygon pours inside; its copper is "pour"
        if r.layer == KEEPOUT_LAYER:
            layer = ALL
        elif r.layer in want:
            layer = r.layer
        else:
            continue
        if r.kind == "keepout" or r.keepout:
            kind = "keepout"
        elif r.kind == "cutout":
            kind = "polygon_cutout"
        elif r.kind == "pour":
            if ignore_pours:
                continue
            kind = "pour"
        else:
            kind = "fill" if r.source == "fill" else "region"
        ref = r.source if r.source.startswith("polygon:") and r.source != "polygon:" else r.comp
        add(_poly_obstacle(kind, layer, r.outline, r.holes,
                           "" if kind in ("keepout", "polygon_cutout") else r.net, ref))

    if courtyards:
        sides = set()
        copper = board.copper_layers()
        for layer in want:
            if copper and layer == copper[0]:
                sides.add(TOP)
            elif copper and layer == copper[-1]:
                sides.add(BOTTOM)
        for c in board.components:
            if c.side in sides and len(c.courtyard) >= 3:
                add(_poly_obstacle("courtyard", ALL, c.courtyard, [], "", c.ref))
    return out


def edge_gap(shape: geom.Shape, outline: Optional[geom.Shape]) -> float:
    """Gap from ``shape`` to the board edge or a board cutout; negative
    when it crosses or lies outside. Infinite with no outline."""
    if outline is None:
        return math.inf
    return geom.shape_to_outline_clearance(shape, outline)


def candidate(x: float, y: float, diameter: float, width: float, height: float) -> geom.Shape:
    if diameter > 0:
        return geom.circle(x, y, diameter)
    return geom.pad_shape(x, y, width, height, "rect")


class _Bins:
    """Obstacles bucketed by bounding box, for the ones near a point."""

    def __init__(self, obstacles: list[Obstacle], cell: float):
        self.cell = cell
        self.bins: dict[tuple[int, int], list[int]] = {}
        self.big: list[int] = []
        self.obstacles = obstacles
        for i, ob in enumerate(obstacles):
            x0, y0, x1, y1 = ob.box
            gx0, gy0 = int(math.floor(x0 / cell)), int(math.floor(y0 / cell))
            gx1, gy1 = int(math.floor(x1 / cell)), int(math.floor(y1 / cell))
            if (gx1 - gx0 + 1) * (gy1 - gy0 + 1) > 256:
                self.big.append(i)
                continue
            for gx in range(gx0, gx1 + 1):
                for gy in range(gy0, gy1 + 1):
                    self.bins.setdefault((gx, gy), []).append(i)

    def near(self, box: tuple[float, float, float, float]) -> list[Obstacle]:
        x0, y0, x1, y1 = box
        c = self.cell
        seen: set[int] = set()
        found: list[Obstacle] = []
        for gx in range(int(math.floor(x0 / c)), int(math.floor(x1 / c)) + 1):
            for gy in range(int(math.floor(y0 / c)), int(math.floor(y1 / c)) + 1):
                for i in self.bins.get((gx, gy), ()):
                    if i not in seen:
                        seen.add(i)
                        found.append(self.obstacles[i])
        for i in self.big:
            bx0, by0, bx1, by1 = self.obstacles[i].box
            if not (bx1 < x0 or x1 < bx0 or by1 < y0 or y1 < by0):
                found.append(self.obstacles[i])
        return found


def _grow(box, d):
    return (box[0] - d, box[1] - d, box[2] + d, box[3] + d)


def find_free_spots(board: LayoutBoard, *, diameter: float = 0.0, width: float = 0.0,
                    height: float = 0.0, clearance: float = 10.0,
                    edge_clearance: Optional[float] = None,
                    layers: Optional[Iterable[str]] = None,
                    near: Optional[tuple[float, float]] = None,
                    area: Optional[tuple[float, float, float, float]] = None,
                    grid: Optional[float] = None, spacing: Optional[float] = None,
                    count: int = 10, net: str = "", ignore_pours: bool = False,
                    courtyards: Optional[bool] = None, max_points: int = 250_000,
                    max_checks: int = 60_000, report_nearby: int = 3) -> dict[str, Any]:
    """Free spots for a via (``diameter``) or a part (``width`` x ``height``).

    Returns ``{"ok", "candidates": [{x, y, distance, gap, nearby: [...]}],
    "near_blocked_by", "blocked_by", ...}``, mils throughout, candidates
    nearest ``near`` (else the search area's centre) first.
    """
    if diameter <= 0 and (width <= 0 or height <= 0):
        return {"ok": False, "error": "give diameter (a via) or width and height (a part)"}
    if clearance < 0:
        return {"ok": False, "error": "clearance must not be negative"}
    is_via = diameter > 0
    try:
        target = resolve_layers(board, layers, "all" if is_via else "top")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if courtyards is None:
        courtyards = not is_via
    edge = clearance if edge_clearance is None else edge_clearance
    size = diameter if is_via else max(width, height)

    outline = board.outline_shape() if len(board.outline) >= 3 else None
    if area is not None:
        x0, y0, x1, y1 = area
        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    elif outline is not None:
        box = outline.bbox
    elif near is not None:
        r = 20 * (size + clearance)
        box = (near[0] - r, near[1] - r, near[0] + r, near[1] + r)
    else:
        return {"ok": False, "error": "the board has no outline: give a search area or a near point"}
    if box[2] <= box[0] or box[3] <= box[1]:
        return {"ok": False, "error": "the search area is empty"}
    step = grid if grid and grid > 0 else max(1.0, round((size + clearance) / 4.0, 3))
    nx = int(math.floor((box[2] - box[0]) / step)) + 1
    ny = int(math.floor((box[3] - box[1]) / step)) + 1
    coarsened = False
    while nx * ny > max_points:
        step *= 1.5
        coarsened = True
        nx = int(math.floor((box[2] - box[0]) / step)) + 1
        ny = int(math.floor((box[3] - box[1]) / step)) + 1
    apart = spacing if spacing is not None and spacing >= 0 else size + clearance
    origin = near if near is not None else ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)

    obstacles = collect_obstacles(board, target, net=net, ignore_pours=ignore_pours,
                                  courtyards=courtyards)
    need_max = max(clearance, edge, 0.0)
    bins = _Bins(obstacles, cell=max(25.0, 2.0 * (size + need_max)))

    def needed(ob: Obstacle) -> float:
        return 0.0 if ob.kind == "courtyard" else clearance

    def blocks(ob: Obstacle, g: float) -> bool:
        # Two polygons that overlap measure 0 apart, never less, so a
        # courtyard (kept to no gap at all) blocks at 0: touching counts.
        if ob.kind == "courtyard":
            return g <= 1e-9
        return g < clearance - 1e-9

    def first_blocker(shape: geom.Shape) -> Optional[tuple[str, Obstacle | None, float]]:
        g = edge_gap(shape, outline)
        if g < edge - 1e-9:
            return ("board_edge", None, g)
        sbox = shape.bbox
        for ob in bins.near(_grow(sbox, need_max)):
            need = needed(ob)
            if not ob.near(sbox, need):
                continue
            g = ob.gap(shape, need)
            if blocks(ob, g):
                return (ob.kind, ob, g)
        return None

    def nearby(shape: geom.Shape, reach: float, only_blocking: bool = False) -> list[dict]:
        rows = []
        g = edge_gap(shape, outline)
        if g <= reach and (not only_blocking or g < edge - 1e-9):
            rows.append({"kind": "board_edge", "layer": "all", "gap": round(g, 3)})
        sbox = shape.bbox
        for ob in bins.near(_grow(sbox, reach)):
            if not ob.near(sbox, reach):
                continue
            g = ob.gap(shape, reach)
            if g > reach or (only_blocking and not blocks(ob, g)):
                continue
            rows.append(ob.describe(g))
        rows.sort(key=lambda r: r["gap"])
        # One line per object: a pad seen on three layers is one pad.
        seen, out = set(), []
        for r in rows:
            key = (r["kind"], r.get("ref", ""), r.get("net", ""), r["layer"]
                   if r["kind"] not in ("pad", "via") else "")
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        return out

    points = [(box[0] + i * step, box[1] + j * step) for i in range(nx) for j in range(ny)]
    points.sort(key=lambda p: (p[0] - origin[0]) ** 2 + (p[1] - origin[1]) ** 2)

    found: list[dict[str, Any]] = []
    blocked_by: dict[str, int] = {}
    checks = 0
    stopped = False
    for px, py in points:
        if len(found) >= count:
            break
        if any(math.hypot(px - f["x"], py - f["y"]) < apart - 1e-9 for f in found):
            continue
        if checks >= max_checks:
            stopped = True
            break
        checks += 1
        shape = candidate(px, py, diameter, width, height)
        hit = first_blocker(shape)
        if hit is not None:
            blocked_by[hit[0]] = blocked_by.get(hit[0], 0) + 1
            continue
        reach = max(need_max, 0.0) + max(size, step) * 1.5
        close = nearby(shape, reach)[:max(0, report_nearby)]
        gap = min([r["gap"] for r in close] or [math.inf])
        row: dict[str, Any] = {"x": round(px, 3), "y": round(py, 3),
                               "distance": round(math.hypot(px - origin[0], py - origin[1]), 3)}
        if gap != math.inf:
            row["gap"] = gap
        row["nearby"] = close
        found.append(row)

    out: dict[str, Any] = {
        "ok": True,
        "object": "via" if is_via else "part",
        "layers": target,
        "clearance": clearance, "edge_clearance": edge,
        "grid": round(step, 3), "spacing": apart,
        "bounds": {"xmin": round(box[0], 3), "ymin": round(box[1], 3),
                   "xmax": round(box[2], 3), "ymax": round(box[3], 3)},
        "candidates": found,
        "checked": checks,
        # Lists, not {kind: count}: "hole" is a length name to the unit
        # conversion, and a count of holes must not turn into millimetres.
        "blocked_by": [{"kind": k, "count": n}
                       for k, n in sorted(blocked_by.items(), key=lambda kv: -kv[1])],
        "obstacles": _tally(obstacles),
        "courtyards": courtyards, "ignore_pours": ignore_pours,
    }
    if is_via:
        out["diameter"] = diameter
    else:
        out["width"], out["height"] = width, height
    if net:
        out["net"] = net
    if near is not None:
        out["near"] = {"x": near[0], "y": near[1]}
        here = candidate(near[0], near[1], diameter, width, height)
        out["near_free"] = first_blocker(here) is None
        if not out["near_free"]:
            out["near_blocked_by"] = nearby(here, need_max, only_blocking=True)[:8]
    if coarsened:
        out["note"] = (f"the grid was widened to {round(step, 3)} to keep the search to "
                       f"{max_points} points; give a smaller area for a finer one")
    if stopped:
        out["note"] = (out.get("note", "") + "; " if out.get("note") else "") + \
            f"stopped after {max_checks} checks; give a smaller search box"
    if not found:
        out["reason"] = ("no free spot found in the points checked" if stopped
                         else "no free spot in the search area on these layers")
        if blocked_by and max(blocked_by, key=blocked_by.get) == "pour" and not ignore_pours:
            out["reason"] += ("; poured copper rejected most points: give the new object's "
                              "net, or ignore_pours to let the pour flow round it")
    return out


def _tally(obstacles: list[Obstacle]) -> list[dict[str, Any]]:
    out: dict[str, int] = {}
    for ob in obstacles:
        out[ob.kind] = out.get(ob.kind, 0) + 1
    return [{"kind": k, "count": n} for k, n in sorted(out.items())]
