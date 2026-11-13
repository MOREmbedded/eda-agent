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


def test_link_conductivity_is_directional():
    # a 20 mil track along x through the middle of a row of 50 mil cells
    t = Track("TopLayer", 0, 225, 1000, 225, 20, "N1")
    r = rasterize(_board(tracks=[t]), cell_mils=50, supersample=10, links=True)
    kx, ky = r.link_conductivity("TopLayer", 385.0, 0.3)
    assert kx.shape == (10, 19) and ky.shape == (9, 20)
    # along the track: copper over 20/50 of the width, in parallel with resin
    assert kx[4, 5] == pytest.approx(385 * 0.4 + 0.3 * 0.6, rel=1e-6)
    # across the track (row 4 to row 5): one centre sits on the track, the
    # other in bare resin, so the heat must cross resin: far below the area rule
    area_rule = 385 * r.fill["TopLayer"][4, 5] + 0.3 * (1 - r.fill["TopLayer"][4, 5])
    assert ky[4, 5] < 0.02 * area_rule
    assert ky[4, 5] > 0.3
    # a link with no copper at all is the resin
    assert kx[0, 0] == pytest.approx(0.3)


def test_link_conductivity_gap_opens_an_electrical_link():
    # two pours with a 10 mil gap across x at x = 500 (between cells 9 and 10)
    regs = [Region("TopLayer", [(0, 0), (495, 0), (495, 500), (0, 500)], net="A"),
            Region("TopLayer", [(505, 0), (1000, 0), (1000, 500), (505, 500)], net="A")]
    r = rasterize(_board(regions=regs), cell_mils=50, supersample=10, links=True)
    kx, _ = r.link_conductivity("TopLayer", 1.0, 0.0)
    assert kx[3, 9] == 0.0            # the gap opens the link
    assert kx[3, 5] == pytest.approx(1.0)
    with pytest.raises(ValueError):
        rasterize(_board(regions=regs), cell_mils=50).link_conductivity("TopLayer", 1.0, 0.0)


def test_bounds_window_grids_a_part_of_the_board():
    t = Track("TopLayer", 100, 250, 900, 250, 20, "N1")
    vias = [Via(150, 250, 24, 12, "TopLayer", "BottomLayer", "N1"),
            Via(850, 250, 24, 12, "TopLayer", "BottomLayer", "N1")]
    r = rasterize(_board(tracks=[t], vias=vias), cell_mils=5, bounds=(100, 200, 300, 300))
    assert (r.x0, r.y0, r.nx, r.ny) == (100, 200, 40, 20)
    assert len(r.barrels) == 1 and r.barrels[0].x == 150
    assert r.mask.min() == pytest.approx(1.0)


def test_shape_area_by_cell_splits_a_pad_over_its_cells():
    from eda_agent.layout import geom
    r = rasterize(_board(), cell_mils=50)
    # a 60 x 20 rectangle centred on a cell corner at (500, 250)
    cells = r.shape_area_by_cell(geom.pad_shape(500, 250, 60, 20, "rect"), samples=10)
    assert set(cells) == {(9, 4), (10, 4), (9, 5), (10, 5)}
    assert sum(cells.values()) == pytest.approx(1200, rel=1e-6)
    assert cells[(9, 4)] == pytest.approx(300, rel=1e-6)
    # a pad too small to catch a sample still lands in its cell
    tiny = r.shape_area_by_cell(geom.pad_shape(512, 262, 0.5, 0.5, "rect"), samples=2)
    assert list(tiny) == [(10, 5)]
    # off the grid: nothing
    assert r.shape_area_by_cell(geom.circle(5000, 5000, 10)) == {}


def test_edge_length_per_cell():
    # a 100 x 100 pad whose edges cut through the middle of 50 mil cells
    pad = Pad("U1", "1", 525, 275, net="N", copper=[PadCopper("TopLayer", "rect", 100, 100)])
    r = rasterize(_board(pads=[pad]), cell_mils=50, supersample=10, links=True)
    e = r.edge_mils["TopLayer"]
    assert e.sum() == pytest.approx(400, rel=0.02)          # the pad's perimeter
    assert e[5, 9] == pytest.approx(50, rel=1e-6)           # its left edge crosses this cell
    assert e[5, 10] == 0 and r.fill["TopLayer"][5, 10] == 1  # inside the pad
    assert e[0, 0] == 0
    assert rasterize(_board(pads=[pad]), cell_mils=50).edge_mils == {}


