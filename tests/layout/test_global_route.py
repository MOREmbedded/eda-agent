# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Global routing: tile capacities, the join order, and corridors."""

from __future__ import annotations

import numpy as np

from eda_agent.layout.model import Layer, LayoutBoard, Pad, PadCopper, Rule
from eda_agent.layout.route import Router
from eda_agent.layout.route.global_route import GlobalRouter, prim_pairs


def _rules():
    return [Rule("Clearance", "0", "All", "All", 1, True, {"gap": 6.0},
                 "Clearance Constraint (Gap=6mil) (All),(All)"),
            Rule("Width", "1", "All", "All", 1, True, {},
                 "Width Constraint (Min=6mil) (Max=6mil) (Preferred=6mil) (All)")]


def _board(pads=()):
    return LayoutBoard(name="g", outline=[(0, 0), (800, 0), (800, 400), (0, 400)],
                       layers=[Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)],
                       pads=list(pads), rules=_rules())


def _pad(comp, x, y, net):
    return Pad(comp, "1", x, y, net=net, copper=[PadCopper("TopLayer", "rect", 20, 20)])


def test_a_clear_tile_side_holds_its_length_in_tracks():
    r = Router(_board([_pad("R1", 100, 200, "A"), _pad("R2", 700, 200, "A")]))
    gr = GlobalRouter(r, tile_mils=40.0)
    side = gr.T * r.pitch
    per_track = r.w_def + r.c
    # A tile in the middle of an empty board, away from the pads.
    gy, gx = gr.GY // 2, gr.GX // 2 + 3
    assert gr.cap_e[0, gy, gx] == int(side // per_track)


def test_pads_eat_the_capacity_of_the_sides_they_sit_on():
    pads = [_pad(f"U{i}", 400, 40 + 40 * i, f"N{i}") for i in range(9)]
    r = Router(_board(pads + [_pad("R1", 100, 200, "A"), _pad("R2", 700, 200, "A")]))
    gr = GlobalRouter(r, tile_mils=40.0)
    col = int(400 / r.pitch) // gr.T
    clear = gr.cap_e[0, :, col + 3].sum()
    crowded = gr.cap_e[0, :, col].sum() + gr.cap_e[0, :, col - 1].sum()
    assert crowded < 2 * clear


def test_the_join_order_takes_the_nearest_pad_to_what_is_joined():
    cx = np.array([0.0, 100.0, 10.0, 300.0])
    cy = np.array([0.0, 0.0, 0.0, 0.0])
    assert prim_pairs(cx, cy) == [(2, 0), (1, 2), (3, 1)]


def test_two_crossing_nets_settle_with_nothing_overfull():
    pads = [_pad("R1", 100, 200, "A"), _pad("R2", 700, 200, "A"),
            _pad("R3", 400, 50, "B"), _pad("R4", 400, 350, "B")]
    r = Router(_board(pads))
    gr = GlobalRouter(r, tile_mils=40.0)
    stats = gr.run(r.jobs)
    assert stats["overflow"] == 0
    for job in r.jobs:
        m = gr.corridor(job, 0, grow=0)
        assert m is not None and m.any()


def _crowded_board():
    """Six nets that must all cross a wall of pads with one gap a track
    wide: the gap's side stays overfull whatever the negotiation does."""
    wall = [Pad("W", str(k), 400, 10 + 20 * k, net="",
                copper=[PadCopper("TopLayer", "rect", 20, 20),
                        PadCopper("BottomLayer", "rect", 20, 20)], hole=10)
            for k in range(20) if k != 10]
    pads = list(wall)
    for i in range(6):
        y = 40 + 60 * i
        pads += [_pad(f"A{i}", 100, y, f"N{i}"), _pad(f"B{i}", 700, y, f"N{i}")]
    return _board(pads)


def test_the_tile_graph_kept_between_searches_is_the_one_built_afresh():
    from scipy.sparse import csr_matrix

    r = Router(_crowded_board())
    gr = GlobalRouter(r, tile_mils=40.0)
    gr.run(r.jobs, iterations=3)

    def fresh(pres):
        over = np.maximum(0, gr.use + 1 - gr.cap)[gr.eid]
        full = gr.cap[gr.eid] <= 0
        w = gr.base * (1.0 + gr.hist[gr.eid]) * (1.0 + pres * over)
        w = np.where(full & (gr.eid < 2 * gr.n_e), w * 50.0, w)
        rng = np.random.default_rng(0).permutation(len(w))
        return csr_matrix((w[rng], (gr.src[rng], gr.dst[rng])), shape=(gr.n_nodes, gr.n_nodes))

    # Once after a whole round, once after one more net's routes moved the
    # use of a few edges (the weights updated in place).
    for step in range(2):
        kept, built = gr._graph(1.7), fresh(1.7)
        assert np.array_equal(kept.indptr, built.indptr)
        assert np.array_equal(kept.indices, built.indices)
        assert np.array_equal(kept.data, built.data)
        gr._route_net(r.jobs[step], 1.7)


def test_a_negotiation_that_stops_gaining_stops_when_asked():
    r = Router(_crowded_board())
    stats = GlobalRouter(r, tile_mils=40.0).run(r.jobs, iterations=30)
    assert stats["iterations"] == 30 and stats["overflow"] > 0 and not stats.get("stalled")
    r = Router(_crowded_board())
    stats = GlobalRouter(r, tile_mils=40.0).run(r.jobs, iterations=30, stall=True)
    assert stats["stalled"] and stats["iterations"] < 30
    assert stats["iterations"] > GlobalRouter.STALL_WINDOW
