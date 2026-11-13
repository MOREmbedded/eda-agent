# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Global routing: which tiles each connection passes through.

The board is cut into square tiles a few tracks wide. Each tile side, on
each layer, holds as many tracks as fit through the free cells along it,
and each tile as many vias as fit in its free area. Connections are
routed tile to tile, and nets negotiate for overfull sides the way the
detailed router negotiates for cells (PathFinder): an overfull side
costs more each round and keeps a history, until every side holds what
crosses it.

The answer is a corridor per connection: the tiles its global route
takes, and their neighbours. The detailed router searches inside it
first. Negotiating on a few thousand tiles instead of a few million
cells is what makes a crowded board settle: routed cell by cell alone, a
dense multilayer board still had dozens of nets in conflict after six
rounds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

#: The four moves between tiles on a layer: (dx, dy).
TILE_MOVES = ((1, 0), (-1, 0), (0, 1), (0, -1))


def prim_pairs(cx: np.ndarray, cy: np.ndarray) -> list[tuple[int, int]]:
    """The order a net's pads are joined in: from the lowest-left pad,
    each next pad is the one nearest to any already joined, paired with
    that nearest joined pad. Detailed and global routing share it, so the
    k-th global route is the corridor of the k-th detailed connection."""
    n = len(cx)
    if n < 2:
        return []
    start = int(np.argmin(cx + cy))
    best = np.hypot(cx - cx[start], cy - cy[start])
    near = np.full(n, start)
    best[start] = np.inf
    done = {start}
    out = []
    while len(done) < n:
        k = int(np.argmin(best))
        if not np.isfinite(best[k]):
            break
        out.append((k, int(near[k])))
        done.add(k)
        best[k] = np.inf
        d = np.hypot(cx - cx[k], cy - cy[k])
        closer = (d < best) & np.isfinite(best)
        best = np.where(closer, d, best)
        near = np.where(closer, k, near)
    return out


@dataclass
class GlobalNet:
    job: object
    routes: list = field(default_factory=list)      # per connection: list of tile nodes
    edges: list = field(default_factory=list)       # per connection: used edge ids
    vias: list = field(default_factory=list)        # per connection: used hub ids