def test_a_pour_outline_is_not_copper():
    # read_altium keeps a polygon's outline (kind "pour_boundary") beside
    # the copper it poured (kind "pour"): only the poured copper counts.
    from eda_agent.layout.model import Layer, LayoutBoard, Region
    b = LayoutBoard(outline=[(0, 0), (1000, 0), (1000, 1000), (0, 1000)],
                    layers=[Layer("TopLayer", "signal", 0), Layer("InternalPlane1", "plane", 1)])
    b.regions += [
        Region("TopLayer", [(100, 400), (900, 400), (900, 600), (100, 600)], [], "P", "pour"),
        Region("TopLayer", [(0, 0), (1000, 0), (1000, 1000), (0, 1000)], [], "P", "pour_boundary"),
        Region("InternalPlane1", [(0, 0), (500, 0), (500, 1000), (0, 1000)], [], "P", "copper"),
        Region("InternalPlane1", [(0, 0), (1000, 0), (1000, 1000), (0, 1000)], [], "P", "pour_boundary"),
    ]
    r = rasterize(b, 20.0, nets=["P"])
    assert r.copper_area_mils2("TopLayer") == pytest.approx(800 * 200, rel=0.01)
    assert r.copper_area_mils2("InternalPlane1") == pytest.approx(500 * 1000, rel=0.01)


def test_links_follow_the_copper_not_the_cell():
    from eda_agent.layout.model import Layer, LayoutBoard, Region
    b = LayoutBoard(outline=[(0, 0), (400, 0), (400, 200), (0, 200)],
                    layers=[Layer("TopLayer", "signal", 0)])
    # two pours 6 mil apart (gap at x = 197..203, inside one 20 mil cell),
    # the lower half of the board only
    b.regions += [Region("TopLayer", [(0, 0), (197, 0), (197, 100), (0, 100)], [], "P", "pour"),
                  Region("TopLayer", [(203, 0), (400, 0), (400, 100), (203, 100)], [], "P", "pour")]
    r = rasterize(b, 20.0, nets=["P"], supersample=8, links=True)
    lx, ly = r.link_x["TopLayer"], r.link_y["TopLayer"]
    assert lx.shape == (10, 19) and ly.shape == (9, 20)
    assert np.all(lx[:5, :8] == 1.0) and np.all(lx[:5, 11:] == 1.0)   # solid copper
    assert np.all(lx[:5, 9] == 0.0)          # the gap breaks the link across it
    assert np.all(lx[5:, :] == 0.0)          # no copper above y = 100
    assert np.all(ly[:4, :9] == 1.0) and np.all(ly[4:, :] == 0.0)
    # a face half covered conducts by half: copper to y = 110 on a 20 mil grid
    b.regions = [Region("TopLayer", [(0, 0), (400, 0), (400, 110), (0, 110)], [], "P", "pour")]
    r = rasterize(b, 20.0, nets=["P"], supersample=8, links=True)
    assert np.allclose(r.link_x["TopLayer"][5, :], 0.5)
    assert not rasterize(b, 20.0, nets=["P"]).link_x          # off by default


# -- exact areas ---------------------------------------------------------------

