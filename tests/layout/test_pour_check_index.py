# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Checking the pours for foreign copper without walking every edge.

MEASURED 2026-11-20 on a board with 283 pours, the largest with 24,676
vertices and the most holed with 1,288 holes: Python took 12.3 s of a
100 s read, and 12 s of it was this check. Each pour tested every via,
pad and track of the board one by one, and every point in a pour's box
walked the whole outline and its holes.

The indexes answer exactly what the old walk answered. These tests hold
them to that on random shapes, with the old code kept here as the
reference.
"""
from __future__ import annotations

import random

import pytest

from eda_agent.layout import geom
from eda_agent.layout import read_altium as ra
from eda_agent.layout.model import (
    Layer, LayoutBoard, Pad, PadCopper, Region, Track, Via,
)


# --- the old code, kept as the reference ----------------------------------

def _old_foreign_copper(board, region, net) -> bool:
    body = region.shape()
    x0, y0, x1, y1 = body.bbox
    layer = region.layer

    def inside(x, y):
        return x0 <= x <= x1 and y0 <= y <= y1 and geom.point_in_poly(x, y, body)

    for v in board.vias:
        if v.net != net and layer in board.layers_between(v.low_layer, v.high_layer) \
                and inside(v.x, v.y):
            return True
    for p in board.pads:
        if p.net != net and (layer in p.layers() or (p.hole > 0 and p.plated)) \
                and inside(p.x, p.y):
            return True
    for t in board.tracks:
        if t.net != net and t.layer == layer and not t.keepout \
                and inside((t.x1 + t.x2) / 2, (t.y1 + t.y2) / 2):
            return True
    return False


# --- random shapes --------------------------------------------------------

def _ring(rng, n, cx, cy, size, grid):
    """A ring of ``n`` points round (cx, cy): star-shaped, or anything at
    all, self-crossing included. On a coarse grid, so points land exactly
    on vertices and edges, and some edges come out level."""
    pts = []
    if rng.random() < 0.7:
        import math
        for k in range(n):
            a = 2 * math.pi * k / n
            r = size * (0.4 + 0.6 * rng.random())
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    else:
        pts = [(cx + size * (2 * rng.random() - 1), cy + size * (2 * rng.random() - 1))
               for _ in range(n)]
    if grid:
        pts = [(round(x / grid) * grid, round(y / grid) * grid) for x, y in pts]
    return pts


def _shape(rng, grid):
    outer = _ring(rng, rng.randint(3, 60), 0.0, 0.0, 100.0, grid)
    holes = [_ring(rng, rng.randint(3, 12), rng.uniform(-80, 80), rng.uniform(-80, 80),
                   rng.uniform(1, 15), grid)
             for _ in range(rng.randint(0, 25))]
    return geom.polygon(outer, holes)


def _probes(rng, shape, grid):
    """Every vertex, every edge's midpoint, and points at random."""
    out = list(shape.pts)
    for ring in (shape.pts, *shape.holes):
        out += list(ring)
        out += [((ring[i][0] + ring[i - 1][0]) / 2, (ring[i][1] + ring[i - 1][1]) / 2)
                for i in range(len(ring))]
    for _ in range(300):
        x, y = rng.uniform(-110, 110), rng.uniform(-110, 110)
        if grid and rng.random() < 0.5:
            x, y = round(x / grid) * grid, round(y / grid) * grid
        out.append((x, y))
    return out


@pytest.mark.parametrize("seed", range(60))
def test_a_body_says_inside_exactly_where_point_in_poly_does(seed):
    rng = random.Random(seed)
    grid = rng.choice([0.0, 0.25, 5.0, 20.0])
    shape = _shape(rng, grid)
    body = ra._Body(shape)
    for x, y in _probes(rng, shape, grid):
        assert body.contains(x, y) == geom.point_in_poly(x, y, shape), (seed, x, y)


def test_a_point_in_one_hole_of_many_walks_that_hole_alone(monkeypatch):
    holes = [[(x, y), (x + 4, y), (x + 4, y + 4), (x, y + 4)]
             for x in range(10, 990, 20) for y in range(10, 990, 20)]
    shape = geom.polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)], holes)
    assert len(shape.holes) == 2401
    body = ra._Body(shape)
    walked = []
    real = geom._inside_ring
    monkeypatch.setattr(geom, "_inside_ring",
                        lambda px, py, ring: walked.append(ring) or real(px, py, ring))
    assert body.contains(512.0, 512.0) is False
    assert len(walked) == 1


