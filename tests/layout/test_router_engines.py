# SPDX-License-Identifier: Apache-2.0
"""Both routing engines through the routing job, on the boards where one
of them went wrong: a via on copper the grid does not see (a short), or
too near a hole, or leaving a solder mask sliver, whether the search, a
BGA's fan-out or the plane fan-out laid it; a gloss that ran through a
keep-out or off the board; and a board track the job lost.

Euan's router failed the via cases until the default router's via guards
were ported into it, and the gloss cases with its own gloss, which the
job no longer uses (ours runs for both engines). The default router's
fan-outs skipped the hidden copper and hole checks its search makes, and
its gloss missed keep-outs drawn on the Keep-Out layer.
"""
from __future__ import annotations

import pytest

from eda_agent.design import live
from eda_agent.layout import geom
from eda_agent.layout.drc import hole_gaps, run_drc, verdict
from eda_agent.layout.model import Pad, PadCopper, Region, Rule, Track, Via
from eda_agent.layout.route.job import route_job
from tests.layout import test_route_join_existing as join
from tests.layout import test_route_mask_webs as webs
from tests.layout.test_route import _board, _pad

ENGINES = ("default", "euan")


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(live, "publish_safe", lambda *a, **k: None)


def _run(engine, monkeypatch, board, **params):
    monkeypatch.setenv("EDA_AGENT_ROUTER", engine)
    res = route_job({"board": board, **params})
    assert res["router"] == engine
    return res


def _written(board, res):
    """The board as Altium ends up with it: what it had plus what was sent."""
    out = board
    out.tracks = list(board.tracks) + [
        Track(t["layer"], t["x1"], t["y1"], t["x2"], t["y2"], t["width"], t["net_name"])
        for t in res["tracks"]]
    out.vias = list(board.vias) + [
        Via(v["x"], v["y"], v["size"], v["hole_size"], v["low_layer"], v["high_layer"], v["net"])
        for v in res["vias"]]
    return out


def _copper(res):
    """Each sent track as the copper it lays down, pen width included."""
    return [geom.capsule(t["x1"], t["y1"], t["x2"], t["y2"], t["width"]) for t in res["tracks"]]


@pytest.mark.parametrize("engine", ENGINES)
def test_a_via_clears_copper_on_an_excluded_layer(engine, monkeypatch):
    b = join._through(150, 550)
    # MidLayer1, not routed on, is another net's copper but for a window.
    b.regions = [Region("MidLayer1", [(20, 20), (680, 20), (680, 480), (20, 480)],
                        holes=[[(320, 200), (380, 200), (380, 300), (320, 300)]], net="Z")]
    res = _run(engine, monkeypatch, b, stages={"exclude_layers": ["MidLayer1"]})
    assert res["summary"]["completion"] == 1.0
    assert res["summary"]["drc_status"] == "pass", res["violations"][:2]
    assert res["vias"] and all(320 < v["x"] < 380 for v in res["vias"])


@pytest.mark.parametrize("engine", ENGINES)
def test_a_via_clears_another_nets_tracks_on_a_plane_layer(engine, monkeypatch):
    b = join._through(150, 550)
    # MidLayer1 is a GND pour edge to edge (a plane to both engines) that
    # also carries net Z's tracks between A's pads.
    b.regions = [Region("MidLayer1", [(-5, -5), (705, -5), (705, 505), (-5, 505)], [],
                        "GND", "pour_boundary")]
    b.tracks = [Track("MidLayer1", x, 0, x, 500, 10.0, "Z") for x in range(170, 560, 60)]
    b.pads += [_pad("C1", "1", 60, 60, "GND"), _pad("C2", "1", 640, 440, "GND")]
    res = _run(engine, monkeypatch, b)
    assert "MidLayer1:GND" in res["summary"]["planes"]
    on_plane = [x for x in res["violations"] if x["layer"] == "MidLayer1"]
    assert not on_plane, on_plane[:2]
    assert res["summary"]["drc_status"] == "pass", res["violations"][:2]