@pytest.mark.parametrize("cell", [10.0, 25.0, 50.0, 100.0])
def test_areas_are_exact_whatever_the_cell(cell):
    """A track, a round pad, a rectangle and a rounded rectangle, all off
    the grid lines: each keeps its true area on any grid (centre sampling
    gave a 30 mil track 25 mil of width on 50 mil cells)."""
    def area(**kw) -> float:
        return rasterize(_board(**kw), cell_mils=cell).copper_area_mils2("TopLayer")

    w, length = 30.0, 600.0
    capsule = w * length + math.pi * (w / 2) ** 2
    assert area(tracks=[Track("TopLayer", 203, 151, 803, 151, w, "N")]) \
        == pytest.approx(capsule, rel=1e-6)                       # along x
    assert area(tracks=[Track("TopLayer", 403, 51, 403, 451, w, "N")]) \
        == pytest.approx(w * 400 + math.pi * (w / 2) ** 2, rel=1e-6)   # along y
    assert area(pads=[Pad("U1", "1", 333.3, 222.2, net="N",
                          copper=[PadCopper("TopLayer", "round", 61, 61)])]) \
        == pytest.approx(math.pi * 30.5 ** 2, rel=1e-6)
    assert area(pads=[Pad("U1", "1", 333.3, 222.2, net="N",
                          copper=[PadCopper("TopLayer", "rect", 87, 43)])]) \
        == pytest.approx(87 * 43, rel=1e-6)
    cr = 0.5 * 43 / 2                                             # 50 % of half the short side
    rounded = 87 * 43 - (4 - math.pi) * cr ** 2
    assert area(pads=[Pad("U1", "1", 333.3, 222.2, net="N",
                          copper=[PadCopper("TopLayer", "roundrect", 87, 43, corner_pct=50)])]) \
        == pytest.approx(rounded, rel=1e-6)


@pytest.mark.parametrize("angle", [17.0, 45.0, 73.0, 120.0])
def test_a_slanted_track_keeps_its_width(angle):
    w, length = 12.0, 400.0
    dx, dy = math.cos(math.radians(angle)) * length, math.sin(math.radians(angle)) * length
    t = Track("TopLayer", 500 - dx / 2, 250 - dy / 2, 500 + dx / 2, 250 + dy / 2, w, "N")
    for cell in (20.0, 50.0):
        got = rasterize(_board(tracks=[t]), cell_mils=cell).copper_area_mils2("TopLayer")
        assert got == pytest.approx(w * length + math.pi * (w / 2) ** 2, rel=0.01)


def test_a_drill_hole_takes_its_exact_area_out_of_a_pad():
    pad = Pad("J1", "1", 407, 263, net="N", hole=37.0,
              copper=[PadCopper("TopLayer", "rect", 90, 90), PadCopper("BottomLayer", "rect", 90, 90)])
    r = rasterize(_board(pads=[pad]), cell_mils=50)
    assert r.copper_area_mils2("TopLayer") == pytest.approx(90 * 90 - math.pi * 18.5 ** 2, rel=1e-6)


def test_overlapping_copper_is_counted_once():
    pads = [Pad("U1", "1", 400, 250, net="N", copper=[PadCopper("TopLayer", "rect", 100, 100)])]
    tracks = [Track("TopLayer", 400, 250, 430, 250, 20, "N")]     # wholly inside the pad
    r = rasterize(_board(pads=pads, tracks=tracks), cell_mils=30)
    assert r.copper_area_mils2("TopLayer") == pytest.approx(100 * 100, rel=1e-6)


def test_a_polygon_cutout_is_not_a_hole_in_the_board():
    """Region kind "cutout" is a no-pour area on one copper layer
    (read_altium: copper False). It is not copper, and the board under it
    is still board: tracks cross it and the mask stays whole."""
    b = _board(tracks=[Track("TopLayer", 100, 250, 900, 250, 40, "N")],
               regions=[Region("TopLayer", [(400, 100), (600, 100), (600, 400), (400, 400)],
                               [], "", "cutout")])
    r = rasterize(b, cell_mils=50)
    assert r.mask.min() == 1.0
    assert r.fill["TopLayer"][5, 10] > 0.3                  # the track inside the cutout
    assert r.copper_area_mils2("TopLayer") == pytest.approx(800 * 40 + math.pi * 20 ** 2, rel=1e-6)


def test_an_unpoured_polygon_is_reported():
    ring = [(100, 100), (900, 100), (900, 400), (100, 400)]
    b = _board(regions=[Region("TopLayer", ring, [], "GND", "pour_boundary", False, "",
                               "polygon:GND_L1")])
    r = rasterize(b, cell_mils=50)
    assert r.copper_area_mils2("TopLayer") == 0.0
    assert any("no poured copper" in n and "GND_L1 on TopLayer" in n for n in r.notes)
    b.regions.append(Region("TopLayer", [(150, 150), (850, 150), (850, 350), (150, 350)], [],
                            "GND", "pour", False, "", "polygon:GND_L1"))
    assert not any("no poured copper" in n for n in rasterize(b, cell_mils=50).notes)


