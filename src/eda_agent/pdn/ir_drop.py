# SPDX-License-Identifier: Apache-2.0
"""DC IR drop of one power net (and optionally its return), from the
board's real copper.

The net's copper becomes a resistor network solved exactly by a sparse
direct solve (Kirchhoff's current law at every node):

* Area copper (pads, regions, pours, split and solid planes) on a square
  grid from ``layout.copper_raster``. Each cell is a node; two neighbours
  are joined by the sheet conductance sigma*t times the harmonic mean of
  their copper fill. That is the five-point finite-volume discretisation of
  div(sigma t grad V) = 0, second-order accurate in the cell size for
  smooth copper.
* Tracks and arcs as exact one-dimensional resistors, R = rho L / (w t),
  split where they enter area copper so copper is never counted twice
  (a track lying inside a pour of its own net is dropped; the pour already
  carries it). Track ends merge when they touch, and an end landing on
  the middle of another track splits that track (a T junction).
* Plated barrels (vias and through-hole pads) as resistors between the
  layers they join, R = rho dz / (pi ((r+t)^2 - r^2)), with the real
  dielectric heights from the stackup.
* Pads as equipotential nodes tied to the cells under them (a soldered
  lead and its fillet are far thicker than the foil); the sources hold
  their pads at the rail voltage, the sinks draw their current, split
  equally among their pads (``sink_model="per_pin"``) or drawn through one
  equipotential node joined to all of them (``"equipotential"``, the
  die-side bump model).

Copper resistivity follows the temperature (``temp_c``). Thermal reliefs
are taken fully connected and the copper is taken to its nominal
thickness, both stated in the result.

The solve is checked by a second solve on a grid twice as coarse; the
change in the worst drop estimates the discretisation error (Richardson,
second order) and is reported as ``convergence``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import numpy as np

from ..layout import geom
from ..layout.model import LayoutBoard, Pad
from . import physics as ph

#: Conductance that makes two nodes one potential (a pad and the cells
#: under it), in units of the layer's sheet conductance: 1e-4 of a square
#: in series, far below anything reported, and no strain on a direct solve.
STIFF = 1.0e4
#: Cells below this copper fraction are not nodes.
FILL_MIN = 0.02
#: A track over cells this full is inside area copper, which carries its
#: current, and is dropped there. Only whole cells count: a cell a pad or
#: a pour edge fills in part is still the track's to cross.
INSIDE_POUR = 0.97
#: A point on area copper this full is joined to the cell.
TOUCH = 0.25
#: Default upper bound on grid nodes for the automatic cell size.
MAX_NODES_AUTO = 800_000
#: A round contact wider than this many cells is drawn on the grid like any
#: other shape; a narrower one goes through the well model.
ROUND_AS_SHAPE = 2.5


class IRDropError(ValueError):
    pass


@dataclass
class _Net:
    """The network being assembled: nodes with metadata and conductances."""
    n: int = 0
    a: list[int] = field(default_factory=list)
    b: list[int] = field(default_factory=list)
    g: list[float] = field(default_factory=list)
    kind: list[str] = field(default_factory=list)   # per edge
    tag: list[Any] = field(default_factory=list)    # per edge

    def node(self) -> int:
        self.n += 1
        return self.n - 1

    def edge(self, a: int, b: int, g: float, kind: str, tag: Any = None) -> None:
        if a == b or not (g > 0) or not math.isfinite(g):
            return
        self.a.append(a)
        self.b.append(b)
        self.g.append(g)
        self.kind.append(kind)
        self.tag.append(tag)


def _layer_thickness(board: LayoutBoard, stack, layer: str) -> float:
    if stack is not None:
        cl = stack.get(layer)
        if cl is not None and cl.copper_mils > 0:
            return cl.copper_mils
    for l in board.layers:
        if l.name == layer and l.copper_mils > 0:
            return l.copper_mils
    return 1.4


def _layer_z(stack, order: list[str]) -> dict[str, float]:
    if stack is not None and stack.layers:
        z = {l.name: l.z_mils for l in stack.layers}
        if all(n in z for n in order):
            return z
    # evenly spread over 1.6 mm when no stack
    n = len(order)
    t = 62.99
    return {name: t * (1 - i / max(1, n - 1)) for i, name in enumerate(order)}


def _pad_ref(spec: str) -> tuple[str, str]:
    if "." in spec:
        ref, pin = spec.rsplit(".", 1)
        return ref, pin
    return spec, ""


def resolve_pads(board: LayoutBoard, net: str, spec: dict) -> list[Pad]:
    """The pads of ``net`` named by a source or sink spec: ``ref`` (all its
    pads on the net) and/or ``pads`` (["U1.5", ...])."""
    out: list[Pad] = []
    seen = set()
    refs = spec.get("ref") or spec.get("refs") or []
    if isinstance(refs, str):
        refs = [refs]
    pads = spec.get("pads") or []
    if isinstance(pads, str):
        pads = [pads]
    for p in board.pads:
        if p.net != net:
            continue
        hit = p.comp in refs or f"{p.comp}.{p.name}" in pads
        if hit and id(p) not in seen:
            seen.add(id(p))
            out.append(p)
    return out


def net_board(board: LayoutBoard, net: str) -> tuple[LayoutBoard, list[str]]:
    """The board as the raster should see it for one net: no polygon
    outlines (only poured copper is copper), and, for a net with no plane
    of its own, cropped to the net's copper so a small rail on a large
    board is not gridded board-wide."""
    from dataclasses import replace
    notes: list[str] = []
    regions = [r for r in board.regions if r.kind != "pour_boundary"]
    poured = {(r.layer, r.source) for r in regions if r.net == net and r.kind == "pour"}
    unpoured = sorted({r.source or r.layer for r in board.regions
                       if r.kind == "pour_boundary" and r.net == net
                       and (r.layer, r.source) not in poured})
    if unpoured:
        notes.append(f"{len(unpoured)} polygon(s) of {net} carry no poured copper "
                     f"(unpoured or shelved) and are not counted: {', '.join(unpoured[:5])}")
    plane_names = {l.name for l in board.layers if l.kind == "plane"}
    is_plane = (any(l.kind == "plane" and l.plane_net == net for l in board.layers)
                or any(r.net == net and r.layer in plane_names for r in regions))
    outline = board.outline
    if not is_plane:
        xs: list[float] = []
        ys: list[float] = []
        for p in board.pads:
            if p.net == net:
                reach = max([max(c.w, c.h) for c in p.copper] or [p.hole]) / 2.0 + 1.0
                xs += [p.x - reach, p.x + reach]; ys += [p.y - reach, p.y + reach]
        for t in board.tracks:
            if t.net == net:
                xs += [t.x1 - t.width, t.x2 + t.width, t.x1 + t.width, t.x2 - t.width]
                ys += [t.y1 - t.width, t.y2 + t.width, t.y1 + t.width, t.y2 - t.width]
        for a in board.arcs:
            if a.net == net:
                rr = a.radius + a.width
                xs += [a.cx - rr, a.cx + rr]; ys += [a.cy - rr, a.cy + rr]
        for v in board.vias:
            if v.net == net:
                xs += [v.x - v.diameter, v.x + v.diameter]; ys += [v.y - v.diameter, v.y + v.diameter]
        for r in regions:
            if r.net == net and not r.keepout and r.kind not in ("cutout", "keepout"):
                xs += [q[0] for q in r.outline]; ys += [q[1] for q in r.outline]
        if xs:
            m = 20.0
            x0, x1, y0, y1 = min(xs) - m, max(xs) + m, min(ys) - m, max(ys) + m
            if len(board.outline) >= 3:
                bx = [q[0] for q in board.outline]; by = [q[1] for q in board.outline]
                full = (max(bx) - min(bx)) * (max(by) - min(by))
            else:
                full = float("inf")
            if (x1 - x0) * (y1 - y0) < 0.8 * full:
                outline = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return replace(board, regions=regions, outline=outline), notes


def auto_cell_mils(board: LayoutBoard, net: str, max_nodes: int = MAX_NODES_AUTO,
                   notes: Optional[list[str]] = None) -> float:
    """The finest cell that keeps the net's area copper under ``max_nodes``
    cells and the grid under the raster's cap, aiming at a quarter of the
    net's smallest pad; 2..25 mil. ``board`` is ``net_board``'s."""
    from ..layout import copper_raster as cr
    x0, y0, x1, y1 = cr._bounds(board)
    floor = 1.02 * math.sqrt(max(1.0, (x1 - x0) * (y1 - y0)) / cr.MAX_CELLS)
    probe = max(25.0, floor)
    coarse = cr.rasterize(board, probe, nets=[net], kinds={"pads", "regions", "planes"},
                          supersample=2)
    area = sum(coarse.copper_area_mils2(l) for l in coarse.layers)
    small = [min(c.w, c.h) for p in board.pads if p.net == net for c in p.copper
             if min(c.w, c.h) > 0]
    want = min(small) / 4.0 if small else 5.0
    budget = math.sqrt(area / max_nodes) if area > 0 else 0.0
    cell = max(budget, want, floor)
    cell = max(2.0, cell)
    if cell <= 25.0:
        cell = min(25.0, math.ceil(cell * 4) / 4.0)
    if notes is not None and cell > want * 1.01:
        why = "the node budget" if budget >= floor else "the board's size"
        notes.append(f"cell {cell:g} mil is coarser than a quarter of the smallest pad "
                     f"({want:g} mil) because of {why}; see convergence, or pass cell_mils")
    return cell


def _arc_chords(a) -> list[tuple[float, float, float, float]]:
    pts = geom.arc_points(a.cx, a.cy, a.radius, a.a1, a.a2, tol=0.05)
    return [(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]) for i in range(len(pts) - 1)]


#: Two points nearer than this are one point (mils).
COINCIDE = 0.05


def build_network(board: LayoutBoard, net: str, raster, stack, rho: float,
                  plating_mils: float):
    """Assemble the network. Returns (net, info) with node maps.

    Connectivity follows the copper, as Altium's does: two objects of the
    net are joined where their copper overlaps. A track is joined to a pad
    or a via land its capsule overlaps, to another track whose end lies in
    its copper or that crosses it, and to the area copper it runs into.
    """
    from scipy.spatial import cKDTree
    from ..layout.copper_raster import _within

    order = board.copper_layers()
    zmap = _layer_z(stack, order)
    N = _Net()
    h = raster.cell_mils
    sheet_g = {l: _layer_thickness(board, stack, l) * ph.MIL / rho for l in raster.layers}
    plane_layers = {l.name for l in board.layers if l.kind == "plane"}
    node_at: dict[int, tuple[str, float, float]] = {}

    # --- grid cells -------------------------------------------------------
    cell_id: dict[str, np.ndarray] = {}
    for layer in raster.layers:
        f = raster.fill[layer]
        ids = np.full(f.shape, -1, dtype=np.int64)
        on = f >= FILL_MIN
        k = int(on.sum())
        ids[on] = np.arange(N.n, N.n + k)
        N.n += k
        cell_id[layer] = ids
        g0 = sheet_g[layer]
        for ia, ib, lk in ((ids[:, :-1], ids[:, 1:], raster.link_x[layer]),
                           (ids[:-1, :], ids[1:, :], raster.link_y[layer])):
            both = (ia >= 0) & (ib >= 0) & (lk > 0)
            n_e = int(both.sum())
            N.a.extend(ia[both].tolist())
            N.b.extend(ib[both].tolist())
            N.g.extend((g0 * lk[both]).tolist())
            N.kind.extend(["cell"] * n_e)
            N.tag.extend([layer] * n_e)

    def cell_at(layer: str, x: float, y: float) -> tuple[int, float]:
        if layer not in cell_id:
            return -1, 0.0
        ix, iy = raster.cell_of(x, y)
        return int(cell_id[layer][iy, ix]), float(raster.fill[layer][iy, ix])

    def cells_in(layer: str, shape: geom.Shape, grow: float = 0.0,
                 fallback: bool = True) -> list[tuple[int, float]]:
        """Cells (id, fill) with copper whose centre lies in the shape
        grown by ``grow``; failing any, the cell under its first point."""
        if layer not in cell_id:
            return []
        x0, y0, x1, y1 = shape.bbox
        i0, j0 = raster.cell_of(x0 - grow, y0 - grow)
        i1, j1 = raster.cell_of(x1 + grow, y1 + grow)
        xs = raster.x0 + (np.arange(i0, i1 + 1) + 0.5) * h
        ys = raster.y0 + (np.arange(j0, j1 + 1) + 0.5) * h
        X, Y = np.meshgrid(xs, ys)
        ids = cell_id[layer][j0:j1 + 1, i0:i1 + 1]
        sel = _within(shape, X, Y, shape.r + grow) & (ids >= 0)
        out = list(zip(ids[sel].tolist(), raster.fill[layer][j0:j1 + 1, i0:i1 + 1][sel].tolist()))
        if not out and fallback:
            cid, fv = cell_at(layer, *shape.pts[0])
            if cid >= 0:
                out.append((cid, fv))
        return out

    def tie_cells(nid: int, layer: str, cells) -> None:
        seen = set()
        for cid, fv in cells:
            if cid in seen:
                continue
            seen.add(cid)
            N.edge(nid, cid, STIFF * sheet_g[layer] * max(fv, 0.05), "contact", layer)

    def contact(nid: int, layer: str, shape: geom.Shape, restore: bool = False) -> float:
        """Join a node to the area copper as an equipotential ``shape``.

        The cells whose centres lie in the shape take the node's potential.
        A cell just outside is joined to the node through the stretch of
        copper between its centre and the shape's edge, not through a whole
        cell (Shortley-Weller): without that the shape would act as if its
        edge ran through the outermost centres, half a cell inside.
        ``restore`` takes the links into the shape as whole copper (a drill
        hole inside it has cut them). Returns the share of the shape's rim
        that has copper beyond it."""
        if layer not in cell_id:
            return 0.0
        ids_all = cell_id[layer]
        x0, y0, x1, y1 = shape.bbox
        i0, j0 = raster.cell_of(x0 - h, y0 - h)
        i1, j1 = raster.cell_of(x1 + h, y1 + h)
        xs = raster.x0 + (np.arange(i0, i1 + 1) + 0.5) * h
        ys = raster.y0 + (np.arange(j0, j1 + 1) + 0.5) * h
        X, Y = np.meshgrid(xs, ys)
        ids = ids_all[j0:j1 + 1, i0:i1 + 1]
        fill = raster.fill[layer][j0:j1 + 1, i0:i1 + 1]
        inside = _within(shape, X, Y, shape.r)
        sel = inside & (ids >= 0)
        if not inside.any():
            cid, fv = cell_at(layer, (x0 + x1) / 2, (y0 + y1) / 2)
            if cid >= 0:
                tie_cells(nid, layer, [(cid, fv)])
            return 0.0
        tie_cells(nid, layer, zip(ids[sel].tolist(), fill[sel].tolist()))
        g0 = sheet_g[layer]
        lx, ly = raster.link_x[layer], raster.link_y[layer]
        rim = 0
        beyond = 0.0
        for dj, di in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            # outside cell (j, i) whose neighbour (j + dj, i + di) is inside
            nb = np.zeros_like(inside)
            src = inside[max(dj, 0):inside.shape[0] + min(dj, 0),
                         max(di, 0):inside.shape[1] + min(di, 0)]
            nb[max(-dj, 0):inside.shape[0] + min(-dj, 0),
               max(-di, 0):inside.shape[1] + min(-di, 0)] = src
            edge_ = (~inside) & nb
            rim += int(edge_.sum())
            jj, ii = np.nonzero(edge_ & (ids >= 0))
            if not jj.size:
                continue
            beyond += float(fill[jj, ii].sum())
            ax, ay = X[jj, ii], Y[jj, ii]
            lo = np.zeros(jj.size)
            hi = np.ones(jj.size)
            for _ in range(20):
                mid_ = (lo + hi) / 2
                inn = _within(shape, ax + di * h * mid_, ay + dj * h * mid_, shape.r)
                hi = np.where(inn, mid_, hi)
                lo = np.where(inn, lo, mid_)
            theta = np.maximum(hi, 0.02)
            gj, gi = jj + j0, ii + i0                    # in the whole grid
            if di:
                lk = lx[gj, np.minimum(gi, gi + di)]
            else:
                lk = ly[np.minimum(gj, gj + dj), gi]
            there = ids[jj + dj, ii + di] >= 0
            # the grid already joins the two cells with g0 lk when both exist
            have = np.where(there, g0 * lk, 0.0)
            want = g0 / theta if restore else np.where(there, g0 * lk / theta, g0 / theta)
            for cid, g in zip(ids[jj, ii].tolist(), (want - have).tolist()):
                N.edge(nid, cid, g, "contact", layer)
        return beyond / rim if rim else 0.0

    def contact_round(nid: int, layer: str, x: float, y: float, a: float,
                      a_hole: float = 0.0) -> None:
        """Join a node to the area copper as a round conductor of radius
        ``a``: a barrel wall, a round pad.

        A wide solid one is drawn on the grid like any shape. Otherwise the
        cells round it (all those its drill hole, radius ``a_hole``, cuts)
        are made one node, which on the grid stands for a disc of a known
        radius, and the copper between that radius and ``a`` is put in
        series: (Rs / 2 pi) ln(r_grid / a), the well model of Peaceman.
        For a few cells r_grid comes from the lattice Green's function
        (``physics.lattice_capacity_radius``); for many, the set is a
        circle drawn on the grid."""
        if layer not in cell_id:
            return
        g0 = sheet_g[layer]
        if a >= ROUND_AS_SHAPE * h:
            if a_hole <= 0:
                contact(nid, layer, geom.circle(x, y, 2 * a))
                return
            r_grid = max(a, a_hole + 0.71 * h)
            well = N.node()
            node_at[well] = (layer, x, y)
            phi = contact(well, layer, geom.circle(x, y, 2 * r_grid), restore=True)
            if phi < 0.25 or r_grid <= a * 1.001:
                N.edge(nid, well, STIFF * g0, "contact", layer)
            else:
                N.edge(nid, well, 2 * math.pi * g0 * phi / math.log(r_grid / a),
                       "contact", layer)
            return
        ids_all = cell_id[layer]
        fill_all = raster.fill[layer]
        ny_, nx_ = ids_all.shape
        ci, cj = raster.cell_of(x, y)

        def cid_at(di: int, dj: int) -> int:
            if 0 <= ci + di < nx_ and 0 <= cj + dj < ny_:
                return int(ids_all[cj + dj, ci + di])
            return -1
        rho = max(a_hole + 0.71 * h, a + 0.5 * h)
        for _ in range(6):
            k = int(math.ceil(rho / h)) + 1
            offs = [(di, dj) for di in range(-k, k + 1) for dj in range(-k, k + 1)
                    if math.hypot(raster.x0 + (ci + di + 0.5) * h - x,
                                  raster.y0 + (cj + dj + 0.5) * h - y) <= rho]
            if not offs:
                offs = [(0, 0)]
            r_grid = ph.lattice_capacity_radius(offs) * h
            if r_grid >= a:
                break
            rho += 0.25 * h
        members = set(offs)
        cells = [(cid_at(di, dj), float(fill_all[cj + dj, ci + di]))
                 for di, dj in offs if cid_at(di, dj) >= 0]
        if not cells:
            return
        rim = [(di, dj, di + ei, dj + ej) for di, dj in offs
               for ei, ej in ((1, 0), (-1, 0), (0, 1), (0, -1))
               if (di + ei, dj + ej) not in members]
        ring = {(oi, oj) for _, _, oi, oj in rim}
        phi = sum(float(fill_all[cj + oj, ci + oi]) for oi, oj in ring
                  if cid_at(oi, oj) >= 0) / max(1, len(ring))
        if phi < 0.25 or r_grid <= a * 1.001:
            # no sheet round it to spread in (a land on a track), or
            # nothing to add
            tie_cells(nid, layer, cells)
            return
        well = N.node()
        node_at[well] = (layer, x, y)
        tie_cells(well, layer, cells)
        if a_hole > 0:
            # the drill hole has cut the links out of the set; the well
            # model stands on whole copper there
            lx, ly = raster.link_x[layer], raster.link_y[layer]
            for di, dj, oi, oj in rim:
                c_in, c_out = cid_at(di, dj), cid_at(oi, oj)
                if c_out < 0:
                    continue
                if oj == dj:
                    lk = float(lx[cj + dj, ci + min(di, oi)])
                else:
                    lk = float(ly[cj + min(dj, oj), ci + di])
                if c_in >= 0:
                    N.edge(c_in, c_out, g0 * (1.0 - lk), "cell", layer)
                else:
                    N.edge(well, c_out, g0, "contact", layer)
        N.edge(nid, well, 2 * math.pi * g0 * phi / math.log(r_grid / a), "contact", layer)

    def contact_pad(nid: int, layer: str, shape: geom.Shape, a_hole: float = 0.0) -> None:
        if shape.kind == "point":
            contact_round(nid, layer, shape.pts[0][0], shape.pts[0][1], shape.r, a_hole)
        else:
            contact(nid, layer, shape)

    # --- pads ---------------------------------------------------------------
    pad_nodes: dict[int, dict[str, int]] = {}   # id(pad) -> layer -> node
    lands: dict[str, list[tuple[geom.Shape, int]]] = {}    # per layer: (shape, node)
    net_pads = [p for p in board.pads if p.net == net]
    for p in net_pads:
        through = p.hole > 0 and p.plated
        layers = order if through else (p.layers() or order[:1])
        per = {}
        for layer in layers:
            if layer not in sheet_g:
                continue
            shape = p.shape_on(layer)
            if shape is None and not through:
                continue
            nid = N.node()
            node_at[nid] = (layer, p.x, p.y)
            per[layer] = nid
            if through and (shape is None or layer in plane_layers):
                # no land (a plane has none): the barrel meets the copper
                # at the hole wall
                a_wall = p.hole / 2.0 + plating_mils
                shape = geom.circle(p.x, p.y, 2 * a_wall)
                contact_round(nid, layer, p.x, p.y, a_wall, p.hole / 2.0)
            else:
                # a soldered land is one potential
                contact_pad(nid, layer, shape, p.hole / 2.0 if through else 0.0)
            lands.setdefault(layer, []).append((shape, nid))
        pad_nodes[id(p)] = per

    # --- barrels -------------------------------------------------------------
    barrel_edges: list[dict] = []
    pad_by_xy = {(round(p.x, 3), round(p.y, 3), p.comp, p.name): p for p in net_pads}
    for bi, br in enumerate(raster.barrels):
        if br.net != net:
            continue
        layer_node: dict[str, int] = {}
        if br.kind == "pad":
            p = pad_by_xy.get((round(br.x, 3), round(br.y, 3), br.comp, br.pin))
            if p is not None:
                layer_node = dict(pad_nodes.get(id(p), {}))
        else:
            land = br.diameter_mils or (br.hole_mils + 8.0)
            for layer in br.layers:
                if layer not in sheet_g:
                    continue
                # The barrel meets a layer's area copper at the hole wall;
                # its land is that same sheet of copper, not a soldered
                # pad. A track ends on the land (a plane layer has none).
                a_wall = br.hole_mils / 2.0 + plating_mils
                dia = 2 * a_wall if layer in plane_layers else max(land, 2 * a_wall)
                nid = N.node()
                node_at[nid] = (layer, br.x, br.y)
                layer_node[layer] = nid
                lands.setdefault(layer, []).append((geom.circle(br.x, br.y, dia), nid))
                contact_round(nid, layer, br.x, br.y, a_wall, br.hole_mils / 2.0)
        span = [l for l in br.layers if l in layer_node]
        span.sort(key=lambda l: -zmap.get(l, 0.0))
        for la, lb in zip(span, span[1:]):
            dz = abs(zmap.get(la, 0.0) - zmap.get(lb, 0.0))
            if dz <= 0:
                dz = 1.0
            r = ph.via_barrel_resistance(dz, br.hole_mils, plating_mils, rho)
            tag = {"barrel": bi, "x": br.x, "y": br.y, "kind": br.kind,
                   "comp": br.comp, "pin": br.pin, "hole": br.hole_mils,
                   "from": la, "to": lb}
            N.edge(layer_node[la], layer_node[lb], 1.0 / r, "barrel", tag)
            barrel_edges.append(tag)

    # --- tracks and arcs -----------------------------------------------------
    segs: list[tuple] = []   # (layer, x1, y1, x2, y2, w, source, gain)
    seen_seg = set()
    for t in board.tracks:
        if t.net == net and not t.keepout and t.layer in sheet_g and t.width > 0:
            key = (t.layer, round(t.x1, 2), round(t.y1, 2), round(t.x2, 2), round(t.y2, 2))
            if key in seen_seg or key[1:3] == key[3:5]:
                continue
            seen_seg.add(key)
            seen_seg.add((key[0], key[3], key[4], key[1], key[2]))
            segs.append((t.layer, t.x1, t.y1, t.x2, t.y2, t.width, "track", 1.0))
    for a in board.arcs:
        if a.net == net and not a.keepout and a.layer in sheet_g and a.width > 0:
            # A bent strip conducts as rho theta / (t ln(r_o / r_i)): the
            # current takes the inside of the bend, so it is a little
            # better than its centreline length says (3 % at w = 0.6 r).
            gain = 1.0
            if a.radius > a.width / 2.0 * 1.001:
                gain = (a.radius / a.width) * math.log((a.radius + a.width / 2.0)
                                                       / (a.radius - a.width / 2.0))
            for c in _arc_chords(a):
                segs.append((a.layer, *c, a.width, "arc", gain))

    # Cut each piece where it runs inside area copper, which already
    # carries that current. Inside a pad or a via land the cut is exact
    # (where the centreline crosses the shape's edge) and the cut end
    # joins the pad's node; inside a pour it is taken from the grid and
    # the cut end is anchored to the cells across the track's end face.
    land_index: dict[str, tuple] = {}
    for layer, shapes in lands.items():
        centres = np.array([[(sh.bbox[0] + sh.bbox[2]) / 2, (sh.bbox[1] + sh.bbox[3]) / 2]
                            for sh, _ in shapes])
        reach = max(math.hypot(sh.bbox[2] - sh.bbox[0], sh.bbox[3] - sh.bbox[1]) / 2
                    for sh, _ in shapes)
        land_index[layer] = (cKDTree(centres), centres, reach, shapes)

    def land_cuts(layer, x1, y1, x2, y2, w, L):
        """[(t_in, t_out, node)] where the centreline lies inside a land."""
        idx = land_index.get(layer)
        if idx is None:
            return []
        tree, centres, reach, shapes = idx
        out = []
        cap = geom.capsule(x1, y1, x2, y2, w)
        for c in tree.query_ball_point(((x1 + x2) / 2, (y1 + y2) / 2), L / 2 + w / 2 + reach):
            shape, nid = shapes[c]
            if geom.clearance(cap, shape) > 0.0:
                continue
            bx0, by0, bx1, by1 = shape.bbox
            half = math.hypot(bx1 - bx0, by1 - by0) / 2
            tc = ((centres[c][0] - x1) * (x2 - x1) + (centres[c][1] - y1) * (y2 - y1)) / (L * L)
            lo, hi = max(0.0, tc - half / L), min(1.0, tc + half / L)
            if hi <= lo:
                continue

            def inside(t):
                return geom.clearance(
                    geom.circle(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, 0.0), shape) <= 0.0
            ts = np.linspace(lo, hi, 17)
            flags = [inside(t) for t in ts]
            k = 0
            while k < len(ts):
                if not flags[k]:
                    k += 1
                    continue
                j = k
                while j + 1 < len(ts) and flags[j + 1]:
                    j += 1
                t_in, t_out = ts[k], ts[j]
                if k > 0:                        # refine the entry
                    a_, b_ = ts[k - 1], ts[k]
                    for _ in range(24):
                        mid_ = (a_ + b_) / 2
                        if inside(mid_):
                            b_ = mid_
                        else:
                            a_ = mid_
                    t_in = b_
                if j + 1 < len(ts):              # refine the exit
                    a_, b_ = ts[j], ts[j + 1]
                    for _ in range(24):
                        mid_ = (a_ + b_) / 2
                        if inside(mid_):
                            a_ = mid_
                        else:
                            b_ = mid_
                    t_out = a_
                if t_out > t_in:
                    out.append((float(t_in), float(t_out), nid))
                k = j + 1
        return out

    pieces: list[list] = []     # [layer, x1, y1, x2, y2, w, src, {end: anchor}, gain]
    dropped = 0
    for (layer, x1, y1, x2, y2, w, src, gain) in segs:
        L = math.hypot(x2 - x1, y2 - y1)
        if L < 1e-6:
            continue
        cuts = land_cuts(layer, x1, y1, x2, y2, w, L)
        land_only = list(cuts)
        n = max(1, int(math.ceil(L / (h * 0.5))))
        tt = (np.arange(n) + 0.5) / n
        ix = np.clip(np.floor((x1 + (x2 - x1) * tt - raster.x0) / h).astype(int), 0, raster.nx - 1)
        iy = np.clip(np.floor((y1 + (y2 - y1) * tt - raster.y0) / h).astype(int), 0, raster.ny - 1)
        inside = raster.fill[layer][iy, ix] >= INSIDE_POUR

        def edge_of(t_out: float, t_in: float) -> float:
            """Where the centreline passes from a full cell (at t_in) to one
            that is not, towards t_out; t_out itself when it is full too."""
            for step in range(31):
                if step == 0:
                    tm_ = t_out
                else:
                    tm_ = (t_out + t_in) / 2
                cx_ = min(raster.nx - 1, max(0, int(math.floor((x1 + (x2 - x1) * tm_ - raster.x0) / h))))
                cy_ = min(raster.ny - 1, max(0, int(math.floor((y1 + (y2 - y1) * tm_ - raster.y0) / h))))
                if raster.fill[layer][cy_, cx_] >= INSIDE_POUR:
                    if step == 0:
                        return t_out
                    t_in = tm_
                else:
                    t_out = tm_
            return t_in
        i = 0
        while i < n:
            if not inside[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and inside[j + 1]:
                j += 1
            # the run ends where the centreline leaves the last full cell
            ta = edge_of(float(tt[i - 1]) if i else 0.0, float(tt[i]))
            tb = edge_of(float(tt[j + 1]) if j < n - 1 else 1.0, float(tt[j]))
            cuts.append((ta, tb, None))
            if not any(la - 1e-9 <= ta and tb <= lb + 1e-9 for la, lb, _ in land_only):
                dropped += 1            # a pour, not just the cells of a pad
            i = j + 1
        if not cuts:
            pieces.append([layer, x1, y1, x2, y2, w, src, {}, gain])
            continue
        # union of the cut intervals; each side remembers what it ends in
        cuts.sort(key=lambda c: c[0])
        merged: list[list] = []
        for ta, tb, nid in cuts:
            if merged and ta <= merged[-1][1] + 1e-12:
                if tb > merged[-1][1]:
                    merged[-1][1] = tb
                    merged[-1][3] = nid
            else:
                merged.append([ta, tb, nid, nid])

        def anchor(t_edge, into, nid):
            if nid is not None:
                return ("node", nid)
            tm = min(1.0, max(0.0, t_edge + into * 0.5 / n))
            return ("cells", (x1 + (x2 - x1) * tm, y1 + (y2 - y1) * tm))
        prev_t, prev_anchor = 0.0, None
        for ta, tb, nid_in, nid_out in merged + [[1.0, 1.0, None, None]]:
            if (ta - prev_t) * L > COINCIDE:
                anc = {}
                if prev_anchor is not None:
                    anc[0] = prev_anchor
                if ta < 1.0 or tb > ta:
                    anc[1] = anchor(ta, +1, nid_in)
                pieces.append([layer, x1 + (x2 - x1) * prev_t, y1 + (y2 - y1) * prev_t,
                               x1 + (x2 - x1) * ta, y1 + (y2 - y1) * ta, w, src, anc, gain])
            prev_t, prev_anchor = tb, anchor(tb, -1, nid_out)

    track_tags: list[dict] = []
    floating_ends = 0
    for layer in sorted({p[0] for p in pieces}):
        P = [p for p in pieces if p[0] == layer]
        m = len(P)
        A = np.array([[p[1], p[2]] for p in P])
        B = np.array([[p[3], p[4]] for p in P])
        W = np.array([p[5] for p in P])
        D = B - A
        Ln = np.hypot(D[:, 0], D[:, 1])
        wmax = float(W.max())
        gstiff = STIFF * sheet_g[layer]
        # points: 2 i + e is end e of piece i; splits are appended
        pts: list[tuple[float, float]] = []
        for i in range(m):
            pts += [tuple(A[i]), tuple(B[i])]
        splits: list[list[tuple[float, int]]] = [[] for _ in range(m)]   # (t, point)
        ties: list[tuple[int, int]] = []          # point - point, stiff
        node_ties: list[tuple[int, int]] = []     # point - node, stiff

        def split(i: int, t: float) -> int:
            """The point of piece i at parameter t (an end, or a new split)."""
            if t * Ln[i] <= COINCIDE:
                return 2 * i
            if (1.0 - t) * Ln[i] <= COINCIDE:
                return 2 * i + 1
            for (t0, k) in splits[i]:
                if abs(t0 - t) * Ln[i] <= COINCIDE:
                    return k
            pts.append((A[i][0] + D[i][0] * t, A[i][1] + D[i][1] * t))
            splits[i].append((t, len(pts) - 1))
            return len(pts) - 1

        def project(i: int, x: float, y: float) -> tuple[float, float]:
            """(unclamped parameter, distance to the clamped point) on piece i."""
            t = ((x - A[i][0]) * D[i][0] + (y - A[i][1]) * D[i][1]) / (Ln[i] * Ln[i])
            tc = min(1.0, max(0.0, t))
            return t, math.hypot(x - (A[i][0] + D[i][0] * tc), y - (A[i][1] + D[i][1] * tc))

        ends = np.vstack([A, B])                  # row k < m: end 0 of k; else end 1
        end_tree = cKDTree(ends)
        mid = (A + B) / 2.0

        # (a) another piece's end lying in this piece's copper, between its ends
        for i in range(m):
            for c in end_tree.query_ball_point(mid[i], Ln[i] / 2 + (W[i] + wmax) / 2):
                j, e = (c, 0) if c < m else (c - m, 1)
                if j == i:
                    continue
                ex, ey = ends[c]
                t, d = project(i, ex, ey)
                if not (0.0 < t < 1.0) or d > (W[i] + W[j]) / 2.0:
                    continue
                if min(t, 1.0 - t) * Ln[i] <= COINCIDE:
                    continue                       # an end-to-end meeting
                k = split(i, t)
                ties.append((k, 2 * j + e))

        # (b) two pieces crossing between their ends
        if m > 1:
            bucket: dict[tuple[int, int], list[int]] = {}
            size = max(200.0, float(np.median(Ln)) * 2)
            for i in range(m):
                bx0, bx1 = sorted((A[i][0], B[i][0])); by0, by1 = sorted((A[i][1], B[i][1]))
                for gx in range(int(bx0 // size), int(bx1 // size) + 1):
                    for gy in range(int(by0 // size), int(by1 // size) + 1):
                        bucket.setdefault((gx, gy), []).append(i)
            done = set()
            for members in bucket.values():
                for u in range(len(members)):
                    i = members[u]
                    for v in range(u + 1, len(members)):
                        j = members[v]
                        if (i, j) in done:
                            continue
                        done.add((i, j))
                        den = D[i][0] * D[j][1] - D[i][1] * D[j][0]
                        if abs(den) < 1e-12:
                            continue
                        qx, qy = A[j][0] - A[i][0], A[j][1] - A[i][1]
                        ti = (qx * D[j][1] - qy * D[j][0]) / den
                        tj = (qx * D[i][1] - qy * D[i][0]) / den
                        if (COINCIDE < ti * Ln[i] < Ln[i] - COINCIDE
                                and COINCIDE < tj * Ln[j] < Ln[j] - COINCIDE):
                            ties.append((split(i, ti), split(j, tj)))

        # (c) a pad or a via land the piece's copper overlaps
        shapes = lands.get(layer, [])
        if shapes:
            centres = np.array([[(s.bbox[0] + s.bbox[2]) / 2, (s.bbox[1] + s.bbox[3]) / 2]
                                for s, _ in shapes])
            reach = max(math.hypot(s.bbox[2] - s.bbox[0], s.bbox[3] - s.bbox[1]) / 2
                        for s, _ in shapes)
            land_tree = cKDTree(centres)
            for i in range(m):
                cap = geom.capsule(A[i][0], A[i][1], B[i][0], B[i][1], W[i])
                for c in land_tree.query_ball_point(mid[i], Ln[i] / 2 + W[i] / 2 + reach):
                    shape, nid = shapes[c]
                    if geom.clearance(cap, shape) > 0.0:
                        continue
                    t, d = project(i, centres[c][0], centres[c][1])
                    if not (0.0 < t < 1.0):
                        # The nearest point is an end. When another piece
                        # carries on from that end towards the land (the
                        # next chord of an arc, the stub left by a cut),
                        # that piece makes the joint, not this one: tying
                        # every piece whose width reaches the land would
                        # short the track along its last few mils.
                        e = 0 if t <= 0.0 else 1
                        nearer = False
                        for c2 in end_tree.query_ball_point(ends[i + e * m], COINCIDE):
                            j = c2 if c2 < m else c2 - m
                            if j != i and project(j, centres[c][0], centres[c][1])[1] < d - 1e-9:
                                nearer = True
                                break
                        if nearer:
                            continue
                    node_ties.append((split(i, min(1.0, max(0.0, t))), nid))

        # points that coincide are one node
        arr = np.array(pts)
        parent = list(range(len(pts)))

        def find(u: int) -> int:
            while parent[u] != u:
                parent[u] = parent[parent[u]]
                u = parent[u]
            return u
        for u, v in cKDTree(arr).query_pairs(COINCIDE):
            parent[find(u)] = find(v)
        node_of: dict[int, int] = {}

        def node(k: int) -> int:
            r = find(k)
            if r not in node_of:
                node_of[r] = N.node()
                node_at[node_of[r]] = (layer, pts[r][0], pts[r][1])
            return node_of[r]

        degree: dict[int, int] = {}
        tied: set[int] = set()
        for i in range(m):
            ts = sorted([(0.0, 2 * i)] + splits[i] + [(1.0, 2 * i + 1)])
            for (t0, k0), (t1, k1) in zip(ts, ts[1:]):
                n0, n1 = node(k0), node(k1)
                length = (t1 - t0) * Ln[i]
                if n0 == n1 or length <= 0:
                    continue
                x0_, y0_ = A[i] + D[i] * t0
                x1_, y1_ = A[i] + D[i] * t1
                tag = {"layer": layer, "x1": float(x0_), "y1": float(y0_),
                       "x2": float(x1_), "y2": float(y1_),
                       "width": float(W[i]), "source": P[i][6]}
                N.edge(n0, n1, sheet_g[layer] * W[i] / length * P[i][8], "track", tag)
                track_tags.append(tag)
                degree[n0] = degree.get(n0, 0) + 1
                degree[n1] = degree.get(n1, 0) + 1
        for k0, k1 in ties:
            n0, n1 = node(k0), node(k1)
            N.edge(n0, n1, gstiff, "contact", layer)
            tied.update((n0, n1))
        for k, nid in node_ties:
            N.edge(node(k), nid, gstiff, "contact", layer)
            tied.add(node(k))
        # cut ends go into the area copper across the track's end face
        for i in range(m):
            for e, (how, what) in P[i][7].items():
                nd = node(2 * i + e)
                tied.add(nd)
                if how == "node":
                    N.edge(nd, what, gstiff, "contact", layer)
                    continue
                ax, ay = what
                ux, uy = D[i] / Ln[i]
                half = W[i] / 2.0
                face = geom.capsule(ax - uy * half, ay + ux * half,
                                    ax + uy * half, ay - ux * half, 0.0)
                tie_cells(nd, layer, cells_in(layer, face, grow=h * 0.5))
        # (d) an end still joined to nothing: area copper under it, else a
        # neighbour's end its round cap overlaps
        for i in range(m):
            for e in (0, 1):
                nd = node(2 * i + e)
                if nd in tied or degree.get(nd, 0) != 1:
                    continue
                x, y = pts[2 * i + e]
                cid, fv = cell_at(layer, x, y)
                if cid >= 0 and fv >= TOUCH:
                    ux, uy = D[i] / Ln[i]
                    half = W[i] / 2.0
                    face = geom.capsule(x - uy * half, y + ux * half,
                                        x + uy * half, y - ux * half, 0.0)
                    tie_cells(nd, layer, cells_in(layer, face, grow=h * 0.5))
                    tied.add(nd)
                    continue
                best = None
                for c in end_tree.query_ball_point((x, y), (W[i] + wmax) / 2):
                    j = c if c < m else c - m
                    if j == i:
                        continue
                    d = math.hypot(ends[c][0] - x, ends[c][1] - y)
                    if d <= (W[i] + W[j]) / 2.0 and (best is None or d < best[0]):
                        best = (d, 2 * j + (0 if c < m else 1))
                if best is not None:
                    N.edge(nd, node(best[1]), gstiff, "contact", layer)
                    tied.add(nd)
                else:
                    floating_ends += 1

    info = {"cell_id": cell_id, "pad_nodes": pad_nodes, "node_at": node_at,
            "sheet_g": sheet_g, "segments_dropped_in_pour": dropped,
            "tracks": len(track_tags), "barrels": len(barrel_edges),
            "open_track_ends": floating_ends}
    return N, info


def _solve(N: _Net, fixed: dict[int, float], inject: dict[int, float]):
    """Node voltages with ``fixed`` held and ``inject`` amps flowing in.
    Returns (V, reached) with nodes not connected to any fixed node NaN."""
    import scipy.sparse as sp
    from scipy.sparse.csgraph import connected_components
    from scipy.sparse.linalg import spsolve

    n = N.n
    a = np.asarray(N.a, dtype=np.int64)
    b = np.asarray(N.b, dtype=np.int64)
    g = np.asarray(N.g, dtype=float)
    adj = sp.coo_matrix((np.ones_like(g), (a, b)), shape=(n, n)).tocsr()
    ncomp, labels = connected_components(adj, directed=False)
    live_labels = {labels[k] for k in fixed}
    live = np.isin(labels, list(live_labels))
    V = np.full(n, np.nan)
    rows = np.concatenate([a, b, a, b])
    cols = np.concatenate([a, b, b, a])
    vals = np.concatenate([g, g, -g, -g])
    G = sp.coo_matrix((vals, (rows, cols)), shape=(n, n)).tocsr()
    I = np.zeros(n)
    for k, amps in inject.items():
        I[k] += amps
    fixed_idx = np.array(sorted(fixed), dtype=np.int64)
    fixed_v = np.array([fixed[k] for k in sorted(fixed)])
    free = np.where(live)[0]
    free = free[~np.isin(free, fixed_idx)]
    if free.size:
        Gff = G[free][:, free].tocsc()
        rhs = I[free] - G[free][:, fixed_idx] @ fixed_v
        V[free] = spsolve(Gff, rhs)
    V[fixed_idx] = fixed_v
    return V, live


def _png(path, arr: np.ndarray, vmin: float, vmax: float) -> None:
    from PIL import Image
    span = vmax - vmin if vmax > vmin else 1.0
    t = np.clip((np.nan_to_num(arr, nan=vmin) - vmin) / span, 0, 1)
    # perceptual-ish ramp: dark blue -> cyan -> yellow -> red
    stops = np.array([[0.13, 0.17, 0.48], [0.0, 0.6, 0.8], [0.95, 0.85, 0.2], [0.85, 0.15, 0.1]])
    pos = t * (len(stops) - 1)
    i0 = np.clip(np.floor(pos).astype(int), 0, len(stops) - 2)
    frac = (pos - i0)[..., None]
    rgb = stops[i0] * (1 - frac) + stops[i0 + 1] * frac
    img = (rgb * 255).astype(np.uint8)
    img[np.isnan(arr)] = (24, 24, 24)
    img = img[::-1]  # y up
    scale = max(1, int(800 / max(arr.shape)))
    Image.fromarray(img).resize((arr.shape[1] * scale, arr.shape[0] * scale),
                                Image.NEAREST).save(path)


def _run_one(board, net, stack, sources, sinks, cell, rho, plating, sink_model,
             ground: bool):
    from ..layout.copper_raster import rasterize
    raster = rasterize(board, cell, nets=[net], kinds={"pads", "vias", "regions", "planes"},
                       supersample=4, links=True)
    N, info = build_network(board, net, raster, stack, rho, plating)
    fixed: dict[int, float] = {}
    inject: dict[int, float] = {}
    sink_info = []
    for src in sources:
        pads = resolve_pads(board, net, src)
        if not pads:
            raise IRDropError(f"source {src} has no pads on net {net}")
        v = 0.0 if ground else float(src["volts"])
        for p in pads:
            for nid in info["pad_nodes"].get(id(p), {}).values():
                fixed[nid] = v
    for snk in sinks:
        pads = resolve_pads(board, net, snk)
        amps = float(snk["amps"])
        if not pads:
            raise IRDropError(f"sink {snk} has no pads on net {net}")
        sign = 1.0 if ground else -1.0
        nodes_per_pad = [list(info["pad_nodes"].get(id(p), {}).values()) for p in pads]
        if sink_model == "equipotential":
            hub = N.node()
            for nodes in nodes_per_pad:
                for nid in nodes:
                    N.edge(hub, nid, 1e6, "hub")
            inject[hub] = inject.get(hub, 0.0) + sign * amps
        else:
            per = amps / len(pads)
            for nodes in nodes_per_pad:
                if not nodes:
                    continue
                pin = nodes[0]
                if len(nodes) > 1:
                    # a through-hole pin is one piece of metal (lead, solder
                    # and barrel) on every layer, as a source pin is
                    pin = N.node()
                    for nid in nodes:
                        N.edge(pin, nid, 1e6, "hub")
                inject[pin] = inject.get(pin, 0.0) + sign * per
        sink_info.append((snk, pads, nodes_per_pad))
    if not fixed:
        raise IRDropError("no source pads resolved")
    V, live = _solve(N, fixed, inject)
    return raster, N, info, V, live, sink_info


_SOURCE_KEYS = {"ref", "refs", "pads", "volts", "name", "return_pads"}
_SINK_KEYS = {"ref", "refs", "pads", "amps", "name", "return_pads"}


def check_specs(sources: list[dict], sinks: list[dict]) -> None:
    """Sources need a voltage and sinks a current: a missing or misspelt
    one must be an error, never a silent zero."""
    if not sources:
        raise IRDropError("give at least one source: [{ref or pads, volts}]")
    if not sinks:
        raise IRDropError("give at least one sink: [{ref or pads, amps}]")
    for kind, specs, keys, num in (("source", sources, _SOURCE_KEYS, "volts"),
                                   ("sink", sinks, _SINK_KEYS, "amps")):
        for spec in specs:
            if not isinstance(spec, dict):
                raise IRDropError(f"each {kind} is a dict, got {spec!r}")
            unknown = sorted(set(spec) - keys)
            if unknown:
                raise IRDropError(f"{kind} {_label(spec)}: unknown key(s) {unknown}; "
                                  f"use {sorted(keys)}")
            if not (spec.get("ref") or spec.get("refs") or spec.get("pads")):
                raise IRDropError(f"{kind} {spec!r} needs ref or pads")
            try:
                v = float(spec[num])
            except (KeyError, TypeError, ValueError):
                raise IRDropError(f"{kind} {_label(spec)} needs {num} (a number)") from None
            if not math.isfinite(v) or v <= 0:
                raise IRDropError(f"{kind} {_label(spec)}: {num} must be above 0, got {v:g}")


def return_spec(board: LayoutBoard, net: str, spec: dict) -> dict:
    """The same source or sink on the return net: ``return_pads`` when
    given, else the return-net pads of the components its pads are on."""
    out = {k: v for k, v in spec.items() if k in ("volts", "amps", "name")}
    out.setdefault("name", _label(spec))
    if spec.get("return_pads"):
        out["pads"] = spec["return_pads"]
    else:
        out["ref"] = sorted({p.comp for p in resolve_pads(board, net, spec) if p.comp})
    return out


def _label(snk: dict) -> str:
    if snk.get("name"):
        return str(snk["name"])
    r = snk.get("ref") or snk.get("pads") or "?"
    return ",".join(r) if isinstance(r, list) else str(r)


def analyse_net(board: LayoutBoard, net: str, stack, sources: list[dict],
                sinks: list[dict], *, cell_mils: float = 0.0, temp_c: float = 25.0,
                rho_factor: float = 1.0, plating_mils: float = ph.DEFAULT_PLATING_MILS,
                sink_model: str = "per_pin", ground: bool = False,
                temp_rise_c: float = 10.0, check_convergence: bool = True,
                limit: int = 15) -> dict:
    """Solve one net. ``ground`` makes the sources 0 V returns and the
    sinks inject their current (the return path's rise)."""
    if sink_model not in ("per_pin", "equipotential"):
        raise IRDropError("sink_model is per_pin or equipotential")
    if not (plating_mils > 0):
        raise IRDropError("plating_mils must be above 0")
    check_specs(sources, sinks)
    if net not in board.nets():
        raise IRDropError(f"net {net!r} is not on the board")
    rho = ph.copper_resistivity(temp_c, rho_factor)
    has_planes = any(l.kind == "plane" for l in board.layers)
    board, notes = net_board(board, net)
    cell = (float(cell_mils) if cell_mils and cell_mils > 0
            else auto_cell_mils(board, net, notes=notes))
    raster, N, info, V, live, sink_info = _run_one(
        board, net, stack, sources, sinks, cell, rho, plating_mils, sink_model, ground)
    vref = 0.0 if ground else max(float(s["volts"]) for s in sources)

    # ---- sinks
    out_sinks = []
    for snk, pads, nodes_per_pad in sink_info:
        pins = []
        for p, nodes in zip(pads, nodes_per_pad):
            vs = [V[n] for n in nodes if not math.isnan(V[n])]
            pins.append({"pad": f"{p.comp}.{p.name}",
                         "volts": round(float(np.mean(vs)), 7) if vs else None})
        vals = [p["volts"] for p in pins if p["volts"] is not None]
        if len(vals) < len(pins):
            raise IRDropError(f"sink {_label(snk)} is not connected to a source "
                              f"through net {net}'s copper")
        worst = min(vals) if not ground else max(vals)
        best = max(vals) if not ground else min(vals)
        drop = (vref - worst) if not ground else worst
        pins.sort(key=lambda q: q["volts"], reverse=ground)
        out_sinks.append({"sink": _label(snk), "amps": float(snk["amps"]),
                          "pin_count": len(pins),
                          "worst_pin": pins[0]["pad"],
                          "worst_pin_volts": round(worst, 6),
                          "best_pin_volts": round(best, 6),
                          "mean_pin_volts": round(float(np.mean(vals)), 6),
                          "drop_mv": round(drop * 1e3, 4),
                          "drop_pct": round(100 * drop / vref, 4) if vref else None,
                          "pins": pins[:limit],
                          "_pins": pins})

    # ---- branch currents
    a = np.asarray(N.a); b = np.asarray(N.b); g = np.asarray(N.g)
    dv = V[a] - V[b]
    ok = ~np.isnan(dv)
    cur = np.where(ok, g * np.nan_to_num(dv), 0.0)
    loss = np.where(ok, g * np.nan_to_num(dv) ** 2, 0.0)
    kinds = np.asarray(N.kind)
    total_loss = float(loss.sum())

    loss_by_layer: dict[str, float] = {}
    tracks = []
    vias: dict[int, dict] = {}
    for k in np.nonzero(ok & (kinds != "cell") & (kinds != "contact") & (kinds != "hub"))[0]:
        tag = N.tag[k]
        if N.kind[k] == "track":
            w, layer = tag["width"], tag["layer"]
            t_m = _layer_thickness(board, stack, layer) * ph.MIL
            amps = float(abs(cur[k]))
            j = amps / (w * ph.MIL * t_m) / 1e6
            outer = layer in (board.copper_layers()[:1] + board.copper_layers()[-1:])
            cap = ph.ipc2221_current_capacity(w * t_m / ph.MIL, temp_rise_c, outer)
            tracks.append({"layer": layer, "from": [round(tag["x1"], 2), round(tag["y1"], 2)],
                           "to": [round(tag["x2"], 2), round(tag["y2"], 2)],
                           "width_mils": round(w, 3), "amps": round(amps, 5),
                           "j_a_per_mm2": round(j, 3),
                           "ipc2221_capacity_a": round(cap, 3),
                           "over_capacity": bool(cap and amps > cap)})
            loss_by_layer[layer] = loss_by_layer.get(layer, 0.0) + float(loss[k])
        elif N.kind[k] == "barrel":
            bi = tag["barrel"]
            e = vias.setdefault(bi, {"at": [round(tag["x"], 2), round(tag["y"], 2)],
                                     "kind": tag["kind"], "hole_mils": tag["hole"],
                                     "pad": f"{tag['comp']}.{tag['pin']}" if tag["comp"] else "",
                                     "amps": 0.0, "loss_w": 0.0})
            e["amps"] = max(e["amps"], abs(float(cur[k])))
            e["loss_w"] += float(loss[k])
    for k in np.nonzero(ok & ((kinds == "cell") | (kinds == "contact")))[0]:
        layer = N.tag[k]
        loss_by_layer[layer] = loss_by_layer.get(layer, 0.0) + float(loss[k])

    # ---- area current density per cell (A/mm2) from link currents
    h_m = raster.cell_mils * ph.MIL
    planes = []
    jmaps: dict[str, np.ndarray] = {}
    vmaps: dict[str, np.ndarray] = {}
    cellmask = kinds == "cell"
    ca, cb, ccur = a[cellmask], b[cellmask], cur[cellmask]
    floating_cells = 0
    for layer in raster.layers:
        ids = info["cell_id"][layer]
        on = ids >= 0
        if not on.any():
            continue
        vm = np.full(ids.shape, np.nan)
        vm[on] = V[ids[on]]
        vmaps[layer] = vm
        # map node id -> (iy, ix)
        lo, hi = ids[on].min(), ids[on].max()
        pos = np.full(hi - lo + 1, -1, dtype=np.int64)
        flat = np.flatnonzero(on)
        pos[ids[on] - lo] = flat
        sel = (ca >= lo) & (ca <= hi) & (cb >= lo) & (cb <= hi)
        fa = pos[ca[sel] - lo]; fb = pos[cb[sel] - lo]
        ii = ccur[sel]
        horiz = (fb - fa) == 1
        Ix = np.zeros(ids.size); Iy = np.zeros(ids.size)
        np.add.at(Ix, fa[horiz], ii[horiz] / 2); np.add.at(Ix, fb[horiz], ii[horiz] / 2)
        np.add.at(Iy, fa[~horiz], ii[~horiz] / 2); np.add.at(Iy, fb[~horiz], ii[~horiz] / 2)
        fill = raster.fill[layer].ravel()
        t_m = _layer_thickness(board, stack, layer) * ph.MIL
        with np.errstate(divide="ignore", invalid="ignore"):
            J = np.hypot(Ix, Iy) / (h_m * t_m * np.maximum(fill, 0.25)) / 1e6
        J = J.reshape(ids.shape)
        J[~on] = np.nan
        jmaps[layer] = J
        live_cells = np.isfinite(vm)
        floating_cells += int((on & ~live_cells & (raster.fill[layer] >= 0.5)).sum())
        J[~live_cells] = np.nan
        k = int(np.nanargmax(J)) if live_cells.any() and np.isfinite(J).any() else -1
        if k >= 0:
            iy, ix = divmod(k, ids.shape[1])
            cx, cy = raster.centre(ix, iy)
            planes.append({"layer": layer, "max_j_a_per_mm2": round(float(J[iy, ix]), 3),
                           "at": [round(cx, 1), round(cy, 1)],
                           "max_sheet_a_per_mm": round(float(J[iy, ix]) * t_m * 1e3, 4),
                           "min_volts": round(float(np.nanmin(vm)), 6),
                           "max_volts": round(float(np.nanmax(vm)), 6),
                           "loss_w": round(loss_by_layer.get(layer, 0.0), 6)})

    # consecutive pieces of one trace carry one current: list each run once
    runs: dict[tuple, dict] = {}
    for t in tracks:
        length = math.hypot(t["to"][0] - t["from"][0], t["to"][1] - t["from"][1])
        key = (t["layer"], t["width_mils"], round(t["amps"], 4))
        r0 = runs.get(key)
        if r0 is None:
            runs[key] = dict(t, length_mils=length, pieces=1, _longest=length)
        else:
            r0["length_mils"] += length
            r0["pieces"] += 1
            if length > r0["_longest"]:
                r0.update({"from": t["from"], "to": t["to"], "_longest": length})
    n_over = sum(1 for t in tracks if t["over_capacity"])
    tracks = list(runs.values())
    for t in tracks:
        t.pop("_longest")
        t["length_mils"] = round(t["length_mils"], 1)
    tracks.sort(key=lambda t: -t["j_a_per_mm2"])
    via_list = sorted(vias.values(), key=lambda v: -v["amps"])
    for v in via_list:
        cap = ph.ipc2221_current_capacity(ph.barrel_area_m2(v["hole_mils"], plating_mils)
                                          / ph.MIL ** 2, temp_rise_c, external=False)
        v["ipc2221_capacity_a"] = round(cap, 3)
        v["over_capacity"] = bool(cap and v["amps"] > cap)
        v["amps"] = round(v["amps"], 5)
        v["loss_w"] = round(v["loss_w"], 7)

    worst = max(out_sinks, key=lambda s: s["drop_mv"]) if out_sinks else None
    result = {
        "net": net, "role": "return" if ground else "supply",
        "cell_mils": raster.cell_mils, "nodes": N.n, "temp_c": temp_c,
        "rho_ohm_m": rho, "plating_mils": plating_mils, "sink_model": sink_model,
        "sinks": out_sinks,
        "worst": {"sink": worst["sink"], "drop_mv": worst["drop_mv"],
                  "drop_pct": worst["drop_pct"]} if worst else None,
        "total_amps": round(sum(float(s["amps"]) for s in sinks), 6),
        "loss_w": round(total_loss, 7),
        "layers": planes,
        "tracks": tracks[:limit],
        "tracks_over_capacity": n_over,
        "vias": via_list[:limit],
        "vias_over_capacity": sum(1 for v in via_list if v["over_capacity"]),
        "network": {"tracks": info["tracks"], "barrels": info["barrels"],
                    "track_parts_inside_pours": info["segments_dropped_in_pour"],
                    "open_track_ends": info["open_track_ends"]},
        "notes": notes + [n for n in raster.notes
                          if has_planes or not n.startswith("plane clearance")],
    }
    if floating_cells:
        area = floating_cells * raster.cell_mils ** 2 / 1e6
        result["notes"].append(
            f"{area:.3f} in2 of {net} copper is not connected to any source through "
            f"the net's own copper (islands, or copper joined only through another net) "
            f"and carries no current here")
    if check_convergence and worst and raster.cell_mils * 2 <= 50:
        try:
            r2, N2, i2, V2, l2, s2 = _run_one(board, net, stack, sources, sinks,
                                              raster.cell_mils * 2, rho, plating_mils,
                                              sink_model, ground)
            worst2 = 0.0
            for snk, pads, nodes_per_pad in s2:
                vals = [float(np.mean([V2[n] for n in nodes])) for nodes in nodes_per_pad
                        if nodes]
                vals = [v for v in vals if math.isfinite(v)]
                if not vals:
                    raise IRDropError("the coarse grid does not connect the sink")
                w2 = (vref - min(vals)) if not ground else max(vals)
                worst2 = max(worst2, w2)
            d1 = worst["drop_mv"] / 1e3
            # The scheme is second order inside the copper but first order
            # at pad and copper edges (staircase), so the change from the
            # coarse grid is taken whole as the error left in the fine one.
            err = abs(d1 - worst2)
            result["convergence"] = {
                "coarse_cell_mils": r2.cell_mils,
                "coarse_worst_drop_mv": round(worst2 * 1e3, 4),
                "estimated_error_mv": round(err * 1e3, 4),
                "estimated_error_pct_of_drop": round(100 * err / d1, 2) if d1 > 0 else 0.0,
            }
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            result["convergence"] = {"error": str(exc)}
    result["_maps"] = {"raster": raster, "V": vmaps, "J": jmaps, "N": N, "Vn": V,
                       "loss": loss, "info": info}
    return result


def power_density(result: dict) -> dict[str, np.ndarray]:
    """Joule heat per grid cell (W), per layer, on the raster's grid, for
    the thermal solver. Cell links put half their loss in each cell;
    tracks spread theirs along their length; barrels put theirs in the
    cell of the barrel, half on each layer they join."""
    m = result["_maps"]
    raster, N, loss = m["raster"], m["N"], m["loss"]
    out = {l: np.zeros((raster.ny, raster.nx)) for l in raster.layers}
    node_at = m["info"]["node_at"]
    where: dict[int, tuple[str, int, int]] = {}
    for layer, ids in m["info"]["cell_id"].items():
        iy, ix = np.nonzero(ids >= 0)
        for a_, b_, c_ in zip(ids[iy, ix], iy, ix):
            where[int(a_)] = (layer, int(b_), int(c_))
    for k, kind in enumerate(N.kind):
        q = float(loss[k])
        if q == 0.0:
            continue
        if kind in ("cell", "contact"):
            ends = []
            for e in (N.a[k], N.b[k]):
                if e in where:
                    ends.append(where[e])
                elif e in node_at:
                    layer, x, y = node_at[e]
                    ix, iy = raster.cell_of(x, y)
                    ends.append((layer, iy, ix))
            for layer, iy, ix in ends:
                out[layer][iy, ix] += q / len(ends)
        elif kind == "track":
            t = N.tag[k]
            L = math.hypot(t["x2"] - t["x1"], t["y2"] - t["y1"])
            n = max(1, int(math.ceil(L / raster.cell_mils)))
            for i in range(n):
                s = (i + 0.5) / n
                ix, iy = raster.cell_of(t["x1"] + (t["x2"] - t["x1"]) * s,
                                        t["y1"] + (t["y2"] - t["y1"]) * s)
                if t["layer"] in out:
                    out[t["layer"]][iy, ix] += q / n
        elif kind == "barrel":
            t = N.tag[k]
            ix, iy = raster.cell_of(t["x"], t["y"])
            for layer in (t["from"], t["to"]):
                if layer in out:
                    out[layer][iy, ix] += q / 2
    return out


def write_maps(result: dict, folder) -> list[str]:
    """Voltage and current-density PNGs per layer plus an .npz with the
    arrays (V in volts, J in A/mm2, heat in W per cell) for other tools."""
    from pathlib import Path
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    m = result["_maps"]
    files = []
    vals = [np.nanmin(v) for v in m["V"].values()] + [np.nanmax(v) for v in m["V"].values()]
    vmin, vmax = (min(vals), max(vals)) if vals else (0.0, 1.0)
    net = safe_name(result["net"])
    for layer, vm in m["V"].items():
        p = folder / f"{net}_{layer}_volts.png"
        _png(p, vm, vmin, vmax)
        files.append(str(p))
        J = m["J"].get(layer)
        if J is not None and np.isfinite(J).any():
            p = folder / f"{net}_{layer}_current_density.png"
            _png(p, J, 0.0, float(np.nanpercentile(J, 99.5)) or 1.0)
            files.append(str(p))
    heat = power_density(result)
    raster = m["raster"]
    npz = folder / f"{net}_ir_drop.npz"
    np.savez_compressed(npz, x0=raster.x0, y0=raster.y0, cell_mils=raster.cell_mils,
                        layers=np.array(raster.layers),
                        **{f"V_{l}": v for l, v in m["V"].items()},
                        **{f"J_{l}": v for l, v in m["J"].items()},
                        **{f"W_{l}": v for l, v in heat.items()})
    files.append(str(npz))
    pins = folder / f"{net}_pins.csv"
    with open(pins, "w", encoding="utf-8") as fh:
        fh.write("sink,pad,volts\n")
        for snk in result["sinks"]:
            for q in snk.get("_pins", snk["pins"]):
                fh.write(f"{snk['sink']},{q['pad']},{q['volts']}\n")
    files.append(str(pins))
    return files


def safe_name(text: str) -> str:
    """A net or board name as a file-name stem Windows accepts."""
    import re
    out = re.sub(r'[\\/:*?"<>|\s]+', "_", str(text)).strip("._ ")
    return out or "net"


def public(result: dict) -> dict:
    """The result without its working arrays, safe to serialise."""
    def clean(v):
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items() if not str(k).startswith("_")}
        if isinstance(v, (list, tuple)):
            return [clean(x) for x in v]
        if isinstance(v, np.generic):
            v = v.item()
        if isinstance(v, float) and not math.isfinite(v):
            return None
        return v
    return clean(result)


