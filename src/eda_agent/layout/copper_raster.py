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
  (``layout.geom``) on a grid of ``supersample`` samples per cell side
  and averaged down. Tracks, circles and rectangles lying along the axes
  are painted with the exact area each sample holds of them, so a track
  keeps its width and a pad its area whatever the cell size; other
  polygons (pours) are taken at the sample centres, an edge within half
  a sample.
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
#: ``supersample=0``: the sample pitch a conduction model wants, mils. The
#: narrowest copper web and clearance on a board are 3 to 4 mil; a web is
#: told from a cut only when it is more than a sample wide, so a coarser
#: sample reads the web between two antipads of a BGA field as cut and the
#: plane under the BGA as open.
SAMPLE_MILS = 2.0
#: ... and the most samples one layer may take for it (float32 each, and a
#: few times that while the links are made). A board too large for
#: ``SAMPLE_MILS`` gets the finest pitch that fits.
MAX_SAMPLES = 16_000_000


#: Region kinds that are not copper: a polygon cutout, a keepout, and a
#: polygon pour's OUTLINE (``read_altium`` keeps it beside the copper the
#: pour actually made, kind "pour"; painted, it would fill every clearance
#: void, neck and removed island of the pour solid).
_NOT_COPPER = ("cutout", "keepout", "pour_boundary")

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
    #: Only with ``rasterize(links=True)``: per layer, how much of the way
    #: from one cell centre to the next is continuous copper. ``link_x`` is
    #: ``(ny, nx - 1)`` (cell ``ix`` to ``ix + 1``), ``link_y`` is
    #: ``(ny - 1, nx)``; values 0..1.
    link_x: dict[str, np.ndarray] = field(default_factory=dict)
    link_y: dict[str, np.ndarray] = field(default_factory=dict)
    #: Only with ``links=True``: the copper fraction of each of the
    #: ``supersample`` slices across the way from one cell centre to the
    #: next, ``slice_x[layer]`` shaped (ny, nx - 1, s) for the link from
    #: (iy, ix) to (iy, ix + 1) and ``slice_y[layer]`` shaped
    #: (ny - 1, nx, s) for (iy, ix) to (iy + 1, ix). ``link_conductivity``
    #: turns them into a conductivity per link.
    slice_x: dict[str, np.ndarray] = field(default_factory=dict)
    slice_y: dict[str, np.ndarray] = field(default_factory=dict)
    #: Only with ``links=True``: length of copper edge inside each cell,
    #: mils, ``edge_mils[layer]`` shaped (ny, nx).
    edge_mils: dict[str, np.ndarray] = field(default_factory=dict)

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

    def shape_area_by_cell(self, shape: geom.Shape, samples: int = 8
                           ) -> dict[tuple[int, int], float]:
        """Area of ``shape`` falling in each cell, {(ix, iy): mils^2}.

        Exact for circles, tracks and rectangles along the axes; any other
        shape is sampled at ``cell_mils / samples``. Only cells of the
        grid count. A shape that slips between every sample goes to the
        cell under its centre with its bounding-box area."""
        n = max(1, int(samples))
        p = self.cell_mils / n
        out: dict[tuple[int, int], float] = {}
        got = _cover(shape, self.x0, self.y0, p, self.nx * n, self.ny * n)
        if got is not None:
            i0, j0, cov = got
            jj, ii = np.nonzero(cov > 0.0)
            if len(jj):
                keys, inv = np.unique((jj + j0) // n * self.nx + (ii + i0) // n,
                                      return_inverse=True)
                area = np.bincount(inv, weights=cov[jj, ii]) * p * p
                for k, a in zip(keys.tolist(), area.tolist()):
                    out[(k % self.nx, k // self.nx)] = a
                return out
        bx0, by0, bx1, by1 = shape.bbox
        mx, my = (bx0 + bx1) / 2.0, (by0 + by1) / 2.0
        if self.x0 <= mx <= self.x0 + self.nx * self.cell_mils and \
                self.y0 <= my <= self.y0 + self.ny * self.cell_mils:
            out[self.cell_of(mx, my)] = max((bx1 - bx0) * (by1 - by0), 1e-9)
        return out

    def link_conductivity(self, layer: str, k_copper: float, k_between: float
                          ) -> tuple[np.ndarray, np.ndarray]:
        """Effective conductivity of every link between neighbouring cells
        of ``layer``: (kx shaped (ny, nx-1), ky shaped (ny-1, nx)).

        The path between two cell centres is cut into slices across it
        (``slice_x`` / ``slice_y``). Copper and the material between the
        copper share a slice side by side (parallel), and the slices
        follow one another (series). A track running along the link then
        conducts with its full width, a diagonal one as the staircase it
        is on the grid (which has the diagonal's own resistance), a track
        or a gap lying across the link conducts only through what is
        between the copper, and with ``k_between`` = 0 (electrical
        conduction) a gap that crosses the whole link opens it. The plain
        area rule gives the same number to all of these."""
        if layer not in self.slice_x:
            raise ValueError("rasterize(..., links=True) to get link conductivities")
        out = []
        for f in (self.slice_x[layer], self.slice_y[layer]):
            k = k_copper * f + k_between * (1.0 - f)
            with np.errstate(divide="ignore"):
                r = np.where(k > 0, 1.0 / np.where(k > 0, k, 1.0), np.inf)
            rs = r.mean(axis=-1)
            out.append(np.where(np.isfinite(rs), 1.0 / np.where(rs > 0, rs, 1.0), 0.0))
        return out[0], out[1]

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
# Painting shapes on a subgrid of coverage fractions
# ---------------------------------------------------------------------------

class _Canvas:
    """Copper coverage, 0..1, of each sample of a grid at ``cell / s``
    pitch. Painted shapes join as a union (the larger coverage wins, which
    is right where they overlap, as connected copper does); a cut takes
    its own coverage away."""

    def __init__(self, x0: float, y0: float, pitch: float, nx: int, ny: int):
        self.x0, self.y0, self.p = x0, y0, pitch
        self.nx, self.ny = nx, ny
        self.a = np.zeros((ny, nx), dtype=np.float32)

    def paint(self, shape: geom.Shape, value: bool = True, grow: float = 0.0) -> None:
        got = _cover(shape, self.x0, self.y0, self.p, self.nx, self.ny, grow)
        if got is None:
            return
        i0, j0, cov = got
        sub = self.a[j0:j0 + cov.shape[0], i0:i0 + cov.shape[1]]
        if value:
            np.maximum(sub, cov, out=sub, casting="unsafe")
        else:
            np.minimum(sub, 1.0 - cov, out=sub, casting="unsafe")

    def cells(self, s: int, nx: int, ny: int) -> np.ndarray:
        """Area fraction per coarse cell (each s x s block averaged)."""
        return self.a.reshape(ny, s, nx, s).mean(axis=(1, 3), dtype=np.float64)

    def links(self, s: int, nx: int, ny: int) -> tuple[np.ndarray, np.ndarray]:
        """Continuity of the copper between neighbouring cell centres.

        Each of the ``s`` subsample rows of a cell is a strip running to
        the next cell's centre, ``s`` subsamples long; it conducts when
        every subsample along it is copper (at least half covered). The
        link is the fraction of strips that conduct: 1 through solid
        copper, the covered fraction of the face where copper only partly
        fills it, and 0 across any gap the subsamples resolve, however
        narrow against the cell.
        """
        a = self.a >= 0.5
        h0 = s // 2
        csx = np.zeros((ny * s, nx * s + 1), dtype=np.int32)
        np.cumsum(a, axis=1, out=csx[:, 1:])
        starts = np.arange(nx - 1) * s + h0
        runs = csx[:, starts + s] - csx[:, starts]
        link_x = (runs == s).reshape(ny, s, nx - 1).mean(axis=1) if nx > 1 else np.zeros((ny, 0))
        csy = np.zeros((ny * s + 1, nx * s), dtype=np.int32)
        np.cumsum(a, axis=0, out=csy[1:, :])
        starts = np.arange(ny - 1) * s + h0
        runs = csy[starts + s, :] - csy[starts, :]
        link_y = (runs == s).reshape(ny - 1, nx, s).mean(axis=2) if ny > 1 else np.zeros((0, nx))
        return link_x, link_y

    def slices(self, s: int, nx: int, ny: int) -> tuple[np.ndarray, np.ndarray]:
        """Copper fraction of each of the ``s`` slices across the way from
        one cell centre to the next: (ny, nx - 1, s) and (ny - 1, nx, s).

        A sample's coverage says how much of it is copper, not which way
        the copper runs through it. A track's edge leaves a row of partly
        covered samples that conduct along the row with that share of
        their width; a clearance across the way leaves one or two partly
        covered samples that conduct nothing. The second is told from the
        first by its neighbours along the way: a sample with less copper
        than there is on both sides of it is a gap (``_open_gaps``), and
        counts as no copper for conduction in that direction. So a gap is
        open even when it is narrower than a sample."""
        h = s // 2
        col = _open_gaps(self.a, 1).reshape(ny, s, nx * s).mean(axis=1)    # (ny, nx*s)
        sx = np.ascontiguousarray(col[:, h:(nx - 1) * s + h].reshape(ny, nx - 1, s))
        row = _open_gaps(self.a, 0).reshape(ny * s, nx, s).mean(axis=2)    # (ny*s, nx)
        sy = np.ascontiguousarray(
            row[h:(ny - 1) * s + h, :].reshape(ny - 1, s, nx).transpose(0, 2, 1))
        return sx, sy

    def edges(self, s: int, nx: int, ny: int) -> np.ndarray:
        """Length of copper edge inside each coarse cell, in mils.

        The coverage's gradient, taken over each square of four samples
        and summed: 1 across an edge whatever the samples' share of it, so
        an edge along the axes or at 45 degrees reads its true length and
        any other within a few percent."""
        a = self.a
        g = np.zeros(a.shape, dtype=np.float32)
        d1 = a[1:, 1:] - a[:-1, :-1]
        d2 = a[1:, :-1] - a[:-1, 1:]
        g[:-1, :-1] = np.sqrt((d1 * d1 + d2 * d2) / 2.0)
        return g.reshape(ny, s, nx, s).sum(axis=(1, 3), dtype=np.float64) * self.p


#: A sample this much short of the copper on both sides of it is a gap.
_GAP_DIP = 0.02


def _open_gaps(a: np.ndarray, axis: int) -> np.ndarray:
    """``a`` with every dip along ``axis`` set to 0: a sample holding less
    copper than the fuller of the two samples before it and the fuller of
    the two after it."""
    n = a.shape[axis]
    pad = np.pad(a, [(2, 2) if k == axis else (0, 0) for k in range(a.ndim)], mode="edge")

    def shifted(k: int) -> np.ndarray:
        return np.take(pad, np.arange(2 + k, 2 + k + n), axis=axis)

    before = np.maximum(shifted(-1), shifted(-2))
    after = np.maximum(shifted(1), shifted(2))
    return np.where(a < np.minimum(before, after) - _GAP_DIP, 0.0, a).astype(np.float32)


def _cover(shape: geom.Shape, gx0: float, gy0: float, p: float, nx: int, ny: int,
           grow: float = 0.0) -> Optional[tuple[int, int, np.ndarray]]:
    """Share of each sample square covered by ``shape`` on a grid at pitch
    ``p``: (i0, j0, array) for the window of samples the shape's bounding
    box touches, None when that is off the grid.

    Exact area for a circle, a track at any angle (along its length; the
    round ends of a slanted one to within a fraction of a sample) and a
    rectangle or rounded rectangle lying along the axes. Any other
    polygon is in or out by each sample's centre."""
    r = shape.r + grow
    bx0, by0, bx1, by1 = shape.bbox
    i0 = max(0, int(math.floor((bx0 - grow - gx0) / p)))
    i1 = min(nx, int(math.ceil((bx1 + grow - gx0) / p)))
    j0 = max(0, int(math.floor((by0 - grow - gy0) / p)))
    j1 = min(ny, int(math.ceil((by1 + grow - gy0) / p)))
    if i0 >= i1 or j0 >= j1:
        return None
    xc = gx0 + np.arange(i0, i1 + 1) * p                # sample corners
    yc = gy0 + np.arange(j0, j1 + 1) * p
    area = _exact_area(shape, r, xc, yc, p)
    if area is not None:
        return i0, j0, np.clip(area / (p * p), 0.0, 1.0)
    xs = (xc[:-1] + xc[1:]) / 2.0
    ys = (yc[:-1] + yc[1:]) / 2.0
    if shape.kind == "poly" and r <= 0.0 and len(shape.pts) >= 3:
        hit = _scan_fill(shape, xs, ys)
    else:
        X, Y = np.meshgrid(xs, ys)
        hit = _within(shape, X, Y, r)
    return i0, j0, hit.astype(np.float32)


def _exact_area(shape: geom.Shape, r: float, xc: np.ndarray, yc: np.ndarray,
                p: float) -> Optional[np.ndarray]:
    """Area of ``shape`` in each sample of the lattice ``xc`` x ``yc``,
    or None for a shape with no exact rule."""
    pts = shape.pts
    if shape.kind == "point":
        (cx, cy), = pts
        return _disc_area(xc - cx, yc - cy, r) if r > 0.0 else None
    if shape.kind == "segment":
        (ax, ay), (bx, by) = pts
        if r <= 0.0:
            return None
        if ay == by:                                     # along x: body and two half discs
            lo, hi = min(ax, bx), max(ax, bx)
            return (_rect_area(xc, yc, lo, hi, ay - r, ay + r)
                    + _disc_area(xc - lo, yc - ay, r, x_lim=(-math.inf, 0.0))
                    + _disc_area(xc - hi, yc - ay, r, x_lim=(0.0, math.inf)))
        if ax == bx:                                     # along y
            lo, hi = min(ay, by), max(ay, by)
            return (_rect_area(xc, yc, ax - r, ax + r, lo, hi)
                    + _disc_area(xc - ax, yc - lo, r, y_lim=(-math.inf, 0.0))
                    + _disc_area(xc - ax, yc - hi, r, y_lim=(0.0, math.inf)))
        return _strip_area(xc, yc, ax, ay, bx, by, r, p)
    if shape.kind == "poly" and len(pts) == 4 and not shape.holes:
        box = _axis_box(pts)
        if box is not None:
            x0, y0, x1, y1 = box
            if r <= 0.0:
                return _rect_area(xc, yc, x0, x1, y0, y1)
            # a rounded rectangle: a cross of three rectangles and a
            # quarter disc at each corner, none overlapping
            out = (_rect_area(xc, yc, x0 - r, x1 + r, y0, y1)
                   + _rect_area(xc, yc, x0, x1, y1, y1 + r)
                   + _rect_area(xc, yc, x0, x1, y0 - r, y0))
            left, right = (-math.inf, 0.0), (0.0, math.inf)
            for qx, xl in ((x0, left), (x1, right)):
                for qy, yl in ((y0, left), (y1, right)):
                    out = out + _disc_area(xc - qx, yc - qy, r, x_lim=xl, y_lim=yl)
            return out
    return None


def _axis_box(pts) -> Optional[tuple[float, float, float, float]]:
    """(x0, y0, x1, y1) when four points are a rectangle along the axes."""
    xs = [q[0] for q in pts]
    ys = [q[1] for q in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    tol = 1e-9 * max(abs(x0), abs(x1), abs(y0), abs(y1), 1.0)
    if x1 - x0 <= tol or y1 - y0 <= tol:
        return None
    for k in range(4):
        (ax, ay), (bx, by) = pts[k], pts[(k + 1) % 4]
        on_x = abs(ay - by) <= tol and abs(abs(ax - bx) - (x1 - x0)) <= tol
        on_y = abs(ax - bx) <= tol and abs(abs(ay - by) - (y1 - y0)) <= tol
        if not (on_x or on_y):
            return None
    return x0, y0, x1, y1


def _rect_area(xc, yc, x0, x1, y0, y1) -> np.ndarray:
    wx = np.diff(np.clip(xc, x0, x1))
    wy = np.diff(np.clip(yc, y0, y1))
    return wy[:, None] * wx[None, :]


def _disc_area(xr: np.ndarray, yr: np.ndarray, r: float,
               x_lim=(-math.inf, math.inf), y_lim=(-math.inf, math.inf)) -> np.ndarray:
    """Area of the disc of radius ``r`` about the origin in each rectangle
    of the lattice with corners ``xr`` x ``yr``; with ``x_lim`` / ``y_lim``
    only the part of the disc inside those limits (a half or a quarter).

    G(x, y), the disc's area left of x and below y, is the integral of the
    chord sqrt(r^2 - u^2) in closed form; a rectangle's share is G at its
    four corners, added and subtracted."""
    x = np.clip(xr, max(-r, x_lim[0]), min(r, x_lim[1]))
    y = np.clip(yr, max(-r, y_lim[0]), min(r, y_lim[1]))
    r2 = r * r

    def chord(u):                       # running integral of sqrt(r^2 - u^2)
        return 0.5 * (u * np.sqrt(np.maximum(r2 - u * u, 0.0))
                      + r2 * np.arcsin(np.clip(u / r, -1.0, 1.0)))

    lo = -0.25 * math.pi * r2           # chord(-r)
    ya = np.abs(y)[:, None]
    a = np.sqrt(np.maximum(r2 - ya * ya, 0.0))
    X = x[None, :]
    upper = ((chord(np.minimum(X, -a)) - lo) + ya * (np.clip(X, -a, a) + a)
             + (chord(np.maximum(X, a)) - chord(a)))
    g = (chord(x)[None, :] - lo) + np.sign(y)[:, None] * upper
    return np.diff(np.diff(g, axis=0), axis=1)


def _box_share(t, a: float, b: float) -> np.ndarray:
    """Share of a sample square on the inner side of a straight edge.

    ``t`` is the distance of the square's centre from the edge, positive
    inside. ``a`` >= ``b`` >= 0 are the square's side times the larger and
    the smaller of |cos| and |sin| of the edge's direction: the square's
    shadow on the edge's normal is a trapezoid of base a + b and top
    a - b, and this is its running integral."""
    t = np.asarray(t, dtype=float)
    flat = np.clip(0.5 + t / a, 0.0, 1.0)
    if b <= 1e-9 * a:
        return flat
    hi, lo = (a + b) / 2.0, (a - b) / 2.0
    up = np.square(np.clip(t + hi, 0.0, None)) / (2.0 * a * b)
    down = 1.0 - np.square(np.clip(hi - t, 0.0, None)) / (2.0 * a * b)
    return np.where(t < -lo, up, np.where(t > lo, down, flat))


def _strip_area(xc, yc, ax, ay, bx, by, r: float, p: float) -> np.ndarray:
    """A slanted track: its body between the two ends, by the share of
    each sample between its edges (exact along its length), and half a
    disc beyond each end."""
    xs = ((xc[:-1] + xc[1:]) / 2.0)[None, :]
    ys = ((yc[:-1] + yc[1:]) / 2.0)[:, None]
    length = math.hypot(bx - ax, by - ay)
    ux, uy = (bx - ax) / length, (by - ay) / length
    a, b = p * max(abs(ux), abs(uy)), p * min(abs(ux), abs(uy))
    along = (xs - ax) * ux + (ys - ay) * uy             # from the first end
    across = np.abs((ys - ay) * ux - (xs - ax) * uy)    # from the centre line
    before = _box_share(-along, a, b)                   # share of the sample before the first end
    after = _box_share(along - length, a, b)
    body = (_box_share(r - across, a, b) - _box_share(-r - across, a, b)) * (1.0 - before - after)
    pp = p * p
    return (np.clip(body, 0.0, 1.0) * pp
            + _disc_area(xc - ax, yc - ay, r) * before
            + _disc_area(xc - bx, yc - by, r) * after)


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


def _unpoured(board: LayoutBoard, want: list[str], keep) -> list[str]:
    """A note for polygon pours that have an outline and no copper: not
    poured (or shelved) in the PCB editor, so there is nothing to count."""
    poured = [(r.layer, r.source, r.shape().bbox) for r in board.regions
              if r.kind == "pour" and len(r.outline) >= 3]
    empty = []
    for b in board.regions:
        if b.kind != "pour_boundary" or b.layer not in want or len(b.outline) < 3 \
                or not keep(b.net):
            continue
        x0, y0, x1, y1 = b.shape().bbox
        named = b.source not in ("", "polygon:")
        if not any(layer == b.layer and (
                (named and src == b.source)
                or (px0 < x1 and px1 > x0 and py0 < y1 and py1 > y0))
                for layer, src, (px0, py0, px1, py1) in poured):
            name = b.source.partition(":")[2] or b.net or "unnamed"
            empty.append(f"{name} on {b.layer}")
    if not empty:
        return []
    more = f" and {len(empty) - 6} more" if len(empty) > 6 else ""
    return [f"{len(empty)} polygon pour(s) have an outline and no poured copper and are "
            f"left out (repour them in the PCB editor): {', '.join(empty[:6])}{more}"]


def fine_supersample(cell_mils: float, cells: int, sample_mils: float = SAMPLE_MILS,
                     max_samples: int = MAX_SAMPLES) -> int:
    """Samples per cell side for a sample pitch of ``sample_mils``, as
    far as ``max_samples`` on a grid of ``cells`` allows; never under 4."""
    want = int(math.ceil(cell_mils / sample_mils - 1e-9))
    room = int(math.sqrt(max_samples / max(1, cells)))
    return max(4, min(want, room))


def rasterize(board: LayoutBoard, cell_mils: float = 25.0,
              nets: Optional[Iterable[str]] = None,
              layers: Optional[Iterable[str]] = None,
              supersample: int = 4,
              plane_clearance_mils: Optional[float] = None,
              margin_cells: int = 0,
              kinds: Optional[Iterable[str]] = None,
              links: bool = False,
              bounds: Optional[tuple[float, float, float, float]] = None) -> CopperRaster:
    """The board's copper as fill fractions on a ``cell_mils`` grid.

    ``nets``: only copper of these nets. ``layers``: only these copper
    layers (default all, top first). ``supersample``: samples per cell
    side; it sets how finely a pour's edge and a gap are placed (to half
    a sample), not the area of tracks, circles and rectangles, which is
    exact. 0 picks it for conduction: a sample of ``SAMPLE_MILS`` or the
    finest that keeps a layer under ``MAX_SAMPLES``, and at least 4 per
    cell (``fine_supersample``). ``kinds``: paint only these object kinds, a subset of
    ``KINDS`` (default all); an IR-drop model that treats tracks as exact
    1D resistors leaves ``tracks`` and ``arcs`` out. Drill holes are
    always cut. ``links`` also fills ``link_x`` / ``link_y``, the copper's
    continuity between neighbouring cells (see ``_Canvas.links``: a
    conduction model built on them does not bridge a gap narrower than a
    cell), ``slice_x`` / ``slice_y`` (``CopperRaster.link_conductivity``)
    and ``edge_mils``. ``bounds``: (x0, y0, x1, y1) in mils, the
    rectangle to grid instead of the board's extent (a window of the
    board at a finer cell)."""
    kind_set = set(KINDS) if kinds is None else set(kinds)
    unknown = kind_set - set(KINDS)
    if unknown:
        raise ValueError(f"unknown kinds {sorted(unknown)}; use {list(KINDS)}")
    if cell_mils <= 0:
        raise ValueError("cell_mils must be > 0")
    order = board.copper_layers() or ["TopLayer", "BottomLayer"]
    want = [l for l in order if layers is None or l in set(layers)]
    net_set = None if nets is None else {n for n in nets}
    bx0, by0, bx1, by1 = bounds if bounds is not None else _bounds(board)
    if bx1 <= bx0 or by1 <= by0:
        raise ValueError("bounds must be (x0, y0, x1, y1) with x1 > x0 and y1 > y0")
    x0 = bx0 - margin_cells * cell_mils
    y0 = by0 - margin_cells * cell_mils
    nx = max(1, int(math.ceil((bx1 - x0) / cell_mils - 1e-9)) + margin_cells)
    ny = max(1, int(math.ceil((by1 - y0) / cell_mils - 1e-9)) + margin_cells)
    if nx * ny > MAX_CELLS:
        raise ValueError(f"{nx} x {ny} cells is too fine for this board; "
                         f"use a larger cell_mils")
    s = int(supersample)
    s = fine_supersample(cell_mils, nx * ny) if s <= 0 else s
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
        cm.a[:] = 1.0
        notes.append("no board outline: the grid covers the copper's extent")
    # A Region of kind "cutout" is a polygon-pour cutout on one copper
    # layer, not a hole in the board (those are ``board.cutouts``): it is
    # not copper and it does not cut the mask.
    mask = cm.cells(s, nx, ny)
    if "regions" in kind_set:
        notes += _unpoured(board, want, keep)

    holes = _barrels(board, order)
    kinds = {l.name: l for l in board.layers}
    fill: dict[str, np.ndarray] = {}
    link_x: dict[str, np.ndarray] = {}
    link_y: dict[str, np.ndarray] = {}
    slice_x: dict[str, np.ndarray] = {}
    slice_y: dict[str, np.ndarray] = {}
    edge_mils: dict[str, np.ndarray] = {}
    for layer in want:
        lk = kinds.get(layer)
        is_plane = lk is not None and lk.kind == "plane"
        c = canvas()
        if is_plane and "planes" in kind_set:
            # one mask per plane net, antipads cut for other-net holes
            per_net: dict[str, list[geom.Shape]] = {}
            for r in board.regions:
                if r.layer != layer or r.keepout or r.kind in _NOT_COPPER:
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
                np.maximum(c.a, pc.a, out=c.a)
        elif not is_plane and "regions" in kind_set:
            for r in board.regions:
                if (r.layer == layer and not r.keepout and r.kind not in _NOT_COPPER
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
        # copper counts only inside the board
        np.minimum(c.a, cm.a, out=c.a)
        fill[layer] = c.cells(s, nx, ny)
        if links:
            link_x[layer], link_y[layer] = c.links(s, nx, ny)
            slice_x[layer], slice_y[layer] = c.slices(s, nx, ny)
            edge_mils[layer] = c.edges(s, nx, ny)

    raster = CopperRaster(x0, y0, cell_mils, nx, ny, want, mask, fill,
                          nets=sorted(net_set) if net_set is not None else None,
                          plane_clearance_mils=clear, supersample=s, notes=notes,
                          kinds=sorted(kind_set), link_x=link_x, link_y=link_y,
                          slice_x=slice_x, slice_y=slice_y, edge_mils=edge_mils)
    for (hx, hy, hole, span, hnet, kind, comp, pin, land) in holes:
        if not keep(hnet):
            continue
        if not (x0 <= hx <= x0 + nx * cell_mils and y0 <= hy <= y0 + ny * cell_mils):
            continue  # outside a ``bounds`` window
        ix, iy = raster.cell_of(hx, hy)
        raster.barrels.append(Barrel(ix, iy, hx, hy, hole, [l for l in span if l in want] or span,
                                     hnet, kind, comp, pin, land))
    return raster
