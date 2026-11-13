# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Gloss (v0 owner review, 2026-10-18): lines that turned back on themselves
past a pin, or hugged a pad's long side to reach a via past its end, are
re-laid as short 45/90 runs; entries stay straight and centred; other nets'
copper keeps its clearance."""

from __future__ import annotations

import math

from eda_agent.layout import geom
from eda_agent.layout.model import Component, Layer, LayoutBoard, Pad, PadCopper, Track, Via
from eda_agent.layout.route_euan.gloss import _turns, gloss_board


def _pad(c, n, x, y, net, w, h, rot=0.0):
    return Pad(c, n, x, y, net=net, rotation=rot, copper=[PadCopper("TopLayer", "rect", w, h)])


def _board(pads, tracks, vias=(), comps=()):
    return LayoutBoard(name="g", outline=[(0, 0), (2000, 0), (2000, 2000), (0, 2000)],
                       layers=[Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)],
                       pads=list(pads), tracks=list(tracks), vias=list(vias), components=list(comps))


def _chain(b, net):
    """The net's track points in order from the first pad centre."""
    ts = [t for t in b.tracks if t.net == net]
    return ts


def _max_turn(b, net):
    ends = {}
    for t in b.tracks:
        if t.net != net:
            continue
        for p, q in (((t.x1, t.y1), (t.x2, t.y2)), ((t.x2, t.y2), (t.x1, t.y1))):
            ends.setdefault((round(p[0], 2), round(p[1], 2)), []).append(q)
    worst = 0.0
    for p, qs in ends.items():
        if len(qs) == 2:
            u = (qs[0][0] - p[0], qs[0][1] - p[1]); v = (qs[1][0] - p[0], qs[1][1] - p[1])
            lu, lv = math.hypot(*u), math.hypot(*v)
            ang = math.degrees(math.acos(max(-1, min(1, (u[0] * v[0] + u[1] * v[1]) / (lu * lv)))))
            worst = max(worst, 180.0 - ang)
    return worst


def _len(b, net):
    return sum(math.hypot(t.x2 - t.x1, t.y2 - t.y1) for t in b.tracks if t.net == net)


def test_a_spike_past_a_pin_is_re_laid_without_turning_back():
    # U824 pin 3 on v0: out of the pin, down, right past the corner, then
    # back at 45 degrees down-left to the resistor (a 135 degree turn)
    pads = [_pad("U1", "3", 1048.8, 1062.6, "N", 43.3, 23.6), _pad("U1", "4", 1100, 1062.6, "X", 43.3, 23.6),
            _pad("U1", "5", 1100, 1100, "Y", 43.3, 23.6),
            _pad("R1", "2", 996.7, 999.0, "N", 17.7, 19.7), _pad("R1", "1", 996.7, 950.0, "Z", 17.7, 19.7)]
    pts = [(1048.8, 1062.6), (1022.2, 1062.6), (1022.2, 1037.3), (1026.5, 1033.0), (1030.7, 1033.0),
           (1012.9, 1015.2), (998.0, 1015.2), (996.7, 1013.8), (996.7, 999.0)]
    tr = [Track("TopLayer", a[0], a[1], c[0], c[1], 6.0, "N") for a, c in zip(pts, pts[1:])]
    b = _board(pads, tr, comps=[Component("U1", x=1120, y=1080), Component("R1", x=996.7, y=975)])
    assert _max_turn(b, "N") > 90
    L0 = _len(b, "N")
    r = gloss_board(b, clearance=5.0)
    assert r.chains_changed == 1
    assert _max_turn(b, "N") <= 90.5
    assert _len(b, "N") < L0
    # the pin is still left straight along its long axis (x), away from its part
    first = [t for t in b.tracks if t.net == "N" and (round(t.x1, 1), round(t.y1, 1)) == (1048.8, 1062.6)
             or t.net == "N" and (round(t.x2, 1), round(t.y2, 1)) == (1048.8, 1062.6)]
    assert len(first) == 1
    t = first[0]
    assert abs(t.y1 - t.y2) < 1e-6 and min(t.x1, t.x2) < 1048.8 - 21.65


