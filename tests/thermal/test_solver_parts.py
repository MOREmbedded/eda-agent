# SPDX-License-Identifier: Apache-2.0
"""The board solver's parts, vias on planes, clamps, copper direction,
grid independence and input checks."""

from __future__ import annotations

import numpy as np
import pytest

from eda_agent.layout.copper_raster import rasterize
from eda_agent.layout.model import Pad, PadCopper, Region, Track, Via
from eda_agent.thermal import physics as ph
from eda_agent.thermal.solver import (
    Clamp, HeatArea, Package, Setup, match_powers, solve,
)

from .boards import (
    add_chip, add_leaded, add_qfn, demo_board, four_layer, pour_round_pads, rect, two_layer,
)

PLAIN = dict(radiation=False, edges=False, solder_mask=False)


def _solve(board, stack, cell=25.0, **kw):
    """Solved as the tool solves it: copper sampled at 2 mil (supersample=0)."""
    return solve(board, stack, rasterize(board, cell_mils=cell, links=True, supersample=0, islands=True), **kw)


def _part(res, ref):
    return next(p for p in res.parts if p["ref"] == ref)


# -- parts -------------------------------------------------------------------

def test_theta_jb_adds_power_times_theta_above_the_pads():
    board, stack = four_layer(1000, 1000)
    add_qfn(board, "U1", 500, 500)
    bare = _part(_solve(board, stack, powers={"U1": 1.2}), "U1")
    assert "t_junction_c" not in bare           # no theta_jb: board side only
    res = _solve(board, stack, powers={"U1": 1.2},
                 packages={"U1": Package(theta_jb=9.0, tj_max_c=125.0)})
    p = _part(res, "U1")
    assert p["t_junction_c"] - p["t_pads_c"] == pytest.approx(1.2 * 9.0, abs=2e-3)
    # Without theta_jb the pads are tied into one isothermal source; with it
    # the heat is shared by pad area. The mean pad temperature barely moves
    # (0.4 K on a 58 K rise).
    assert p["t_pads_c"] == pytest.approx(bare["t_pads_c"], abs=0.6)
    assert p["margin_k"] == pytest.approx(125.0 - p["t_junction_c"], abs=1e-3)
    assert res.balance["error_pct"] < 1e-5


def test_case_path_carries_part_of_the_heat_and_a_heatsink_more():
    board, stack = four_layer(1000, 1000)
    add_qfn(board, "U1", 500, 500)
    base = _part(_solve(board, stack, powers={"U1": 2.0},
                        packages={"U1": Package(theta_jb=9.0)}), "U1")
    res = _solve(board, stack, powers={"U1": 2.0},
                 packages={"U1": Package(theta_jb=9.0, theta_jc=15.0)})
    p = _part(res, "U1")
    assert p["t_junction_c"] < base["t_junction_c"]
    assert p["t_pads_c"] < p["t_case_c"] < p["t_junction_c"]
    case_w = (p["t_junction_c"] - p["t_case_c"]) / 15.0
    board_w = (p["t_junction_c"] - p["t_pads_c"]) / 9.0
    assert case_w + board_w == pytest.approx(2.0, abs=2e-3)
    b = res.balance
    assert b["part_cases_convection_w"] + b["part_cases_radiation_w"] == pytest.approx(case_w, abs=2e-3)

    sink = _solve(board, stack, powers={"U1": 2.0},
                  packages={"U1": Package(theta_jb=9.0, theta_jc=15.0, heatsink_theta_sa=20.0)})
    ps = _part(sink, "U1")
    assert ps["t_junction_c"] < p["t_junction_c"] - 5.0
    assert sink.balance["heatsinks_w"] == pytest.approx((ps["t_case_c"] - 25.0) / 20.0, abs=2e-3)
    assert sink.balance["error_pct"] < 1e-5


def test_bottom_side_part_heats_the_bottom_layer_first():
    board, stack = two_layer(1000, 1000, pours=False)
    add_chip(board, "R1", 500, 500, side="bottom")
    res = _solve(board, stack, powers={"R1": 0.3})
    assert np.nanmax(res.temps["BottomLayer"]) > np.nanmax(res.temps["TopLayer"]) + 1.0
    assert _part(res, "R1")["side"] == "bottom"