class GlobalRouter:
    def __init__(self, router, tile_mils: float = 40.0):
        self.r = router
        g = router.grid
        T = max(4, int(round(tile_mils / router.pitch)))
        self.T = T
        L, ny, nx = router.L, router.ny, router.nx
        self.L = L
        self.GY, self.GX = -(-ny // T), -(-nx // T)
        GY, GX = self.GY, self.GX
        c = router.c
        whole = (slice(None), slice(None))
        free = np.empty((L, ny, nx), dtype=bool)
        via_room = np.full((ny, nx), np.inf)
        for l in range(L):
            # A generic track, every piece of copper foreign to it.
            s = g.slack(-7, l, whole, lambda who: np.full(who.shape, c),
                        router.rr.edge_clearance)
            free[l] = s >= router.w_def / 2
            via_room = np.minimum(via_room, s)
        rv = router.via_radii[-1] if router.via_radii else 10.0
        via_ok = via_room >= rv
        pad_y, pad_x = GY * T - ny, GX * T - nx
        free = np.pad(free, ((0, 0), (0, pad_y), (0, pad_x)))
        via_ok = np.pad(via_ok, ((0, pad_y), (0, pad_x)))
        track = (router.w_def + c) / router.pitch      # cells per track
        # East side of each tile: the last column of cells in it.
        east = free[:, :, T - 1::T].reshape(L, GY, T, GX).sum(axis=2)
        north = free[:, T - 1::T, :].reshape(L, GY, GX, T).sum(axis=3)
        self.cap_e = np.floor(east / track).astype(np.int32)      # (L, GY, GX)
        self.cap_n = np.floor(north / track).astype(np.int32)
        via_cells = ((2 * rv + c) / router.pitch) ** 2
        self.cap_v = np.floor(via_ok.reshape(GY, T, GX, T).sum(axis=(1, 3)) / via_cells).astype(np.int32)
        # Edge ids: east edges, then north edges, then via (tile hub) uses.
        self.n_e = L * GY * GX
        self.use = np.zeros(2 * self.n_e + GY * GX, dtype=np.int32)
        self.hist = np.zeros_like(self.use, dtype=np.float64)
        self.cap = np.concatenate([self.cap_e.ravel(), self.cap_n.ravel(), self.cap_v.ravel()])
        self.nets: dict[int, GlobalNet] = {}
        self._build_structure()

    # -- graph -------------------------------------------------------------

    def _build_structure(self):
        """Fixed arcs of the tile graph and the edge each one uses."""
        L, GY, GX = self.L, self.GY, self.GX
        n_t = L * GY * GX
        node = np.arange(n_t).reshape(L, GY, GX)
        src, dst, eid, length = [], [], [], []
        step = self.T * self.r.pitch
        mf = self.r.move_factor
        for k, (dx, dy) in enumerate(TILE_MOVES):
            ys, xs = np.mgrid[0:GY, 0:GX]
            ny, nx = ys + dy, xs + dx
            ok = (ny >= 0) & (ny < GY) & (nx >= 0) & (nx < GX)
            for l in range(L):
                a = node[l][ok]
                b = node[l][ny[ok], nx[ok]]
                if dx:   # the east side of the western tile
                    e = (l * GY + ys[ok]) * GX + np.minimum(xs[ok], nx[ok])
                else:    # the north side of the southern tile
                    e = self.n_e + (l * GY + np.minimum(ys[ok], ny[ok])) * GX + xs[ok]
                src.append(a)
                dst.append(b)
                eid.append(e)
                # The layer's preference for this direction, from the
                # detailed router's straight moves.
                length.append(np.full(len(a), step * mf[l, k]))
        hub = n_t + np.arange(GY * GX).reshape(GY, GX)
        for l in range(L):
            a = node[l].ravel()
            h = hub.ravel()
            v = 2 * self.n_e + np.arange(GY * GX)
            src += [a, h]
            dst += [h, a]
            eid += [v, v]
            length += [np.full(len(a), self.r.via_base / 2)] * 2
        src = np.concatenate(src)
        dst = np.concatenate(dst)
        eid = np.concatenate(eid)
        base = np.concatenate(length)
        self.n_nodes = n_t + GY * GX
        # The arcs in the order a sparse matrix keeps them (by row, then
        # column; no two arcs share both), so the graph is built once and
        # only its weights change from search to search.
        order = np.lexsort((dst, src))
        self.src, self.dst, self.eid, self.base = src[order], dst[order], eid[order], base[order]
        self._indptr = np.concatenate(
            [[0], np.cumsum(np.bincount(self.src, minlength=self.n_nodes))]).astype(np.int32)
        self._indices = self.dst.astype(np.int32)
        # Arcs whose edge is full get the wall factor; which they are never
        # changes.
        self._wall = np.where((self.cap[self.eid] <= 0) & (self.eid < 2 * self.n_e), 50.0, 1.0)
        # Edge -> the arcs over it, for the weights a change of use moves.
        by_edge = np.argsort(self.eid, kind="stable")
        self._arcs_of = (by_edge, np.searchsorted(self.eid[by_edge], np.arange(len(self.use) + 1)))
        self._w = None
        self._w_pres = None
        self._stale: list = []

    def _weights(self, arcs, pres: float) -> np.ndarray:
        e = self.eid[arcs]
        over = np.maximum(0, self.use[e] + 1 - self.cap[e])
        w = self.base[arcs] * (1.0 + self.hist[e]) * (1.0 + pres * over)
        return np.where(self._wall[arcs] != 1.0, w * 50.0, w)

    def _graph(self, pres: float) -> csr_matrix:
        """The tile graph at present-cost pres: every arc's weight from
        its edge's use, capacity and history. Weights are recomputed whole
        when pres or the history moved, else only over the edges whose
        use changed since (_stale); either way each is the same number."""
        if self._w is None or pres != self._w_pres:
            self._w = self._weights(np.arange(len(self.eid)), pres)
            self._w_pres = pres
        elif self._stale:
            edges = np.unique(np.concatenate(self._stale))
            by_edge, start = self._arcs_of
            lo, n = start[edges], start[edges + 1] - start[edges]
            if n.sum():
                # The arcs of each edge, run after run.
                first = np.repeat(lo - (np.cumsum(n) - n), n)
                arcs = by_edge[first + np.arange(n.sum())]
                self._w[arcs] = self._weights(arcs, pres)
        self._stale = []
        return csr_matrix((self._w, self._indices, self._indptr),
                          shape=(self.n_nodes, self.n_nodes))

    def _use(self, edges, delta: int) -> None:
        np.add.at(self.use, edges, delta)
        self._stale.append(np.asarray(edges, dtype=np.int64))

    # -- tiles ---------------------------------------------------------------

    def tile_of(self, cells: np.ndarray) -> np.ndarray:
        """Tile nodes of grid cells (l, y, x)."""
        return (cells[:, 0] * self.GY + cells[:, 1] // self.T) * self.GX + cells[:, 2] // self.T

    def decode(self, n: int) -> tuple[int, int, int]:
        n_t = self.L * self.GY * self.GX
        if n >= n_t:
            k = n - n_t
            return -1, k // self.GX, k % self.GX
        l, rest = divmod(n, self.GY * self.GX)
        return l, rest // self.GX, rest % self.GX

    # -- routing -------------------------------------------------------------

    def _route_net(self, job, pres: float) -> GlobalNet:
        gn = self.nets.get(job.id)
        if gn is not None:
            for es in gn.edges:
                self._use(es, -1)
        gn = GlobalNet(job)
        self.nets[job.id] = gn
        terms = job.terminals
        cx = np.array([t.center[0] for t in terms])
        cy = np.array([t.center[1] for t in terms])
        pairs = prim_pairs(cx, cy)
        if not pairs:
            return gn
        start = int(np.argmin(cx + cy))
        tree = set(self.tile_of(terms[start].cells).tolist())
        for k, _q in pairs:
            graph = self._graph(pres)
            sources = np.unique(self.tile_of(terms[k].cells))
            targets = np.array(sorted(tree), dtype=np.int64)
            dist, pred, _ = dijkstra(graph, directed=True, indices=sources,
                                     return_predecessors=True, min_only=True)
            j = int(np.argmin(dist[targets]))
            n = int(targets[j])
            if not np.isfinite(dist[n]):
                gn.routes.append(None)
                gn.edges.append(np.zeros(0, dtype=np.int64))
                continue
            path = [n]
            while pred[n] >= 0:
                n = int(pred[n])
                path.append(n)
            path.reverse()
            used = self._edges_of(path)
            self._use(used, 1)
            gn.routes.append(path)
            gn.edges.append(used)
            tree.update(path)
            tree.update(self.tile_of(terms[k].cells).tolist())
        return gn

    def _edges_of(self, path: list[int]) -> np.ndarray:
        n_t = self.L * self.GY * self.GX
        out = []
        for a, b in zip(path, path[1:]):
            if a >= n_t or b >= n_t:
                hub = a if a >= n_t else b
                out.append(2 * self.n_e + (hub - n_t))
                continue
            la, ya, xa = self.decode(a)
            lb, yb, xb = self.decode(b)
            if ya == yb:
                out.append((la * self.GY + ya) * self.GX + min(xa, xb))
            else:
                out.append(self.n_e + (la * self.GY + min(ya, yb)) * self.GX + xa)
        # A via is one use of its tile's room, however many layers it spans.
        return np.unique(np.array(out, dtype=np.int64))

    def overflow(self) -> np.ndarray:
        return np.maximum(0, self.use - self.cap)

    #: With stall, the negotiation stops once the last STALL_WINDOW
    #: rounds brought the overflow no lower than STALL_PROGRESS of the best
    #: round before them. MEASURED on four slow boards: the overflow levels
    #: off by round 7 to 16 and stays there (one board's rises from the
    #: first round), while the rounds after it took up to 180 s of 550.
    STALL_WINDOW = 4
    STALL_PROGRESS = 0.98

    def run(self, jobs, iterations: int = 30, log=None, stall: bool = False) -> dict:
        log = log or (lambda s: None)
        jobs = [j for j in jobs if j.plane_mask is None]
        pres = 0.5
        todo = list(jobs)
        stats = {}
        seen: list[int] = []
        for it in range(iterations):
            for j in todo:
                self._route_net(j, pres)
            over = self.overflow()
            bad = over > 0
            self.hist[bad] += 1.0
            self._w = None
            total = int(over.sum())
            stats = {"iterations": it + 1, "overflow": total,
                     "tiles": (self.L, self.GY, self.GX), "tile_cells": self.T}
            log(f"global iteration {it + 1}: overflow {total}")
            if not total:
                break
            seen.append(total)
            w = self.STALL_WINDOW
            if stall and len(seen) > w and min(seen[-w:]) > self.STALL_PROGRESS * min(seen[:-w]):
                stats["stalled"] = True
                break
            todo = [j for j in jobs if any(bad[e].any() for e in self.nets[j.id].edges)]
            pres *= 1.5
        return stats

    # -- corridors -------------------------------------------------------------

    def corridor(self, job, k: int, grow: int = 1) -> np.ndarray | None:
        """Tile mask (GY, GX) of connection k's route, grown by ``grow``
        tiles, or None when it has no global route."""
        gn = self.nets.get(job.id)
        if gn is None or k >= len(gn.routes) or gn.routes[k] is None:
            return None
        m = np.zeros((self.GY, self.GX), dtype=bool)
        for n in gn.routes[k]:
            _, gy, gx = self.decode(n)
            m[gy, gx] = True
        if grow:
            from scipy.ndimage import binary_dilation
            m = binary_dilation(m, iterations=grow, structure=np.ones((3, 3), bool))
        return m
