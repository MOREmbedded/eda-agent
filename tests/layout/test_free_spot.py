# SPDX-License-Identifier: Apache-2.0
"""The free-spot finder keeps clear of every kind of object, not only
courtyards: a via it calls free must not land on a track, an arc, a
fill, a keepout, a polygon cutout, a pour of another net or a hole."""
from __future__ import annotations

import asyncio
import inspect
import math

import pytest

from eda_agent import lengths
from eda_agent.layout import geom
from eda_agent.layout.free_spot import collect_obstacles, find_free_spots, resolve_layers
from eda_agent.layout.model import (
    Arc, Component, Layer, LayoutBoard, Pad, PadCopper, Region, Rule, Track, Via,
)

SQUARE = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]


def _board(**kw) -> LayoutBoard:
    b = LayoutBoard(name="t.PcbDoc", outline=list(SQUARE),
                    layers=[Layer("TopLayer", order=0), Layer("BottomLayer", order=1)])
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _box(cx, cy, half):
    return [(cx - half, cy - half), (cx + half, cy - half),
            (cx + half, cy + half), (cx - half, cy + half)]


def _via_spots(board, near=(500, 500), **kw):
    args = dict(diameter=20, clearance=10, near=near, count=5, grid=5)
    args.update(kw)
    return find_free_spots(board, **args)


def _min_gap(board, x, y, diameter, layers, net=""):
    """Independent check: the gap from a via at (x, y) to every obstacle
    on ``layers``, straight from geom."""
    shape = geom.circle(x, y, diameter)
    return min(geom.clearance(ob.shape, shape)
               for ob in collect_obstacles(board, layers, net=net))


# -- what blocks ------------------------------------------------------------------

def test_a_track_blocks_the_spot_a_courtyard_search_would_call_free():
    board = _board(tracks=[Track("TopLayer", 0, 500, 1000, 500, 10, "SIG")])
    got = _via_spots(board)
    assert got["ok"] and got["near_free"] is False
    assert got["near_blocked_by"][0]["kind"] == "track"
    best = got["candidates"][0]
    # 10 mil track, 20 mil via, 10 mil clearance: 25 mil from its centre line.
    assert abs(best["y"] - 500) >= 25 - 1e-9
    assert abs(best["y"] - 500) <= 30
    assert best["nearby"][0]["kind"] == "track" and best["nearby"][0]["net"] == "SIG"


@pytest.mark.parametrize("region,kind", [
    (Region("KeepOutLayer", _box(500, 500, 40), kind="keepout", keepout=True), "keepout"),
    (Region("TopLayer", _box(500, 500, 40), kind="cutout"), "polygon_cutout"),
    (Region("BottomLayer", _box(500, 500, 40), net="VCC", kind="copper",
            source="fill"), "fill"),
    (Region("TopLayer", _box(500, 500, 40), net="VCC", kind="copper",
            source="region"), "region"),
])
def test_regions_fills_cutouts_and_keepouts_block(region, kind):
    board = _board(regions=[region])
    got = _via_spots(board)
    assert got["near_blocked_by"][0]["kind"] == kind
    for c in got["candidates"]:
        assert _min_gap(board, c["x"], c["y"], 20, ["TopLayer", "BottomLayer"]) >= 10 - 1e-6


def test_a_keepout_track_on_the_keepout_layer_blocks_every_layer():
    board = _board(tracks=[Track("KeepOutLayer", 400, 500, 600, 500, 10, keepout=True)])
    got = _via_spots(board, layers=["bottom"])
    # Centre on the track: 5 mil of track and 10 of via overlap.
    assert got["near_blocked_by"][0] == {"kind": "keepout", "layer": "all", "gap": -15.0}


