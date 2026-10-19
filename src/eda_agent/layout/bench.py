# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Benchmark: strip a finished board, let an engine redo it, score both.

A board a person finished is the only honest yardstick. The harness takes
one, removes what an engine is meant to produce, hands the rest to the
engine, and scores the result with the same exact DRC it scores the
person's board with. The person's numbers (vias, copper length) are the
reference; completion and violations are absolute.

What a routing benchmark removes is everything a router would lay down:
free tracks, arcs and vias, poured copper, and free copper regions. What
it keeps is the design the router is given: placement, footprint copper
(including footprint vias), planes and their splits, pour boundaries,
keepouts, cutouts and rules. Poured copper goes because it was poured
around the person's tracks: left in, it would hand the router channels
cut to the shape of the answer.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

from .drc import run_drc
from .model import LayoutBoard
from .rules import RuleSet

Engine = Callable[[LayoutBoard], LayoutBoard]


def copy_board(board: LayoutBoard) -> LayoutBoard:
    return LayoutBoard.from_dict(board.to_dict())


def strip_routing(board: LayoutBoard) -> LayoutBoard:
    """The board as a router receives it: placed, unrouted."""
    out = copy_board(board)
    out.tracks = [t for t in out.tracks if t.comp or t.keepout]
    out.arcs = [a for a in out.arcs if a.comp or a.keepout]
    out.vias = [v for v in out.vias if v.comp]
    out.regions = [r for r in out.regions
                   if r.kind in ("plane", "pour_boundary", "keepout", "cutout")
                   or r.comp]
    return out


def routing_stats(board: LayoutBoard) -> dict:
    """Copper a router laid: free vias, and free track length per layer.

    Signal layers only. A finished board also has free tracks on its
    mechanical and overlay layers (outlines, dimensions, notes) and split
    lines on its plane layers, which are voids cut out of the plane, not
    copper (``drc.conductive_items``). Counted, they made the person's
    length as much as a third longer than the copper they laid on the
    public sample boards, and every engine's length look short beside
    it."""
    signal = set(board.signal_layers())
    per_layer: dict[str, float] = {}
    segments = 0
    for t in board.tracks:
        if t.comp or t.keepout or t.layer not in signal:
            continue
        segments += 1
        per_layer[t.layer] = per_layer.get(t.layer, 0.0) + math.hypot(t.x2 - t.x1, t.y2 - t.y1)
    for a in board.arcs:
        if a.comp or a.keepout or a.layer not in signal:
            continue
        segments += 1
        span = abs(a.a2 - a.a1) % 360.0 or 360.0
        per_layer[a.layer] = per_layer.get(a.layer, 0.0) + a.radius * math.radians(span)
    return {
        "vias": sum(1 for v in board.vias if not v.comp),
        "length": round(sum(per_layer.values()), 1),
        "length_by_layer": {k: round(v, 1) for k, v in sorted(per_layer.items())},
        # Tracks and arcs: a staircase is many short pieces where a person
        # draws one.
        "segments": segments,
    }


def octilinear(dx: float, dy: float) -> float:
    """The shortest path between two points in straight and 45 degree
    pieces: the diagonal over the shorter side, straight for the rest."""
    a, b = abs(dx), abs(dy)
    return max(a, b) + (math.sqrt(2.0) - 1.0) * min(a, b)


#: A routed connection more than this many times the octilinear distance
#: between its pads is counted as a detour.
DETOUR_AT = (1.5, 2.0)
#: Connections whose pads are closer than this, mils, give no ratio.
DETOUR_FLOOR = 1.0
#: How near, mils, a track's end must come to copper of its net to join it.
JOIN_TOL = 1e-3