def test_power_globs_and_exact_names():
    board, _ = demo_board()
    got = match_powers(board, {"R*": 0.1, "R2": 0.5, "U1": 1.0, "C5": 0.0})
    assert got == {"R1": 0.1, "R2": 0.5, "R3": 0.1, "U1": 1.0}
    with pytest.raises(ValueError, match="no component 'U9'"):
        match_powers(board, {"U9": 1.0})
    with pytest.raises(ValueError, match="matches"):
        match_powers(board, {"Q*": 1.0})
    with pytest.raises(ValueError, match=">= 0"):
        match_powers(board, {"U1": -1.0})


def test_under_resolved_part_is_flagged():
    board, stack = two_layer(1000, 1000, pours=False)
    add_chip(board, "R1", 500, 500)
    res = _solve(board, stack, cell=50.0, powers={"R1": 0.2})
    assert _part(res, "R1").get("under_resolved") is True
    assert any("R1" in n and "smaller cell_mils" in n for n in res.notes)
    fine = _solve(board, stack, cell=10.0, powers={"R1": 0.2})
    assert "under_resolved" not in _part(fine, "R1")


# -- vias and planes -----------------------------------------------------------

def test_a_via_only_dumps_heat_into_a_plane_of_its_own_net():
    def pad_temp(net: str) -> float:
        board, stack = four_layer(1000, 1000)
        add_qfn(board, "U1", 500, 500, net=net)
        return _part(_solve(board, stack, cell=20.0, powers={"U1": 1.0}), "U1")["t_pads_c"]

    gnd, sig = pad_temp("GND"), pad_temp("SIG")
    # SIG vias pass both planes through antipads and only reach the bottom pour
    assert sig > gnd + 3.0
    board, stack = four_layer(1000, 1000)
    add_qfn(board, "U1", 500, 500, vias=0)
    none = _part(_solve(board, stack, cell=20.0, powers={"U1": 1.0}), "U1")["t_pads_c"]
    assert none > sig > gnd


def test_thermal_vias_grid_independent():
    """The sub-cell via and fringe models keep a part on thermal vias at
    the same temperature from 40 mil cells down to 10 (a single node per
    cell without them gave 67 to 105 C here)."""
    board, stack = four_layer(800, 800)
    add_qfn(board, "U1", 403, 403)
    temps = [_part(_solve(board, stack, cell=c, powers={"U1": 1.0}), "U1")["t_pads_c"]
             for c in (40.0, 20.0, 10.0)]
    rise = temps[-1] - 25.0
    assert max(temps) - min(temps) < 0.03 * rise


def test_pad_over_plane_grid_independent():
    """An exposed pad with no vias, 8 mil above a plane and deliberately
    off the cell lines: the fringe model keeps coarse grids within a few
    percent of the fine one (without it the 40 mil grid read 19 K low)."""
    board, stack = four_layer(800, 800)
    add_qfn(board, "U1", 403, 403, vias=0)
    temps = [_part(_solve(board, stack, cell=c, powers={"U1": 1.0}), "U1")["t_pads_c"]
             for c in (40.0, 20.0, 10.0)]
    rise = temps[-1] - 25.0
    assert max(temps) - min(temps) < 0.04 * rise


# -- copper direction ----------------------------------------------------------

BRIDGE_CORE = 4.0       # mils: thin, so the track carries nearly all the heat


def _bridge(gap: bool = False, cross: bool = False, width: float = 30.0, y: float = 150.0,
            cell: float = 50.0) -> float:
    """Heater at the left, chassis clamp at the right, one track between
    them on a thin bare laminate. Returns the heater's temperature rise."""
    board, stack = two_layer(2000, 300, core_mils=BRIDGE_CORE, pours=False)
    board.regions += [Region("TopLayer", rect(50, 50, 250, 250), net="H"),
                      Region("TopLayer", rect(1750, 50, 1950, 250), net="H")]
    if cross:
        board.tracks.append(Track("TopLayer", 1000, 20, 1000, 280, width, "X"))
    elif gap:
        board.tracks += [Track("TopLayer", 250, y, 990, y, width, "H"),
                         Track("TopLayer", 1030, y, 1750, y, width, "H")]
    else:
        board.tracks.append(Track("TopLayer", 250, y, 1750, y, width, "H"))
    res = _solve(board, stack, cell=cell,
                 setup=Setup(h_top=0.0, h_bottom=0.0, **PLAIN),
                 heat_areas=[HeatArea("TopLayer", 50, 50, 250, 250, 0.2)],
                 clamps=[Clamp(1750, 50, 1950, 250, 25.0, side="top", h_contact=1e5)])
    assert res.balance["clamps_w"] == pytest.approx(0.2, rel=1e-6)
    return res.at("TopLayer", 150, 150) - 25.0