def test_arcs_pads_holes_and_vias_block():
    board = _board(
        # A ring 15 mil round the point: 1 mil from a 20 mil via there.
        arcs=[Arc("TopLayer", 500, 500, 15, 0, 360, 8, "A")],
        pads=[Pad("J1", "1", 200, 200, copper=[PadCopper("TopLayer", "round", 60, 60),
                                                PadCopper("BottomLayer", "round", 60, 60)],
                  hole=35, net="N1"),
              Pad("", "MH", 800, 800, hole=120, plated=False)],
        vias=[Via(200, 800, 24, 12, "TopLayer", "BottomLayer", "GND")])
    for near, kind in (((500, 500), "arc"), ((200, 200), "pad"),
                       ((800, 800), "hole"), ((200, 800), "via")):
        got = _via_spots(board, near=near)
        assert got["near_free"] is False, kind
        assert kind in {r["kind"] for r in got["near_blocked_by"]}, (kind, got["near_blocked_by"])
        for c in got["candidates"]:
            assert _min_gap(board, c["x"], c["y"], 20, ["TopLayer", "BottomLayer"]) >= 10 - 1e-6


def test_copper_of_the_new_objects_net_does_not_block():
    pour = Region("TopLayer", _box(500, 500, 200), net="GND", kind="pour", source="polygon:GND")
    board = _board(regions=[pour])
    assert _via_spots(board)["near_free"] is False
    assert _via_spots(board, net="GND")["near_free"] is True
    assert _via_spots(board, ignore_pours=True)["near_free"] is True
    blocked = _via_spots(board)["near_blocked_by"][0]
    assert blocked["kind"] == "pour" and blocked["ref"] == "polygon:GND"


def test_a_polygon_cutout_blocks_even_a_same_net_via():
    board = _board(regions=[Region("TopLayer", _box(500, 500, 40), kind="cutout")])
    assert _via_spots(board, net="GND")["near_free"] is False


def test_only_the_chosen_layers_count():
    board = _board(tracks=[Track("BottomLayer", 0, 500, 1000, 500, 10, "SIG")])
    assert _via_spots(board)["near_free"] is False                 # a through via
    assert _via_spots(board, layers=["TopLayer"])["near_free"] is True
    part = find_free_spots(board, width=40, height=20, clearance=10, near=(500, 500))
    assert part["layers"] == ["TopLayer"] and part["near_free"] is True


def test_the_board_edge_and_its_cutouts_keep_their_clearance():
    board = _board(cutouts=[_box(500, 500, 50)])
    corner = _via_spots(board, near=(0, 0), edge_clearance=15)
    assert corner["near_blocked_by"][0]["kind"] == "board_edge"
    best = corner["candidates"][0]
    assert best["x"] >= 25 - 1e-9 and best["y"] >= 25 - 1e-9
    hole = _via_spots(board)
    assert hole["near_free"] is False
    for c in hole["candidates"]:
        assert max(abs(c["x"] - 500), abs(c["y"] - 500)) >= 50 + 20 - 1e-9


def test_a_part_keeps_out_of_courtyards_on_its_side_only():
    top = Component("U1", x=500, y=500, side="top", courtyard=_box(500, 500, 50))
    bottom = Component("U2", x=500, y=500, side="bottom", courtyard=_box(500, 500, 50))
    got = find_free_spots(_board(components=[top]), width=40, height=40,
                          clearance=10, near=(500, 500))
    assert got["near_blocked_by"][0]["kind"] == "courtyard"
    assert got["near_blocked_by"][0]["ref"] == "U1"
    assert find_free_spots(_board(components=[bottom]), width=40, height=40,
                           clearance=10, near=(500, 500))["near_free"] is True
    # A via ignores courtyards unless asked.
    assert _via_spots(_board(components=[top]))["near_free"] is True
    assert _via_spots(_board(components=[top]), courtyards=True)["near_free"] is False


# -- the search ---------------------------------------------------------------------

def test_candidates_come_nearest_first_and_spaced_apart():
    board = _board(tracks=[Track("TopLayer", 0, 500, 1000, 500, 10, "SIG")])
    got = _via_spots(board, count=6)
    d = [c["distance"] for c in got["candidates"]]
    assert d == sorted(d) and len(d) == 6
    pts = [(c["x"], c["y"]) for c in got["candidates"]]
    for i, a in enumerate(pts):
        for b in pts[i + 1:]:
            assert math.dist(a, b) >= got["spacing"] - 1e-9


def test_a_search_box_limits_the_answers():
    got = _via_spots(_board(), near=None, area=(100, 100, 200, 200), count=50)
    assert got["candidates"]
    for c in got["candidates"]:
        assert 100 <= c["x"] <= 200 and 100 <= c["y"] <= 200