def connection_lengths(board: LayoutBoard) -> list[tuple[float, float, int]]:
    """For each two pads of a net that are neighbours in the net's
    shortest tree (octilinear distance between pad centres) and that its
    tracks and vias join: the length along that copper from one pad's
    centre to the other's, the octilinear distance, and the track pieces
    on the way.

    Pours and planes are left out: a pair they join has no track length to
    measure, on the person's board and the engine's alike. A track's end
    joins a pad or via of its net it lies in (at the distance from there to
    the centre), a track of its net it lies on (a tee: the length along
    that track counts from the point) and any track ending there."""
    from collections import defaultdict

    import numpy as np
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components, dijkstra

    from . import geom

    signal = set(board.signal_layers())
    tracks: dict[str, list] = defaultdict(list)
    for t in board.tracks:
        if not (t.comp or t.keepout) and t.net and t.layer in signal:
            tracks[t.net].append((t.layer, t.x1, t.y1, t.x2, t.y2, t.width,
                                  math.hypot(t.x2 - t.x1, t.y2 - t.y1)))
    for a in board.arcs:
        if not (a.comp or a.keepout) and a.net and a.layer in signal:
            span = abs(a.a2 - a.a1) % 360.0 or 360.0
            ends = [(a.cx + a.radius * math.cos(math.radians(g)),
                     a.cy + a.radius * math.sin(math.radians(g))) for g in (a.a1, a.a2)]
            tracks[a.net].append((a.layer, *ends[0], *ends[1], a.width,
                                  a.radius * math.radians(span)))
    vias: dict[str, list] = defaultdict(list)
    for v in board.vias:
        if not v.comp and v.net:
            vias[v.net].append(v)
    out = []
    for net, pads in board.pads_by_net().items():
        if len(pads) < 2 or not tracks.get(net):
            continue
        ts, vs = tracks[net], vias.get(net, [])
        P, V = len(pads), len(vs)
        ends: dict[tuple, int] = {}
        edges: list[tuple[int, int, float, int]] = []

        def node(layer, x, y):
            key = (layer, round(x, 3), round(y, 3))
            if key not in ends:
                ends[key] = P + V + len(ends)
            return ends[key]

        for (layer, x1, y1, x2, y2, _, length) in ts:
            edges.append((node(layer, x1, y1), node(layer, x2, y2), length, 1))
        # Pads, vias and tracks per layer, in buckets, for the joins.
        cell = 50.0
        grid: dict[tuple, list] = defaultdict(list)

        def file(layer, box, item):
            for gx in range(int(math.floor(box[0] / cell)), int(math.floor(box[2] / cell)) + 1):
                for gy in range(int(math.floor(box[1] / cell)), int(math.floor(box[3] / cell)) + 1):
                    grid[(layer, gx, gy)].append(item)

        for i, p in enumerate(pads):
            for layer in p.layers():
                s = p.shape_on(layer)
                if s is not None and layer in signal:
                    file(layer, s.bbox, ("pad", i, s))
        for k, v in enumerate(vs):
            for layer in board.layers_between(v.low_layer, v.high_layer):
                if layer in signal:
                    s = v.shape_on(layer)
                    file(layer, s.bbox, ("via", k, s))
        for k, (layer, x1, y1, x2, y2, w, _) in enumerate(ts):
            file(layer, (min(x1, x2) - w / 2, min(y1, y2) - w / 2,
                         max(x1, x2) + w / 2, max(y1, y2) + w / 2), ("track", k, None))
        for (layer, x, y), e in list(ends.items()):
            seen = set()
            for kind, k, s in grid.get((layer, int(math.floor(x / cell)),
                                        int(math.floor(y / cell))), ()):
                if (kind, k) in seen:
                    continue
                seen.add((kind, k))
                if kind == "pad" or kind == "via":
                    if geom.clearance(geom.circle(x, y, 0.0), s) <= JOIN_TOL:
                        c = pads[k] if kind == "pad" else vs[k]
                        edges.append((k if kind == "pad" else P + k, e,
                                      math.hypot(x - c.x, y - c.y), 0))
                    continue
                _, x1, y1, x2, y2, w, length = ts[k]
                dx, dy = x2 - x1, y2 - y1
                ll = dx * dx + dy * dy
                u = 0.0 if ll == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / ll))
                if math.hypot(x - (x1 + u * dx), y - (y1 + u * dy)) > w / 2 + JOIN_TOL:
                    continue
                a, b = node(layer, x1, y1), node(layer, x2, y2)
                if e not in (a, b):
                    edges.append((e, a, u * length, 1))
                    edges.append((e, b, (1.0 - u) * length, 1))
        n = P + V + len(ends)
        # One edge per pair of nodes, the shortest (a sparse matrix sums
        # repeated entries); a zero length is a join all the same, kept just
        # above zero.
        short: dict[tuple, tuple] = {}
        for a, b, x, f in edges:
            if a == b:
                continue
            key = (a, b) if a < b else (b, a)
            if key not in short or x < short[key][0]:
                short[key] = (max(x, 1e-9), f)
        if not short:
            continue
        keys = list(short)
        rows = np.array([a for a, _ in keys] + [b for _, b in keys])
        cols = np.array([b for _, b in keys] + [a for a, _ in keys])
        w = np.array([short[k][0] for k in keys] * 2)
        graph = csr_matrix((w, (rows, cols)), shape=(n, n))
        # The net's shortest tree by octilinear distance (Prim).
        xs = np.array([p.x for p in pads])
        ys = np.array([p.y for p in pads])
        inside = np.zeros(P, dtype=bool)
        inside[0] = True
        best = np.full(P, np.inf)
        frm = np.zeros(P, dtype=np.int64)
        last = 0
        pairs = []
        for _ in range(P - 1):
            dx, dy = np.abs(xs - xs[last]), np.abs(ys - ys[last])
            d = np.maximum(dx, dy) + (math.sqrt(2.0) - 1.0) * np.minimum(dx, dy)
            better = ~inside & (d < best)
            best[better], frm[better] = d[better], last
            cand = np.where(inside, np.inf, best)
            last = int(np.argmin(cand))
            inside[last] = True
            pairs.append((int(frm[last]), last, float(best[last])))
        _, comp = connected_components(graph, directed=False)
        pairs = [(i, j, d) for i, j, d in pairs if d >= DETOUR_FLOOR and comp[i] == comp[j]]
        sources = sorted({i for i, _, _ in pairs})
        # A few sources at a time: a whole ground net at once is a matrix of
        # pads by nodes.
        for k0 in range(0, len(sources), 32):
            chunk = sources[k0:k0 + 32]
            dist, pred = dijkstra(graph, directed=False, indices=chunk, return_predecessors=True)
            row = {s: k for k, s in enumerate(chunk)}
            for i, j, d in pairs:
                if i not in row:
                    continue
                r = row[i]
                count, at = 0, j
                while at != i and at >= 0:
                    up = int(pred[r, at])
                    key = (up, at) if up < at else (at, up)
                    count += short.get(key, (0, 0))[1]
                    at = up
                if count:
                    out.append((float(dist[r, j]), d, count))
    return out


