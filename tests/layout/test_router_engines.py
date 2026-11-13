# SPDX-License-Identifier: Apache-2.0
"""Both routing engines through the routing job, on the boards where one
of them went wrong: a via on copper the grid does not see (a short) or
in a keep-out drawn on a layer no route uses, or too near a hole, or
leaving a solder mask sliver, whether the search (at a cell or at a pad's
centre), a BGA's fan-out or the plane fan-out laid it; a gloss that ran
through a keep-out or off the board; and a board track the job lost.

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
from eda_agent.layout.model import Arc, Pad, PadCopper, Region, Rule, Track, Via
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


def _in_keepouts(board, res):
    """The sent vias that stand in one of the board's keep-outs on a layer
    they pass. The DRC does not look at keep-outs, so this does."""
    shapes = [(r.layer, r.shape()) for r in board.regions if r.keepout or r.kind == "keepout"]
    shapes += [(t.layer, t.shape()) for t in board.tracks if t.keepout]
    shapes += [(a.layer, s) for a in board.arcs if a.keepout for s in a.shapes()]
    out = []
    for v in res["vias"]:
        span = board.layers_between(v["low_layer"], v["high_layer"])
        disc = geom.circle(v["x"], v["y"], v["size"])
        out += [(v["x"], v["y"], layer) for layer, s in shapes
                if (layer in span or layer == "KeepOutLayer") and geom.clearance(disc, s) < -1e-6]
    return out


WINDOW = [(320, 200), (380, 200), (380, 300), (320, 300)]


def _hidden(kind, layer, outline, windows, net="Z"):
    """What the grid does not see on ``layer``: another net's copper, or a
    keep-out, over ``outline`` but for the windows."""
    if kind == "copper":
        return Region(layer, outline, holes=windows, net=net)
    return Region(layer, outline, holes=windows, net="", kind="keepout", keepout=True)


@pytest.mark.parametrize("kind", ("copper", "keep-out"))
@pytest.mark.parametrize("engine", ENGINES)
def test_a_via_clears_copper_on_an_excluded_layer(engine, kind, monkeypatch):
    def board():
        b = join._through(150, 550)
        # MidLayer1, not routed on, is another net's copper, or a keep-out,
        # but for a window.
        b.regions = [_hidden(kind, "MidLayer1", [(20, 20), (680, 20), (680, 480), (20, 480)],
                             [WINDOW])]
        return b
    res = _run(engine, monkeypatch, board(), stages={"exclude_layers": ["MidLayer1"]})
    assert res["summary"]["completion"] == 1.0
    assert res["summary"]["drc_status"] == "pass", res["violations"][:2]
    assert res["vias"] and all(320 < v["x"] < 380 for v in res["vias"])
    assert not _in_keepouts(board(), res)


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
    assert res["summary"]["completion"] == 1.0
    assert [v for v in res["vias"] if v["net"] == "A"], "A's via is what the test is about"
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
    assert res["vias"], "the board needs a via, or the test proves nothing"
    assert res["summary"]["mask_slivers"] == 0
    assert res["summary"]["drc_status"] == "pass", res["violations"][:2]
    # Where two openings run into each other the verdict sees one opening
    # and no sliver, so measure the web itself: each via's opening (4 mil
    # of expansion) keeps the 10 mil sliver rule from each pad's (4 mil).
    out = _written(webs._board(), res)
    for v in res["vias"]:
        for p in out.pads:
            gap = geom.clearance(geom.circle(v["x"], v["y"], v["size"]), p.shape_on("TopLayer"))
            assert gap >= 4 + 4 + 10 - 1e-6, (p.comp, p.name, round(gap, 2))


def _router(engine, board, excluded=("MidLayer1",)):
    """The engine's router itself, set up for ``board``, not run."""
    if engine == "default":
        from eda_agent.layout.route.router import Router
        from eda_agent.layout.route.stages import StageOptions
        return Router(board, stages=StageOptions(exclude_layers=tuple(excluded)))
    from eda_agent.layout.route_euan.router import Router
    return Router(board, exclude_layers=frozenset(excluded))