def test_edge_length_of_round_and_slanted_copper():
    pad = Pad("U1", "1", 503, 251, net="N", copper=[PadCopper("TopLayer", "round", 200, 200)])
    r = rasterize(_board(pads=[pad]), cell_mils=25, supersample=8, links=True)
    assert r.edge_mils["TopLayer"].sum() == pytest.approx(math.pi * 200, rel=0.05)


def test_a_gap_narrower_than_a_sample_is_open_and_an_edge_row_conducts():
    # two pours 4 mil apart on 50 mil cells (samples of 12.5 mil), and a
    # track whose edge falls inside a sample row
    b = _board(regions=[Region("TopLayer", [(100, 100), (498, 100), (498, 200), (100, 200)], [], "A"),
                        Region("TopLayer", [(502, 100), (900, 100), (900, 200), (502, 200)], [], "B")],
               tracks=[Track("TopLayer", 100, 353, 900, 353, 30, "C")])
    r = rasterize(b, cell_mils=50, links=True)
    kx, _ = r.link_conductivity("TopLayer", 1.0, 0.0)
    assert kx[2, 9] == 0.0                      # across the gap at x = 500, row y = 100..150
    assert kx[2, 5] == pytest.approx(1.0)       # solid pour
    assert kx[2, 12] == pytest.approx(1.0)      # and solid again beyond the gap
    # the same at the 2 mil sample a conduction model uses: a 3 mil gap
    # leaves no sample empty, and is still open
    b.regions[1] = Region("TopLayer", [(501, 100), (900, 100), (900, 200), (501, 200)], [], "B")
    fine = rasterize(b, cell_mils=50, links=True, supersample=0)
    assert fine.supersample == 25
    fx, _ = fine.link_conductivity("TopLayer", 1.0, 0.0)
    assert fx[2, 9] == 0.0 and fx[2, 5] == pytest.approx(1.0) and fx[2, 12] == pytest.approx(1.0)
    # the track spans y = 338..368: 12 mil in row 6 (300..350), 18 in row 7
    assert kx[6, 8] == pytest.approx(12 / 50, rel=1e-6)
    assert kx[7, 8] == pytest.approx(18 / 50, rel=1e-6)


# -- sample pitch for conduction ----------------------------------------------

def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def test_fine_supersample_holds_the_sample_near_two_mils():
    from eda_agent.layout.copper_raster import MAX_SAMPLES, SAMPLE_MILS, fine_supersample
    assert SAMPLE_MILS == 2.0
    assert fine_supersample(25.0, 40_000) == 13          # 1.9 mil
    assert fine_supersample(40.0, 40_000) == 20
    assert fine_supersample(5.0, 40_000) == 4            # never under 4
    # a grid too large for it takes the finest sample that fits
    s = fine_supersample(50.0, 1_000_000)
    assert s == 4 and 1_000_000 * s * s <= MAX_SAMPLES
    assert fine_supersample(50.0, 250_000) == 8
    board = LayoutBoard(name="b", outline=rect(0, 0, 400, 400),
                        layers=[Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)])
    assert rasterize(board, cell_mils=40.0, supersample=0).supersample == 20
    assert rasterize(board, cell_mils=40.0).supersample == 4


def _sheet_conductance(raster, layer):
    """Conductance per square of ``layer`` from its left edge to its right
    one, 1 for solid copper, from the raster's link conductivities."""
    kx, ky = raster.link_conductivity(layer, 1.0, 0.0)
    ny, nx = raster.ny, raster.nx
    idx = np.arange(ny * nx).reshape(ny, nx)
    a = np.zeros((ny * nx, ny * nx))
    for i, j, g in ((idx[:, :-1], idx[:, 1:], kx), (idx[:-1, :], idx[1:, :], ky)):
        i, j, g = i.ravel(), j.ravel(), g.ravel()
        np.add.at(a, (i, i), g)
        np.add.at(a, (j, j), g)
        np.add.at(a, (i, j), -g)
        np.add.at(a, (j, i), -g)
    b = np.zeros(ny * nx)
    big = 1e6
    a[idx[:, 0], idx[:, 0]] += big
    b[idx[:, 0]] += big
    a[idx[:, -1], idx[:, -1]] += big
    v = np.linalg.solve(a + 1e-12 * np.eye(ny * nx), b).reshape(ny, nx)
    mid = nx // 2
    current = float((kx[:, mid] * (v[:, mid] - v[:, mid + 1])).sum())
    return current * (nx - 1) / ny


