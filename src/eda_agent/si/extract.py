# SPDX-License-Identifier: Apache-2.0
"""A net's routed path, from the board's copper, as line sections and vias.

The copper of one net (tracks, arcs, vias, pads) is joined into a graph:
a track end that lands on a pad, a via, another track's end, or the
middle of another track on the same layer is connected to it. The path
the signal takes is the shortest route through that graph between the
two pins (given, or the two farthest apart). Everything else that hangs
off the path is a stub, with its length and the point it leaves from.

Along the path, runs on one layer with one width merge into one
section. For a differential pair the two nets are walked the same way
and then laid side by side: each stretch of the positive net is matched
to the nearest copper of the negative net on its layer, and stretches
with a steady gap become coupled sections; the rest is uncoupled
(the breakouts at either end).

Lengths are in mils, as on the board.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

import networkx as nx

from ..layout import geom
from ..layout.model import LayoutBoard, Pad

SNAP = 0.05          # mils: track ends this close are one point
SAMPLE = 5.0         # mils between samples when matching a pair's sides
GAP_STEP = 0.25      # mils: gaps closer than this are one coupled section
#: A side further than this many trace widths from its partner is
#: uncoupled.
COUPLED_WITHIN = 4.0
#: A coupled stretch shorter than this is part of a breakout: uncoupled.
MIN_COUPLED = 25.0


@dataclass
class Section:
    """One stretch of the path."""

    kind: str                       # "line" | "via"
    layer: str = ""
    width: float = 0.0
    length: float = 0.0
    gap: Optional[float] = None     # pairs: edge gap of a coupled section
    via: Optional[int] = None       # index into board.vias
    from_layer: str = ""
    to_layer: str = ""
    pts: list[tuple[float, float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind}
        if self.kind == "line":
            d.update(layer=self.layer, width_mils=round(self.width, 4),
                     length_mils=round(self.length, 3))
            if self.gap is not None:
                d["gap_mils"] = round(self.gap, 3)
        else:
            d.update(via=self.via, from_layer=self.from_layer, to_layer=self.to_layer)
        return d


@dataclass
class Stub:
    at: int                          # path section it leaves after (-1: the start)
    layer: str
    width: float
    length: float
    to: str = ""                     # a pad it reaches, if any

    def to_dict(self) -> dict[str, Any]:
        return {"after_section": self.at, "layer": self.layer,
                "width_mils": round(self.width, 4), "length_mils": round(self.length, 3),
                "to": self.to}


@dataclass
class NetPath:
    net: str
    start: str
    end: str
    sections: list[Section]
    stubs: list[Stub] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    pins: list[str] = field(default_factory=list)

    @property
    def length(self) -> float:
        return sum(s.length for s in self.sections if s.kind == "line")

    @property
    def vias(self) -> int:
        return sum(1 for s in self.sections if s.kind == "via")

    def to_dict(self) -> dict[str, Any]:
        return {"net": self.net, "from": self.start, "to": self.end,
                "length_mils": round(self.length, 3), "vias": self.vias,
                "sections": [s.to_dict() for s in self.sections],
                "stubs": [s.to_dict() for s in self.stubs],
                "pins": self.pins, "notes": self.notes}


class ExtractError(ValueError):
    pass


# -- the graph --------------------------------------------------------------------

def _seg_param(px, py, ax, ay, bx, by) -> tuple[float, float]:
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    qx, qy = ax + t * dx, ay + t * dy
    return t, math.hypot(px - qx, py - qy)


@dataclass
class _Piece:
    layer: str
    width: float
    pts: list[tuple[float, float]]

    @property
    def length(self) -> float:
        return sum(math.dist(self.pts[i], self.pts[i + 1]) for i in range(len(self.pts) - 1))


def _pieces(board: LayoutBoard, net: str) -> list[_Piece]:
    out = []
    for t in board.tracks:
        if t.net == net and not t.keepout:
            out.append(_Piece(t.layer, t.width, [(t.x1, t.y1), (t.x2, t.y2)]))
    for a in board.arcs:
        if a.net == net and not a.keepout:
            out.append(_Piece(a.layer, a.width,
                              geom.arc_points(a.cx, a.cy, a.radius, a.a1, a.a2, 0.02)))
    return out


def _split_tees(pieces: list[_Piece]) -> list[_Piece]:
    """Split a piece where another piece's end lands on its middle."""
    ends = [(p.layer, p.pts[0]) for p in pieces] + [(p.layer, p.pts[-1]) for p in pieces]
    out: list[_Piece] = []
    for p in pieces:
        cuts: list[tuple[int, float, tuple[float, float]]] = []
        for layer, (x, y) in ends:
            if layer != p.layer:
                continue
            for i in range(len(p.pts) - 1):
                (ax, ay), (bx, by) = p.pts[i], p.pts[i + 1]
                t, d = _seg_param(x, y, ax, ay, bx, by)
                if d <= p.width / 2 and 1e-6 < t < 1 - 1e-6:
                    seg = math.dist((ax, ay), (bx, by))
                    if seg * min(t, 1 - t) > SNAP:
                        # cut at the landing end itself, so the two meet
                        # (a kink of at most half the width)
                        cuts.append((i, t, (x, y)))
        if not cuts:
            out.append(p)
            continue
        cuts.sort()
        pts = [p.pts[0]]
        cur = 0
        for i, _t, q in cuts:
            while cur < i:
                cur += 1
                pts.append(p.pts[cur])
            pts.append(q)
            out.append(_Piece(p.layer, p.width, pts))
            pts = [q]
        pts.extend(p.pts[cur + 1:])
        out.append(_Piece(p.layer, p.width, pts))
    return out


