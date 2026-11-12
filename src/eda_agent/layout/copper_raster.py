# SPDX-License-Identifier: Apache-2.0
"""A board's copper on a regular grid: fill fraction per cell per layer.

The field solvers that work on a whole board (thermal spreading, PDN
IR drop) do not need every edge of every pour; they need, for each copper
layer, how much of each grid cell is copper, and where the plated barrels
that join the layers are. This module computes exactly that from a
``LayoutBoard``:

* ``fill[layer]``: a ``(ny, nx)`` float array, 0..1, the area fraction of
  each cell covered by copper on that layer. Tracks, arcs, pads, vias,
  regions, pours and split planes are painted as their real shapes
  (``layout.geom``) on a supersampled grid and averaged down, so a cell
  half covered by a track reads 0.5 to within 1/supersample^2.
* ``mask``: the area fraction of each cell inside the board outline and
  outside its cutouts.
* ``barrels``: every plated hole (vias and plated through-hole pads) with
  its cell, the copper layers it joins, its hole and its net.

Plane layers. A plane layer in Altium is negative: copper everywhere the
split planes say, less an antipad round every hole of another net. A plane
layer with a plane net and no split-plane objects is solid inside the
outline. Antipads are cut as circles of the hole radius plus the plane
clearance (``plane_clearance_mils``, default from a "Power Plane
Clearance" rule on the board, else 10 mil). Thermal-relief spokes are not
drawn: a same-net hole is taken as fully connected, which is what a
thermal or IR-drop model wants to within the spokes' width.

Net filter. ``nets`` keeps only copper of those nets (the IR-drop grid of
one rail); without it every net is painted, netless copper included,
which is what heat spreading sees.

Units are mils. Cell ``(iy, ix)`` spans ``x0 + ix*cell .. x0 + (ix+1)*cell``
and the same in y; ``cell_of`` maps a point to its cell.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

import numpy as np

from . import geom
from .model import LayoutBoard

#: Plane clearance used when the board has no Power Plane Clearance rule.
DEFAULT_PLANE_CLEARANCE_MILS = 10.0
#: Hard cap on cells per layer, so a tiny cell on a large board cannot
#: take the machine's memory. The caller is told to coarsen.
MAX_CELLS = 4_000_000


#: Object kinds ``rasterize`` can paint.
KINDS = ("tracks", "arcs", "pads", "vias", "regions", "planes")


@dataclass
class Barrel:
    """One plated hole joining copper layers."""

    ix: int
    iy: int
    x: float
    y: float
    hole_mils: float
    layers: list[str]             # copper layers it passes through, top first
    net: str = ""
    kind: str = "via"             # via | pad
    comp: str = ""
    pin: str = ""
    diameter_mils: float = 0.0    # land diameter (vias) or the pad's larger side


@dataclass
class CopperRaster:
    x0: float
    y0: float
    cell_mils: float
    nx: int
    ny: int
    layers: list[str]                                   # top first
    mask: np.ndarray                                    # (ny, nx) 0..1
    fill: dict[str, np.ndarray] = field(default_factory=dict)
    barrels: list[Barrel] = field(default_factory=list)
    nets: Optional[list[str]] = None
    plane_clearance_mils: float = DEFAULT_PLANE_CLEARANCE_MILS
    supersample: int = 4
    kinds: list[str] = field(default_factory=lambda: list(KINDS))
    notes: list[str] = field(default_factory=list)

    def cell_of(self, x: float, y: float) -> tuple[int, int]:
        """(ix, iy) of the cell holding the point, clamped to the grid."""
        ix = int(math.floor((x - self.x0) / self.cell_mils))
        iy = int(math.floor((y - self.y0) / self.cell_mils))
        return min(max(ix, 0), self.nx - 1), min(max(iy, 0), self.ny - 1)

    def centre(self, ix: int, iy: int) -> tuple[float, float]:
        return (self.x0 + (ix + 0.5) * self.cell_mils,
                self.y0 + (iy + 0.5) * self.cell_mils)

    def copper_area_mils2(self, layer: str) -> float:
        return float(self.fill[layer].sum()) * self.cell_mils ** 2

    def summary(self) -> dict:
        area = float(self.mask.sum()) * self.cell_mils ** 2
        return {
            "grid": {"x0": self.x0, "y0": self.y0, "cell_mils": self.cell_mils,
                     "nx": self.nx, "ny": self.ny},
            "board_area_mils2": round(area, 1),
            "layers": [{"name": l,
                        "copper_fraction": round(self.copper_area_mils2(l) / area, 4)
                        if area else 0.0}
                       for l in self.layers],
            "barrels": len(self.barrels),
            "nets": self.nets,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Painting shapes on a boolean subgrid
# ---------------------------------------------------------------------------

class _Canvas:
    """A boolean grid at ``cell / s`` pitch; painted shapes OR into it."""

    def __init__(self, x0: float, y0: float, pitch: float, nx: int, ny: int):
        self.x0, self.y0, self.p = x0, y0, pitch
        self.nx, self.ny = nx, ny
        self.a = np.zeros((ny, nx), dtype=bool)

    def _window(self, bbox, pad: float = 0.0):
        x0, y0, x1, y1 = bbox
        i0 = max(0, int(math.floor((x0 - pad - self.x0) / self.p - 0.5)))
        i1 = min(self.nx, int(math.ceil((x1 + pad - self.x0) / self.p + 0.5)))
        j0 = max(0, int(math.floor((y0 - pad - self.y0) / self.p - 0.5)))
        j1 = min(self.ny, int(math.ceil((y1 + pad - self.y0) / self.p + 0.5)))
        if i0 >= i1 or j0 >= j1:
            return None
        xs = self.x0 + (np.arange(i0, i1) + 0.5) * self.p
        ys = self.y0 + (np.arange(j0, j1) + 0.5) * self.p
        return i0, i1, j0, j1, xs, ys

    def paint(self, shape: geom.Shape, value: bool = True, grow: float = 0.0) -> None:
        r = shape.r + grow
        win = self._window(shape.bbox, grow)
        if win is None:
            return
        i0, i1, j0, j1, xs, ys = win
        if shape.kind == "poly" and r <= 0.0 and len(shape.pts) >= 3:
            hit = _scan_fill(shape, xs, ys)
        else:
            X, Y = np.meshgrid(xs, ys)
            hit = _within(shape, X, Y, r)
        sub = self.a[j0:j1, i0:i1]
        if value:
            sub |= hit
        else:
            sub &= ~hit

    def cells(self, s: int, nx: int, ny: int) -> np.ndarray:
        """Area fraction per coarse cell (each s x s block averaged)."""
        return self.a.reshape(ny, s, nx, s).mean(axis=(1, 3))


def _seg_dist2(X, Y, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 0.0:
        return (X - ax) ** 2 + (Y - ay) ** 2
    t = np.clip(((X - ax) * dx + (Y - ay) * dy) / L2, 0.0, 1.0)
    return (X - ax - t * dx) ** 2 + (Y - ay - t * dy) ** 2


def _within(shape: geom.Shape, X, Y, r: float) -> np.ndarray:
    pts = shape.pts
    if shape.kind == "point":
        (cx, cy), = pts
        return (X - cx) ** 2 + (Y - cy) ** 2 <= r * r
    if shape.kind == "segment":
        (ax, ay), (bx, by) = pts
        return _seg_dist2(X, Y, ax, ay, bx, by) <= r * r
    inside = _even_odd(shape, X, Y)
    if r > 0.0:
        n = len(pts)
        for k in range(n):
            ax, ay = pts[k]
            bx, by = pts[(k + 1) % n]
            inside |= _seg_dist2(X, Y, ax, ay, bx, by) <= r * r
    return inside


def _rings(shape: geom.Shape):
    yield shape.pts
    for h in shape.holes:
        yield h


def _even_odd(shape: geom.Shape, X, Y) -> np.ndarray:
    inside = np.zeros(X.shape, dtype=bool)
    for ring in _rings(shape):
        n = len(ring)
        for k in range(n):
            ax, ay = ring[k]
            bx, by = ring[(k + 1) % n]
            if ay == by:
                continue
            cond = (ay > Y) != (by > Y)
            xc = ax + (Y - ay) * (bx - ax) / (by - ay)
            inside ^= cond & (X < xc)
    return inside


def _scan_fill(shape: geom.Shape, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Even-odd scanline fill of a polygon (holes included as rings) at the
    sample centres. One row at a time, all edges at once: a pour of
    thousands of vertices costs rows x edges, not samples x edges."""
    ax, ay, bx, by = [], [], [], []
    for ring in _rings(shape):
        r = np.asarray(ring, dtype=float)
        if len(r) < 3:
            continue
        s = np.roll(r, -1, axis=0)
        ax.append(r[:, 0]); ay.append(r[:, 1]); bx.append(s[:, 0]); by.append(s[:, 1])
    out = np.zeros((len(ys), len(xs)), dtype=bool)
    if not ax:
        return out
    ax, ay, bx, by = (np.concatenate(v) for v in (ax, ay, bx, by))
    keep = ay != by
    ax, ay, bx, by = ax[keep], ay[keep], bx[keep], by[keep]
    lo, hi = np.minimum(ay, by), np.maximum(ay, by)
    for j, y in enumerate(ys):
        sel = (lo <= y) & (hi > y)
        if not sel.any():
            continue
        xc = ax[sel] + (y - ay[sel]) * (bx[sel] - ax[sel]) / (by[sel] - ay[sel])
        xc.sort()
        # samples left of an odd number of crossings are inside
        cnt = len(xc) - np.searchsorted(xc, xs, side="right")
        out[j] = (cnt % 2) == 1
    return out


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------

