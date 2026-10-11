# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The benchmark harness: what it strips, what it keeps, how it scores."""

from __future__ import annotations

from eda_agent.layout.bench import (connection_lengths, copy_board, detour_stats,
                                    octilinear, route_benchmark, routing_stats,
                                    strip_routing, via_clusters)
from eda_agent.layout.model import (Arc, Layer, LayoutBoard, Pad, PadCopper,
                                    Region, Rule, Track, Via)


def _board() -> LayoutBoard:
    sq = [(0, 0), (400, 0), (400, 100), (0, 100)]
    return LayoutBoard(
        name="bench",
        layers=[Layer("TopLayer", "signal", 0), Layer("InternalPlane1", "plane", 1),
                Layer("BottomLayer", "signal", 2)],
        pads=[Pad("R1", "1", 0, 0, net="A", copper=[PadCopper("TopLayer", "rect", 10, 10)]),
              Pad("R2", "1", 300, 0, net="A", copper=[PadCopper("TopLayer", "rect", 10, 10)])],
        tracks=[Track("TopLayer", 0, 0, 300, 0, 8, "A"),
                Track("TopLayer", -5, -5, 5, -5, 2, "", comp="R1"),
                Track("TopLayer", 0, 50, 100, 50, 5, "", keepout=True)],
        arcs=[Arc("TopLayer", 0, 0, 100, 0, 90, 8, "A")],
        vias=[Via(150, 0, 20, 10, "TopLayer", "BottomLayer", "A"),
              Via(0, 0, 12, 6, "TopLayer", "BottomLayer", "A", comp="R1")],
        regions=[Region("TopLayer", sq, [], "GND", "pour"),
                 Region("TopLayer", sq, [], "GND", "pour_boundary"),
                 Region("TopLayer", sq, [], "VCC", "copper"),
                 Region("TopLayer", [(-30, -30), (-20, -30), (-20, -20)], [], "", "copper",
                        comp="R1"),
                 Region("InternalPlane1", sq, [], "GND", "plane"),
                 Region("TopLayer", sq, [], "", "keepout", keepout=True)],
        rules=[Rule("Clearance", "0", "All", "All", 1, True, {"gap": 6.0},
                    "Clearance Constraint (Gap=6mil) (All),(All)")],
    )


def test_strip_removes_what_a_router_lays_and_keeps_the_design():
    s = strip_routing(_board())
    assert [(t.comp, t.keepout) for t in s.tracks] == [("R1", False), ("", True)]
    assert s.arcs == []
    assert [v.comp for v in s.vias] == ["R1"], "a footprint via is part of the footprint"
    assert sorted((r.kind, r.comp) for r in s.regions) == [
        ("copper", "R1"), ("keepout", ""), ("plane", ""), ("pour_boundary", "")]


def test_strip_leaves_the_original_alone():
    b = _board()
    before = b.to_dict()
    strip_routing(b)
    assert b.to_dict() == before


def test_routing_stats_count_only_free_copper():
    st = routing_stats(_board())
    assert st["vias"] == 1
    # 300 of track and a quarter circle of radius 100.
    assert abs(st["length"] - (300 + 157.1)) < 0.1


def _with_drawings(board: LayoutBoard) -> LayoutBoard:
    """The board with what a finished board also draws as free tracks: a
    mechanical outline, overlay text strokes and a split line on its plane
    layer. None of it is copper a router lays."""
    board.tracks += [Track("Mechanical1", 0, 0, 400, 0, 5),
                     Track("Mechanical1", 400, 0, 400, 100, 5),
                     Track("TopOverlay", 10, 10, 60, 10, 6),
                     Track("InternalPlane1", 200, 0, 200, 100, 10)]
    board.arcs.append(Arc("Mechanical1", 50, 50, 20, 0, 360, 5))
    return board


def test_routing_stats_count_signal_layer_copper_only():
    st = routing_stats(_with_drawings(_board()))
    assert abs(st["length"] - (300 + 157.1)) < 0.1
    assert list(st["length_by_layer"]) == ["TopLayer"]


def test_the_persons_drawings_do_not_count_as_their_routing():
    # The engine lays exactly the person's copper; the person's board also
    # carries drawings. Their lengths must agree.
    def reroute(board):
        board.tracks.append(Track("TopLayer", 0, 0, 300, 0, 8, "A"))
        board.arcs.append(Arc("TopLayer", 0, 0, 100, 0, 90, 8, "A"))
        return board

    r = route_benchmark(_with_drawings(_board()), reroute).as_dict()
    assert r["human"]["length"] == r["engine"]["length"]
    assert r["length_vs_human"] == 1.0


def test_a_benchmark_scores_the_person_the_start_and_the_engine():
    def reroute(board):
        board.tracks.append(Track("TopLayer", 0, 0, 300, 0, 8, "A"))
        return board

    r = route_benchmark(_board(), reroute).as_dict()
    assert r["human"]["completion"] == 1.0
    assert r["start"]["completion"] == 0.0
    assert r["engine"]["completion"] == 1.0
    assert r["engine"]["vias"] == 0 and r["vias_vs_human"] == 0.0


def test_copy_is_deep():
    b = _board()
    c = copy_board(b)
    c.tracks[0].x1 = 999
    assert b.tracks[0].x1 == 0