def test_no_room_says_so():
    board = _board(regions=[Region("KeepOutLayer", SQUARE, kind="keepout", keepout=True)])
    got = _via_spots(board)
    assert got["candidates"] == [] and "no free spot" in got["reason"]
    assert got["blocked_by"][0]["kind"] in ("keepout", "board_edge")


def test_a_board_full_of_pour_says_how_to_place_into_it():
    pour = Region("TopLayer", SQUARE, net="GND", kind="pour", source="polygon:GND")
    got = _via_spots(_board(regions=[pour]), grid=50)
    assert got["candidates"] == [] and "ignore_pours" in got["reason"]
    assert got["blocked_by"][0]["kind"] == "pour"


def test_bad_input_is_refused():
    assert find_free_spots(_board())["ok"] is False
    assert "no copper layer" in find_free_spots(_board(), diameter=20, layers=["Mid9"])["error"]
    assert resolve_layers(_board(), ["top layer", "BOTTOM"], "top") == ["TopLayer", "BottomLayer"]


# -- units and the tool --------------------------------------------------------------

def test_a_mm_session_gets_every_length_in_mm_and_counts_unchanged():
    board = _board(tracks=[Track("TopLayer", 0, 500, 1000, 500, 10, "SIG")],
                   pads=[Pad("", "MH", 800, 800, hole=120, plated=False)])
    got = _via_spots(board)
    mm = lengths.reply_in("pcb_find_free_spot", got, "mm")
    assert mm["length_units"] == "mm"
    assert mm["clearance"] == pytest.approx(0.254) and mm["edge_clearance"] == pytest.approx(0.254)
    assert mm["diameter"] == pytest.approx(0.508)
    c0, m0 = got["candidates"][0], mm["candidates"][0]
    assert m0["x"] == pytest.approx(c0["x"] * 0.0254)
    assert m0["distance"] == pytest.approx(c0["distance"] * 0.0254)
    assert m0["nearby"][0]["gap"] == pytest.approx(c0["nearby"][0]["gap"] * 0.0254)
    assert mm["near"]["x"] == pytest.approx(12.7)
    assert mm["obstacles"] == got["obstacles"] and mm["checked"] == got["checked"]


def _tool():
    from eda_agent.tools.pcb import register_pcb_tools

    class Capture:
        def __init__(self):
            self.t = {}

        def tool(self, *a, **k):
            def deco(fn):
                self.t[k.get("name") or fn.__name__] = fn
                return fn
            return deco

        def resource(self, *a, **k):
            return lambda f: f

        def prompt(self, *a, **k):
            return lambda f: f

    cap = Capture()
    register_pcb_tools(cap)
    return cap.t["pcb_find_free_spot"]


def test_the_tool_reads_a_saved_board_and_takes_the_board_rule(tmp_path):
    board = _board(tracks=[Track("TopLayer", 0, 500, 1000, 500, 10, "SIG")],
                   rules=[Rule(name="Clearance", kind="clearance", scope1="All", scope2="All",
                               descriptor="Clearance Constraint (Gap=12mil) (All),(All)")])
    path = tmp_path / "board.json"
    board.save(path)
    tool = _tool()
    got = asyncio.run(tool(board_json_path=str(path), diameter=20, x=500, y=500, grid=1))
    assert got["ok"] and got["source"] == "file" and got["clearance_from"] == "board rule"
    assert got["clearance"] == 12.0
    assert abs(got["candidates"][0]["y"] - 500) >= 27 - 1e-9
    assert asyncio.run(tool(diameter=20))["ok"] is False
    assert "x and y" in asyncio.run(tool(board_json_path=str(path), diameter=20, x=1))["error"]


def test_the_tool_is_readonly_and_declares_its_lengths():
    from eda_agent.tools.metadata import interaction_of
    assert interaction_of("pcb_find_free_spot") == "readonly"
    params = inspect.signature(_tool()).parameters
    keys = lengths.plain_lengths("pcb_find_free_spot")
    for name in ("diameter", "width", "height", "clearance", "edge_clearance", "x", "y",
                 "xmin", "ymin", "xmax", "ymax", "grid", "spacing"):
        assert name in params and name in keys, name