def _via_field(pitch=40.0, hole=8.0, n=12, stagger=False):
    w = n * pitch
    layers = [Layer("TopLayer", "signal", 0), Layer("InternalPlane1", "plane", 1, "GND"),
              Layer("BottomLayer", "signal", 2)]
    board = LayoutBoard(name="bga", outline=rect(0, 0, w, w), layers=layers)
    for i in range(n):
        for j in range(n):
            x = (i + 0.5) * pitch + (pitch / 2 if stagger and j % 2 else 0.0)
            if x < w - 1:
                board.vias.append(Via(x, (j + 0.5) * pitch, hole + 10, hole,
                                      "TopLayer", "BottomLayer", "SIG"))
    return board


@pytest.mark.parametrize("cell,rel", [(8.0, 0.04), (16.0, 0.04), (24.0, 0.06), (40.0, 0.15)])
def test_a_plane_under_a_via_field_conducts_as_the_perforated_sheet_it_is(cell, rel):
    """A plane a BGA's vias pass through: round antipads of 28 mil at
    40 mil pitch, webs of 12 mil between them. Rayleigh's result for a
    square array of round holes of area share f in a sheet is
    1 - 2f / (1 + f - 0.3058 f^4). With a sample of 2 mil the webs are
    seen whatever the cell; with four samples to a 40 mil cell every web
    read as a cut and the plane as open (conductance 0)."""
    board = _via_field()
    f = math.pi * 14.0 ** 2 / 40.0 ** 2
    rayleigh = 1.0 - 2.0 * f / (1.0 + f - 0.305827 * f ** 4)
    r = rasterize(board, cell_mils=cell, links=True, supersample=0, plane_clearance_mils=10.0)
    assert r.fill["InternalPlane1"].mean() == pytest.approx(1.0 - f, abs=2e-3)
    assert _sheet_conductance(r, "InternalPlane1") == pytest.approx(rayleigh, rel=rel)


def test_staggered_holes_do_not_cut_the_plane_either():
    board = _via_field(stagger=True)
    for cell in (16.0, 40.0):
        r = rasterize(board, cell_mils=cell, links=True, supersample=0,
                      plane_clearance_mils=10.0)
        assert 0.40 < _sheet_conductance(r, "InternalPlane1") < 0.55


# ---------------------------------------------------------------------------
# Islands: copper that does not touch, lying in one cell
# ---------------------------------------------------------------------------

def _pad_in_pour(clear=8.0, pad=(440.0, 230.0, 20.0, 60.0), tie=False):
    """A pour over the top layer and one pad of another net in a
    clearance cut into it (``tie``: the pad's net is the pour's and
    there is no cut)."""
    x, y, w, h = pad
    hole = rect(x - w / 2 - clear, y - h / 2 - clear, x + w / 2 + clear, y + h / 2 + clear)
    return _board(
        regions=[Region("TopLayer", list(OUTLINE), holes=[] if tie else [hole], net="GND",
                        kind="pour")],
        pads=[Pad("U1", "1", x, y, net="GND" if tie else "SIG",
                  copper=[PadCopper("TopLayer", "rect", w, h)])])


