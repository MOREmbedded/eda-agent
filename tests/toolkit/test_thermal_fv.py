# SPDX-License-Identifier: Apache-2.0
"""The trace thermal model: energy balance, limits and the right trends."""
from __future__ import annotations

import math

import pytest

from eda_agent.engineering.toolkit.thermal_fv import (
    Board, Plane, current_for_rise, solve_trace, width_for_current,
)
from eda_agent.thermal import physics as tp

MIL = 25.4e-6
OZ = 1.378 * MIL


def _board(**kw):
    base = dict(thickness=1.6e-3, width=0.1, length=0.1, mask=0.0)
    base.update(kw)
    return Board(**base)


def test_full_width_heater_matches_the_one_dimensional_balance():
    # A copper sheet across the whole board is a uniform heater: every
    # watt leaves through the two faces, so the rise follows from the
    # same film coefficients by hand.
    b = _board(width=0.05, length=0.2, thickness=0.2e-3, k_xy=385.0, k_z=385.0)
    current = 30.0
    r = solve_trace(current, b.width, OZ, 0.0, b)
    lc = tp.plate_length(b.width * b.length, 2 * (b.width + b.length))
    t = r.t_trace
    h_top = tp.h_convection(t, 25.0, lc, "up") + tp.h_radiation(t, 25.0, b.emissivity)
    h_bot = tp.h_convection(t, 25.0, lc, "down") + tp.h_radiation(t, 25.0, b.emissivity)
    expected = r.power_per_m / (b.width * (h_top + h_bot))
    assert r.rise == pytest.approx(expected, rel=0.02)
    assert r.power_per_m == pytest.approx(
        current ** 2 * tp.rho_copper(t) / (b.width * OZ), rel=1e-6)


def test_rise_scales_with_current_squared_at_small_rise():
    b = _board()
    r1 = solve_trace(0.2, 20 * MIL, OZ, 0.0, b)
    r2 = solve_trace(0.4, 20 * MIL, OZ, 0.0, b)
    # Slightly under 4: the film coefficients grow with the rise.
    assert 3.3 < r2.rise / r1.rise < 4.05


def test_copper_resistance_rises_with_its_own_temperature():
    b = _board()
    cool = solve_trace(0.1, 10 * MIL, OZ, 0.0, b)
    hot = solve_trace(3.0, 10 * MIL, OZ, 0.0, b)
    assert hot.r_per_m / cool.r_per_m == pytest.approx(
        (1 + tp.ALPHA_COPPER * (hot.t_trace - 20)) / (1 + tp.ALPHA_COPPER * (cool.t_trace - 20)),
        rel=1e-3)


def test_a_plane_cools_the_trace_and_a_closer_plane_cools_it_more():
    bare = solve_trace(3.0, 20 * MIL, OZ, 0.0, _board()).rise
    far = solve_trace(3.0, 20 * MIL, OZ, 0.0,
                      _board(planes=[Plane(OZ + 40 * MIL, OZ)])).rise
    near = solve_trace(3.0, 20 * MIL, OZ, 0.0,
                       _board(planes=[Plane(OZ + 5 * MIL, OZ)])).rise
    assert near < far < bare
    assert near < 0.6 * bare      # IPC-2152: a close plane is the biggest lever


def test_trends_width_copper_airflow_laminate():
    b = _board()
    base = solve_trace(3.0, 20 * MIL, OZ, 0.0, b).rise
    assert solve_trace(3.0, 40 * MIL, OZ, 0.0, b).rise < base
    assert solve_trace(3.0, 20 * MIL, 2 * OZ, 0.0, b).rise < base
    assert solve_trace(3.0, 20 * MIL, OZ, 0.0, _board(airflow=2.0)).rise < base
    assert solve_trace(3.0, 20 * MIL, OZ, 0.0, _board(k_xy=0.25, k_z=0.25)).rise > base
    assert solve_trace(3.0, 20 * MIL, OZ, 0.0, _board(ambient=85.0)).rise > 0


def test_an_inner_trace_runs_close_to_an_outer_one():
    # IPC-2152's finding against IPC-2221: inner traces are not twice as
    # bad; the laminate conducts better than still air.
    b = _board()
    outer = solve_trace(2.0, 20 * MIL, OZ, 0.0, b).rise
    inner = solve_trace(2.0, 20 * MIL, OZ, 0.4e-3, b).rise
    assert 0.6 * outer < inner < 1.3 * outer


def test_within_the_band_of_the_ipc_charts_for_a_plain_board():
    # 1 oz, 10 C rise, 70 mil FR-4, no planes. IPC-2221's external curve
    # gives 0.048 * 10^0.44 * A^0.725; IPC-2152 lands within about +/-40 %
    # of it over this range. The physics model must be in the same band.
    b = _board(thickness=70 * MIL, width=0.075, length=0.3)
    for w_mil in (10, 30, 100):
        i_model = current_for_rise(10.0, w_mil * MIL, OZ, 0.0, b)
        i_2221 = 0.048 * 10 ** 0.44 * (w_mil * 1.378) ** 0.725
        assert 0.6 < i_model / i_2221 < 1.9


def test_inverse_solves_round_trip():
    b = _board()
    i = current_for_rise(20.0, 25 * MIL, OZ, 0.0, b)
    assert solve_trace(i, 25 * MIL, OZ, 0.0, b).rise == pytest.approx(20.0, rel=0.005)
    w = width_for_current(i, 20.0, OZ, 0.0, b)
    assert w == pytest.approx(25 * MIL, rel=0.01)


def test_runaway_is_reported_not_returned_as_a_number():
    r = solve_trace(200.0, 5 * MIL, OZ, 0.0, _board())
    assert r.runaway


def test_grid_independence():
    b = _board()
    coarse = solve_trace(3.0, 20 * MIL, OZ, 0.0, b, resolution=3.0).rise
    fine = solve_trace(3.0, 20 * MIL, OZ, 0.0, b, resolution=8.0).rise
    assert coarse == pytest.approx(fine, rel=0.01)


def test_a_trace_outside_the_board_is_refused():
    with pytest.raises(ValueError):
        solve_trace(1.0, 20 * MIL, OZ, 2e-3, _board())
    assert math.isfinite(solve_trace(0.0, 20 * MIL, OZ, 0.0, _board()).rise)
