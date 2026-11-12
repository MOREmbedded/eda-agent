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

from .boards import add_chip, add_qfn, demo_board, four_layer, rect, two_layer

PLAIN = dict(radiation=False, edges=False, solder_mask=False)


def _solve(board, stack, cell=25.0, **kw):
    return solve(board, stack, rasterize(board, cell_mils=cell, links=True), **kw)


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
    # the heat is shared by pad area. The mean pad temperature barely moves.
    assert p["t_pads_c"] == pytest.approx(bare["t_pads_c"], abs=0.3)
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
    r = rasterize(board, cell_mils=50, links=True)
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
    r = rasterize(board, cell_mils=100, links=True)
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
    r = rasterize(board, cell_mils=50, links=True)
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