def test_copper_that_does_not_touch_is_split_inside_a_cell():
    """A 20 x 60 mil pad in an 8 mil clearance, cells of 50 mil: the
    pad and the pour share cells and are two islands there. The pour
    keeps the cells' nodes, the pad is a piece in each, and the two
    together are the cell's copper."""
    board = _pad_in_pour()
    r = rasterize(board, cell_mils=50, links=True, supersample=0, islands=True)
    isl = r.islands["TopLayer"]
    ncell = r.nx * r.ny
    pad_label, pad_node = r.island_at("TopLayer", 440.0, 230.0)
    pour_label, pour_node = r.island_at("TopLayer", 410.0, 230.0)       # same cell
    assert pad_label and pour_label and pad_label != pour_label
    assert r.cell_of(440.0, 230.0) == r.cell_of(410.0, 230.0) == (8, 4)
    assert pour_node == 4 * r.nx + 8 and pad_node >= ncell              # main, and a piece
    assert r.island_at("TopLayer", 426.0, 230.0) == (0, -1)             # in the clearance
    assert r.island_at("TopLayer", -5.0, 230.0) == (0, -1)              # off the grid
    # the pad spans y 200..260: cells iy 4 and 5, a piece in each
    assert len(isl.label) == 2 and set(isl.label) == {pad_label}
    assert sorted(isl.cell) == [4 * r.nx + 8, 5 * r.nx + 8]
    assert isl.fill.sum() * 2500 == pytest.approx(20 * 60, rel=1e-6)
    assert (isl.fill_main + np.bincount(isl.cell, weights=isl.fill, minlength=ncell)
            .reshape(r.ny, r.nx)) == pytest.approx(r.fill["TopLayer"], abs=1e-9)
    # its edge, but for the 20 mil end lying on a cell border (y = 200),
    # which falls to the cell below
    assert isl.edge_mils.sum() == pytest.approx(2 * (20 + 60) - 20, rel=0.05)
    assert (isl.label_main == pour_label).all()
    # the pad's copper, node by node, is its own island's and all of it
    nodes = r.island_nodes("TopLayer", board.pads[0].shape_on("TopLayer"))
    assert set(nodes) == {ncell, ncell + 1}
    assert sum(nodes.values()) == pytest.approx(20 * 60, rel=1e-6)
    assert r.island_nodes("TopLayer", rect_shape(700, 100, 720, 120)).keys() == {2 * r.nx + 14}
    # a shape lying over the pad, its clearance and some pour is on the pad
    # (1000 mil^2 of it against 600 of pour): the pour's copper is not counted
    over = r.island_nodes("TopLayer", rect_shape(425, 205, 470, 250))
    assert set(over) == {ncell} and over[ncell] == pytest.approx(20 * 45, rel=1e-6)
    # its two pieces are joined through the pad, and to nothing else
    assert len(isl.link_w) == 1 and {int(isl.link_a[0]), int(isl.link_b[0])} == {ncell, ncell + 1}
    assert isl.link_w[0] == pytest.approx((20 / r.cell_mils * r.supersample)
                                          / (30 / r.cell_mils * r.supersample), rel=0.1)


def rect_shape(x0, y0, x1, y1):
    from eda_agent.layout import geom
    return geom.polygon(rect(x0, y0, x1, y1))


def test_the_island_with_most_copper_in_a_cell_is_its_node():
    """A pad that fills most of a cell is that cell's node and the pour
    is the piece there; a cell's link to the next is copper only where
    both hold the same island."""
    board = _pad_in_pour(pad=(425.0, 225.0, 36.0, 36.0), clear=4.0)
    r = rasterize(board, cell_mils=50, links=True, supersample=0, islands=True)
    isl = r.islands["TopLayer"]
    pad_label, pad_node = r.island_at("TopLayer", 425.0, 225.0)
    pour_label, pour_node = r.island_at("TopLayer", 401.0, 201.0)
    assert pad_node == 4 * r.nx + 8                                     # the cell itself
    assert pour_node >= r.nx * r.ny and pour_label == isl.label_main[4, 7]
    assert isl.fill_main[4, 8] == pytest.approx(36 * 36 / 2500, rel=1e-6)
    kx, ky = r.link_conductivity("TopLayer", 1.0, 0.0)
    assert kx[4, 7] == 0.0 and kx[4, 8] == 0.0 and ky[3, 8] == 0.0 and ky[4, 8] == 0.0
    assert kx[4, 5] == pytest.approx(1.0) and ky[1, 8] == pytest.approx(1.0)
    # the pour's ring in the pad's cell is joined to the pour on all four sides
    ring = pour_node
    joined = {int(b if a == ring else a) for a, b in zip(isl.link_a, isl.link_b)
              if ring in (a, b)}
    assert joined == {4 * r.nx + 7, 4 * r.nx + 9, 3 * r.nx + 8, 5 * r.nx + 8}