def plane_clearance_from_rules(board: LayoutBoard) -> Optional[float]:
    best = None
    for rule in board.rules:
        if not rule.enabled or rule.kind != "plane_clearance":
            continue
        for key in ("clearance", "gap", "value", "min"):
            v = rule.values.get(key)
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v > 0:
                best = v if best is None else min(best, v)
                break
    return best


def _bounds(board: LayoutBoard) -> tuple[float, float, float, float]:
    pts = list(board.outline)
    if len(pts) < 3:
        for p in board.pads:
            pts.append((p.x, p.y))
        for t in board.tracks:
            pts += [(t.x1, t.y1), (t.x2, t.y2)]
        for r in board.regions:
            pts += list(r.outline)
        for v in board.vias:
            pts.append((v.x, v.y))
    if not pts:
        raise ValueError("the board has no outline and no copper to size a grid on")
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _barrels(board: LayoutBoard, order: list[str]) -> list[tuple]:
    """(x, y, hole, layers, net, kind, comp, pin, land) for every plated hole."""
    out = []
    for v in board.vias:
        if v.hole <= 0:
            continue
        span = board.layers_between(v.low_layer or order[-1], v.high_layer or order[0])
        out.append((v.x, v.y, v.hole, span, v.net, "via", v.comp, "", v.diameter))
    for p in board.pads:
        if p.hole <= 0 or not p.plated:
            continue
        hole = p.hole if p.hole_type != "slot" else max(p.hole, p.hole_width) * 0.5 + p.hole * 0.5
        land = max([max(c.w, c.h) for c in p.copper] or [p.hole])
        out.append((p.x, p.y, hole, list(order), p.net, "pad", p.comp, p.name, land))
    return out


