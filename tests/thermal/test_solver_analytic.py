# SPDX-License-Identifier: Apache-2.0
"""The board solver against closed-form heat-conduction solutions.

Each test builds a board whose temperature field is known exactly and
compares: a layered slab (series resistances), a thin fin with a point
source (modified Bessel K0), a finite source on an anisotropic slab over
a film coefficient (the Fourier series of Yovanovich, Muzychka and
Culham), a plate losing heat by natural convection and T^4 radiation (a
scalar nonlinear balance), the Churchill-Chu and flat-plate averages on
a vertical and a ventilated board, and plated barrels in parallel.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy.optimize import brentq
from scipy.special import k0

from eda_agent.layout.copper_raster import rasterize
from eda_agent.layout.model import Via
from eda_agent.thermal import physics as ph
from eda_agent.thermal.solver import HeatArea, Setup, solve

from .boards import two_layer

MIL = ph.MIL
#: Conduction only: fixed film coefficients, no radiation, mask or edges.
PLAIN = dict(radiation=False, edges=False, solder_mask=False)


def _uniform(raster, watts: float) -> np.ndarray:
    return np.full((raster.ny, raster.nx), watts / (raster.nx * raster.ny))


def test_layered_slab_matches_series_resistances():
    """Uniform heat into the top copper, both faces cooled: two nodes in
    series through the core, solved by hand."""
    board, stack = two_layer(1000, 1000)
    r = rasterize(board, cell_mils=50, links=True, supersample=0)
    res = solve(board, stack, r, setup=Setup(h_top=10, h_bottom=10, **PLAIN),
                extra_heat_w={"TopLayer": _uniform(r, 1.0)})
    area = (1000 * MIL) ** 2
    q = 1.0 / area
    r_core = 1.4 * MIL / ph.K_COPPER + 60 * MIL / 0.29
    h = 10.0
    top = q / (h * (1 + 1 / (1 + h * r_core)))
    bottom = top / (1 + h * r_core)
    assert np.nanmax(res.temps["TopLayer"]) - 25 == pytest.approx(top, rel=1e-9)
    assert np.nanmin(res.temps["TopLayer"]) - 25 == pytest.approx(top, rel=1e-9)
    assert np.nanmax(res.temps["BottomLayer"]) - 25 == pytest.approx(bottom, rel=1e-9)
    assert res.balance["error_pct"] < 1e-6
    assert res.balance["top_convection_w"] == pytest.approx(h * top * area, rel=1e-6)


def test_point_source_on_a_fin_matches_bessel_k0():
    """A point source on a thin conducting sheet that convects from both
    faces: T(r) = Q / (2 pi k t) K0(r / L), L = sqrt(k t / h)."""
    board, stack = two_layer(12000, 12000, core_mils=4.0)
    r = rasterize(board, cell_mils=100, links=True, supersample=0)
    heat = np.zeros((r.ny, r.nx))
    c = r.nx // 2
    heat[c, c] = 1.0
    # no sideways conduction in the laminate: the two copper sheets are the fin
    res = solve(board, stack, r, extra_heat_w={"TopLayer": heat},
                setup=Setup(h_top=10, h_bottom=10, k_xy=1e-9, k_z=0.29, **PLAIN))
    kt = 2 * ph.K_COPPER * 1.4 * MIL
    length = math.sqrt(kt / 20.0)
    t = res.temps["BottomLayer"]
    for cells in (5, 10, 20):
        x = cells * 100 * MIL
        exact = k0(x / length) / (2 * math.pi * kt)
        assert t[c, c + cells] - 25 == pytest.approx(exact, rel=0.006)
        assert t[c + cells, c] - 25 == pytest.approx(exact, rel=0.006)
    assert res.solver["linear_solver"] == "amg-cg"
    assert res.balance["error_pct"] < 1e-6


def _spreading_series(a, b, c, d, t, k_xy, k_z, h, terms=300):
    """Mean and centre temperature rise per watt of a centred 2a x 2b
    uniform source on a 2c x 2d slab of thickness t, film coefficient h
    underneath, everything else adiabatic. Separation of variables:
    each mode cos(dm x) cos(ln y) decays through the slab with
    g = sqrt(k_xy / k_z) * sqrt(dm^2 + ln^2)."""
    def phi(beta):
        g = beta * math.sqrt(k_xy / k_z)
        e = np.tanh(g * t)
        return (g + (h / k_z) * e) / (g * e + h / k_z) / (k_z * g)

    m = np.arange(1, terms + 1)
    dm, ln = m * math.pi / c, m * math.pi / d
    sa, sb = np.sin(dm * a), np.sin(ln * b)
    area = 4 * c * d
    one_d = (t / k_z + 1 / h) / area
    # per watt: flux q = 1 / (4 a b)
    q = 1.0 / (4 * a * b)
    am = q * (b / d) * 2 * sa / (dm * c) * phi(dm)
    bn = q * (a / c) * 2 * sb / (ln * d) * phi(ln)
    beta = np.sqrt(dm[:, None] ** 2 + ln[None, :] ** 2)
    cmn = q * (2 * sa / (dm * c))[:, None] * (2 * sb / (ln * d))[None, :] * phi(beta)
    centre = one_d + am.sum() + bn.sum() + cmn.sum()
    mean = (one_d + (am * sa / (dm * a)).sum() + (bn * sb / (ln * b)).sum()
            + (cmn * (sa / (dm * a))[:, None] * (sb / (ln * b))[None, :]).sum())
    return mean, centre


@pytest.mark.parametrize("cell,mean_tol,peak_tol", [(20.0, 0.025, 0.025), (10.0, 0.02, 0.005)])
def test_finite_source_on_anisotropic_slab_matches_fourier_series(cell, mean_tol, peak_tol):
    """Spreading resistance of a 200 x 100 mil source on a 1000 x 800 mil
    FR-4 slab, 60 mil thick, cooled from below. Heat put straight onto
    bare laminate is the hardest case for the grid (measured: mean +1.3 %,
    peak -0.01 % at 10 mil cells and 5 mil planes)."""
    board, stack = two_layer(1000, 800, copper_mils=0.05, pours=False)
    r = rasterize(board, cell_mils=cell, links=True, supersample=0)
    setup = Setup(h_top=0.0, h_bottom=500.0, max_sublayer_mils=5.0, **PLAIN)
    res = solve(board, stack, r, setup=setup,
                heat_areas=[HeatArea("TopLayer", 400, 350, 600, 450, 1.0)])
    kxy, kz = setup.laminate()
    mean, centre = _spreading_series(100 * MIL, 50 * MIL, 500 * MIL, 400 * MIL,
                                     60.1 * MIL, kxy, kz, 500.0)
    t = res.temps["TopLayer"] - 25.0
    src = t[int(350 / cell):int(450 / cell), int(400 / cell):int(600 / cell)]
    assert src.mean() == pytest.approx(mean, rel=mean_tol)
    assert np.nanmax(t) == pytest.approx(centre, rel=peak_tol)
    assert res.balance["bottom_convection_w"] == pytest.approx(1.0, rel=1e-6)


def _face_flux(t_node: float, ta: float, length: float, orient: str, eps: float,
               r_mask: float) -> float:
    """W/m^2 leaving a face whose copper is at ``t_node``: convection and
    radiation act on the mask's outer surface."""
    q = 0.0
    for _ in range(60):
        ts = t_node - q * r_mask
        q = (ph.h_natural(ts, ta, length, orient) + ph.h_radiation(ts, ta, eps)) * (ts - ta)
    return q