def test_a_board_whose_cells_hold_one_island_each_is_unchanged_by_islands():
    """Solid copper, a lone track, a plane with antipads: no cell holds
    two islands, and the links are what they were. The antipads' cuts
    leave rounding in the samples (1e-16 of copper); those are not
    islands."""
    board = _board(
        layers=[Layer("TopLayer", "signal", 0), Layer("InternalPlane1", "plane", 1, "GND"),
                Layer("BottomLayer", "signal", 2)],
        regions=[Region("BottomLayer", list(OUTLINE), net="GND", kind="pour")],
        tracks=[Track("TopLayer", 100, 225, 900, 225, 10.0, "A")],
        vias=[Via(380 + 40 * i, 180 + 40 * j, 18, 8, "TopLayer", "BottomLayer", "S")
              for i in range(4) for j in range(4)])
    plain = rasterize(board, cell_mils=20, links=True, supersample=0)
    isl = rasterize(board, cell_mils=20, links=True, supersample=0, islands=True)
    assert not plain.islands and set(isl.islands) == set(isl.layers)
    assert len(isl.islands["InternalPlane1"].label) == 0
    assert len(isl.islands["BottomLayer"].label) == 0
    for layer in ("InternalPlane1", "BottomLayer"):
        assert np.array_equal(isl.slice_x[layer], plain.slice_x[layer])
        assert np.array_equal(isl.slice_y[layer], plain.slice_y[layer])
        assert isl.islands[layer].fill_main == pytest.approx(isl.fill[layer], abs=1e-12)
        got = isl.link_conductivity(layer, 390.0, 0.3)
        want = plain.link_conductivity(layer, 390.0, 0.3)
        assert got[0] == pytest.approx(want[0]) and got[1] == pytest.approx(want[1])


def test_a_pad_that_stops_short_of_the_cell_centres_is_still_one_piece_of_copper():
    """A 32 mil pad across the border of two 50 mil cells reaches neither
    centre. Measured from centre to centre the way would begin and end in
    laminate and the two halves would not be joined; with islands the
    node is the copper, and the path starts where the copper does. A
    clearance in the way is still a cut."""
    pad = _board(pads=[Pad("U1", "1", 500.0, 225.0, net="A",
                           copper=[PadCopper("TopLayer", "rect", 32.0, 30.0)])])
    r = rasterize(pad, cell_mils=50, links=True, supersample=0, islands=True)
    kx, _ = r.link_conductivity("TopLayer", 1.0, 0.0)
    assert kx[4, 9] == pytest.approx(30 / 32, rel=0.02)        # a strip 30 wide and 32 long
    assert np.count_nonzero(kx) == 1
    old, _ = rasterize(pad, cell_mils=50, links=True, supersample=0).link_conductivity(
        "TopLayer", 1.0, 0.0)
    assert old[4, 9] == 0.0
    two = _board(pads=[Pad("U1", "1", 486.0, 225.0, net="A",
                           copper=[PadCopper("TopLayer", "rect", 20.0, 30.0)]),
                       Pad("U1", "2", 514.0, 225.0, net="B",
                           copper=[PadCopper("TopLayer", "rect", 20.0, 30.0)])])
    r2 = rasterize(two, cell_mils=50, links=True, supersample=0, islands=True)
    kx2, _ = r2.link_conductivity("TopLayer", 1.0, 0.0)
    assert not kx2.any()                       # 8 mil between them, right on the border
    assert len(r2.islands["TopLayer"].label) == 0


def test_a_piece_lying_in_the_way_adds_nothing_to_the_main_islands_link():
    """A 20 mil pad in a 28 mil hole of a pour, on the border of two
    50 mil cells: the pour passes the hole with 22 of its 50 mil for 28
    of the 50 mil between the cell centres. The pad's copper in that
    stretch is not the pour's and carries none of the pour's heat."""
    board = _pad_in_pour(pad=(450.0, 225.0, 20.0, 20.0), clear=4.0)
    r = rasterize(board, cell_mils=50, links=True, supersample=0, islands=True)
    assert len(r.islands["TopLayer"].label) == 2                 # the pad, a piece in both cells
    kx, _ = r.link_conductivity("TopLayer", 1.0, 0.0)
    assert kx[4, 8] == pytest.approx(50 / (22 + 28 / (22 / 50)), rel=0.03)
    assert kx[4, 7] == pytest.approx(1.0) and kx[3, 8] == pytest.approx(1.0)