def _attach(board: LayoutBoard, net: str, layer: str, x: float, y: float,
            pads: list[tuple[int, Pad]], vias: list[int]) -> Optional[tuple]:
    for i in vias:
        v = board.vias[i]
        if math.hypot(x - v.x, y - v.y) <= v.layer_diameters.get(layer, v.diameter) / 2 + SNAP:
            return ("via", i)
    for i, p in pads:
        s = p.shape_on(layer)
        if s is not None and geom.core_distance(geom.Shape("point", ((x, y),)), s) <= s.r + SNAP:
            return ("pad", i)
    return None


def net_graph(board: LayoutBoard, net: str) -> nx.MultiGraph:
    pads = [(i, p) for i, p in enumerate(board.pads) if p.net == net]
    vias = [i for i, v in enumerate(board.vias) if v.net == net]
    g = nx.MultiGraph()
    for i, p in pads:
        g.add_node(("pad", i), label=p.key)
    for i in vias:
        g.add_node(("via", i))
    pieces = _split_tees(_pieces(board, net))
    for p in pieces:
        nodes = []
        for (x, y) in (p.pts[0], p.pts[-1]):
            hit = _attach(board, net, p.layer, x, y, pads, vias)
            nodes.append(hit or ("pt", p.layer, round(x / SNAP), round(y / SNAP)))
        if nodes[0] == nodes[1] and nodes[0][0] != "pt":
            # a short piece inside one pad or via: no length on the path
            continue
        g.add_edge(nodes[0], nodes[1], layer=p.layer, width=p.width, length=p.length,
                   pts=p.pts)
    # Through-hole pads and vias also join copper that merely overlaps
    # them without an end on them; that is covered by _attach above.
    return g


# -- paths ---------------------------------------------------------------------------

def _pad_index(board: LayoutBoard, net: str, key: str) -> int:
    for i, p in enumerate(board.pads):
        if p.net == net and p.key == key:
            return i
    raise ExtractError(f"pin {key} is not on net {net}")


def _component_pads(board: LayoutBoard, g: nx.MultiGraph) -> list[int]:
    return [n[1] for n in g.nodes if n[0] == "pad" and board.pads[n[1]].comp]


def _ends(board: LayoutBoard, g: nx.MultiGraph, net: str, start: str, end: str
          ) -> tuple[int, int]:
    if start and end:
        return _pad_index(board, net, start), _pad_index(board, net, end)
    pins = _component_pads(board, g)
    if len(pins) < 2:
        raise ExtractError(f"net {net} has fewer than two component pins")
    if start:
        a = _pad_index(board, net, start)
        dist = nx.single_source_dijkstra_path_length(g, ("pad", a), weight="length")
        b = max((p for p in pins if p != a and ("pad", p) in dist),
                key=lambda p: dist[("pad", p)], default=None)
        if b is None:
            raise ExtractError(f"no routed pin of {net} is reachable from {start}")
        return a, b
    best, pair = -1.0, None
    for a in pins:
        dist = nx.single_source_dijkstra_path_length(g, ("pad", a), weight="length")
        for b in pins:
            if b != a and ("pad", b) in dist and dist[("pad", b)] > best:
                best, pair = dist[("pad", b)], (a, b)
    if pair is None:
        raise ExtractError(f"net {net} is not routed between any two pins")
    if end:
        return pair[0], _pad_index(board, net, end)
    return pair


def _edge_sequence(g: nx.MultiGraph, nodes: list) -> list[dict[str, Any]]:
    out = []
    for u, v in zip(nodes, nodes[1:]):
        data = min(g.get_edge_data(u, v).values(), key=lambda d: d["length"])
        pts = data["pts"]
        # orient the polyline from u to v
        if u[0] == "pt" and (round(pts[0][0] / SNAP), round(pts[0][1] / SNAP)) != (u[2], u[3]):
            pts = pts[::-1]
        elif v[0] == "pt" and (round(pts[-1][0] / SNAP), round(pts[-1][1] / SNAP)) != (v[2], v[3]):
            pts = pts[::-1]
        out.append({**data, "pts": pts, "u": u, "v": v})
    return out


