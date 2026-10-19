# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The routed copper path between two pads of one net (offline).

Builds one net's copper as a graph: track and arc endpoints are nodes on
their layer, a via joins its layers, and a pad joins every track end that
lands on its copper (all layers for a through-hole pad). Dijkstra by length
then gives the path a current actually follows from one pad to another,
which is what loop areas and pad-entry checks are measured on.

Approximations, stated so a reader can judge a result: a pad's copper is
taken as a circle of half its longer side, and a track end counts as landing
on a pad when it is inside that circle. Pours and planes are not traced;
callers treat a plane net as a straight return.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from typing import Optional

from eda_agent.layout import geom
from eda_agent.layout.model import LayoutBoard, Pad

Node = tuple[str, int, int]        # (layer, x, y) at 0.01 mil


def _key(layer: str, x: float, y: float) -> Node:
    return (layer, int(round(x * 100)), int(round(y * 100)))


def _xy(node: Node) -> tuple[float, float]:
    return node[1] / 100.0, node[2] / 100.0


def pad_layers(board: LayoutBoard, pad: Pad) -> list[str]:
    if pad.hole > 0:
        return board.copper_layers()
    return [c.layer for c in pad.copper] or board.copper_layers()[:1]


def pad_reach(pad: Pad) -> float:
    """Half the pad's longer side: how far from its centre a track end may
    land and still be on the pad."""
    sizes = [max(c.w, c.h) for c in pad.copper if c.w or c.h]
    return (max(sizes) / 2.0) if sizes else 10.0