def _bridge_rise(width: float) -> float:
    """0.2 W through 1500 mil of track and the laminate beside and under
    it, side by side: L / (k_cu w t + k_xy (rest of the section))."""
    k_xy = ph.DIELECTRICS["fr4"].k_xy
    section = (ph.K_COPPER * width * 1.4
               + k_xy * (300 * BRIDGE_CORE + (300 - width) * 1.4 + 300 * 1.4))   # W/m/K mil^2
    return 0.2 * 1500 / (section * ph.MIL)


@pytest.mark.parametrize("width,y,cell", [(30.0, 150.0, 50.0), (30.0, 163.0, 50.0),
                                          (12.0, 141.0, 50.0), (30.0, 150.0, 20.0),
                                          (8.0, 157.0, 25.0)])
def test_a_track_conducts_with_its_own_width(width, y, cell):
    """Whatever the cell size and wherever the track lies in its cells,
    it conducts as copper of its width (centre sampling made a 30 mil
    track 25 mil wide on 50 mil cells, 17 % off)."""
    assert _bridge(width=width, y=y, cell=cell) == pytest.approx(_bridge_rise(width), rel=0.03)


def test_a_gap_or_a_crossing_track_does_not_bridge():
    whole = _bridge()
    # a 10 mil break (pen ends are round), narrower than a sample: the heat
    # has to go round through the laminate
    assert _bridge(gap=True) > 1.3 * whole
    # a track lying across the path bridges nothing
    assert _bridge(cross=True) > 5.0 * whole


# -- clamps and sources --------------------------------------------------------

def test_clamp_takes_the_heat_and_holds_its_temperature():
    board, stack = two_layer(1000, 600)
    res = _solve(board, stack, heat_areas=[HeatArea("TopLayer", 100, 200, 300, 400, 1.0)],
                 clamps=[Clamp(800, 0, 1000, 600, 40.0)],
                 setup=Setup(h_top=0.0, h_bottom=0.0, **PLAIN))
    assert res.balance["clamps_w"] == pytest.approx(1.0, rel=1e-6)
    assert res.balance["error_pct"] < 1e-5
    t = res.temps["BottomLayer"]
    assert 40.0 < np.nanmin(t) < 41.5
    assert res.at("TopLayer", 200, 300) > res.at("TopLayer", 600, 300) > res.at("TopLayer", 900, 300)
    miss = _solve(board, stack, heat_areas=[HeatArea("TopLayer", 100, 200, 300, 400, 1.0)],
                  clamps=[Clamp(5000, 5000, 5100, 5100, 40.0)])
    assert any("covers no board cell" in n for n in miss.notes)


def test_extra_heat_map_adds_to_the_part_powers():
    board, stack = two_layer(1000, 1000)
    add_chip(board, "R1", 500, 500)
    r = rasterize(board, cell_mils=50, links=True, supersample=0, islands=True)
    joule = np.zeros((r.ny, r.nx))
    joule[2:6, 2:6] = 0.5 / 16
    res = solve(board, stack, r, powers={"R1": 0.25}, extra_heat_w={"BottomLayer": joule})
    assert res.balance["power_in_w"] == pytest.approx(0.75)
    assert res.balance["error_pct"] < 1e-5
    with pytest.raises(ValueError, match="grid is"):
        solve(board, stack, r, extra_heat_w={"TopLayer": np.zeros((3, 3))})
    with pytest.raises(ValueError, match="unknown layer"):
        solve(board, stack, r, extra_heat_w={"MidLayer7": joule})


def test_symmetric_board_gives_a_symmetric_field():
    board, stack = two_layer(1000, 1000)
    res = _solve(board, stack, cell=50.0,
                 heat_areas=[HeatArea("TopLayer", 400, 400, 600, 600, 1.0)])
    t = res.temps["TopLayer"]
    assert np.allclose(t, t[::-1, :], atol=1e-6)
    assert np.allclose(t, t[:, ::-1], atol=1e-6)
    assert np.allclose(t, t.T, atol=1e-6)


