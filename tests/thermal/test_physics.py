# SPDX-License-Identifier: Apache-2.0
"""thermal.physics against tabulated data and hand calculations."""

from __future__ import annotations

import math

import pytest

from eda_agent.thermal import physics as ph


def test_air_matches_reference_data_at_300k():
    # NIST / Lemmon et al. dry air at 1 atm: rho 1.177 kg/m3 (Incropera's
    # table A.4 lists 1.1614, below the ideal-gas value; NIST is used here)
    a = ph.air(300.0)
    assert a.rho == pytest.approx(1.177, rel=0.002)
    assert a.mu == pytest.approx(184.6e-7, rel=0.01)
    assert a.k == pytest.approx(26.3e-3, rel=0.015)
    assert a.pr == pytest.approx(0.707, rel=0.015)


def test_air_at_350k():
    a = ph.air(350.0)
    assert a.mu == pytest.approx(208.2e-7, rel=0.01)
    assert a.k == pytest.approx(30.0e-3, rel=0.02)
    assert a.nu == pytest.approx(208.2e-7 / 1.0085, rel=0.02)


def test_lloyd_moran_hand_calc_hot_plate_up():
    # 100 mm square, A/P = 25 mm, 55 C surface, 25 C air
    L = ph.plate_length(0.01, 0.4)
    assert L == pytest.approx(0.025)
    film = ph.air(273.15 + 40)
    ra = 9.80665 * (1 / 298.15) * 30 * L ** 3 / (film.nu * film.alpha)
    h_hand = 0.54 * ra ** 0.25 * film.k / L
    h = ph.h_convection(55, 25, L, "up")
    assert h == pytest.approx(h_hand, rel=1e-9)
    assert 6.5 < h < 9.5           # the textbook range for a small hot board


def test_down_facing_is_weaker_than_up_and_vertical_in_between_order():
    L = 0.025
    up = ph.h_convection(60, 25, L, "up")
    down = ph.h_convection(60, 25, L, "down")
    assert down < up
    assert ph.h_convection(60, 25, 0.1, "vertical") > 3.0


def test_churchill_chu_reference_value():
    # Churchill-Chu at Ra=1e9, Pr=0.71 gives Nu ~ 121 (Incropera Ex. 9.2 order)
    nu = ph.nusselt_natural(1e9, 0.71, "vertical")
    assert nu == pytest.approx(121.0, rel=0.02)


def test_forced_flow_raises_h_and_matches_laminar_formula():
    L = 0.1
    h = ph.h_convection(50, 25, L, "up", velocity_ms=2.0, flow_length_m=L)
    film = ph.air(273.15 + 37.5)
    re = 2.0 * L / film.nu
    hf = 0.664 * re ** 0.5 * film.pr ** (1 / 3) * film.k / L
    assert h > hf               # combined with natural
    assert h == pytest.approx(hf, rel=0.05)  # forced dominates at 2 m/s


def test_radiation_linearisation_is_exact_for_the_flux():
    hr = ph.h_radiation(85, 25, 0.9)
    q_exact = 0.9 * ph.SIGMA * ((85 + 273.15) ** 4 - (25 + 273.15) ** 4)
    assert hr * 60 == pytest.approx(q_exact, rel=1e-12)


def test_barrel_resistance_hand_calc():
    # 0.3 mm hole, 25 um plating, 1.6 mm board
    r = ph.barrel_resistance(0.3e-3, 1.6e-3)
    a = math.pi / 4 * (0.3e-3 ** 2 - 0.25e-3 ** 2)
    assert r == pytest.approx(1.6e-3 / (385 * a), rel=1e-12)
    assert 190 < r < 200          # the well-known ~190 K/W of a 12 mil via
    assert ph.barrel_resistance(0.3e-3, 1.6e-3, fill="copper") < r / 2
    assert ph.barrel_resistance(0, 1e-3) == math.inf


def test_copper_resistivity_temperature():
    assert ph.rho_copper(20) == ph.RHO_COPPER_20C
    assert ph.rho_copper(120) == pytest.approx(1.724e-8 * 1.393)
