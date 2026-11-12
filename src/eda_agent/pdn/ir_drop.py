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

#: Contact conductance between a pad or barrel node and the copper cell
#: under it, in units of the layer's sheet conductance. Large enough that
#: the contact is a few percent of one square, small enough to keep the
#: matrix well conditioned.
STIFF = 25.0
#: Cells below this copper fraction are not nodes.
FILL_MIN = 0.02
#: A track sub-segment whose midpoint sits on area copper this full is
#: inside a pour and dropped.
INSIDE_POUR = 0.9
#: A point on area copper this full is joined to the cell.
TOUCH = 0.25
#: Default upper bound on grid nodes for the automatic cell size.
MAX_NODES_AUTO = 800_000


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


def auto_cell_mils(board: LayoutBoard, net: str, max_nodes: int = MAX_NODES_AUTO) -> float:
    """The finest cell keeping the net's area copper under ``max_nodes``
    cells, clamped to 2..25 mil."""
    from ..layout.copper_raster import rasterize
    coarse = rasterize(board, 25.0, nets=[net], kinds={"pads", "regions", "planes"},
                       supersample=2)
    area = sum(coarse.copper_area_mils2(l) for l in coarse.layers)
    if area <= 0:
        return 5.0
    # four cells across the smallest pad of the net, as far as the node
    # budget allows
    small = [min(c.w, c.h) for p in board.pads if p.net == net for c in p.copper
             if min(c.w, c.h) > 0]
    want = min(small) / 4.0 if small else 5.0
    cell = max(math.sqrt(area / max_nodes), want)
    cell = min(25.0, max(2.0, cell))
    return round(cell * 4) / 4.0


def _arc_chords(a) -> list[tuple[float, float, float, float]]:
    pts = geom.arc_points(a.cx, a.cy, a.radius, a.a1, a.a2, tol=0.05)
    return [(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]) for i in range(len(pts) - 1)]


