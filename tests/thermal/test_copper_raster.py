# SPDX-License-Identifier: Apache-2.0
"""copper_raster: fill fractions, planes, antipads, barrels, net filter."""

from __future__ import annotations

import math

import numpy as np
import pytest

from eda_agent.layout.copper_raster import rasterize
from eda_agent.layout.model import (
    Layer, LayoutBoard, Pad, PadCopper, Region, Rule, Track, Via,
)

OUTLINE = [(0, 0), (1000, 0), (1000, 500), (0, 500)]


def _board(**kw) -> LayoutBoard:
    layers = kw.pop("layers", [Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)])
    return LayoutBoard(name="t", outline=list(OUTLINE), layers=layers, **kw)


def test_grid_covers_outline_and_mask_is_full():
    r = rasterize(_board(), cell_mils=50)
    assert (r.nx, r.ny) == (20, 10)
    assert r.mask.min() == pytest.approx(1.0)
    assert r.layers == ["TopLayer", "BottomLayer"]
    assert r.fill["TopLayer"].sum() == 0


def test_track_area_matches_geometry():
    t = Track("TopLayer", 100, 250, 900, 250, 20, "N1")
    r = rasterize(_board(tracks=[t]), cell_mils=10, supersample=8)
    exact = 800 * 20 + math.pi * 10 ** 2
    assert r.copper_area_mils2("TopLayer") == pytest.approx(exact, rel=0.01)
    assert r.fill["BottomLayer"].sum() == 0


def test_cutout_removed_from_mask():
    b = _board(cutouts=[[(400, 100), (600, 100), (600, 400), (400, 400)]])
    r = rasterize(b, cell_mils=10)
    assert r.mask.sum() * 100 == pytest.approx(1000 * 500 - 200 * 300, rel=1e-6)


def test_pour_with_hole_scanline():
    reg = Region("TopLayer", [(0, 0), (1000, 0), (1000, 500), (0, 500)],
                 holes=[[(100, 100), (300, 100), (300, 300), (100, 300)]], net="GND", kind="pour")
    r = rasterize(_board(regions=[reg]), cell_mils=10)
    assert r.copper_area_mils2("TopLayer") == pytest.approx(500000 - 40000, rel=1e-6)


def test_plane_layer_solid_with_antipads_for_other_nets_only():
    layers = [Layer("TopLayer", "signal", 0), Layer("InternalPlane1", "plane", 1, "GND"),
              Layer("BottomLayer", "signal", 2)]
    vias = [Via(200, 250, 24, 12, "TopLayer", "BottomLayer", "SIG"),
            Via(800, 250, 24, 12, "TopLayer", "BottomLayer", "GND")]
    b = _board(layers=layers, vias=vias)
    r = rasterize(b, cell_mils=5, supersample=8, plane_clearance_mils=10)
    area = r.copper_area_mils2("InternalPlane1")
    anti = math.pi * (6 + 10) ** 2       # other-net via: hole + clearance
    hole = math.pi * 6 ** 2               # same-net via: just its drill
    assert area == pytest.approx(500000 - anti - hole, rel=2e-3)
    assert len(r.barrels) == 2
    assert r.barrels[0].layers == ["TopLayer", "InternalPlane1", "BottomLayer"]


def test_net_filter_keeps_one_rail():
    layers = [Layer("TopLayer", "signal", 0), Layer("InternalPlane1", "plane", 1, "GND"),
              Layer("BottomLayer", "signal", 2)]
    tracks = [Track("TopLayer", 0, 100, 1000, 100, 50, "VCC"),
              Track("TopLayer", 0, 400, 1000, 400, 50, "SIG")]
    vias = [Via(500, 100, 24, 12, "TopLayer", "BottomLayer", "VCC")]
    r = rasterize(_board(layers=layers, tracks=tracks, vias=vias), cell_mils=10, nets=["VCC"])
    assert r.fill["InternalPlane1"].sum() == 0
    top = r.copper_area_mils2("TopLayer")
    assert top == pytest.approx(1000 * 50 - math.pi * 6 ** 2, rel=0.02)
    assert [b.net for b in r.barrels] == ["VCC"]
    assert r.nets == ["VCC"]


def test_split_plane_regions_respect_their_net():
    layers = [Layer("TopLayer", "signal", 0), Layer("InternalPlane1", "plane", 1),
              Layer("BottomLayer", "signal", 2)]
    regs = [Region("InternalPlane1", [(0, 0), (500, 0), (500, 500), (0, 500)], net="3V3", kind="plane"),
            Region("InternalPlane1", [(500, 0), (1000, 0), (1000, 500), (500, 500)], net="1V8", kind="plane")]
    r = rasterize(_board(layers=layers, regions=regs), cell_mils=10, nets=["3V3"])
    f = r.fill["InternalPlane1"]
    assert f[:, :50].mean() == pytest.approx(1.0)
    assert f[:, 50:].sum() == 0


def test_th_pad_is_a_barrel_with_comp_and_pin():
    pad = Pad("J1", "1", 300, 300, net="VIN", hole=40,
              copper=[PadCopper("TopLayer", "round", 70, 70), PadCopper("BottomLayer", "round", 70, 70)])
    r = rasterize(_board(pads=[pad]), cell_mils=10)
    (b,) = r.barrels
    assert (b.kind, b.comp, b.pin, b.net) == ("pad", "J1", "1", "VIN")
    assert r.cell_of(300, 300) == (b.ix, b.iy) == (30, 30)
    ring = math.pi * (35 ** 2 - 20 ** 2)
    assert r.copper_area_mils2("TopLayer") == pytest.approx(ring, rel=0.03)


def test_plane_clearance_from_rule():
    rule = Rule("PlaneClearance", "plane_clearance", values={"clearance": 15})
    r = rasterize(_board(rules=[rule]), cell_mils=50)
    assert r.plane_clearance_mils == 15


def test_too_fine_grid_refused():
    with pytest.raises(ValueError):
        rasterize(_board(), cell_mils=0.1)


def test_fill_never_exceeds_mask():
    reg = Region("TopLayer", [(-100, -100), (1100, -100), (1100, 600), (-100, 600)], net="GND")
    b = _board(regions=[reg], cutouts=[[(400, 100), (600, 100), (600, 400), (400, 400)]])
    r = rasterize(b, cell_mils=25)
    assert np.all(r.fill["TopLayer"] <= r.mask + 1e-12)


def test_kinds_filter_leaves_tracks_out():
    t = Track("TopLayer", 100, 250, 900, 250, 20, "N1")
    pad = Pad("U1", "1", 500, 100, net="N1", copper=[PadCopper("TopLayer", "rect", 40, 20)])
    b = _board(tracks=[t], pads=[pad])
    r = rasterize(b, cell_mils=10, kinds={"pads", "regions", "planes"})
    assert r.copper_area_mils2("TopLayer") == pytest.approx(800, rel=0.02)
    assert "tracks" not in r.kinds
    with pytest.raises(ValueError):
        rasterize(b, kinds={"bogus"})