@pytest.mark.parametrize("engine", ENGINES)
def test_a_new_via_keeps_the_hole_to_hole_gap(engine, monkeypatch):
    def board():
        b = join._through(150, 550)
        b.rules.append(Rule("HoleToHole", "9", "All", "All", 1, True, {},
                            "Hole To Hole Clearance (Allow Stacked Micro Vias=No) (Gap=40mil) "
                            "(All),(All)"))
        # Another net's vias in a row 30 mil off the straight path: the copper
        # clearance allows a via on the path, the hole gap allows none there.
        b.vias = [Via(x, 280, 16, 8, "TopLayer", "BottomLayer", "Z")
                  for x in range(110, 600, 60)]
        return b
    assert not hole_gaps(board())
    res = _run(engine, monkeypatch, board())
    assert res["summary"]["completion"] == 1.0
    assert res["vias"], "the board needs a via, or the test proves nothing"
    assert not hole_gaps(_written(board(), res))


@pytest.mark.parametrize("engine", ENGINES)
def test_a_new_via_leaves_no_solder_mask_sliver(engine, monkeypatch):
    res = _run(engine, monkeypatch, webs._board())
    assert res["summary"]["completion"] == 1.0
    assert res["summary"]["mask_slivers"] == 0
    assert res["summary"]["drc_status"] == "pass", res["violations"][:2]


# -- vias laid outside the search: a BGA's fan-out and the plane fan-out ---

MASK_RULES = [Rule("Sliver", "mask_sliver", "All", "All", 1, True, {},
                   "Minimum Solder Mask Sliver (Gap=10mil) (All),(All)"),
              Rule("Mask", "soldermask", "All", "", 1, True, {},
                   "Solder Mask Expansion (Expansion =4mil) (All)")]
PITCH, BALL = 39.37, 16.0


def _bga():
    """A 6 x 6 BGA whose middle four balls carry nets to the corners on the
    bottom layer. Two rings of balls get out on the top layer; the middle
    four each need the fan-out's via, a dog-bone on the diagonal by
    preference, in the ball where that does not fit."""
    b = _board(w=600, h=600, layers=("TopLayer", "MidLayer1", "MidLayer2", "BottomLayer"))
    x0 = 300 - PITCH * 2.5
    corner = {(-1, -1): (60, 60), (1, -1): (540, 60), (-1, 1): (60, 540), (1, 1): (540, 540)}
    for i in range(6):
        for j in range(6):
            x, y = x0 + i * PITCH, x0 + j * PITCH
            net = f"B{i}{j}" if min(i, j, 5 - i, 5 - j) == 2 else ""
            b.pads.append(Pad("U1", f"{i}{j}", x, y, net=net,
                              copper=[PadCopper("TopLayer", "round", BALL, BALL)]))
            if net:
                cx, cy = corner[(1 if x > 300 else -1, 1 if y > 300 else -1)]
                b.pads.append(_pad("R" + net, "1", cx, cy, net, layer="BottomLayer"))
    return b


def _middle_balls(b):
    """Each middle ball, with the dog-bone spot the fan-out tries first."""
    for p in b.pads:
        if p.comp == "U1" and p.net:
            sx, sy = (1 if p.x > 300 else -1), (1 if p.y > 300 else -1)
            yield p, (p.x + sx * PITCH / 2, p.y + sy * PITCH / 2), sx


def _in_the_balls(b, res):
    balls = {(round(p.x, 2), round(p.y, 2)) for p, _spot, _sx in _middle_balls(b)}
    return sorted((round(v["x"], 2), round(v["y"], 2)) in balls for v in res["vias"])


@pytest.mark.parametrize("engine", ENGINES)
def test_the_bga_fanout_keeps_its_vias_off_hidden_copper(engine, monkeypatch):
    def board():
        b = _bga()
        # MidLayer1, not routed on, is net Z's copper under the whole BGA
        # but for a window round each middle ball: the dog-bone spots are
        # on Z, so each via must go in its ball.
        windows = [[(p.x - 17, p.y - 17), (p.x + 17, p.y - 17), (p.x + 17, p.y + 17),
                    (p.x - 17, p.y + 17)] for p, _spot, _sx in _middle_balls(b)]
        b.regions = [Region("MidLayer1", [(150, 150), (450, 150), (450, 450), (150, 450)],
                            holes=windows, net="Z")]
        return b
    res = _run(engine, monkeypatch, board(), stages={"exclude_layers": ["MidLayer1"]})
    assert res["summary"]["completion"] == 1.0
    assert _in_the_balls(board(), res) == [True] * 4
    assert not verdict(_written(board(), res))["violations"]