def test_natural_convection_and_radiation_balance():
    """Uniform heating of a pour-covered board in still air. The field is
    uniform in the plane, so two unknowns (top and bottom copper) settle
    a nonlinear balance that is solved here independently."""
    board, stack = two_layer(2000, 1500)
    r = rasterize(board, cell_mils=100, links=True, supersample=0)
    power = 2.0
    res = solve(board, stack, r, setup=Setup(edges=False, ambient_c=30.0),
                extra_heat_w={"TopLayer": _uniform(r, power)})
    area = 2000 * 1500 * MIL * MIL
    length = area / (2 * (2000 + 1500) * MIL)
    r_mask = ph.SOLDER_MASK_MILS * MIL / ph.K_SOLDER_MASK
    r_core = 1.4 * MIL / ph.K_COPPER + 60 * MIL / 0.29

    def residual(t_top: float) -> float:
        q_top = _face_flux(t_top, 30.0, length, "up", 0.9, r_mask)
        q_down = power / area - q_top          # what goes through the core
        t_bot = t_top - q_down * r_core
        return q_down - _face_flux(t_bot, 30.0, length, "down", 0.9, r_mask)

    t_top = brentq(residual, 30.01, 200.0, xtol=1e-10)
    t_bot = t_top - (power / area - _face_flux(t_top, 30.0, length, "up", 0.9, r_mask)) * r_core
    assert np.nanmax(res.temps["TopLayer"]) == pytest.approx(t_top, abs=2e-3)
    assert np.nanmin(res.temps["TopLayer"]) == pytest.approx(t_top, abs=2e-3)
    assert np.nanmax(res.temps["BottomLayer"]) == pytest.approx(t_bot, abs=2e-3)
    b = res.balance
    assert b["error_pct"] < 1e-5
    # radiation carries a real share of the heat at these temperatures
    rad = b["top_radiation_w"] + b["bottom_radiation_w"]
    assert 0.3 * power < rad < 0.7 * power
    assert res.h_used["top"]["convection_w_m2k_mean"] > res.h_used["bottom"]["convection_w_m2k_mean"]