@pytest.mark.parametrize("engine", ENGINES)
def test_a_point_is_measured_against_all_the_grid_does_not_show(engine):
    """via_clear_of_hidden, which the fan-outs and the pad-centre check
    ask, at a point half a mil either side of each limit. Net A's via is
    20 mil with a 10 mil hole; the clearance is 6 mil, the hole gap 40."""
    b = join._through(150, 550)
    b.rules.append(Rule("HoleToHole", "9", "All", "All", 1, True, {},
                        "Hole To Hole Clearance (Allow Stacked Micro Vias=No) (Gap=40mil) "
                        "(All),(All)"))
    # On MidLayer1, which no route uses: net Z's track, its edge at x 305;
    # a keep-out square; a keep-out ring (an arc keeps out its stroke, 28
    # to 32 mil from its centre, not the disc inside); net Z's via, its
    # 8 mil hole at (150, 370).
    b.tracks = [Track("MidLayer1", 300, 100, 300, 300, 10.0, "Z")]
    b.regions = [Region("MidLayer1", [(400, 100), (450, 100), (450, 150), (400, 150)], [],
                        "", "keepout", keepout=True)]
    b.arcs = [Arc("MidLayer1", 500, 300, 30, 0, 360, 4.0, keepout=True)]
    b.vias = [Via(150, 370, 16, 8, "TopLayer", "BottomLayer", "Z")]
    r = _router(engine, b)
    v = r.jobs[0].vias[0]
    assert (v.diameter, v.hole) == (20.0, 10.0)

    def clear(x, y, span=None):
        return r.via_clear_of_hidden(v, x, y, "A", span)
    # Copper: 5 + 6 + 10 from the track's centre line.
    assert not clear(320.5, 200) and clear(321.5, 200)
    assert clear(320.5, 200.0, span=["TopLayer", "BottomLayer"])
    # The keep-out square: the via may touch it, not stand in it.
    assert not clear(459.5, 125) and clear(460.5, 125)
    assert clear(459.5, 125, span=["TopLayer", "BottomLayer"])
    # The ring: clear inside it, not on its stroke.
    assert clear(500, 300) and not clear(541.5, 300) and clear(542.5, 300)
    # The hole gap: 4 + 40 + 5 between the centres, whatever the layers.
    assert not clear(150, 321.5) and clear(150, 320.5)
    assert not clear(150, 321.5, span=["TopLayer"])


MASK_RULES = [Rule("Sliver", "mask_sliver", "All", "All", 1, True, {},
                   "Minimum Solder Mask Sliver (Gap=10mil) (All),(All)"),
              Rule("Mask", "soldermask", "All", "", 1, True, {},
                   "Solder Mask Expansion (Expansion =4mil) (All)")]
PAD_AT = (151.7, 251.3)


def _small_pad(hazard=None):
    """Net A from a 4 mil pad on top, smaller than a cell, to a pad on the
    bottom, with any pad taking a via: the search's via goes in the small
    pad, which it lays at the pad's centre, 2.3 mil from the centre of the
    cell its guards judge. The hazard stands straight on from the cell's
    centre past the pad's: net Z's via 48 mil from the pad's centre (39
    mil of hole gap there where 40 are asked, 41.3 at the cell's), or net
    Q's round pad 37 mil from it (a 9 mil mask web there where 10 are
    asked, 11.3 at the cell's)."""
    b = join._through(PAD_AT[0], 550, y=PAD_AT[1])
    b.pads[0] = Pad("P1", "1", *PAD_AT, net="A", copper=[PadCopper("TopLayer", "rect", 4, 4)])
    pitch = 4.164411545494762               # both engines' grid on this board
    cx, cy = 36 * pitch, 60 * pitch         # the cell nearest the pad's centre
    dx, dy = PAD_AT[0] - cx, PAD_AT[1] - cy
    d = (dx * dx + dy * dy) ** 0.5

    def out(far):
        return PAD_AT[0] + dx / d * far, PAD_AT[1] + dy / d * far
    if hazard == "hole":
        b.rules.append(Rule("HoleToHole", "9", "All", "All", 1, True, {},
                            "Hole To Hole Clearance (Allow Stacked Micro Vias=No) (Gap=40mil) "
                            "(All),(All)"))
        b.vias = [Via(*out(48), 16, 8, "TopLayer", "BottomLayer", "Z")]
    elif hazard == "mask":
        b.rules += MASK_RULES
        b.meta["mask_read"] = True
        b.pads.append(Pad("Q1", "1", *out(37), net="Q",
                          copper=[PadCopper("TopLayer", "round", 20, 20)]))
    return b