@pytest.mark.parametrize("engine", ENGINES)
def test_the_bga_fanout_keeps_the_hole_to_hole_gap(engine, monkeypatch):
    def board():
        b = _bga()
        b.rules.append(Rule("HoleToHole", "9", "All", "All", 1, True, {},
                            "Hole To Hole Clearance (Allow Stacked Micro Vias=No) (Gap=25mil) "
                            "(All),(All)"))
        # Net Z's via 31 mil out from each dog-bone spot: the copper
        # clearance allows the dog-bone, the 25 mil hole gap does not.
        b.vias = [Via(dx + sx * 31, dy, 16, 8, "TopLayer", "BottomLayer", "Z")
                  for _p, (dx, dy), sx in _middle_balls(b)]
        return b
    assert not verdict(board())["violations"]
    res = _run(engine, monkeypatch, board())
    assert res["summary"]["completion"] == 1.0
    assert _in_the_balls(board(), res) == [True] * 4
    assert not verdict(_written(board(), res))["violations"]


@pytest.mark.parametrize("engine", ENGINES)
def test_the_bga_fanout_leaves_no_mask_sliver(engine, monkeypatch):
    def board():
        b = _bga()
        b.rules += MASK_RULES
        b.meta["mask_read"] = True
        return b
    plain = _run(engine, monkeypatch, _bga())
    assert _in_the_balls(_bga(), plain) == [False] * 4     # dog-bones, without the mask
    res = _run(engine, monkeypatch, board())
    assert res["summary"]["completion"] == 1.0
    # A dog-bone's opening comes 2 mil from four balls' openings.
    assert _in_the_balls(board(), res) == [True] * 4
    assert res["summary"]["mask_slivers"] == 0


def _caps(extra=()):
    """Two capacitors with a GND pad each over a GND plane on MidLayer2,
    their other pads joined on the top. The plane fan-out drops a dog-bone
    via from each GND pad, straight out of the part by preference: at
    (200, 276) and (400, 276) on an empty board."""
    b = _board(w=600, h=500, layers=("TopLayer", "MidLayer1", "MidLayer2", "BottomLayer"))
    b.regions = [Region("MidLayer2", [(-5, -5), (605, -5), (605, 505), (-5, 505)], [],
                        "GND", "pour_boundary")]
    b.pads = [_pad("C1", "1", 200, 250, "GND"), _pad("C1", "2", 200, 210, "N"),
              _pad("C2", "1", 400, 250, "GND"), _pad("C2", "2", 400, 210, "N"), *extra]
    return b


FANOUT = {"stages": {"plane_fanout": True}}


@pytest.mark.parametrize("engine", ENGINES)
def test_the_plane_fanout_keeps_its_vias_off_other_nets_tracks_on_the_plane(engine, monkeypatch):
    def board():
        b = _caps()
        # Net Z's tracks on the plane layer, across both dog-bone spots.
        b.tracks = [Track("MidLayer2", 150, 278, 250, 278, 10.0, "Z"),
                    Track("MidLayer2", 350, 278, 450, 278, 10.0, "Z")]
        return b
    assert sorted((v["x"], v["y"]) for v in _run(engine, monkeypatch, _caps(), **FANOUT)["vias"]) \
        == [(200.0, 276.0), (400.0, 276.0)]
    res = _run(engine, monkeypatch, board(), **FANOUT)
    assert res["summary"]["completion"] == 1.0
    assert all(v["y"] != 276.0 for v in res["vias"])
    assert not verdict(_written(board(), res))["violations"]


@pytest.mark.parametrize("engine", ENGINES)
def test_the_plane_fanout_leaves_no_mask_sliver(engine, monkeypatch):
    def board(mask=True):
        b = _caps()
        # Another net's tracks either side of each GND pad leave the
        # dog-bone only the way straight out. Once the mask is read the
        # dog-bone's via first tries (x, 288), clear of its own pad's
        # opening; this other net's via, 32 mil from there at 45 degrees,
        # leaves room for the copper (12 mil) but only 4 mil of mask
        # between the two openings.
        b.tracks = [Track("TopLayer", x + dx, 235, x + dx, 285, 6.0, net)
                    for x, net in ((200, "Q"), (400, "P")) for dx in (-22, 22)]
        b.vias = [Via(x + 22.63, 310.63, 20, 10, "TopLayer", "BottomLayer", net)
                  for x, net in ((200, "Q"), (400, "P"))]
        if mask:
            b.rules += MASK_RULES
        b.meta["mask_read"] = mask
        return b
    unmasked = _run(engine, monkeypatch, board(mask=False), **FANOUT)
    assert sorted((v["x"], v["y"]) for v in unmasked["vias"] if v["net"] == "GND") \
        == [(200.0, 276.0), (400.0, 276.0)]
    res = _run(engine, monkeypatch, board(), **FANOUT)
    assert res["summary"]["completion"] == 1.0
    assert not [v for v in res["vias"] if v["net"] == "GND" and v["y"] == 276.0]
    assert res["summary"]["mask_slivers"] == 0
    assert not verdict(_written(board(), res))["violations"]