def _sections(edges: list[dict[str, Any]]) -> tuple[list[Section], list[int]]:
    """Merged sections, and the section each edge went into."""
    out: list[Section] = []
    edge_sec: list[int] = []
    for e in edges:
        if e["u"][0] == "via" and out and out[-1].kind == "line" and out[-1].layer != e["layer"]:
            out.append(Section("via", via=e["u"][1], from_layer=out[-1].layer, to_layer=e["layer"]))
        last = out[-1] if out else None
        if (last is not None and last.kind == "line" and last.layer == e["layer"]
                and abs(last.width - e["width"]) < 0.05):
            last.length += e["length"]
            last.pts.extend(e["pts"][1:])
        else:
            out.append(Section("line", layer=e["layer"], width=e["width"], length=e["length"],
                               pts=list(e["pts"])))
        edge_sec.append(len(out) - 1)
    return out, edge_sec


def _stubs(board: LayoutBoard, g: nx.MultiGraph, nodes: list, edge_sec: list[int]
           ) -> list[Stub]:
    """Copper hanging off the path, one stub per branch."""
    on_path = set(nodes)
    path_edges = {frozenset((u, v)) for u, v in zip(nodes, nodes[1:])}
    rest = nx.MultiGraph()
    for u, v, d in g.edges(data=True):
        if frozenset((u, v)) not in path_edges or u == v:
            rest.add_edge(u, v, **d)
    out = []
    for k, n in enumerate(nodes):
        if n not in rest:
            continue
        others = rest.subgraph([m for m in rest.nodes if m != n])
        seen: set = set()
        for m in rest.neighbors(n):
            if m in seen or m in on_path:
                continue
            comp = nx.node_connected_component(others, m)
            seen |= comp
            if comp & on_path:
                continue        # a second route back to the path, not a stub
            sub = rest.subgraph(comp | {n})
            length = sum(d["length"] for _, _, d in sub.edges(data=True))
            first = max((d for _, mm, d in rest.edges(n, data=True) if mm in comp),
                        key=lambda d: d["length"])
            pins = [board.pads[x[1]].key for x in comp if x[0] == "pad"]
            out.append(Stub(at=edge_sec[k - 1] if k > 0 else -1, layer=first["layer"],
                            width=first["width"], length=length, to=",".join(pins)))
    return out


def net_path(board: LayoutBoard, net: str, start: str = "", end: str = "") -> NetPath:
    """The routed path of ``net`` between pins ``start`` and ``end``
    (``"U1.5"``), or between the two pins farthest apart."""
    g = net_graph(board, net)
    if not g.number_of_edges():
        raise ExtractError(f"net {net} has no routed copper")
    a, b = _ends(board, g, net, start, end)
    try:
        nodes = nx.shortest_path(g, ("pad", a), ("pad", b), weight="length")
    except nx.NetworkXNoPath:
        raise ExtractError(f"{board.pads[a].key} and {board.pads[b].key} are not connected "
                           f"by copper on net {net}") from None
    sections, edge_sec = _sections(_edge_sequence(g, nodes))
    notes = []
    pins = sorted(board.pads[n[1]].key for n in g.nodes if n[0] == "pad" and board.pads[n[1]].comp)
    stubs = _stubs(board, g, nodes, edge_sec)
    if len(pins) > 2:
        notes.append(f"net has {len(pins)} pins; the path runs {board.pads[a].key} to "
                     f"{board.pads[b].key} and the other branches are stubs")
    return NetPath(net, board.pads[a].key, board.pads[b].key, sections, stubs, notes, pins)


# -- pairs -------------------------------------------------------------------------

def _samples(pts: list[tuple[float, float]], step: float) -> list[tuple[float, float, float]]:
    """(x, y, ds) along a polyline every ``step``."""
    out = []
    for i in range(len(pts) - 1):
        (ax, ay), (bx, by) = pts[i], pts[i + 1]
        L = math.dist((ax, ay), (bx, by))
        n = max(1, int(math.ceil(L / step)))
        for k in range(n):
            t = (k + 0.5) / n
            out.append((ax + t * (bx - ax), ay + t * (by - ay), L / n))
    return out


def _nearest(x: float, y: float, sections: list[Section], layer: str) -> tuple[float, float]:
    best, width = math.inf, 0.0
    for s in sections:
        if s.kind != "line" or s.layer != layer:
            continue
        for i in range(len(s.pts) - 1):
            _, d = _seg_param(x, y, *s.pts[i], *s.pts[i + 1])
            if d < best:
                best, width = d, s.width
    return best, width