# --- random boards --------------------------------------------------------

_LAYERS = ["Top", "In1", "In2", "Bottom"]
_NETS = ["", "GND", "VCC", "A", "B"]


def _board(rng):
    b = LayoutBoard()
    b.layers = [Layer(name, "plane" if name == "In2" else "signal", order=i)
                for i, name in enumerate(_LAYERS)]
    grid = rng.choice([0.0, 5.0])

    def at():
        x, y = rng.uniform(-110, 110), rng.uniform(-110, 110)
        if grid:
            x, y = round(x / grid) * grid, round(y / grid) * grid
        return x, y

    def via(x, y, net):
        lo, hi = rng.choice(_LAYERS + ["Nowhere"]), rng.choice(_LAYERS)
        b.vias.append(Via(x, y, 10.0, 5.0, lo, hi, net))

    def pad(x, y, net):
        layers = rng.sample(_LAYERS, rng.randint(0, 2))
        b.pads.append(Pad(comp="U1", name="1", x=x, y=y, net=net,
                          copper=[PadCopper(l, "round", 10.0, 10.0) for l in layers],
                          hole=rng.choice([0.0, 0.0, 12.0]), plated=rng.random() < 0.7))

    def track(x1, y1, x2, y2, net, layer=None):
        b.tracks.append(Track(layer or rng.choice(_LAYERS), x1, y1, x2, y2, 6.0, net,
                              keepout=rng.random() < 0.1))

    # A pour as a real one comes back: foreign copper in its voids, its
    # own net's copper in its body. Then a little copper anywhere at all.
    for _ in range(rng.randint(1, 4)):
        shape = _shape(rng, grid)
        net = rng.choice(_NETS[1:])
        layer = rng.choice(_LAYERS)
        b.regions.append(Region(layer, list(shape.pts), [list(h) for h in shape.holes],
                                net, "pour"))
        for hole in shape.holes:
            x = sum(p[0] for p in hole) / len(hole)
            y = sum(p[1] for p in hole) / len(hole)
            rng.choice([via, pad])(x, y, rng.choice(_NETS))
        for _ in range(rng.randint(0, 6)):
            x1, y1 = rng.choice(shape.pts)
            x2, y2 = rng.choice(shape.pts)
            track(0.2 * x1, 0.2 * y1, 0.2 * x2, 0.2 * y2, net, layer)
    if rng.random() < 0.5:
        for _ in range(rng.randint(1, 3)):
            rng.choice([via, pad])(*at(), rng.choice(_NETS))
        (x1, y1), (x2, y2) = at(), at()
        track(x1, y1, x2, y2, rng.choice(_NETS))
    return b


def test_foreign_copper_is_found_exactly_where_the_old_walk_found_it():
    seen = set()
    for seed in range(300):
        rng = random.Random(1000 + seed)
        board = _board(rng)
        standing = ra._Standing(board)
        for region in board.regions:
            body = ra._Body(region.shape())
            for net in _NETS:
                old = _old_foreign_copper(board, region, net)
                assert ra._foreign_copper(board, region, net, standing, body) == old, \
                    (seed, region.layer, net)
                assert ra._foreign_copper(board, region, net) == old
                seen.add((net == region.net, old))
    # Both answers, for a pour's own net and for others: a generator that
    # only ever found foreign copper would prove nothing about the rest.
    assert seen == {(True, True), (True, False), (False, True), (False, False)}


def test_each_via_span_is_worked_out_once_per_board(monkeypatch):
    """363,418 layers_between calls on the measured board, one per via per
    pour: each sorted the layer stack again."""
    rng = random.Random(7)
    board = _board(rng)
    while len(board.regions) < 6:
        board.regions += _board(rng).regions
    calls = []
    real = LayoutBoard.layers_between
    monkeypatch.setattr(LayoutBoard, "layers_between",
                        lambda self, lo, hi: calls.append((lo, hi)) or real(self, lo, hi))
    ra.repair_pour_nets(board)
    assert len(calls) == len(set(calls)) <= len({(v.low_layer, v.high_layer)
                                                 for v in board.vias})