def test_vertical_board_sums_to_churchill_chu_and_is_hotter_at_the_top():
    """Local coefficients on a vertical board: a nearly isothermal board
    loses exactly what the Churchill-Chu average says, and a board that
    conducts poorly runs hotter at its top edge."""
    board, stack = two_layer(1000, 2000, copper_mils=60.0, core_mils=20.0)
    r = rasterize(board, cell_mils=100, links=True, supersample=0)
    setup = Setup(orientation="vertical", **PLAIN)
    res = solve(board, stack, r, setup=setup, extra_heat_w={"TopLayer": _uniform(r, 1.5)})
    area = 2 * 1000 * 2000 * MIL * MIL
    height = 2000 * MIL
    t_iso = brentq(lambda t: ph.h_natural(t, 25.0, height, "vertical") * area * (t - 25.0) - 1.5,
                   25.01, 300.0)
    t = res.temps["TopLayer"]
    assert np.nanmean(t) == pytest.approx(t_iso, abs=0.02 * (t_iso - 25))
    assert res.h_used["top"]["faces"] == "vertical"
    # +y is up: the lowest row sees fresh air and the best coefficient
    assert res.h_used["top"]["convection_w_m2k_max"] > 1.5 * res.h_used["top"]["convection_w_m2k_min"]

    thin, thin_stack = two_layer(1000, 2000, pours=False)
    r2 = rasterize(thin, cell_mils=100, links=True, supersample=0)
    res2 = solve(thin, thin_stack, r2, setup=setup, extra_heat_w={"TopLayer": _uniform(r2, 1.5)})
    t2 = res2.temps["TopLayer"]
    assert t2[-1].mean() > t2[0].mean() + 5.0
    # turned upside down (up_deg 270: -y is up), the hot end swaps
    flipped = solve(thin, thin_stack, r2, extra_heat_w={"TopLayer": _uniform(r2, 1.5)},
                    setup=Setup(orientation="vertical", up_deg=270.0, **PLAIN))
    assert flipped.temps["TopLayer"][0].mean() == pytest.approx(t2[-1].mean(), rel=1e-6)
    # the mean model has one coefficient: a uniformly heated board is uniform
    mean = solve(thin, thin_stack, r2, extra_heat_w={"TopLayer": _uniform(r2, 1.5)},
                 setup=Setup(orientation="vertical", convection="mean", **PLAIN))
    tm = mean.temps["TopLayer"]
    assert tm[-1].mean() == pytest.approx(tm[0].mean(), abs=1e-6)