def detour_stats(board: LayoutBoard) -> dict:
    """How far the routing goes round, per connection its tracks make
    (``connection_lengths``): routed length over octilinear distance, its
    median and 95th percentile, how many connections go past each of
    DETOUR_AT, and the median count of track pieces per connection."""
    rows = connection_lengths(board)
    if not rows:
        return {"connections": 0}
    ratios = sorted(length / d for length, d, _ in rows)
    pieces = sorted(k for _, _, k in rows)
    n = len(ratios)
    out = {"connections": n,
           "median": round(ratios[n // 2] if n % 2 else (ratios[n // 2 - 1] + ratios[n // 2]) / 2, 4),
           "p95": round(ratios[min(n - 1, int(math.ceil(0.95 * n)) - 1)], 4),
           "pieces_median": pieces[n // 2]}
    for at in DETOUR_AT:
        out[f"over_{at:g}".replace(".", "_")] = sum(1 for r in ratios if r > at)
    return out


#: Two vias whose centres are closer than this many via pitches (the two
#: radii and the clearance) stand together; a chain of VIA_CLUSTER_LEAST or
#: more is a cluster.
VIA_CLUSTER_PITCHES = 1.5
VIA_CLUSTER_LEAST = 3


def via_clusters(board: LayoutBoard, clearance: float | None = None) -> dict:
    """Free vias standing in clusters: how many clusters, how many vias in
    them, and the largest."""
    vs = [v for v in board.vias if not v.comp]
    if len(vs) < VIA_CLUSTER_LEAST:
        return {"clusters": 0, "vias": 0, "largest": 0}
    c = RuleSet.from_board(board).default_clearance() if clearance is None else clearance
    reach = VIA_CLUSTER_PITCHES * (max(v.diameter for v in vs) + c)
    parent = list(range(len(vs)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    buckets: dict[tuple, list] = {}
    for i, v in enumerate(vs):
        buckets.setdefault((int(math.floor(v.x / reach)), int(math.floor(v.y / reach))), []).append(i)
    for (bx, by), members in buckets.items():
        for i in members:
            a = vs[i]
            for gx in (bx - 1, bx, bx + 1):
                for gy in (by - 1, by, by + 1):
                    for j in buckets.get((gx, gy), ()):
                        if j <= i:
                            continue
                        b = vs[j]
                        pitch = (a.diameter + b.diameter) / 2 + c
                        if math.hypot(a.x - b.x, a.y - b.y) < VIA_CLUSTER_PITCHES * pitch:
                            parent[find(i)] = find(j)
    sizes: dict[int, int] = {}
    for i in range(len(vs)):
        r = find(i)
        sizes[r] = sizes.get(r, 0) + 1
    big = [s for s in sizes.values() if s >= VIA_CLUSTER_LEAST]
    return {"clusters": len(big), "vias": sum(big), "largest": max(big, default=0)}


@dataclass
class Score:
    completion: float
    missing_connections: int
    connections_needed: int
    violations: int
    vias: int
    length: float
    seconds: float = 0.0
    unrouted: dict[str, int] = field(default_factory=dict)
    unevaluated_rules: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "completion": round(self.completion, 4),
            "missing_connections": self.missing_connections,
            "connections_needed": self.connections_needed,
            "violations": self.violations,
            "vias": self.vias, "length": self.length,
            "seconds": round(self.seconds, 2),
            "unevaluated_rules": self.unevaluated_rules,
        }


def score(board: LayoutBoard, seconds: float = 0.0) -> Score:
    rules = RuleSet.from_board(board)
    rep = run_drc(board, rules)
    stats = routing_stats(board)
    return Score(rep.completion, rep.missing_connections, rep.connections_needed,
                 len(rep.violations), stats["vias"], stats["length"], seconds,
                 dict(rep.unrouted), rules.unevaluated())


@dataclass
class RouteResult:
    board: str
    human: Score
    start: Score
    engine: Score

    def as_dict(self) -> dict:
        h, e = self.human, self.engine
        return {
            "board": self.board,
            "human": h.as_dict(), "start": self.start.as_dict(), "engine": e.as_dict(),
            # Ratios against the person's board; above 1 is worse.
            "vias_vs_human": round(e.vias / h.vias, 3) if h.vias else None,
            "length_vs_human": round(e.length / h.length, 3) if h.length else None,
        }


def route_benchmark(board: LayoutBoard, engine: Engine) -> RouteResult:
    """Score the person's board, strip it, route it, score the engine's."""
    human = score(board)
    stripped = strip_routing(board)
    start = score(stripped)
    t0 = time.perf_counter()
    routed = engine(copy_board(stripped))
    engine_score = score(routed, time.perf_counter() - t0)
    return RouteResult(board.name, human, start, engine_score)


# ---------------------------------------------------------------------------
# Placement benchmark
# ---------------------------------------------------------------------------

#: Parts a placement benchmark leaves where the person put them: what sits
#: where it does for mechanical reasons, not for the circuit.
FIXED_PREFIXES = ("J", "P", "CN", "USB", "H", "MH", "FID", "TP", "T", "BTN", "SW",
                  "K", "BT", "ANT")


def is_fixed(comp) -> bool:
    # A part with no designator cannot be moved by name: moving "" moved
    # every pad of no part with it, mounting holes and all.
    if comp.locked or not comp.ref:
        return True
    prefix = "".join(ch for ch in comp.ref if ch.isalpha()).upper()
    return prefix in FIXED_PREFIXES


def scramble_placement(board: LayoutBoard, seed: int = 0) -> LayoutBoard:
    """The board with every movable part dropped at random on it: a
    placement problem whose answer the person's board already holds."""
    import random

    from . import geom
    from .place import set_pose

    out = copy_board(board)
    rng = random.Random(seed)
    xs = [p[0] for p in out.outline]
    ys = [p[1] for p in out.outline]
    shape = out.outline_shape()
    for c in out.components:
        if is_fixed(c):
            continue
        for _ in range(200):
            x, y = rng.uniform(min(xs), max(xs)), rng.uniform(min(ys), max(ys))
            if geom.point_in_poly(x, y, shape):
                break
        set_pose(out, c.ref, x=x, y=y, rotation=rng.choice((0.0, 90.0, 180.0, 270.0)))
    return out


def hpwl(board: LayoutBoard, skip_nets: set[str] | None = None) -> float:
    """Half the perimeter of each net's pad box, summed: the wirelength
    estimate placement is scored on."""
    total = 0.0
    for net, pads in board.pads_by_net().items():
        if skip_nets and net in skip_nets or len(pads) < 2:
            continue
        xs = [p.x for p in pads]
        ys = [p.y for p in pads]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


def overlaps(board: LayoutBoard, min_area: float = 1.0) -> list[tuple[str, str]]:
    """Pairs of parts on one side whose courtyards share more than
    ``min_area`` square mils. Touching is not overlapping: two polygons
    that meet measure a distance of 0, so area is the test, not distance."""
    from . import geom

    # What each part keeps out: its courtyard or body, or, when the reader
    # had only its pads, the pads one by one (an edge connector's pads can
    # span the board and fence nothing between them).
    pads_of: dict[str, list] = {}
    for p in board.pads:
        if p.comp:
            pads_of.setdefault(p.comp, []).append(p)
    shapes = []
    for c in board.components:
        if c.courtyard_source == "pads":
            for p in pads_of.get(c.ref, []):
                s = p.shape_on(p.layers()[0]) if p.layers() else None
                if s is not None:
                    x0, y0, x1, y1 = s.bbox
                    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                    shapes.append((c.ref, c.side, ring, geom.polygon(ring)))
        elif len(c.courtyard) >= 3:
            shapes.append((c.ref, c.side, c.courtyard, geom.polygon(c.courtyard)))
    out = set()
    for i in range(len(shapes)):
        ra, sa, pa, a = shapes[i]
        for j in range(i + 1, len(shapes)):
            rb, sb, pb, b = shapes[j]
            if ra == rb or sa != sb or not geom.bboxes_near(a, b, 0.0):
                continue
            if geom.overlap_area(pa, pb) > min_area:
                out.add(tuple(sorted((ra, rb))))
    return sorted(out)


def decoupling_distances(board: LayoutBoard, ic_pads: int = 8) -> list[float]:
    """For each part that decouples an IC (two pads across two nets that
    each reach at least three pads), how far its nearer pad is from a pad
    of the same net on an IC (a part of ``ic_pads`` pads or more). The
    wirelength cannot see this: a supply net's box spans the board, and a
    decoupling part anywhere inside it changes nothing."""
    pads_of: dict[str, list] = {}
    for p in board.pads:
        if p.comp:
            pads_of.setdefault(p.comp, []).append(p)
    by_net = board.pads_by_net()
    ics = {c for c, ps in pads_of.items() if len(ps) >= ic_pads}
    out = []
    for comp, ps in pads_of.items():
        if len(ps) != 2 or comp in ics:
            continue
        nets = [p.net for p in ps]
        if not all(nets) or nets[0] == nets[1] or min(len(by_net.get(n, [])) for n in nets) < 3:
            continue
        best = min((math.hypot(p.x - q.x, p.y - q.y) for p in ps for q in by_net[p.net]
                    if q.comp in ics), default=None)
        if best is not None:
            out.append(best)
    return out


def placement_score(board: LayoutBoard) -> dict:
    from . import geom

    edge = board.outline_shape()
    outside = [c.ref for c in board.components if len(c.courtyard) >= 3
               and not all(geom.point_in_poly(x, y, edge) for x, y in c.courtyard)]
    dec = sorted(decoupling_distances(board))
    return {"hpwl": round(hpwl(board), 1), "overlaps": len(overlaps(board)),
            "outside": len(outside),
            "decoupling_median": round(dec[len(dec) // 2], 1) if dec else None,
            "decoupling_within_100": round(sum(d <= 100.0 for d in dec) / len(dec), 3) if dec else None}