@pytest.mark.parametrize("hazard", ("hole", "mask"))
@pytest.mark.parametrize("engine", ENGINES)
def test_a_via_in_a_pad_is_judged_where_it_is_laid(engine, hazard, monkeypatch):
    stages = {"pad_vias": "any"}
    plain = _run(engine, monkeypatch, _small_pad(), stages=stages)
    assert [(v["x"], v["y"]) for v in plain["vias"]] == [PAD_AT]
    assert not verdict(_small_pad(hazard))["violations"]
    res = _run(engine, monkeypatch, _small_pad(hazard), stages=stages)
    assert res["summary"]["completion"] == 1.0
    assert res["vias"], "the board needs a via, or the test proves nothing"
    assert not hole_gaps(_written(_small_pad(hazard), res))
    assert res["summary"]["mask_slivers"] == 0


# -- vias laid outside the search: a BGA's fan-out and the plane fan-out ---

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


@pytest.mark.parametrize("kind", ("copper", "keep-out"))
@pytest.mark.parametrize("engine", ENGINES)
def test_the_bga_fanout_keeps_its_vias_off_hidden_copper(engine, kind, monkeypatch):
    def board():
        b = _bga()
        # MidLayer1, not routed on, is net Z's copper (or a keep-out) under
        # the whole BGA but for a window round each middle ball: the
        # dog-bone spots are on it, so each via must go in its ball.
        windows = [[(p.x - 17, p.y - 17), (p.x + 17, p.y - 17), (p.x + 17, p.y + 17),
                    (p.x - 17, p.y + 17)] for p, _spot, _sx in _middle_balls(b)]
        b.regions = [_hidden(kind, "MidLayer1", [(150, 150), (450, 150), (450, 450), (150, 450)],
                             windows)]
        return b
    res = _run(engine, monkeypatch, board(), stages={"exclude_layers": ["MidLayer1"]})
    assert res["summary"]["completion"] == 1.0
    assert _in_the_balls(board(), res) == [True] * 4
    assert not verdict(_written(board(), res))["violations"]
    assert not _in_keepouts(board(), res)


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


@pytest.mark.parametrize("kind", ("copper", "keep-out"))
@pytest.mark.parametrize("engine", ENGINES)
def test_the_plane_fanout_keeps_its_vias_off_other_nets_tracks_on_the_plane(engine, kind,
                                                                           monkeypatch):
    def board():
        b = _caps()
        # Net Z's tracks (or keep-out tracks) on the plane layer, across
        # both dog-bone spots.
        b.tracks = [Track("MidLayer2", x0, 278, x0 + 100, 278, 10.0,
                          "Z" if kind == "copper" else "", keepout=kind == "keep-out")
                    for x0 in (150, 350)]
        return b
    assert sorted((v["x"], v["y"]) for v in _run(engine, monkeypatch, _caps(), **FANOUT)["vias"]) \
        == [(200.0, 276.0), (400.0, 276.0)]
    res = _run(engine, monkeypatch, board(), **FANOUT)
    assert res["summary"]["completion"] == 1.0
    assert all(v["y"] != 276.0 for v in res["vias"])
    assert not verdict(_written(board(), res))["violations"]
    assert not _in_keepouts(board(), res)


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


@pytest.mark.parametrize("engine", ENGINES)
def test_the_plane_fanout_keeps_the_hole_to_hole_gap(engine, monkeypatch):
    def board(rule=True):
        b = _caps()
        if rule:
            b.rules.append(Rule("HoleToHole", "9", "All", "All", 1, True, {},
                                "Hole To Hole Clearance (Allow Stacked Micro Vias=No) "
                                "(Gap=40mil) (All),(All)"))
        # Net Z's via 44 mil from each dog-bone spot: room for the copper,
        # 35 mil of hole gap where 40 are asked.
        b.vias = [Via(x + 44, 276, 16, 8, "TopLayer", "BottomLayer", "Z") for x in (200, 400)]
        return b
    unruled = _run(engine, monkeypatch, board(rule=False), **FANOUT)
    assert sorted((v["x"], v["y"]) for v in unruled["vias"] if v["net"] == "GND") \
        == [(200.0, 276.0), (400.0, 276.0)]
    res = _run(engine, monkeypatch, board(), **FANOUT)
    assert res["summary"]["completion"] == 1.0
    assert len([v for v in res["vias"] if v["net"] == "GND"]) == 2
    assert not hole_gaps(_written(board(), res))