def _two_layers(pads, tracks=(), vias=()) -> LayoutBoard:
    return LayoutBoard(
        name="detours", outline=[(-500, -500), (1000, -500), (1000, 1000), (-500, 1000)],
        layers=[Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)],
        pads=list(pads), tracks=list(tracks), vias=list(vias),
        rules=[Rule("Clearance", "0", "All", "All", 1, True, {"gap": 6.0},
                    "Clearance Constraint (Gap=6mil) (All),(All)")])


def _sq(ref, x, y, net, layer="TopLayer"):
    return Pad(ref, "1", x, y, net=net, copper=[PadCopper(layer, "rect", 10, 10)])


def test_octilinear_distance():
    assert octilinear(300, 0) == 300
    assert abs(octilinear(300, -100) - (300 + 100 * (2 ** 0.5 - 1))) < 1e-9
    assert abs(octilinear(100, 100) - 100 * 2 ** 0.5) < 1e-9


def test_a_connection_in_straight_and_45_degree_pieces_has_no_detour():
    # 200 straight, then 45 degrees to a pad 300 along and 100 up: exactly
    # the octilinear distance.
    b = _two_layers([_sq("R1", 0, 0, "A"), _sq("R2", 300, 100, "A")],
                    [Track("TopLayer", 0, 0, 200, 0, 8, "A"),
                     Track("TopLayer", 200, 0, 300, 100, 8, "A")])
    (length, d, pieces), = connection_lengths(b)
    assert abs(length - d) < 1e-6 and pieces == 2
    s = detour_stats(b)
    assert s["connections"] == 1 and s["median"] == 1.0 and s["over_1_5"] == 0
    # A track drawn twice is the same copper, not twice the length.
    b.tracks.append(Track("TopLayer", 0, 0, 200, 0, 8, "A"))
    assert abs(connection_lengths(b)[0][0] - d) < 1e-6


def test_a_route_round_three_sides_is_a_detour():
    # Up 200, across 300, down 100: 600 against 341.4.
    b = _two_layers([_sq("R1", 0, 0, "A"), _sq("R2", 300, 100, "A")],
                    [Track("TopLayer", 0, 0, 0, 200, 8, "A"),
                     Track("TopLayer", 0, 200, 300, 200, 8, "A"),
                     Track("TopLayer", 300, 200, 300, 100, 8, "A")])
    s = detour_stats(b)
    assert abs(s["median"] - 600 / octilinear(300, 100)) < 1e-3
    assert (s["over_1_5"], s["over_2"], s["pieces_median"]) == (1, 0, 3)


def test_a_tee_a_via_and_a_pad_joined_away_from_its_centre_are_followed():
    # R3 joins the trunk R1-R2 by a branch ending on the trunk's middle;
    # R4 is reached on the bottom layer through a via; B's track ends at
    # R5's edge, 5 from its centre.
    pads = [_sq("R1", 0, 0, "A"), _sq("R2", 400, 0, "A"), _sq("R3", 200, 150, "A"),
            _sq("R4", 600, 0, "A", "BottomLayer"), _sq("R5", 0, 300, "B"),
            _sq("R6", 100, 300, "B")]
    tracks = [Track("TopLayer", 0, 0, 400, 0, 8, "A"),
              Track("TopLayer", 200, 150, 200, 0, 8, "A"),
              Track("TopLayer", 400, 0, 500, 0, 8, "A"),
              Track("BottomLayer", 500, 0, 600, 0, 8, "A"),
              Track("TopLayer", 5, 300, 100, 300, 8, "B")]
    vias = [Via(500, 0, 20, 10, "TopLayer", "BottomLayer", "A")]
    got = sorted((round(length, 3), round(d, 3), k)
                 for length, d, k in connection_lengths(_two_layers(pads, tracks, vias)))
    tee = round(octilinear(200, 150), 3)
    assert got == [(100.0, 100.0, 1),       # R5-R6: 95 of track, 5 in R5 to its centre
                   (200.0, 200.0, 2),       # R2-R4: 100 on top, the via, 100 below
                   (350.0, tee, 2),         # R1-R3 and R3-R2: 200 of trunk, 150 of branch
                   (350.0, tee, 2)]


def test_pads_a_pour_joins_give_no_ratio():
    b = _two_layers([_sq("R1", 0, 0, "GND"), _sq("R2", 300, 0, "GND")])
    b.regions = [Region("TopLayer", [(-10, -10), (310, -10), (310, 10), (-10, 10)], [], "GND",
                        "pour")]
    assert connection_lengths(b) == [] and detour_stats(b) == {"connections": 0}


def test_vias_closer_than_a_pitch_and_a_half_in_a_chain_of_three_are_a_cluster():
    # Pitch 20 + 6: links under 39. Three in a row 30 apart, one far off,
    # and a pair on its own.
    def via(x, y):
        return Via(x, y, 20, 10, "TopLayer", "BottomLayer", "A")

    vias = [via(0, 0), via(30, 0), via(60, 0), via(300, 300), via(500, 0), via(530, 0)]
    b = _two_layers([], vias=vias)
    assert via_clusters(b) == {"clusters": 1, "vias": 3, "largest": 3}
    b.vias[1] = via(30, 40)            # 50 from either: the chain breaks
    assert via_clusters(b) == {"clusters": 0, "vias": 0, "largest": 0}