def test_cutout_cells_are_outside_the_solution():
    board, stack = two_layer(1000, 1000)
    board.cutouts.append(rect(400, 400, 600, 600))
    res = _solve(board, stack, cell=50.0, heat_areas=[HeatArea("TopLayer", 100, 100, 300, 300, 0.5)])
    assert np.isnan(res.at("TopLayer", 500, 500))
    assert res.balance["error_pct"] < 1e-5
    # the hole's walls are edges that lose heat too
    assert res.balance["edges_convection_w"] > 0


# -- surroundings --------------------------------------------------------------

def test_orientation_and_altitude_move_the_result_the_right_way():
    board, stack = demo_board()
    powers = {"U1": 1.0, "U2": 1.5}

    def peak(**kw) -> float:
        return _solve(board, stack, cell=50.0, powers=powers, setup=Setup(**kw)).t_max_c

    flat = peak()
    # Flat, the top face convects well and the bottom poorly; upright both
    # are middling and the upper part sits in warmed air; face down the
    # planes still carry the heat to the face that now looks up. All three
    # come out within a few kelvin on a board this size.
    assert peak(orientation="vertical") == pytest.approx(flat, abs=4.0)
    assert peak(orientation="inverted") == pytest.approx(flat, abs=1.0)
    assert peak(orientation="vertical", up_deg=0.0) > peak(orientation="vertical")  # 3 in tall, not 2
    assert peak(pressure_pa=70_000.0) > flat + 0.5       # 3000 m: thinner air
    assert peak(airflow_ms=2.0) < flat - 5.0
    assert peak(radiation=False) > flat + 5.0
    assert peak(emissivity=0.1) > flat + 3.0
    assert peak(ambient_c=50.0) > flat + 20.0
    assert peak(convection="mean") == pytest.approx(flat, abs=1.0)
    assert peak(dielectric="rogers_4350b") < flat - 1.0  # k_z 0.69 against 0.29


def test_bad_inputs_are_refused():
    board, stack = demo_board()
    r = rasterize(board, cell_mils=100, links=True, supersample=0, islands=True)
    for bad in (dict(orientation="sideways"), dict(convection="magic"),
                dict(dielectric="cheese"), dict(emissivity=1.5), dict(k_z=0.0),
                dict(max_sublayer_mils=0.0)):
        with pytest.raises(ValueError):
            solve(board, stack, r, powers={"U1": 1.0}, setup=Setup(**bad))
    with pytest.raises(ValueError, match="no component"):
        solve(board, stack, r, powers={"U1": 1.0}, packages={"U7": Package(theta_jb=5)})
    with pytest.raises(ValueError, match="theta_jb"):
        solve(board, stack, r, powers={"U1": 1.0}, packages={"U1": Package(theta_jb=-1)})
    with pytest.raises(ValueError, match="outside the board"):
        solve(board, stack, r, heat_areas=[HeatArea("TopLayer", 9000, 9000, 9100, 9100, 1.0)])
    with pytest.raises(ValueError, match="unknown layer"):
        solve(board, stack, r, heat_areas=[HeatArea("Nope", 0, 0, 100, 100, 1.0)])
    with pytest.raises(ValueError, match="side"):
        solve(board, stack, r, powers={"U1": 1.0}, clamps=[Clamp(0, 0, 100, 100, 25.0, side="left")])


def test_raster_without_link_slices_still_solves_and_says_so():
    board, stack = demo_board()
    res = solve(board, stack, rasterize(board, cell_mils=100), powers={"U1": 1.0})
    assert any("no link slices" in n for n in res.notes)
    assert res.balance["error_pct"] < 1e-5