def test_a_line_hugging_a_pads_long_side_leaves_through_the_end_instead():
    # R883 pad 2 on v0: out of the long right side, up along the edge, back
    # to a via 16 mil past the pad's end
    pads = [_pad("R1", "2", 977.1, 899.0, "N", 63.0, 129.9), _pad("R1", "1", 977.1, 700.0, "M", 63.0, 129.9)]
    pts = [(977.1, 899.0), (1013.6, 899.0), (1013.6, 972.7), (1006.1, 972.7), (1002.5, 976.3), (1002.5, 980.0)]
    tr = [Track("TopLayer", a[0], a[1], c[0], c[1], 6.0, "N") for a, c in zip(pts, pts[1:])]
    via = Via(1002.5, 980.0, 18.0, 10.0, "TopLayer", "BottomLayer", "N")
    b = _board(pads, tr, vias=[via])
    L0 = _len(b, "N")
    gloss_board(b, clearance=5.0)
    assert _len(b, "N") < L0 - 10
    # straight up through the pad's top edge (here in line with the via), nothing along its side
    top = 899.0 + 129.9 / 2
    assert any(abs(t.x1 - t.x2) < 1e-6 and 945.6 < t.x1 < 1008.6 and min(t.y1, t.y2) < top < max(t.y1, t.y2)
               for t in b.tracks if t.net == "N")
    assert not any(t.net == "N" and min(t.x1, t.x2) > 1008.6 for t in b.tracks)
    assert _max_turn(b, "N") <= 90.5


def test_the_shortcut_keeps_clear_of_another_nets_copper():
    # a detour around another net's pad: the straight shortcut would cross it
    pads = [_pad("R1", "1", 500, 500, "N", 20, 20), _pad("R2", "1", 700, 500, "N", 20, 20),
            _pad("R1", "2", 500, 450, "A", 20, 20), _pad("R2", "2", 700, 450, "B", 20, 20),
            _pad("Q1", "1", 600, 500, "O", 40, 40), _pad("Q1", "2", 600, 650, "P", 40, 40),
            _pad("Q1", "3", 600, 350, "S", 40, 40)]
    pts = [(500, 500), (520, 500), (540, 520), (540, 560), (560, 580), (640, 580), (660, 560), (660, 520),
           (680, 500), (700, 500)]
    tr = [Track("TopLayer", a[0], a[1], c[0], c[1], 6.0, "N") for a, c in zip(pts, pts[1:])]
    b = _board(pads, tr)
    gloss_board(b, clearance=5.0)
    q = next(p for p in b.pads if p.net == "O")
    for t in b.tracks:
        if t.net == "N":
            assert geom.clearance(geom.capsule(t.x1, t.y1, t.x2, t.y2, t.width), q.shape_on("TopLayer")) >= 5.0 - 1e-3
    assert _max_turn(b, "N") <= 90.5


def test_a_clean_line_is_left_alone():
    pads = [_pad("R1", "1", 500, 500, "N", 20, 20), _pad("R2", "1", 700, 600, "N", 20, 20),
            _pad("R1", "2", 500, 450, "A", 20, 20), _pad("R2", "2", 700, 650, "B", 20, 20)]
    # straight out of one pad, one 45, straight into the other
    pts = [(500, 500), (580, 500), (680, 600), (700, 600)]
    tr = [Track("TopLayer", a[0], a[1], c[0], c[1], 6.0, "N") for a, c in zip(pts, pts[1:])]
    b = _board(pads, tr)
    r = gloss_board(b, clearance=5.0)
    assert r.chains_changed == 0 and not r.added


def test_a_run_keeps_every_join_it_had_mid_segment():
    # v0 dry run: a pad (and a track teed on) joined only by lying across the
    # middle of a segment came apart when the segment was re-laid shorter
    pads = [_pad("R1", "1", 500, 500, "N", 20, 20), _pad("R2", "1", 700, 500, "N", 20, 20),
            _pad("R1", "2", 450, 500, "A", 20, 20), _pad("R2", "2", 750, 500, "B", 20, 20),
            _pad("TP1", "1", 600, 400, "N", 30, 30)]
    # down past the test point and back up: straight across would be shorter, and miss it
    pts = [(500, 500), (520, 500), (540, 480), (540, 420), (560, 400), (640, 400), (660, 420), (660, 480),
           (680, 500), (700, 500)]
    tr = [Track("TopLayer", a[0], a[1], c[0], c[1], 6.0, "N") for a, c in zip(pts, pts[1:])]
    b = _board(pads, tr)
    gloss_board(b, clearance=5.0)
    tp = next(p for p in b.pads if p.comp == "TP1")
    assert any(geom.clearance(geom.capsule(t.x1, t.y1, t.x2, t.y2, t.width), tp.shape_on("TopLayer")) <= 0.01
               for t in b.tracks if t.net == "N")