SQUARE = [(250, 120), (450, 120), (450, 280), (250, 280)]


def _keepout_board(kind):
    b = _board(w=700, h=400, layers=("TopLayer",))
    b.pads = [_pad("R1", "1", 100, 200, "A"), _pad("R2", "1", 600, 200, "A")]
    if kind == "region":
        b.regions = [Region("TopLayer", SQUARE, [], "", "keepout", keepout=True)]
    else:
        # Drawn as Altium people often do: a closed outline of tracks on the
        # Keep-Out layer, which bars every copper layer.
        b.tracks = [Track("KeepOutLayer", *SQUARE[i], *SQUARE[(i + 1) % 4], 1.0, keepout=True)
                    for i in range(4)]
    return b


@pytest.mark.parametrize("kind", ("region", "keep-out layer tracks"))
@pytest.mark.parametrize("engine", ENGINES)
def test_the_gloss_keeps_out_of_a_keep_out(engine, kind, monkeypatch):
    board = _keepout_board(kind)
    keepouts = [r.shape() for r in board.regions] + [t.shape() for t in board.tracks]
    res = _run(engine, monkeypatch, board, gloss=True)
    assert res["summary"]["completion"] == 1.0
    # The straight line from R1 to R2 runs through the keep-out, so the
    # route had to go round it and the gloss must not straighten it back.
    assert all(geom.clearance(c, ko) >= 0 for c in _copper(res) for ko in keepouts)


@pytest.mark.parametrize("engine", ENGINES)
def test_the_gloss_keeps_to_the_board(engine, monkeypatch):
    def board():
        b = _board()
        # A notch, off the board, between the pads: the straight line
        # from one to the other runs through it.
        b.outline = [(0, 0), (600, 0), (600, 400), (400, 400), (400, 150),
                     (200, 150), (200, 400), (0, 400)]
        b.pads = [_pad("R1", "1", 100, 350, "A"), _pad("R2", "1", 500, 350, "A")]
        return b
    edge = geom.polygon(board().outline)

    def nearest(res):
        return min(geom.shape_to_outline_clearance(c, edge) for c in _copper(res))

    plain = _run(engine, monkeypatch, board(), gloss=False)
    res = _run(engine, monkeypatch, board(), gloss=True)
    assert res["summary"]["completion"] == 1.0
    centres = [geom.capsule(t["x1"], t["y1"], t["x2"], t["y2"], 0.0) for t in res["tracks"]]
    assert all(geom.shape_to_outline_clearance(c, edge) >= 0 for c in centres)
    # With no outline clearance rule the gloss may run up to the edge but
    # not past it, and where the route itself already reaches past it (a
    # diagonal step round an inward corner can), no further.
    assert nearest(res) >= min(0.0, nearest(plain)) - 1e-6


@pytest.mark.parametrize("engine", ENGINES)
def test_what_is_sent_is_what_was_judged(engine, monkeypatch):
    def board():
        b = _board(w=700, h=400)
        b.pads = [_pad("R1", "1", 100, 200, "A"), _pad("R2", "1", 600, 200, "A"),
                  _pad("R3", "1", 100, 330, "B"), _pad("R4", "1", 600, 330, "B")]
        # A hand-laid stub of A out of R1, joined to nothing yet.
        b.tracks = [Track("TopLayer", 100, 200, 100, 100, 6.0, "A")]
        return b
    res = _run(engine, monkeypatch, board(), gloss=True)
    written = run_drc(_written(board(), res))
    assert written.completion == res["summary"]["completion"] == 1.0
    assert not written.violations
