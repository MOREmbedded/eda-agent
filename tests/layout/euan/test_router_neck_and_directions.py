# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Router options for power routing (v0, 2026-10-18).

neck_pads: a net routes at its rule's preferred width and necks down only
at pads narrower than that; before, the narrowest pad set the whole net's
width (a 20 V rail touching one small pad went 8 mil end to end).
layer_dirs / outer: a layer's direction is set by name, and "outer" means
the board's outer copper, not the ends of the routing list.
outer_cost: long runs prefer the inner layers.
"""

from __future__ import annotations

from eda_agent.layout.model import Layer, LayoutBoard, Pad, PadCopper, Rule
from eda_agent.layout.route_euan import Router
from eda_agent.layout.route_euan.search import MOVES


def _rules(pref=40.0, lo=6.0):
    return [Rule("Clearance", "0", "All", "All", 1, True, {"gap": 6.0},
                 "Clearance Constraint (Gap=6mil) (All),(All)"),
            Rule("Width", "1", "All", "All", 1, True, {},
                 f"Width Constraint (Min={lo:g}mil) (Max=60mil) (Preferred={pref:g}mil) (All)")]


def _two_pad_board(pad_w=16.0, layers=None):
    pads = [Pad(c, "1", x, 300, net="P", copper=[PadCopper("TopLayer", "rect", pad_w, pad_w)])
            for c, x in (("R1", 150), ("R2", 1050))]
    layers = layers or [Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)]
    return LayoutBoard(name="n", outline=[(0, 0), (1200, 0), (1200, 600), (0, 600)],
                       layers=layers, pads=pads, rules=_rules())


def test_without_neck_pads_the_smallest_pad_sets_the_width():
    r = Router(_two_pad_board(), pitch=5.0)
    assert r._net_width("P", r.board.pads_by_net()["P"]) == 16.0


def test_neck_pads_routes_at_full_width_and_necks_at_the_pads():
    r = Router(_two_pad_board(), pitch=5.0, neck_pads=True)
    assert r._net_width("P", r.board.pads_by_net()["P"]) == 40.0
    r.report = r.run()
    out = r.apply(pours=False)
    widths = sorted({round(t.width, 3) for t in out.tracks if t.net == "P"})
    assert widths == [16.0, 40.0], widths
    assert r.report.failed == 0


def _four_layer(names):
    return [Layer(n, "signal", i) for i, n in enumerate(names)]


def test_outer_means_the_board_outer_copper_not_the_routing_ends():
    # Bottom excluded: L6 is the last ROUTING layer but an inner one, and
    # keeps a direction; L3 keeps the other.
    b = _two_pad_board(layers=_four_layer(["TopLayer", "MidLayer2", "MidLayer5", "BottomLayer"]))
    r = Router(b, pitch=10.0, exclude_layers=frozenset({"BottomLayer"}),
               layer_dirs={"MidLayer2": "h", "MidLayer5": "v"}, dir_strength=1.0)
    f = r.move_factor
    names = list(r.grid.layers)
    k_h = [k for k, (dx, dy, _) in enumerate(MOVES) if dy == 0][0]
    k_v = [k for k, (dx, dy, _) in enumerate(MOVES) if dx == 0][0]
    l3, l6, top = names.index("MidLayer2"), names.index("MidLayer5"), names.index("TopLayer")
    assert f[l3, k_h] < f[l3, k_v]            # L3 horizontal
    assert f[l6, k_v] < f[l6, k_h]            # L6 vertical, though last in the routing list
    assert f[top, k_h] == f[top, k_v]         # the real outer layer prefers none


def test_outer_cost_makes_the_outer_layers_dearer():
    b = _two_pad_board(layers=_four_layer(["TopLayer", "MidLayer2", "MidLayer5", "BottomLayer"]))
    r = Router(b, pitch=10.0, exclude_layers=frozenset({"BottomLayer"}), outer_cost=2.0)
    cost = dict(zip(r.grid.layers, r.layer_cost))
    assert cost["TopLayer"] == 2.0 and cost["MidLayer2"] == 1.0 and cost["MidLayer5"] == 1.0


def test_a_pad_the_trunk_cannot_reach_gets_a_narrower_branch_and_the_trunk_stays_wide():
    # R3 sits at the bottom of a 50 mil slot between two tall pads of
    # another net: a 40 mil track (plus 6 mil clearance each side) does not
    # fit in it, 20 mil does. The trunk R1-R2 stays 40 mil.
    big = lambda c, x: Pad(c, "1", x, 300, net="P", copper=[PadCopper("TopLayer", "rect", 50, 50)])
    pads = [big("R1", 100), big("R2", 1100),
            Pad("R3", "1", 600, 520, net="P", copper=[PadCopper("TopLayer", "rect", 16, 16)]),
            Pad("X1", "1", 565, 500, net="Q", copper=[PadCopper("TopLayer", "rect", 20, 200)]),
            Pad("X2", "1", 635, 500, net="Q2", copper=[PadCopper("TopLayer", "rect", 20, 200)])]
    b = LayoutBoard(name="b", outline=[(0, 0), (1200, 0), (1200, 700), (0, 700)],
                    layers=[Layer("TopLayer", "signal", 0)], pads=pads, rules=_rules())
    r = Router(b, pitch=5.0, neck_pads=True, nets=frozenset({"P"}))
    r.report = r.run()
    out = r.apply(pours=False)
    widths = {round(t.width, 3) for t in out.tracks if t.net == "P"}
    assert 40.0 in widths and min(widths) <= 20.0, widths
    assert r.report.failed == 0


def test_join_existing_routes_only_what_the_copper_leaves_apart():
    # R1-R2 already joined by a track; R3 is apart. join_existing routes one
    # connection (R3 to that copper), not the whole net again.
    from eda_agent.layout.model import Track
    pads = [Pad(c, "1", x, y, net="P", copper=[PadCopper("TopLayer", "rect", 20, 20)])
            for c, x, y in (("R1", 100, 300), ("R2", 700, 300), ("R3", 400, 500))]
    b = LayoutBoard(name="j", outline=[(0, 0), (800, 0), (800, 600), (0, 600)],
                    layers=[Layer("TopLayer", "signal", 0)], pads=pads, rules=_rules(10.0),
                    tracks=[Track("TopLayer", 100, 300, 700, 300, 10.0, "P")])
    r = Router(b, pitch=5.0, join_existing=True, nets=frozenset({"P"}))
    job = r.jobs[0]
    assert len(job.pre_groups) == 2
    r.report = r.run()
    assert r.report.failed == 0 and len(job.paths) == 1


def test_join_existing_skips_a_net_already_whole():
    from eda_agent.layout.model import Track
    pads = [Pad(c, "1", x, 300, net="P", copper=[PadCopper("TopLayer", "rect", 20, 20)])
            for c, x in (("R1", 100), ("R2", 700))]
    b = LayoutBoard(name="w", outline=[(0, 0), (800, 0), (800, 600), (0, 600)],
                    layers=[Layer("TopLayer", "signal", 0)], pads=pads, rules=_rules(10.0),
                    tracks=[Track("TopLayer", 100, 300, 700, 300, 10.0, "P")])
    r = Router(b, pitch=5.0, join_existing=True, nets=frozenset({"P"}))
    assert not r.jobs


def test_old_pours_are_stripped_when_their_source_differs_from_the_boundary():
    # A live read gives the boundary source "polygon:..." and its copper
    # "region": matched by (layer, source) nothing was stripped.
    from eda_agent.layout.model import Region
    from eda_agent.layout.route.job import _without_old_pours  # ours, by_net as his
    sq = [(0, 0), (100, 0), (100, 100), (0, 100)]
    b = LayoutBoard(name="p", outline=[(0, 0), (200, 0), (200, 200), (0, 200)],
                    layers=[Layer("TopLayer", "signal", 0)], rules=_rules(),
                    regions=[Region("TopLayer", sq, [], "GND", "pour_boundary", source="polygon:1"),
                             Region("TopLayer", sq, [], "GND", "pour", source="region")])
    out, old = _without_old_pours(b, by_net=True)
    assert [r.kind for r in out.regions] == ["pour_boundary"] and old == ["TopLayer:GND"]


def test_join_existing_does_not_join_adjacent_fine_pitch_pins_or_a_pin_beside_an_exposed_pad():
    # v0 owner review 4: pads modelled as circles of max(w,h)/2 made U401
    # pins 24/25 (7.9 x 23.6 at 15.75 pitch) and a pin beside the exposed pad
    # one group, so pin 25 and those pins were never routed. Real copper only.
    pads = [Pad("U1", "24", 300, 300, net="P", copper=[PadCopper("TopLayer", "rect", 7.9, 23.6)]),
            Pad("U1", "25", 315.75, 300, net="P", copper=[PadCopper("TopLayer", "rect", 7.9, 23.6)]),
            Pad("U1", "15", 300, 400, net="G", rotation=90, copper=[PadCopper("TopLayer", "rect", 7.9, 23.6)]),
            Pad("U1", "EP", 400, 400, net="G", copper=[PadCopper("TopLayer", "rect", 150, 150)])]
    b = LayoutBoard(name="f", outline=[(0, 0), (800, 0), (800, 600), (0, 600)],
                    layers=[Layer("TopLayer", "signal", 0)], pads=pads, rules=_rules(6.0))
    r = Router(b, pitch=2.5, join_existing=True, nets=frozenset({"P", "G"}))
    groups = {job.name: len(job.pre_groups) for job in r.jobs}
    assert groups == {"P": 2, "G": 2}, groups