def test_direct_solver_fallback_when_pyamg_is_missing(monkeypatch):
    import builtins

    import eda_agent.thermal.solver as solver

    board, stack = demo_board()
    r = rasterize(board, cell_mils=50, links=True, supersample=0, islands=True)
    ref = solve(board, stack, r, powers={"U1": 1.0, "U2": 1.5})
    assert ref.solver["linear_solver"] == "amg-cg"
    real_import = builtins.__import__

    def no_pyamg(name, *a, **kw):
        if name == "pyamg":
            raise ImportError("no pyamg")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_pyamg)
    res = solve(board, stack, r, powers={"U1": 1.0, "U2": 1.5})
    assert res.solver["linear_solver"] == "superlu"
    assert any("pyamg is not installed" in n for n in res.notes)
    assert res.t_max_c == pytest.approx(ref.t_max_c, abs=1e-4)
    assert solver.AMG_MIN_UNKNOWNS < res.solver["unknowns"]


def test_no_false_peak_in_the_drill_holes_under_a_pad():
    """On a grid fine enough to see the drill holes of a via-in-pad, the
    cells inside a hole have no copper and must take none of the pad's
    heat: the copper round them does not reach in. A junction feeding
    them through theta_jb would lift them several kelvin over the pad
    (6.4 K here), a false peak. The via centres sit on cell centres, so
    each hole holds a cell with no copper at all."""
    board, stack = four_layer(500, 500)
    add_qfn(board, "U1", 252.5, 247.5)
    res = _solve(board, stack, cell=5.0, powers={"U1": 1.0},
                 packages={"U1": Package(theta_jb=12.0)})
    p = _part(res, "U1")
    assert res.t_max_c - p["t_pads_c"] < 1.0
    assert p["t_pads_max_c"] - p["t_pads_c"] < 1.0
    assert p["t_junction_c"] == pytest.approx(p["t_pads_c"] + 12.0, abs=0.01)


# -- heat through the board and past its inner layers ---------------------------