class NetCopper:
    """One net's routed copper as a weighted graph."""

    def __init__(self, board: LayoutBoard, net: str):
        self.board = board
        self.net = net
        self.adj: dict[object, list[tuple[object, float]]] = defaultdict(list)
        # Pad nodes stay in the graph once attached, so a later path may run
        # through one: remember where each sits.
        self.pad_xy: dict[object, tuple[float, float]] = {}
        self.ends_by_layer: dict[str, list[Node]] = defaultdict(list)
        layers = board.copper_layers()
        order = {name: i for i, name in enumerate(layers)}

        def link(a, b, w):
            self.adj[a].append((b, w))
            self.adj[b].append((a, w))

        for t in board.tracks:
            if t.net != net or t.keepout:
                continue
            a, b = _key(t.layer, t.x1, t.y1), _key(t.layer, t.x2, t.y2)
            link(a, b, math.hypot(t.x2 - t.x1, t.y2 - t.y1))
            self.ends_by_layer[t.layer] += [a, b]
        for arc in board.arcs:
            if arc.net != net or arc.keepout:
                continue
            p1 = (arc.cx + arc.radius * math.cos(math.radians(arc.a1)),
                  arc.cy + arc.radius * math.sin(math.radians(arc.a1)))
            p2 = (arc.cx + arc.radius * math.cos(math.radians(arc.a2)),
                  arc.cy + arc.radius * math.sin(math.radians(arc.a2)))
            sweep = math.radians((arc.a2 - arc.a1) % 360 or 360)
            a, b = _key(arc.layer, *p1), _key(arc.layer, *p2)
            link(a, b, arc.radius * sweep)
            self.ends_by_layer[arc.layer] += [a, b]
        for v in board.vias:
            if v.net != net:
                continue
            lo, hi = order.get(v.low_layer, 0), order.get(v.high_layer,
                                                          len(layers) - 1)
            span = layers[min(lo, hi):max(lo, hi) + 1] or layers
            nodes = [_key(l, v.x, v.y) for l in span]
            for n in nodes:
                self.ends_by_layer[n[0]].append(n)
            for i in range(len(nodes) - 1):
                link(nodes[i], nodes[i + 1], 0.0)
        # Solid copper and poured copper of this net conduct between
        # everything that lands on them. Each region is a hub joined at no
        # cost to the ends inside it; a path through a hub draws straight
        # from where it enters to where it leaves (see ``path``). Without
        # this a supply carried by a fill was traced the long way round on
        # its tracks, giving loops sixty times their real area.
        self.hubs: list[tuple[object, str, object]] = []
        for i, r in enumerate(board.regions):
            if r.net != net or r.keepout or r.kind not in ("copper", "pour"):
                continue
            hub = ("region", i)
            shape = r.shape()
            self.hubs.append((hub, r.layer, shape))
            for end in set(self.ends_by_layer.get(r.layer, ())):
                if geom.point_in_poly(*_xy(end), shape):
                    link(hub, end, 0.0)
        # Track ends that touch without sharing an exact endpoint (a track
        # ending on a via pad or on another track's end within its width)
        # are joined by a short, zero-length hop.
        for layer, ends in self.ends_by_layer.items():
            uniq = sorted(set(ends))
            for i, a in enumerate(uniq):
                ax, ay = _xy(a)
                for b in uniq[i + 1:i + 8]:
                    bx, by = _xy(b)
                    if abs(bx - ax) < 2 and abs(by - ay) < 2:
                        link(a, b, 0.0)

    def attach_pad(self, pad: Pad) -> object:
        """A node for ``pad`` joined to every track end landing on it."""
        node = ("pad", pad.comp, str(pad.name))
        self.pad_xy[node] = (pad.x, pad.y)
        if node in self.adj:
            return node
        reach = pad_reach(pad)
        layers = pad_layers(self.board, pad)
        for layer in layers:
            for end in self.ends_by_layer.get(layer, ()):
                ex, ey = _xy(end)
                d = math.hypot(ex - pad.x, ey - pad.y)
                if d <= reach + 1e-6:
                    self.adj[node].append((end, d))
                    self.adj[end].append((node, d))
        for hub, layer, shape in self.hubs:
            if layer in layers and geom.point_in_poly(pad.x, pad.y, shape):
                self.adj[node].append((hub, 0.0))
                self.adj[hub].append((node, 0.0))
        return node

    def entries(self, pad: Pad) -> list[tuple[str, float, float]]:
        """(layer, x, y) of every track end landing on ``pad``."""
        reach = pad_reach(pad)
        out = []
        for layer in pad_layers(self.board, pad):
            for end in set(self.ends_by_layer.get(layer, ())):
                ex, ey = _xy(end)
                if math.hypot(ex - pad.x, ey - pad.y) <= reach + 1e-6:
                    out.append((layer, ex, ey))
        return out

    def path(self, a: Pad, b: Pad) -> Optional[list[tuple[float, float]]]:
        """Points along the shortest copper path from pad ``a`` to ``b``,
        both pad centres included, or None when copper does not join them."""
        src, dst = self.attach_pad(a), self.attach_pad(b)
        dist = {src: 0.0}
        prev: dict = {}
        heap = [(0.0, 0, src)]
        seq = 1
        while heap:
            d, _, n = heapq.heappop(heap)
            if n == dst:
                break
            if d > dist.get(n, math.inf):
                continue
            for m, w in self.adj.get(n, ()):
                nd = d + w
                if nd < dist.get(m, math.inf):
                    dist[m] = nd
                    prev[m] = n
                    heapq.heappush(heap, (nd, seq, m))
                    seq += 1
        if dst not in dist:
            return None
        nodes = [dst]
        while nodes[-1] != src:
            nodes.append(prev[nodes[-1]])
        nodes.reverse()
        pts = []
        for n in nodes:
            if isinstance(n, tuple) and n and n[0] == "region":
                continue                      # crossing a fill: straight
            p = self.pad_xy[n] if n in self.pad_xy else _xy(n)
            if not pts or pts[-1] != p:
                pts.append(p)
        return pts


def plane_nets(board: LayoutBoard) -> set[str]:
    """Nets carried by a plane layer or a pour region."""
    nets = {l.plane_net for l in board.layers if getattr(l, "plane_net", "")}
    nets |= {r.net for r in board.regions
             if r.net and getattr(r, "kind", "") == "pour"}
    return nets


__all__ = ["NetCopper", "pad_layers", "pad_reach", "plane_nets"]