def analyse(board: LayoutBoard, net: str, stack, sources: list[dict], sinks: list[dict],
            *, return_net: str = "", **kw) -> dict:
    """Supply net, plus the return net when named. The rail a sink sees is
    the source voltage less the supply drop less the return rise.

    On the return net a source or sink is the same components' pads on
    that net, or its ``return_pads`` when given."""
    if return_net and return_net == net:
        raise IRDropError("return_net is the supply net itself")
    sup = analyse_net(board, net, stack, sources, sinks, **kw)
    out = {"supply": sup}
    if return_net:
        if return_net not in board.nets():
            raise IRDropError(f"net {return_net!r} is not on the board")
        r_sources = [return_spec(board, net, s) for s in sources]
        r_sinks = [return_spec(board, net, s) for s in sinks]
        for spec in r_sources + r_sinks:
            if not resolve_pads(board, return_net, spec):
                raise IRDropError(
                    f"{spec['name']} has no pad on {return_net}: name its return "
                    f"pads with return_pads, e.g. {{\"pads\": [\"J1.1\"], "
                    f"\"return_pads\": [\"J1.2\"]}}")
        ret = analyse_net(board, return_net, stack, r_sources, r_sinks, ground=True, **kw)
        out["return"] = ret
        vref = max(float(s["volts"]) for s in sources)
        rails = []
        for s_sup, s_ret in zip(sup["sinks"], ret["sinks"]):
            eff = s_sup["worst_pin_volts"] - s_ret["worst_pin_volts"]
            rails.append({"sink": s_sup["sink"], "rail_volts": round(eff, 6),
                          "total_drop_mv": round((vref - eff) * 1e3, 4),
                          "total_drop_pct": round(100 * (vref - eff) / vref, 4)})
        out["rails"] = rails
    return out
