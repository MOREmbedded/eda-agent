# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Fab via rules (v0, 2026-10-18, JLCPCB multilayer): a via in a pad sits at
its centre, vias of one net keep a gap and reuse each other, wide nets get
several vias at a layer change, and tracks leave pads along an axis.
Owner review: vias offset on pad edges, same-net vias touching, one via on a
40 mil trunk, tracks out of pads at odd angles."""

from __future__ import annotations

import math

import numpy as np

from eda_agent.layout.drc import run_drc
from eda_agent.layout.model import Layer, LayoutBoard, Pad, PadCopper, Rule, Via
from eda_agent.layout.route_euan import Router
from eda_agent.layout.route_euan.search import Window


def _rules(pref=6.0):
    return [Rule("Clearance", "0", "All", "All", 1, True, {"gap": 5.0},
                 "Clearance Constraint (Gap=5mil) (All),(All)"),
            Rule("Width", "1", "All", "All", 1, True, {},
                 f"Width Constraint (Min=4mil) (Max=60mil) (Preferred={pref:g}mil) (All)")]


def _pad(c, x, y, net, w=20, h=20, layer="TopLayer"):
    return Pad(c, "1", x, y, net=net, copper=[PadCopper(layer, "rect", w, h)])


def _board(pads, vias=(), pref=6.0, w=1200, h=600):
    return LayoutBoard(name="v", outline=[(0, 0), (w, 0), (w, h), (0, h)],
                       layers=[Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)],
                       pads=list(pads), vias=list(vias), rules=_rules(pref))


def test_centre_mode_registers_a_via_at_the_centre_of_a_pad_it_fits_only():
    b = _board([_pad("R1", 100, 300, "P"), _pad("R2", 700, 300, "P", w=6, h=20)])
    r = Router(b, pitch=5.0, stages={"pad_vias": "centre"}, via_pad_gap=4.0)
    job = r.jobs[0]
    centres = {(round(px), round(py)) for px, py, _ in job.pad_via.values()}
    assert (100, 300) in centres and (700, 300) not in centres


def test_same_net_vias_keep_their_gap_and_an_existing_one_is_reused():
    old = Via(400, 300, 18.0, 10.0, "TopLayer", "BottomLayer", "P")
    b = _board([_pad("R1", 100, 300, "P"), _pad("R2", 700, 300, "P")], vias=[old])
    r = Router(b, pitch=5.0, same_net_via_gap=4.0, reuse_vias=True)
    job = r.jobs[0]
    spec = r.grid.spec
    ci, cj = spec.cell(400, 300)
    win = Window(ci - 10, cj - 10, 21, 21, r.L)
    _, via = r._window_costs(job, win, 1.0)
    X = spec.x(win.i0 + win.cx)
    Y = spec.y(win.j0 + win.cy)
    d = np.hypot(X - 400, Y - 300)
    reuse = (win.j0 + win.cy == cj) & (win.i0 + win.cx == ci)
    assert np.isfinite(via[reuse]).all() and via[reuse].max() < r.via_base * 0.1
    close = (d < 18 + 4.0) & ~reuse & (d > r.pitch * 0.75)
    assert np.isinf(via[close]).all()


def test_a_wide_net_changing_layer_gets_several_vias_and_stays_clean():
    # a wall of another net's top pads across the board: P must go under it
    wall = [_pad(f"W{k}", 600, 30 + 60 * k, "Q", w=20, h=56) for k in range(10)]
    big = lambda c, x: _pad(c, x, 300, "P", 60, 60)
    b = _board([big("R1", 150), big("R2", 1050)] + wall, pref=40.0)
    b.layers = [Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)]
    r = Router(b, pitch=5.0, nets=frozenset({"P"}), power_vias=20.0, same_net_via_gap=4.0)
    r.report = r.run()
    out = r.apply(pours=False)
    vs = [v for v in out.vias if v.net == "P"]
    assert r.report.failed == 0
    assert len(vs) >= 4, len(vs)
    for i, a in enumerate(vs):
        for c in vs[i + 1:]:
            assert math.hypot(a.x - c.x, a.y - c.y) >= (a.diameter + c.diameter) / 2 + 4.0 - 1e-6
    assert not run_drc(out).violations


def test_axis_entry_leaves_along_the_pad_and_turns_at_45_degrees():
    pad = _pad("R1", 0, 0, "P")
    opts = Router._axis_entries(pad, "TopLayer", (0.0, 0.0), (30.0, 10.0))
    first = opts[0]
    assert first[1] == (20.0, 0.0) and first[2] == (30.0, 10.0)


def test_square_off_leaves_no_segment_off_45_degrees():
    # pads off the grid by a fraction of a pitch: the last piece into each pad was slanted
    b = _board([_pad("R1", 101.3, 302.2, "P"), _pad("R2", 698.7, 451.9, "P")])
    r = Router(b, pitch=5.0, pad_entry="axis")
    r.report = r.run()
    out = r.apply(pours=False)
    for t in out.tracks:
        if t.net != "P" or math.hypot(t.x2 - t.x1, t.y2 - t.y1) < 0.05:
            continue
        ang = math.degrees(math.atan2(t.y2 - t.y1, t.x2 - t.x1)) % 45
        assert min(ang, 45 - ang) <= 0.1, (t.x1, t.y1, t.x2, t.y2)
    assert r.report.failed == 0 and not run_drc(out).violations


def test_a_track_leaves_an_elongated_pad_centred_through_its_short_edge():
    # pads off the grid: the route used to run in offset and cut into the centre at 45 degrees
    pads = [_pad("U1", 100.7, 301.3, "P", w=30, h=10), _pad("U2", 700.3, 418.6, "P", w=30, h=10)]
    b = _board(pads)
    r = Router(b, pitch=5.0, pad_entry="axis")
    r.report = r.run()
    out = r.apply(pours=False)
    tr = [t for t in out.tracks if t.net == "P"]
    for px, py in ((100.7, 301.3), (700.3, 418.6)):
        legs = [t for t in tr if (abs(t.x1 - px) < 1e-6 and abs(t.y1 - py) < 1e-6)
                or (abs(t.x2 - px) < 1e-6 and abs(t.y2 - py) < 1e-6)]
        assert legs, (px, py)
        for t in legs:
            far = (t.x2, t.y2) if abs(t.x1 - px) < 1e-6 and abs(t.y1 - py) < 1e-6 else (t.x1, t.y1)
            assert abs(far[1] - py) < 1e-6, "leaves along the long (x) axis, centred"
            assert abs(far[0] - px) >= 15 + 2 - 1e-6, "straight until clear of the pad"
    assert r.report.failed == 0 and not run_drc(out).violations


def test_a_route_leaves_an_ic_pin_through_its_short_end_not_its_side():
    # IC pin 1 (long axis x); the cap sits right above it: without the side
    # keep-out the route left pin 1 straight up through its long (top) edge
    ic = [Pad("U1", str(k + 1), 300, 300 - 50 * k, net=n, copper=[PadCopper("TopLayer", "rect", 60, 18)])
          for k, n in enumerate(("P", "Q1", "Q2", "Q3"))]
    cap = [_pad("C1", 300, 345, "P", 20, 22), _pad("C1", 300, 385, "GNDX", 20, 22)]
    b = _board(ic + cap)
    r = Router(b, pitch=5.0, pad_entry="axis", nets=frozenset({"P"}))
    r.report = r.run()
    out = r.apply(pours=False)
    legs = [t for t in out.tracks if t.net == "P" and ((abs(t.x1 - 300) < 1e-6 and abs(t.y1 - 300) < 1e-6)
                                                      or (abs(t.x2 - 300) < 1e-6 and abs(t.y2 - 300) < 1e-6))]
    assert legs
    for t in legs:
        assert abs(t.y1 - t.y2) < 1e-6 and max(abs(t.x1 - 300), abs(t.x2 - 300)) >= 30 + 2, (t.x1, t.y1, t.x2, t.y2)
    assert r.report.failed == 0 and not run_drc(out).violations


def test_the_side_keep_out_bars_the_long_sides_of_an_ic_pin_and_not_its_ends():
    ic = [Pad("U1", str(k + 1), 300, 300 - 50 * k, net=n, copper=[PadCopper("TopLayer", "rect", 60, 18)])
          for k, n in enumerate(("P", "Q1", "Q2", "Q3"))]
    b = _board(ic + [_pad("C1", 600, 300, "P")])
    r = Router(b, pitch=5.0, pad_entry="axis", nets=frozenset({"P"}))
    job = r.jobs[0]
    assert job.side_bar is not None
    barred = {(int(y), int(x)) for _, y, x in job.side_bar}
    spec = r.grid.spec
    side = spec.cell(300, 300 + 9 + 4)     # just above the long edge
    end = spec.cell(300 + 30 + 6, 300)     # just past the short end
    assert (side[1], side[0]) in barred
    assert (end[1], end[0]) not in barred


def test_chamfer_cuts_a_square_corner_to_45_degrees():
    from eda_agent.layout.model import Track
    b = _board([_pad("R1", 100, 300, "P"), _pad("R2", 300, 500, "P")])
    r = Router(b, pitch=5.0, pad_entry="axis")
    r.report = r.run()
    out = r.apply(pours=False)
    first = len(b.tracks)
    out.tracks[first:] = [Track("TopLayer", 100, 300, 300, 300, 6.0, "P"),
                          Track("TopLayer", 300, 300, 300, 500, 6.0, "P")]
    r._chamfer_corners(out, first)
    segs = out.tracks[first:]
    assert any(abs(abs(t.x2 - t.x1) - abs(t.y2 - t.y1)) < 1e-6 and abs(t.x2 - t.x1) > 1 for t in segs)
    assert not any((abs(t.x1 - 300) < 1e-6 and abs(t.y1 - 300) < 1e-6) or
                   (abs(t.x2 - 300) < 1e-6 and abs(t.y2 - 300) < 1e-6) for t in segs)
    ends = {(round(t.x1, 3), round(t.y1, 3)) for t in segs} | {(round(t.x2, 3), round(t.y2, 3)) for t in segs}
    assert (100, 300) in ends and (300, 500) in ends