def build_network(board: LayoutBoard, net: str, raster, stack, rho: float,
                  plating_mils: float):
    """Assemble the network. Returns (net, info) with node maps."""
    order = board.copper_layers()
    zmap = _layer_z(stack, order)
    N = _Net()
    h = raster.cell_mils
    sheet_g = {l: _layer_thickness(board, stack, l) * ph.MIL / rho for l in raster.layers}

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
        for axis in (0, 1):
            if axis == 1:
                fa, fb = f[:, :-1], f[:, 1:]
                ia, ib = ids[:, :-1], ids[:, 1:]
            else:
                fa, fb = f[:-1, :], f[1:, :]
                ia, ib = ids[:-1, :], ids[1:, :]
            both = (ia >= 0) & (ib >= 0)
            hm = 2 * fa[both] * fb[both] / (fa[both] + fb[both])
            N.a.extend(ia[both].tolist())
            N.b.extend(ib[both].tolist())
            N.g.extend((g0 * hm).tolist())
            N.kind.extend(["cell"] * int(both.sum()))
            N.tag.extend([layer] * int(both.sum()))

    def cell_at(layer: str, x: float, y: float) -> tuple[int, float]:
        if layer not in cell_id:
            return -1, 0.0
        ix, iy = raster.cell_of(x, y)
        return int(cell_id[layer][iy, ix]), float(raster.fill[layer][iy, ix])

    def cells_in(layer: str, shape: geom.Shape, grow: float = 0.0):
        """Cells (id, fill) whose centre lies in the shape grown by
        ``grow``; at least the cell under the shape's first point."""
        if layer not in cell_id:
            return []
        x0, y0, x1, y1 = shape.bbox
        x0 -= grow; y0 -= grow; x1 += grow; y1 += grow
        i0, j0 = raster.cell_of(x0, y0)
        i1, j1 = raster.cell_of(x1, y1)
        out = []
        ids, f = cell_id[layer], raster.fill[layer]
        for iy in range(j0, j1 + 1):
            for ix in range(i0, i1 + 1):
                cid = ids[iy, ix]
                if cid < 0:
                    continue
                cx, cy = raster.centre(ix, iy)
                if geom.clearance(geom.circle(cx, cy, 0.0), shape) <= grow:
                    out.append((int(cid), float(f[iy, ix])))
        if not out:
            px, py = shape.pts[0]
            cid, fv = cell_at(layer, px, py)
            if cid >= 0:
                out.append((cid, fv))
        return out

    node_at: dict[int, tuple[str, float, float]] = {}

    # --- pads ---------------------------------------------------------------
    pad_nodes: dict[int, dict[str, int]] = {}   # id(pad) -> layer -> node
    pad_shapes: dict[str, list[tuple[geom.Shape, int]]] = {}
    net_pads = [p for p in board.pads if p.net == net]
    for p in net_pads:
        layers = order if p.hole > 0 else p.layers() or order[:1]
        per = {}
        for layer in layers:
            if layer not in sheet_g:
                continue
            shape = p.shape_on(layer)
            if shape is None:
                if p.hole > 0:
                    shape = geom.circle(p.x, p.y, max(p.hole, 1.0))
                else:
                    continue
            nid = N.node()
            node_at[nid] = (layer, p.x, p.y)
            per[layer] = nid
            pad_shapes.setdefault(layer, []).append((shape, nid))
            for cid, fv in cells_in(layer, shape):
                N.edge(nid, cid, STIFF * sheet_g[layer] * max(fv, 0.05), "contact", layer)
        pad_nodes[id(p)] = per

    # --- barrels -------------------------------------------------------------
    plane_layers = {l.name for l in board.layers if l.kind == "plane"}
    via_nodes: dict[str, list[tuple[float, float, float, int]]] = {}
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
                # a plane layer has no land: the barrel meets the plane at
                # the hole wall
                dia = br.hole_mils + 2 * plating_mils if layer in plane_layers else land
                cells = cells_in(layer, geom.circle(br.x, br.y, dia), grow=h * 0.5)
                cells = [(c, fv) for c, fv in cells if fv > 0]
                nid = N.node()
                node_at[nid] = (layer, br.x, br.y)
                layer_node[layer] = nid
                via_nodes.setdefault(layer, []).append((br.x, br.y, land / 2.0, nid))
                for cid, fv in cells:
                    N.edge(nid, cid, STIFF * sheet_g[layer] * max(fv, 0.05), "contact", layer)
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
    segs: list[list] = []   # [layer, x1, y1, x2, y2, w, source]
    for t in board.tracks:
        if t.net == net and not t.keepout and t.layer in sheet_g:
            segs.append([t.layer, t.x1, t.y1, t.x2, t.y2, t.width, "track"])
    for a in board.arcs:
        if a.net == net and not a.keepout and a.layer in sheet_g:
            for c in _arc_chords(a):
                segs.append([a.layer, *c, a.width, "arc"])

    # split tracks where they meet area copper; drop parts inside pours
    pieces: list[list] = []
    dropped = 0
    for s in segs:
        layer, x1, y1, x2, y2, w, src = s
        L = math.hypot(x2 - x1, y2 - y1)
        if L < 1e-6:
            continue
        n = max(1, int(math.ceil(L / (h * 0.5))))
        fills = []
        for i in range(n):
            t = (i + 0.5) / n
            _, fv = cell_at(layer, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
            fills.append(fv)
        if max(fills) < TOUCH:
            pieces.append(s + [{}])
            continue
        # run-length: keep runs of sub-segments not inside a pour
        i = 0
        while i < n:
            inside = fills[i] >= INSIDE_POUR
            j = i
            while j + 1 < n and (fills[j + 1] >= INSIDE_POUR) == inside:
                j += 1
            if inside:
                dropped += 1
            else:
                ta, tb = i / n, (j + 1) / n
                # an end cut at a pour edge is anchored to the pour cell
                # beyond it, so the cut never leaves the track floating
                anc = {}
                if i > 0:
                    tm = (i - 0.5) / n
                    anc[0] = (x1 + (x2 - x1) * tm, y1 + (y2 - y1) * tm)
                if j + 1 < n:
                    tm = (j + 1.5) / n
                    anc[1] = (x1 + (x2 - x1) * tm, y1 + (y2 - y1) * tm)
                pieces.append([layer, x1 + (x2 - x1) * ta, y1 + (y2 - y1) * ta,
                               x1 + (x2 - x1) * tb, y1 + (y2 - y1) * tb, w, src, anc])
            i = j + 1

    # T junctions: an end on another piece's interior splits it
    from scipy.spatial import cKDTree
    by_layer: dict[str, list[int]] = {}
    for i, p in enumerate(pieces):
        by_layer.setdefault(p[0], []).append(i)
    split_at: dict[int, list[float]] = {}
    for layer, idx in by_layer.items():
        ends = []
        for i in idx:
            p = pieces[i]
            ends.append((p[1], p[2]))
            ends.append((p[3], p[4]))
        if not ends:
            continue
        tree = cKDTree(np.array(ends))
        for i in idx:
            _, x1, y1, x2, y2, w, _src, _anc = pieces[i]
            L = math.hypot(x2 - x1, y2 - y1)
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            cand = tree.query_ball_point((mx, my), L / 2 + w)
            for c in cand:
                ex, ey = ends[c]
                t = ((ex - x1) * (x2 - x1) + (ey - y1) * (y2 - y1)) / (L * L)
                if t <= 1e-6 or t >= 1 - 1e-6:
                    continue
                d = abs((ex - x1) * (y2 - y1) - (ey - y1) * (x2 - x1)) / L
                if d <= w / 2 and min(t * L, (1 - t) * L) > w / 2:
                    split_at.setdefault(i, []).append(t)
    final: list[list] = []
    for i, p in enumerate(pieces):
        ts = sorted(set(split_at.get(i, [])))
        if not ts:
            final.append(p)
            continue
        layer, x1, y1, x2, y2, w, src, anc = p
        prev = 0.0
        for t in ts + [1.0]:
            a2 = {}
            if prev == 0.0 and 0 in anc:
                a2[0] = anc[0]
            if t == 1.0 and 1 in anc:
                a2[1] = anc[1]
            final.append([layer, x1 + (x2 - x1) * prev, y1 + (y2 - y1) * prev,
                          x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, w, src, a2])
            prev = t

    # endpoint nodes: merge ends that touch (within half the wider width)
    track_tags = []
    for layer in {p[0] for p in final}:
        idx = [i for i, p in enumerate(final) if p[0] == layer]
        pts, wid, owner = [], [], []
        for i in idx:
            p = final[i]
            pts += [(p[1], p[2]), (p[3], p[4])]
            wid += [p[5], p[5]]
            owner += [(i, 0), (i, 1)]
        arr = np.array(pts)
        tree = cKDTree(arr)
        parent = list(range(len(pts)))

        def find(u):
            while parent[u] != u:
                parent[u] = parent[parent[u]]
                u = parent[u]
            return u
        rmax = max(wid) / 2.0 if wid else 0.0
        for u, v in tree.query_pairs(max(rmax, 0.01)):
            if math.dist(pts[u], pts[v]) <= max(wid[u], wid[v]) / 2.0 + 1e-6:
                parent[find(u)] = find(v)
        root_node: dict[int, int] = {}
        end_node: dict[tuple[int, int], int] = {}
        anchors: dict[int, list] = {}
        for k in range(len(pts)):
            i_, e_ = owner[k]
            a_ = final[i_][7].get(e_)
            if a_ is not None:
                anchors.setdefault(find(k), []).append(a_)
        for k in range(len(pts)):
            r = find(k)
            if r not in root_node:
                root_node[r] = N.node()
                x, y = pts[r]
                node_at[root_node[r]] = (layer, x, y)
                for ax, ay in anchors.get(r, ()):
                    cid, fv = cell_at(layer, ax, ay)
                    if cid >= 0:
                        N.edge(root_node[r], cid, STIFF * sheet_g[layer] * max(fv, 0.05),
                               "contact", layer)
                attached = False
                # on a pad of this layer?
                for shape, pnid in pad_shapes.get(layer, ()):
                    if geom.clearance(geom.circle(x, y, 0.0), shape) <= 0.0:
                        N.edge(root_node[r], pnid, STIFF * sheet_g[layer], "contact", layer)
                        attached = True
                        break
                if not attached:
                    for vx, vy, vr, vnid in via_nodes.get(layer, ()):
                        if math.hypot(x - vx, y - vy) <= vr + 1e-6:
                            N.edge(root_node[r], vnid, STIFF * sheet_g[layer], "contact", layer)
                            attached = True
                            break
                if not attached:
                    cid, fv = cell_at(layer, x, y)
                    if cid >= 0 and fv >= TOUCH:
                        N.edge(root_node[r], cid, STIFF * sheet_g[layer] * fv, "contact", layer)
            end_node[owner[k]] = root_node[r]
        for i in idx:
            layer_, x1, y1, x2, y2, w, src, _a = final[i]
            L = math.hypot(x2 - x1, y2 - y1)
            if L < 1e-9:
                continue
            g = sheet_g[layer_] * w / L
            tag = {"layer": layer_, "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                   "width": w, "source": src}
            N.edge(end_node[(i, 0)], end_node[(i, 1)], g, "track", tag)
            track_tags.append(tag)

    info = {"cell_id": cell_id, "pad_nodes": pad_nodes, "node_at": node_at, "sheet_g": sheet_g,
            "segments_dropped_in_pour": dropped, "tracks": len(track_tags),
            "barrels": len(barrel_edges)}
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
                       supersample=4)
    N, info = build_network(board, net, raster, stack, rho, plating)
    fixed: dict[int, float] = {}
    inject: dict[int, float] = {}
    sink_info = []
    for src in sources:
        pads = resolve_pads(board, net, src)
        if not pads:
            raise IRDropError(f"source {src} has no pads on net {net}")
        v = 0.0 if ground else float(src.get("volts", 0.0))
        for p in pads:
            for nid in info["pad_nodes"].get(id(p), {}).values():
                fixed[nid] = v
    for snk in sinks:
        pads = resolve_pads(board, net, snk)
        amps = float(snk.get("amps", 0.0))
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
                for nid in nodes:
                    inject[nid] = inject.get(nid, 0.0) + sign * per / len(nodes)
        sink_info.append((snk, pads, nodes_per_pad))
    if not fixed:
        raise IRDropError("no source pads resolved")
    V, live = _solve(N, fixed, inject)
    return raster, N, info, V, live, sink_info


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
    rho = ph.copper_resistivity(temp_c, rho_factor)
    cell = float(cell_mils) if cell_mils and cell_mils > 0 else auto_cell_mils(board, net)
    raster, N, info, V, live, sink_info = _run_one(
        board, net, stack, sources, sinks, cell, rho, plating_mils, sink_model, ground)
    vref = 0.0 if ground else max(float(s.get("volts", 0.0)) for s in sources)

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
        drop = (vref - worst) if not ground else worst
        out_sinks.append({"sink": _label(snk), "amps": float(snk.get("amps", 0.0)),
                          "worst_pin_volts": round(worst, 6),
                          "drop_mv": round(drop * 1e3, 4),
                          "drop_pct": round(100 * drop / vref, 4) if vref else None,
                          "mean_pin_volts": round(float(np.mean(vals)), 6),
                          "pins": pins})

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
        k = int(np.nanargmax(J)) if np.isfinite(J).any() else -1
        if k >= 0:
            iy, ix = divmod(k, ids.shape[1])
            cx, cy = raster.centre(ix, iy)
            planes.append({"layer": layer, "max_j_a_per_mm2": round(float(J[iy, ix]), 3),
                           "at": [round(cx, 1), round(cy, 1)],
                           "max_sheet_a_per_mm": round(float(J[iy, ix]) * t_m * 1e3, 4),
                           "min_volts": round(float(np.nanmin(vm)), 6),
                           "max_volts": round(float(np.nanmax(vm)), 6),
                           "loss_w": round(loss_by_layer.get(layer, 0.0), 6)})

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
        "total_amps": round(sum(float(s.get("amps", 0.0)) for s in sinks), 6),
        "loss_w": round(total_loss, 7),
        "layers": planes,
        "tracks": tracks[:limit],
        "tracks_over_capacity": sum(1 for t in tracks if t["over_capacity"]),
        "vias": via_list[:limit],
        "vias_over_capacity": sum(1 for v in via_list if v["over_capacity"]),
        "network": {"tracks": info["tracks"], "barrels": info["barrels"],
                    "track_parts_inside_pours": info["segments_dropped_in_pour"]},
        "notes": list(raster.notes),
    }
    if check_convergence and worst and raster.cell_mils * 2 <= 50:
        try:
            r2, N2, i2, V2, l2, s2 = _run_one(board, net, stack, sources, sinks,
                                              raster.cell_mils * 2, rho, plating_mils,
                                              sink_model, ground)
            worst2 = 0.0
            for snk, pads, nodes_per_pad in s2:
                vals = [np.mean([V2[n] for n in nodes]) for nodes in nodes_per_pad if nodes]
                w2 = (vref - min(vals)) if not ground else max(vals)
                worst2 = max(worst2, w2)
            d1 = worst["drop_mv"] / 1e3
            # second-order: error of the fine solve ~ (fine - coarse) / 3
            err = abs(d1 - worst2) / 3.0
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
    net = result["net"].replace("/", "_").replace("\\", "_")
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
    return files


def public(result: dict) -> dict:
    return {k: v for k, v in result.items() if not k.startswith("_")}


def analyse(board: LayoutBoard, net: str, stack, sources: list[dict], sinks: list[dict],
            *, return_net: str = "", **kw) -> dict:
    """Supply net, plus the return net when named. The rail a sink sees is
    the source voltage less the supply drop less the return rise."""
    sup = analyse_net(board, net, stack, sources, sinks, **kw)
    out = {"supply": sup}
    if return_net:
        ret = analyse_net(board, return_net, stack, sources, sinks, ground=True, **kw)
        out["return"] = ret
        vref = max(float(s.get("volts", 0.0)) for s in sources)
        rails = []
        for s_sup, s_ret in zip(sup["sinks"], ret["sinks"]):
            eff = s_sup["worst_pin_volts"] - s_ret["worst_pin_volts"]
            rails.append({"sink": s_sup["sink"], "rail_volts": round(eff, 6),
                          "total_drop_mv": round((vref - eff) * 1e3, 4),
                          "total_drop_pct": round(100 * (vref - eff) / vref, 4) if vref else None})
        out["rails"] = rails
    return out
