# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Exact clearance checks against copper that grows as stages lay it.

The grid answers "may a track centre sit on this cell"; copper laid off
the grid (a dog-bone, a pair's offset tracks, a bump, a chamfer) has to be
judged on its real shape. The router's own check (``_static_clears``)
gathers every fixed shape once and is rebuilt from scratch when the copper
changes, which is fine for a finished grid and far too slow for a stage
that adds a via per pad and checks the next one against it.

``CopperIndex`` files shapes in square buckets per layer, so a check
looks only at what is near, and shapes can be added and taken out as a
stage goes. Owners are the grid's: net ids, ``NETLESS``, ``EDGE`` and
``KEEPOUT``.
"""

from __future__ import annotations

import math

from .. import geom
from .grid import EDGE, KEEPOUT

EPS = 1e-3


class CopperIndex:
    """Shapes per routing layer, bucketed by their boxes."""

    def __init__(self, n_layers: int, bucket: float = 50.0):
        self.L = n_layers
        self.bucket = bucket
        self.items: list = []            # (layer, owner, shape, tag) or None once removed
        self.cells: dict = {}            # (layer, bx, by) -> [item ids]
        self.tags: dict = {}             # tag -> [item ids]

    @classmethod
    def from_grid(cls, grid, bucket: float = 50.0) -> "CopperIndex":
        """Every fixed shape the grid was built from, keep-outs and the
        board edge included (the edge on every layer)."""
        idx = cls(len(grid.layers), bucket)
        for li, owner, s in grid.shapes:
            idx.add(li, owner, s)
        for s in grid.edge_shapes:
            for li in range(len(grid.layers)):
                idx.add(li, EDGE, s)
        return idx

    def _keys(self, li: int, box, grow: float = 0.0):
        b = self.bucket
        x0, y0, x1, y1 = box
        for bx in range(int(math.floor((x0 - grow) / b)), int(math.floor((x1 + grow) / b)) + 1):
            for by in range(int(math.floor((y0 - grow) / b)), int(math.floor((y1 + grow) / b)) + 1):
                yield (li, bx, by)

    def add(self, li: int, owner: int, shape: geom.Shape, tag=None) -> int:
        k = len(self.items)
        self.items.append((li, owner, shape, tag))
        for key in self._keys(li, shape.bbox):
            self.cells.setdefault(key, []).append(k)
        if tag is not None:
            self.tags.setdefault(tag, []).append(k)
        return k

    def remove_tag(self, tag) -> list:
        """Take out every shape added with ``tag``; returns them as
        (layer, owner, shape)."""
        out = []
        for k in self.tags.pop(tag, []):
            it = self.items[k]
            if it is not None:
                out.append(it[:3])
                self.items[k] = None
        return out

    def near(self, li: int, box, reach: float):
        """Live items on layer ``li`` whose box comes within ``reach`` of
        ``box``: (owner, shape, tag)."""
        seen = set()
        x0, y0, x1, y1 = box
        for key in self._keys(li, box, reach):
            for k in self.cells.get(key, ()):
                if k in seen:
                    continue
                seen.add(k)
                it = self.items[k]
                if it is None:
                    continue
                bx0, by0, bx1, by1 = it[2].bbox
                if bx0 > x1 + reach or bx1 < x0 - reach or by0 > y1 + reach or by1 < y0 - reach:
                    continue
                yield it[1], it[2], it[3]

    def clears(self, shape: geom.Shape, li: int, mine, clearance, edge: float = 0.0,
               reach: float = 0.0, ignore_tags=()) -> bool:
        """Whether ``shape`` on layer ``li`` keeps ``clearance(owner)``
        from every shape whose owner is not in ``mine``, overlaps no
        keep-out and keeps ``edge`` from the board edge. ``reach`` is the
        largest clearance asked, so the search goes no further."""
        for owner, s, tag in self.near(li, shape.bbox, max(reach, edge) + EPS):
            if owner in mine or (tag is not None and tag in ignore_tags):
                continue
            gap = geom.clearance(shape, s)
            if owner == KEEPOUT:
                if gap < 0.0:
                    return False
            elif owner == EDGE:
                if gap < edge - EPS:
                    return False
            elif gap < clearance(owner) - EPS:
                return False
        return True

    def blockers(self, shape: geom.Shape, li: int, mine, clearance, edge: float = 0.0,
                 reach: float = 0.0) -> list:
        """The (owner, tag) of every shape ``shape`` would come too close
        to; empty when it clears."""
        out = []
        for owner, s, tag in self.near(li, shape.bbox, max(reach, edge) + EPS):
            if owner in mine:
                continue
            gap = geom.clearance(shape, s)
            if owner == KEEPOUT:
                bad = gap < 0.0
            elif owner == EDGE:
                bad = gap < edge - EPS
            else:
                bad = gap < clearance(owner) - EPS
            if bad:
                out.append((owner, tag))
        return out


def turn(a, b, c) -> float:
    """The change of direction at ``b`` going from ``a`` to ``c``, in
    degrees: 0 straight on, 180 straight back."""
    ux, uy = b[0] - a[0], b[1] - a[1]
    vx, vy = c[0] - b[0], c[1] - b[1]
    lu, lv = math.hypot(ux, uy), math.hypot(vx, vy)
    if lu < 1e-9 or lv < 1e-9:
        return 0.0
    cos = max(-1.0, min(1.0, (ux * vx + uy * vy) / (lu * lv)))
    return math.degrees(math.acos(cos))


#: A bend counts as sharper than 45 degrees past this much more.
BEND_TOL = 0.5


def sharp_bends(tracks, pads=(), vias=(), limit: float = 45.0) -> list:
    """Where two tracks of one net on one layer meet end to end and turn
    by more than ``limit`` degrees: [(net, layer, x, y, degrees)].

    Only a point where exactly two track ends of the net meet is a bend: a
    third end there, or another track passing through, is a junction. A
    point inside the net's own pad or via copper is not a bend either: the
    turn is under copper that is there anyway.
    """
    ends: dict = {}
    for k, t in enumerate(tracks):
        if t.keepout or t.comp:
            continue
        for (x, y), (ox, oy) in (((t.x1, t.y1), (t.x2, t.y2)), ((t.x2, t.y2), (t.x1, t.y1))):
            key = (t.net, t.layer, round(x, 3), round(y, 3))
            ends.setdefault(key, []).append((k, (ox, oy)))
    covers: dict = {}
    for p in pads:
        for c in p.copper:
            covers.setdefault((p.net, c.layer), []).append(p.shape_on(c.layer))
    for v in vias:
        covers.setdefault((v.net, "*"), []).append(v.shape_on(""))
    out = []
    for (net, layer, x, y), ee in ends.items():
        if len(ee) != 2 or ee[0][0] == ee[1][0]:
            continue
        a, c = ee[0][1], ee[1][1]
        deg = turn(a, (x, y), c)
        if deg <= limit + BEND_TOL:
            continue
        point = geom.circle(x, y, 0.0)
        under = covers.get((net, layer), []) + covers.get((net, "*"), [])
        if any(geom.clearance(point, s) <= 1e-6 for s in under if s is not None):
            continue
        out.append((net, layer, x, y, round(deg, 1)))
    return out