def test_a_track_teed_onto_the_middle_keeps_its_end_on_the_run():
    # v0 dry run, NetC872_1 on L6: a run between two vias with a branch teed
    # onto its middle; re-laid on the other side of the 45 it crossed the
    # branch 13 mil along and left the branch's end hanging (an antenna)
    vias = [Via(257.0, 713.3, 12.0, 6.0, "TopLayer", "BottomLayer", "N"),
            Via(270.87, 802.0, 12.0, 6.0, "TopLayer", "BottomLayer", "N"),
            Via(300.0, 740.0, 18.0, 10.0, "TopLayer", "BottomLayer", "N")]
    pts = [(270.87, 802.0), (269.5, 802.0), (257.5, 790.0), (257.5, 713.8), (257.0, 713.3)]      # walked from the far via, as on v0
    tr = [Track("MidLayer5", a[0], a[1], c[0], c[1], 6.0, "N") for a, c in zip(pts, pts[1:])]
    tr += [Track("MidLayer5", 257.5, 752.5, 287.5, 752.5, 6.0, "N"), Track("MidLayer5", 287.5, 752.5, 300.0, 740.0, 6.0, "N")]
    b = LayoutBoard(name="g", outline=[(0, 0), (2000, 0), (2000, 2000), (0, 2000)],
                    layers=[Layer("TopLayer", "signal", 0), Layer("MidLayer5", "signal", 1), Layer("BottomLayer", "signal", 2)],
                    pads=[], tracks=tr, vias=vias)
    gloss_board(b, clearance=5.0)
    run = [t for t in b.tracks if t.net == "N" and not (t.y1 == 752.5 and t.y2 == 752.5) and t.x1 < 290 and t.x2 < 290]
    assert any(geom.clearance(geom.circle(257.5, 752.5, 0.0), geom.capsule(t.x1, t.y1, t.x2, t.y2, t.width)) <= 0.01
               for t in run)


def test_two_runs_sharing_a_stub_out_of_a_pads_side_are_re_laid_from_the_pad():
    # R883 pad 2 on v0: one stub out of the long right side feeds a run up
    # along the pad's edge to a via past its top, and a run down-right
    pads = [_pad("R1", "2", 977.1, 899.0, "N", 63.0, 129.9), _pad("R1", "1", 977.1, 700.0, "M", 63.0, 129.9),
            _pad("C1", "1", 1104.9, 723.0, "N", 30.7, 35.4), _pad("C1", "2", 1104.9, 650.0, "Q", 30.7, 35.4)]
    up = [(1013.6, 899.0), (1013.6, 972.7), (1006.1, 972.7), (1002.5, 976.3), (1002.5, 980.0)]
    down = [(1013.6, 899.0), (1104.9, 807.7), (1104.9, 723.0)]
    tr = [Track("TopLayer", 977.1, 899.0, 1013.6, 899.0, 6.0, "N")]
    tr += [Track("TopLayer", a[0], a[1], c[0], c[1], 6.0, "N") for a, c in zip(up, up[1:])]
    tr += [Track("TopLayer", a[0], a[1], c[0], c[1], 6.0, "N") for a, c in zip(down, down[1:])]
    b = _board(pads, tr, vias=[Via(1002.5, 980.0, 18.0, 10.0, "TopLayer", "BottomLayer", "N")])
    gloss_board(b, clearance=5.0)
    # the run to the via now leaves through the pad's top, centred
    at_c = [t for t in b.tracks if t.net == "N" and (977.1, 899.0) in ((round(t.x1, 1), round(t.y1, 1)), (round(t.x2, 1), round(t.y2, 1)))]
    assert any(abs(t.x1 - t.x2) < 1e-6 and max(t.y1, t.y2) > 899.0 + 64.95 for t in at_c)
    assert _max_turn(b, "N") <= 90.5
    # still one piece: both pads and the via
    items = [geom.capsule(t.x1, t.y1, t.x2, t.y2, t.width) for t in b.tracks if t.net == "N"]
    items += [p.shape_on("TopLayer") for p in b.pads if p.net == "N"] + [geom.circle(1002.5, 980.0, 18.0)]
    par = list(range(len(items)))
    def f(i):
        while par[i] != i:
            i = par[i]
        return i
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if geom.clearance(items[i], items[j]) <= 0.01:
                par[f(i)] = f(j)
    assert len({f(i) for i in range(len(items))}) == 1