def test_forced_flow_sums_to_the_plate_average_and_heats_downstream():
    board, stack = two_layer(3000, 1000, copper_mils=60.0, core_mils=20.0)
    r = rasterize(board, cell_mils=100, links=True, supersample=0)
    setup = Setup(airflow_ms=3.0, airflow_deg=0.0, **PLAIN)
    res = solve(board, stack, r, setup=setup, extra_heat_w={"TopLayer": _uniform(r, 3.0)})
    area = 3000 * 1000 * MIL * MIL
    length = area / (2 * 4000 * MIL)
    flow = 3000 * MIL

    def lost(t: float) -> float:
        hf = ph.h_forced(t, 25.0, 3.0, flow)
        up = ph.h_mixed(ph.h_natural(t, 25.0, length, "up"), hf)
        down = ph.h_mixed(ph.h_natural(t, 25.0, length, "down"), hf)
        return (up + down) * area * (t - 25.0) - 3.0

    t_iso = brentq(lost, 25.01, 300.0)
    # the cube-rule mix of a local forced and a uniform natural part is not
    # exactly additive, hence the looser bound than on the vertical board
    assert np.nanmean(res.temps["TopLayer"]) == pytest.approx(t_iso, abs=0.04 * (t_iso - 25))

    thin, thin_stack = two_layer(3000, 1000, pours=False)
    r2 = rasterize(thin, cell_mils=100, links=True, supersample=0)
    res2 = solve(thin, thin_stack, r2, setup=setup, extra_heat_w={"TopLayer": _uniform(r2, 1.0)})
    t2 = res2.temps["TopLayer"]
    assert t2[:, -1].mean() > t2[:, 0].mean() + 3.0     # +x is downstream
    back = solve(thin, thin_stack, r2, extra_heat_w={"TopLayer": _uniform(r2, 1.0)},
                 setup=Setup(airflow_ms=3.0, airflow_deg=180.0, **PLAIN))
    assert back.temps["TopLayer"][:, 0].mean() == pytest.approx(t2[:, -1].mean(), rel=1e-6)
    still = solve(thin, thin_stack, r2, extra_heat_w={"TopLayer": _uniform(r2, 1.0)},
                  setup=Setup(**PLAIN))
    assert np.nanmax(still.temps["TopLayer"]) > np.nanmax(t2) + 10.0


@pytest.mark.parametrize("n", [0, 4, 16])
def test_plated_barrels_conduct_in_parallel_with_the_laminate(n):
    """Thick copper both sides (isothermal faces), heat in at the top, out
    at the bottom: the board's resistance is the laminate in parallel
    with n barrels of L / (k A)."""
    board, stack = two_layer(400, 400, copper_mils=40.0)
    side = int(math.isqrt(n))
    for i in range(side):
        for j in range(side):
            board.vias.append(Via(100 + i * 200 / max(side - 1, 1), 100 + j * 200 / max(side - 1, 1),
                                  24.0, 12.0, "TopLayer", "BottomLayer", "GND"))
    r = rasterize(board, cell_mils=20, links=True, supersample=0)
    res = solve(board, stack, r, setup=Setup(h_top=0.0, h_bottom=2000.0, **PLAIN),
                extra_heat_w={"TopLayer": _uniform(r, 1.0)})
    drop = np.nanmean(res.temps["TopLayer"]) - np.nanmean(res.temps["BottomLayer"])
    area = (400 * MIL) ** 2
    g_laminate = 1.0 / ((40 * MIL / ph.K_COPPER + 60 * MIL / 0.29) / area)
    g_barrel = ph.barrel_conductance(12 * MIL, (60 + 40) * MIL)
    assert drop == pytest.approx(1.0 / (g_laminate + n * g_barrel), rel=0.02)
    assert res.solver["plated_holes"] == n
    if n:
        # each barrel is hotter at the top than at the bottom
        assert all(b["t_top_c"] > b["t_bottom_c"] for b in res.barrels)


def test_via_fill_and_plating_change_the_barrel():
    board, stack = two_layer(400, 400, copper_mils=40.0)
    for x in (150.0, 250.0):
        for y in (150.0, 250.0):
            board.vias.append(Via(x, y, 24.0, 12.0, "TopLayer", "BottomLayer", "GND"))
    r = rasterize(board, cell_mils=20, links=True, supersample=0)

    def drop(**kw) -> float:
        res = solve(board, stack, r, setup=Setup(h_top=0.0, h_bottom=2000.0, **PLAIN, **kw),
                    extra_heat_w={"TopLayer": _uniform(r, 1.0)})
        return float(np.nanmean(res.temps["TopLayer"]) - np.nanmean(res.temps["BottomLayer"]))

    base = drop()
    assert drop(plating_um=50.0) < base * 0.9
    assert drop(via_fill="copper") < drop(via_fill="epoxy") < base
    with pytest.raises(ValueError):
        drop(via_fill="butter")