@dataclass
class PairPath:
    name: str
    p: NetPath
    n: NetPath
    #: Zones between vias: per zone a list of coupled stretches
    #: (layer, width, gap, length) and the uncoupled length of each side.
    zones: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"pair": self.name, "positive": self.p.to_dict(), "negative": self.n.to_dict(),
                "zones": self.zones, "notes": self.notes,
                "skew_mils": round(self.p.length - self.n.length, 3)}


def _zones(path: NetPath) -> list[list[Section]]:
    zones: list[list[Section]] = [[]]
    for s in path.sections:
        if s.kind == "via":
            zones.append([])
        else:
            zones[-1].append(s)
    return zones


def pair_path(board: LayoutBoard, name: str, pos: str, neg: str,
              start: tuple[str, str] = ("", ""), end: tuple[str, str] = ("", "")) -> PairPath:
    """Both sides of a pair, matched into coupled and uncoupled stretches."""
    p = net_path(board, pos, start[0], end[0])
    n = net_path(board, neg, start[1], end[1])
    # Make both run the same way: N starts at the component P starts at.
    if p.start.split(".")[0] != n.start.split(".")[0] and p.start.split(".")[0] == n.end.split(".")[0]:
        n = net_path(board, neg, n.end, n.start)
    out = PairPath(name, p, n)
    zp, zn = _zones(p), _zones(n)
    if len(zp) != len(zn):
        out.notes.append(f"the sides change layer a different number of times ({p.vias} and "
                         f"{n.vias} vias); modelled as two uncoupled lines")
        out.zones = [{"coupled": [], "uncoupled_p": [(s.layer, s.width, s.length) for s in p.sections
                                                     if s.kind == "line"],
                      "uncoupled_n": [(s.layer, s.width, s.length) for s in n.sections
                                      if s.kind == "line"]}]
        return out
    for secs_p, secs_n in zip(zp, zn):
        coupled: list[list[Any]] = []
        unc_p: list[list[Any]] = []
        for s in secs_p:
            run_gap, run_len = None, 0.0

            def flush():
                nonlocal run_gap, run_len
                if run_len > 0:
                    if run_gap is None:
                        unc_p.append([s.layer, s.width, run_len])
                    else:
                        coupled.append([s.layer, s.width, run_gap, run_len])
                run_gap, run_len = None, 0.0

            for x, y, ds in _samples(s.pts, SAMPLE):
                d, wn = _nearest(x, y, secs_n, s.layer)
                gap = d - (s.width + wn) / 2
                g = (round(gap / GAP_STEP) * GAP_STEP
                     if gap > 0 and d <= COUPLED_WITHIN * max(s.width, wn) + s.width else None)
                if g != run_gap:
                    flush()
                    run_gap = g
                run_len += ds
            flush()
        # Short stretches (a breakout fanning out) count as uncoupled;
        # the rest merge by layer, width and gap.
        merged: dict[tuple, float] = {}
        for layer, w, gap, L in coupled:
            if L < MIN_COUPLED:
                unc_p.append([layer, w, L])
                continue
            merged[(layer, round(w, 2), gap)] = merged.get((layer, round(w, 2), gap), 0.0) + L
        coupled_list = [[k[0], k[1], k[2], v] for k, v in merged.items()]
        loose: dict[tuple, float] = {}
        for layer, w, L in unc_p:
            loose[(layer, round(w, 2))] = loose.get((layer, round(w, 2)), 0.0) + L
        unc_p = [[k[0], k[1], v] for k, v in loose.items()]
        c_len = sum(c[3] for c in coupled_list)
        n_len = sum(s.length for s in secs_n)
        # The negative side's uncoupled part is what is left of it.
        unc_n = []
        if secs_n:
            main = max(secs_n, key=lambda s: s.length)
            unc_n.append([main.layer, main.width, max(n_len - c_len, 0.0)])
        out.zones.append({"coupled": coupled_list, "uncoupled_p": unc_p, "uncoupled_n": unc_n})
    return out


def pins_of(board: LayoutBoard, net: str) -> list[str]:
    return sorted(p.key for p in board.pads if p.net == net and p.comp)


def find_pair(board: LayoutBoard, name: str) -> tuple[str, str]:
    for d in board.diff_pairs:
        if d.name == name or name in (d.positive, d.negative):
            return d.positive, d.negative
    raise ExtractError(f"no differential pair named {name}; pairs: "
                       f"{', '.join(d.name for d in board.diff_pairs) or 'none'}")


__all__ = ["Section", "Stub", "NetPath", "PairPath", "ExtractError", "net_graph", "net_path",
           "pair_path", "find_pair", "pins_of"]