def _three_layer(kind, w=400.0):
    """Top and bottom bare, a middle layer that is bare, solid, or 5 mil
    tracks at 25 mil pitch (a fifth copper); 8 mil of laminate each side."""
    from eda_agent.emc.stack import stack_from_stackup
    from eda_agent.layout.model import Layer, LayoutBoard
    layers = [Layer("TopLayer", "signal", 0), Layer("MidLayer1", "signal", 1),
              Layer("BottomLayer", "signal", 2)]
    board = LayoutBoard(name="t.PcbDoc", outline=rect(0, 0, w, w), layers=layers)
    if kind == "tracks":
        for k in range(int(w // 25)):
            y = 12.5 + 25.0 * k
            board.tracks.append(Track("MidLayer1", 5, y, w - 5, y, 5.0, f"N{k}"))
    elif kind == "solid":
        board.regions.append(Region("MidLayer1", rect(0, 0, w, w), net="GND", kind="pour"))
    row = {"copper_thickness_mils": 1.4, "dielectric_type": "Core",
           "dielectric_height_mils": 8.0, "dielectric_constant": 4.2}
    stack = stack_from_stackup({"layers": [dict(row, layer_id=1), dict(row, layer_id=2),
                                           {"layer_id": 32, "copper_thickness_mils": 1.4}]},
                               layers)
    return board, stack


def _through_rise(kind, cell, w=400.0):
    """Mean rise of the top face over a bottom face held at 25 C, 1 W
    spread over the top."""
    board, stack = _three_layer(kind, w)
    setup = Setup(ambient_c=25.0, radiation=False, h_top=1e-6, h_bottom=1e-6, edges=False)
    res = solve(board, stack, rasterize(board, cell_mils=cell, links=True, supersample=0, islands=True),
                powers={}, setup=setup, heat_areas=[HeatArea("TopLayer", 0, 0, w, w, 1.0)],
                clamps=[Clamp(0, 0, w, w, 25.0, side="bottom", h_contact=1e6)])
    return float(np.nanmean(res.temps["TopLayer"])) - 25.0


def test_heat_crosses_a_partly_covered_inner_layer():
    """Heat on its way through the board passes an inner layer's tracks
    through the laminate between them. Cells that hold a track and bare
    laminate must let it: with the column cut down to the track's own
    cover the board read 42 % more resistance on 25 mil cells than on
    5 mil ones, which resolve the tracks."""
    bare, solid = _through_rise("none", 25.0), _through_rise("solid", 25.0)
    coarse, fine = _through_rise("tracks", 25.0), _through_rise("tracks", 5.0)
    assert solid < fine < bare                 # a fifth copper helps a little
    assert coarse == pytest.approx(fine, rel=0.05)
    assert solid < coarse < bare
    # no copper, or all copper: nothing to resolve, any cell gives the same
    assert _through_rise("none", 50.0) == pytest.approx(bare, rel=1e-6)
    assert _through_rise("solid", 50.0) == pytest.approx(solid, rel=1e-6)


def _small_pad_rise(kind, cell, size=20.0, at=203.0):
    """Rise of a part with one ``size`` mil pad, 0.1 W, over a bottom face
    held at 25 C; ``kind`` is what the middle layer 8 mil below holds."""
    from eda_agent.layout.model import Component
    board, stack = _three_layer(kind)
    board.components.append(Component("U1", x=at, y=at, side="top", height=20.0,
                                      courtyard=rect(at - 30, at - 30, at + 30, at + 30)))
    board.pads.append(Pad("U1", "1", at, at, net="A",
                          copper=[PadCopper("TopLayer", "rect", size, size)]))
    setup = Setup(ambient_c=25.0, radiation=False, h_top=1e-6, h_bottom=1e-6, edges=False)
    res = _solve(board, stack, cell=cell, powers={"U1": 0.1}, setup=setup,
                 clamps=[Clamp(0, 0, 400, 400, 25.0, side="bottom", h_contact=1e6)])
    return _part(res, "U1")["t_pads_c"] - 25.0


@pytest.mark.parametrize("kind,rel", [("none", 0.08), ("solid", 0.11), ("tracks", 0.08)])
def test_a_pad_far_smaller_than_a_cell_keeps_its_temperature(kind, rel):
    """A 20 mil pad in 50 mil cells, against 2.5 mil cells that resolve it
    and the fringe beside it: over bare laminate, over a plane 8 mil
    below, over an inner layer of tracks. The pad's heat crosses the gap
    in a tube of its own cover; with the laminate nodes held at the tube's
    temperature they leaked sideways as well and the pad read 32 % low
    over bare laminate and 21 % low over the plane."""
    fine = _small_pad_rise(kind, 2.5)
    assert _small_pad_rise(kind, 50.0) == pytest.approx(fine, rel=rel)
    assert _small_pad_rise(kind, 12.5) == pytest.approx(fine, rel=rel)


def test_a_via_land_sits_at_its_barrel_temperature_on_a_fine_grid():
    """A via's land is the barrel's own copper. On cells smaller than the
    land its cells lie inside the ring where the barrel meets the layer;
    untied, they floated 8 K over the laminate round them with heat
    spread over the top."""
    board, stack = four_layer(1200.0, 1200.0)
    board.vias.append(Via(580.0, 580.0, 18.0, 8.0, "TopLayer", "BottomLayer", "SIG"))
    res = _solve(board, stack, cell=10.0, powers={},
                 heat_areas=[HeatArea("TopLayer", 360, 360, 840, 840, 2.0)])
    top = res.temps["TopLayer"]
    land = top[57:59, 57:59]                   # the four cells round (580, 580)
    assert res.raster.fill["TopLayer"][57:59, 57:59].min() > 0.4
    round_it = np.concatenate([top[56, 56:60], top[59, 56:60], top[57:59, 56], top[57:59, 59]])
    assert land.max() < round_it.min() - 3.0   # the barrel draws the heat down
    assert land.max() - res.barrels[0]["t_top_c"] < 3.0


def _bga(cell):
    """2 W in the lands of a 12 x 12 field of signal vias at 40 mil pitch
    over two planes that every via passes through an antipad."""
    board, stack = four_layer(1200.0, 1200.0)
    for i in range(12):
        for j in range(12):
            board.vias.append(Via(380.0 + 40.0 * i, 380.0 + 40.0 * j, 18.0, 8.0,
                                  "TopLayer", "BottomLayer", f"S{i}_{j}"))
    return _solve(board, stack, cell=cell, powers={},
                  heat_areas=[HeatArea("TopLayer", 360, 360, 840, 840, 2.0)])


def test_a_via_field_reads_the_same_on_coarse_and_fine_cells():
    """A BGA's via field: lands on top, antipads in both planes, webs of
    12 mil between them. Cells of half the pitch and of a quarter must
    agree. They did not: heat put into cells that hold a land and bare
    laminate, the plane's webs read as cut, and columns choked by a
    sliver of plane each moved the peak by 10 K and more."""
    coarse, fine = _bga(20.0), _bga(10.0)
    assert coarse.t_max_c - 25.0 == pytest.approx(fine.t_max_c - 25.0, rel=0.02)
    for name in ("InternalPlane1", "InternalPlane2", "BottomLayer"):
        assert (np.nanmax(coarse.temps[name]) - 25.0
                == pytest.approx(np.nanmax(fine.temps[name]) - 25.0, rel=0.03))
    assert coarse.balance["error_pct"] < 1e-4 and fine.balance["error_pct"] < 1e-4


# ---------------------------------------------------------------------------
# Copper islands: a pad, a via and the pour round them in one cell
# ---------------------------------------------------------------------------

def _leaded(cell, islands=True, nets=None):
    """1 W in an eight-pin gull-wing part whose pads sit in 8 mil
    clearances of a top-layer pour, over a plane."""
    board, stack = four_layer(1000.0, 1000.0)
    add_leaded(board, "U1", 500.0, 500.0, nets=nets)
    pour_round_pads(board)
    raster = rasterize(board, cell_mils=cell, links=True, supersample=0, islands=islands)
    return solve(board, stack, raster, powers={"U1": 1.0},
                 packages={"U1": Package(theta_jb=10.0)})


@pytest.mark.parametrize("nets", [None, {"4": "GND"}], ids=["no pin on the pour", "pin 4 on it"])
def test_a_pad_in_a_pour_clearance_is_not_joined_to_the_pour(nets):
    """Found on a real board: a part's junction rose from 53 to 58 C as
    the cells went from 25 to 8 mil. A cell that holds a pad and the
    pour beyond its clearance was one node, so the pour took the pad's
    heat without it crossing the clearance. Each island of a cell is a
    node now, and cells of 50, 25 and 20 mil agree with cells that
    resolve the 8 mil clearance. Without the split they read the rise
    a third to a half low."""
    fine = _leaded(6.25).parts[0]["t_junction_c"] - 25.0
    for cell in (50.0, 25.0, 20.0):
        res = _leaded(cell, nets=nets) if nets else _leaded(cell)
        ref = _leaded(6.25, nets=nets).parts[0]["t_junction_c"] - 25.0 if nets else fine
        assert res.parts[0]["t_junction_c"] - 25.0 == pytest.approx(ref, rel=0.04)
        assert res.solver["island_pieces"] > 0
        assert res.balance["error_pct"] < 1e-4
    joined = _leaded(25.0, islands=False, nets=nets).parts[0]["t_junction_c"] - 25.0
    ref = _leaded(6.25, nets=nets).parts[0]["t_junction_c"] - 25.0
    assert joined < 0.75 * ref
    if nets:
        # the pin on the pour is a real way out: the part runs cooler with it
        assert ref < 0.85 * fine


def test_the_map_and_the_parts_read_each_island_of_a_cell():
    """On 50 mil cells a pad and the pour share cells. The part's pad
    temperature is its pads', the heat map shows the hottest copper of
    each cell (the pad), ``copper_at`` reads the island at a point, and
    a neighbour's pad on the pour reads the pour, not the hot pad."""
    board, stack = four_layer(1000.0, 1000.0)
    add_leaded(board, "U1", 500.0, 500.0)
    board.pads.append(Pad("C9", "1", 440.0, 353.0, net="GND",
                          copper=[PadCopper("TopLayer", "rect", 6.0, 6.0)]))
    from eda_agent.layout.model import Component
    board.components.append(Component("C9", x=440.0, y=353.0, courtyard=rect(435, 350, 445, 356)))
    pour_round_pads(board)
    raster = rasterize(board, cell_mils=50.0, links=True, supersample=0, islands=True)
    res = solve(board, stack, raster, powers={"U1": 1.0}, report_parts=["C9"])
    u1, c9 = _part(res, "U1"), _part(res, "C9")
    pad = board.pads[0]                                   # U1 pin 1 at (425, 395)
    assert raster.cell_of(pad.x, pad.y) == raster.cell_of(440.0, 353.0)
    t_pad = res.copper_at("TopLayer", pad.x, pad.y)
    t_pour = res.copper_at("TopLayer", 440.0, 353.0)
    assert t_pad == pytest.approx(u1["t_pads_c"], abs=0.05)         # pads tied: one temperature
    assert t_pad - t_pour > 15.0                                     # the clearance holds
    assert c9["t_pads_c"] == pytest.approx(t_pour, abs=0.01)
    assert res.at("TopLayer", pad.x, pad.y) == pytest.approx(t_pad, abs=0.01)   # the map: hottest
    assert res.t_max_c == pytest.approx(u1["t_pads_max_c"], abs=0.01)
    ix, iy = raster.cell_of(pad.x, pad.y)
    # in this cell the pad holds the most copper: it is the cell's node, the pour a piece
    assert res.main_temps["TopLayer"][iy, ix] == pytest.approx(t_pad, abs=0.01)
    assert np.nanmin(np.abs(res.piece_temps["TopLayer"] - t_pour)) < 1e-9
    # no copper at a point of the clearance: the cell's own node answers
    assert res.copper_at("TopLayer", 440.0, 395.0) == pytest.approx(t_pad, abs=0.01)


def _vias_in_a_pour(cell, islands=True):
    """2 W in a top pour that 25 vias of another net pass through, each
    in its clearance, down to that net's pour on the bottom."""
    board, stack = two_layer(1000.0, 1000.0, pours=False)
    holes = []
    for i in range(5):
        for j in range(5):
            x, y = 380.0 + 60 * i, 380.0 + 60 * j
            board.vias.append(Via(x, y, 20.0, 10.0, "TopLayer", "BottomLayer", "S"))
            holes.append([(x + 18 * np.cos(a * np.pi / 8), y + 18 * np.sin(a * np.pi / 8))
                          for a in range(16)])
    board.regions.append(Region("TopLayer", rect(0, 0, 1000, 1000), holes=holes, net="GND",
                                kind="pour"))
    board.regions.append(Region("BottomLayer", rect(0, 0, 1000, 1000), net="S", kind="pour"))
    raster = rasterize(board, cell_mils=cell, links=True, supersample=0, islands=islands)
    return solve(board, stack, raster,
                 heat_areas=[HeatArea("TopLayer", 350, 350, 650, 650, 2.0)])


def test_a_via_in_another_nets_pour_stays_apart_from_it():
    """A via's land in a clearance of a pour: on cells larger than the
    clearance the barrel was tied to the pour's cells and carried the
    pour's heat to the other side. It joins its own island now; cells of
    30 and 20 mil agree with 5 mil, where they read the rise 8 % low."""
    fine = _vias_in_a_pour(5.0)
    for cell in (30.0, 20.0):
        res = _vias_in_a_pour(cell)
        assert res.t_max_c - 25.0 == pytest.approx(fine.t_max_c - 25.0, rel=0.02)
        assert res.barrels[12]["t_top_c"] - 25.0 == pytest.approx(
            fine.barrels[12]["t_top_c"] - 25.0, rel=0.03)
        assert res.balance["error_pct"] < 1e-4
    joined = _vias_in_a_pour(30.0, islands=False)
    assert joined.t_max_c - 25.0 < 0.93 * (fine.t_max_c - 25.0)
    assert joined.barrels[12]["t_top_c"] > fine.barrels[12]["t_top_c"] + 3.0


def test_a_heat_area_heats_every_island_under_it_by_its_copper():
    """A rectangle over a pour and the pads in its clearances: each
    island takes heat by its copper inside, all of it arrives, and the
    pads, which only the laminate cools, end hotter than the pour."""
    board, stack = four_layer(1000.0, 1000.0)
    add_leaded(board, "U1", 500.0, 500.0)
    pour_round_pads(board)
    raster = rasterize(board, cell_mils=50.0, links=True, supersample=0, islands=True)
    res = solve(board, stack, raster, heat_areas=[HeatArea("TopLayer", 350, 350, 650, 650, 1.0)],
                report_parts=["U1"])
    assert res.balance["power_in_w"] == pytest.approx(1.0, abs=1e-9)
    assert res.balance["error_pct"] < 1e-4
    t_pad = res.copper_at("TopLayer", 425.0, 395.0)
    assert t_pad > res.copper_at("TopLayer", 500.0, 500.0) + 0.5
    assert _part(res, "U1")["t_pads_c"] == pytest.approx(t_pad, abs=1.0)