def test_the_router_glosses_its_own_lines_and_stays_drc_clean():
    from eda_agent.layout.drc import run_drc
    from eda_agent.layout.model import Rule
    from eda_agent.layout.route_euan import Router
    rules = [Rule("Clearance", "0", "All", "All", 1, True, {"gap": 5.0}, "Clearance Constraint (Gap=5mil) (All),(All)"),
             Rule("Width", "1", "All", "All", 1, True, {}, "Width Constraint (Min=4mil) (Max=60mil) (Preferred=6mil) (All)")]
    pads = [_pad("R1", "1", 100, 300, "N", 20, 20), _pad("R1", "2", 100, 250, "A", 20, 20),
            _pad("R2", "1", 500, 420, "N", 20, 20), _pad("R2", "2", 500, 470, "B", 20, 20),
            _pad("Q1", "1", 300, 340, "O", 60, 60), _pad("Q1", "2", 300, 200, "P", 60, 60)]
    b = LayoutBoard(name="r", outline=[(0, 0), (700, 0), (700, 600), (0, 600)],
                    layers=[Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)],
                    pads=pads, rules=rules)
    r = Router(b, pitch=5.0, nets=frozenset({"N"}), exclude_layers=frozenset({"BottomLayer"}), pad_entry="axis", gloss=True)
    r.report = r.run()
    out = r.apply(pours=False)
    assert r.report.failed == 0
    assert _max_turn(out, "N") <= 90.5
    d = run_drc(out, limit=1000)
    assert not [v for v in d.violations if "N" in (v.a + v.b) and "clearance" in str(v).lower()]


def test_a_run_started_on_a_pads_edge_is_re_laid_from_the_pad():
    # v0 review: a track starting on the bottom edge of a big pad ran along
    # that edge for 100 mil before kinking away (C888 / C887 on v0)
    pads = [_pad("C1", "1", 600, 500, "N", 160, 80), _pad("C1", "2", 600, 380, "Q", 160, 80),
            _pad("R1", "1", 900, 640, "N", 20, 20), _pad("R1", "2", 950, 640, "Z", 20, 20)]
    pts = [(530, 462), (660, 462), (680, 482), (680, 540), (780, 640), (900, 640)]   # from the pad's bottom-left corner
    tr = [Track("TopLayer", a[0], a[1], c[0], c[1], 6.0, "N") for a, c in zip(pts, pts[1:])]
    b = _board(pads, tr)
    gloss_board(b, clearance=5.0)
    # nothing runs along the pad's bottom edge any more
    assert not any(t.net == "N" and abs(t.y1 - 462) < 1 and abs(t.y2 - 462) < 1 for t in b.tracks)
    assert _max_turn(b, "N") <= 90.5
    # and the run leaves the pad straight out of an edge, not slantwise from its corner
    sh = next(p for p in b.pads if p.comp == "C1" and p.net == "N").shape_on("TopLayer")
    leaving = [t for t in b.tracks if t.net == "N" and (geom.clearance(geom.circle(t.x1, t.y1, 0.0), sh) <= 0.0)
               != (geom.clearance(geom.circle(t.x2, t.y2, 0.0), sh) <= 0.0)]
    assert leaving and all(abs(t.x1 - t.x2) < 1e-6 or abs(t.y1 - t.y2) < 1e-6 for t in leaving)


def test_a_wide_track_on_one_pin_that_joins_the_next_pin_stays():
    # v0 dry run: a 40 mil track with both ends on eFuse pin 1 also covered
    # pin 2 and was its only join; dropping it as "pad copper" split the net
    pads = [_pad("U1", "1", 500, 500, "N", 12, 30), _pad("U1", "2", 520, 500, "N", 12, 30),
            _pad("U1", "3", 540, 500, "X", 12, 30), _pad("R1", "1", 500, 300, "N", 20, 20), _pad("R1", "2", 450, 300, "Y", 20, 20)]
    tr = [Track("TopLayer", 500, 495, 500, 505, 40.0, "N"),                      # on pin 1, covers pin 2 as well
          Track("TopLayer", 500, 500, 500, 300, 10.0, "N")]
    b = _board(pads, tr)
    gloss_board(b, clearance=0.5)
    p2 = next(p for p in b.pads if p.name == "2" and p.comp == "U1")
    assert any(geom.clearance(geom.capsule(t.x1, t.y1, t.x2, t.y2, t.width), p2.shape_on("TopLayer")) <= 0.01
               for t in b.tracks if t.net == "N")