SQUARE = [(250, 120), (450, 120), (450, 280), (250, 280)]


def _keepout_board(kind):
    b = _board(w=700, h=400, layers=("TopLayer",))
    b.pads = [_pad("R1", "1", 100, 200, "A"), _pad("R2", "1", 600, 200, "A")]
    if kind == "region":
        b.regions = [Region("TopLayer", SQUARE, [], "", "keepout", keepout=True)]
    elif kind == "keep-out layer tracks":
        # Drawn as Altium people often do: a closed outline of tracks on the
        # Keep-Out layer, which bars every copper layer.
        b.tracks = [Track("KeepOutLayer", *SQUARE[i], *SQUARE[(i + 1) % 4], 1.0, keepout=True)
                    for i in range(4)]
    else:
        # A ring, as round a mounting hole: on the Keep-Out layer, or on
        # the copper layer itself.
        layer = "KeepOutLayer" if kind == "keep-out layer arc" else "TopLayer"
        b.arcs = [Arc(layer, 350, 200, 80, 0, 360, 1.0, keepout=True)]
    return b


@pytest.mark.parametrize("kind", ("region", "keep-out layer tracks", "keep-out layer arc",
                                  "copper layer arc"))
@pytest.mark.parametrize("engine", ENGINES)
def test_the_gloss_keeps_out_of_a_keep_out(engine, kind, monkeypatch):
    board = _keepout_board(kind)
    keepouts = [r.shape() for r in board.regions] + [t.shape() for t in board.tracks] + \
        [s for a in board.arcs for s in a.shapes()]
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


@pytest.mark.parametrize("engine", ENGINES)
def test_a_board_track_the_router_took_up_is_noted_and_judged_in_place(engine, monkeypatch):
    """Nothing on the board is ever taken up, so a track the router dropped
    is not sent as gone: the job notes it and judges the board with it."""
    import eda_agent.layout.route.job as job_mod
    import eda_agent.layout.route_euan.router as euan
    stub = (100, 200, 100, 100, "A")

    def losing(real):
        def route(*a, **k):
            r = real(*a, **k)
            apply = r.apply

            def apply_less():
                out = apply()
                out.tracks = [t for t in out.tracks
                              if (t.x1, t.y1, t.x2, t.y2, t.net) != stub]
                return out
            r.apply = apply_less
            return r
        return route
    monkeypatch.setattr(job_mod, "route_adaptive", losing(job_mod.route_adaptive))
    monkeypatch.setattr(euan, "route_adaptive", losing(euan.route_adaptive))
    b = _board(w=700, h=400)
    b.pads = [_pad("R1", "1", 100, 200, "A"), _pad("R2", "1", 600, 200, "A")]
    b.tracks = [Track("TopLayer", *stub[:4], 6.0, "A")]
    res = _run(engine, monkeypatch, b)
    assert any("took up 1 of the board's own tracks" in n for n in res["notes"]), res["notes"]
    assert not [t for t in res["tracks"] if (t["x1"], t["y1"], t["x2"], t["y2"]) == stub[:4]]
    assert res["summary"]["completion"] == 1.0


@pytest.mark.parametrize("engine", ENGINES)
def test_old_pours_come_off_by_outline_and_for_euan_by_net_too(engine, monkeypatch):
    """The job takes a polygon's old copper off before routing (the board
    is repoured after). Both engines match the copper to its polygon's
    outline by layer and source; the euan engine, as his job did, also
    takes off any pour of a net that has an outline on that layer."""
    whole = [(0, 0), (700, 0), (700, 400), (0, 400)]

    def board(source):
        b = _board(w=700, h=400)
        b.pads = [_pad("R1", "1", 100, 200, "A"), _pad("R2", "1", 600, 200, "A")]
        b.regions = [Region("BottomLayer", whole, [], "GND", "pour_boundary",
                            source="polygon:P1"),
                     Region("BottomLayer", whole, [], "GND", "pour", source=source)]
        return b
    own = _run(engine, monkeypatch, board("polygon:P1"))
    assert own["summary"]["old_pours"] == ["BottomLayer:GND"]
    # Copper of another GND polygon there, P2, whose outline was not read.
    other = _run(engine, monkeypatch, board("polygon:P2"))
    assert other["summary"]["old_pours"] == (["BottomLayer:GND"] if engine == "euan" else [])