def rasterize(board: LayoutBoard, cell_mils: float = 25.0,
              nets: Optional[Iterable[str]] = None,
              layers: Optional[Iterable[str]] = None,
              supersample: int = 4,
              plane_clearance_mils: Optional[float] = None,
              margin_cells: int = 0,
              kinds: Optional[Iterable[str]] = None) -> CopperRaster:
    """The board's copper as fill fractions on a ``cell_mils`` grid.

    ``nets``: only copper of these nets. ``layers``: only these copper
    layers (default all, top first). ``supersample``: samples per cell
    side (4 gives 16 samples, area within ~6 % of a cell). ``kinds``:
    paint only these object kinds, a subset of ``KINDS`` (default all);
    an IR-drop model that treats tracks as exact 1D resistors leaves
    ``tracks`` and ``arcs`` out. Drill holes are always cut."""
    kind_set = set(KINDS) if kinds is None else set(kinds)
    unknown = kind_set - set(KINDS)
    if unknown:
        raise ValueError(f"unknown kinds {sorted(unknown)}; use {list(KINDS)}")
    if cell_mils <= 0:
        raise ValueError("cell_mils must be > 0")
    s = max(1, int(supersample))
    order = board.copper_layers() or ["TopLayer", "BottomLayer"]
    want = [l for l in order if layers is None or l in set(layers)]
    net_set = None if nets is None else {n for n in nets}
    bx0, by0, bx1, by1 = _bounds(board)
    x0 = bx0 - margin_cells * cell_mils
    y0 = by0 - margin_cells * cell_mils
    nx = max(1, int(math.ceil((bx1 - x0) / cell_mils - 1e-9)) + margin_cells)
    ny = max(1, int(math.ceil((by1 - y0) / cell_mils - 1e-9)) + margin_cells)
    if nx * ny > MAX_CELLS:
        raise ValueError(f"{nx} x {ny} cells is too fine for this board; "
                         f"use a larger cell_mils")
    pitch = cell_mils / s
    clear = plane_clearance_mils
    notes = []
    if clear is None:
        clear = plane_clearance_from_rules(board)
        if clear is None:
            clear = DEFAULT_PLANE_CLEARANCE_MILS
            notes.append(f"plane clearance {clear:g} mil assumed (no Power Plane "
                         f"Clearance rule on the board)")

    def canvas() -> _Canvas:
        return _Canvas(x0, y0, pitch, nx * s, ny * s)

    def keep(net: str) -> bool:
        return net_set is None or net in net_set

    # board area
    cm = canvas()
    if len(board.outline) >= 3:
        cm.paint(geom.polygon(board.outline))
        for c in board.cutouts:
            if len(c) >= 3:
                cm.paint(geom.polygon(c), value=False)
    else:
        cm.a[:] = True
        notes.append("no board outline: the grid covers the copper's extent")
    mask = cm.cells(s, nx, ny)
    for r in board.regions:
        if r.kind == "cutout" and len(r.outline) >= 3:
            cm.paint(geom.polygon(r.outline), value=False)
    mask = cm.cells(s, nx, ny)

    holes = _barrels(board, order)
    kinds = {l.name: l for l in board.layers}
    fill: dict[str, np.ndarray] = {}
    for layer in want:
        lk = kinds.get(layer)
        is_plane = lk is not None and lk.kind == "plane"
        c = canvas()
        if is_plane and "planes" in kind_set:
            # one mask per plane net, antipads cut for other-net holes
            per_net: dict[str, list[geom.Shape]] = {}
            for r in board.regions:
                if r.layer != layer or r.keepout or r.kind in ("cutout", "keepout"):
                    continue
                if len(r.outline) >= 3:
                    per_net.setdefault(r.net, []).append(r.shape())
            if not per_net and lk.plane_net and len(board.outline) >= 3:
                per_net[lk.plane_net] = [geom.polygon(board.outline, board.cutouts)]
            for net, shapes in per_net.items():
                if not keep(net):
                    continue
                pc = canvas()
                for sh in shapes:
                    pc.paint(sh)
                for (hx, hy, hole, span, hnet, *_rest) in holes:
                    if layer in span and (hnet != net or not hnet):
                        pc.paint(geom.circle(hx, hy, hole + 2 * clear), value=False)
                c.a |= pc.a
        elif not is_plane and "regions" in kind_set:
            for r in board.regions:
                if (r.layer == layer and not r.keepout and r.kind not in ("cutout", "keepout")
                        and len(r.outline) >= 3 and keep(r.net)):
                    c.paint(r.shape())
        # discrete copper (on plane layers too: pads and vias keep their land)
        for t in (board.tracks if "tracks" in kind_set else ()):
            if t.layer == layer and not t.keepout and keep(t.net):
                c.paint(geom.capsule(t.x1, t.y1, t.x2, t.y2, t.width))
        for a in (board.arcs if "arcs" in kind_set else ()):
            if a.layer == layer and not a.keepout and keep(a.net):
                for sh in a.shapes():
                    c.paint(sh)
        for p in (board.pads if "pads" in kind_set else ()):
            if not keep(p.net):
                continue
            if is_plane and p.hole > 0 and (not lk.plane_net or p.net != lk.plane_net):
                continue  # a TH pad on a negative plane is an antipad, not a land
            sh = p.shape_on(layer)
            if sh is not None:
                c.paint(sh)
        for v in (board.vias if "vias" in kind_set else ()):
            if not keep(v.net) or is_plane:
                continue
            span = board.layers_between(v.low_layer or order[-1], v.high_layer or order[0])
            if layer in span:
                c.paint(v.shape_on(layer))
        # drilled holes carry no copper area on the layer
        for (hx, hy, hole, span, hnet, *_rest) in holes:
            if layer in span and keep(hnet):
                c.paint(geom.circle(hx, hy, hole), value=False)
        f = c.cells(s, nx, ny)
        fill[layer] = np.minimum(f, mask)

    raster = CopperRaster(x0, y0, cell_mils, nx, ny, want, mask, fill,
                          nets=sorted(net_set) if net_set is not None else None,
                          plane_clearance_mils=clear, supersample=s, notes=notes,
                          kinds=sorted(kind_set))
    for (hx, hy, hole, span, hnet, kind, comp, pin, land) in holes:
        if not keep(hnet):
            continue
        ix, iy = raster.cell_of(hx, hy)
        raster.barrels.append(Barrel(ix, iy, hx, hy, hole, [l for l in span if l in want] or span,
                                     hnet, kind, comp, pin, land))
    return raster
