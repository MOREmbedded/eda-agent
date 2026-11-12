# SPDX-License-Identifier: Apache-2.0
"""The closed forms and loss models against exact values and limits."""
from __future__ import annotations

import math

import pytest

from eda_agent.engineering.toolkit import tline
from eda_agent.engineering.toolkit.fdsolver import C0, EPS0, MU0


def test_constants():
    assert tline.ETA0 == pytest.approx(376.730313, rel=1e-8)
    assert 1 / math.sqrt(MU0 * EPS0) == pytest.approx(C0, rel=1e-9)


def test_skin_depth_of_copper():
    # sqrt(rho / (pi f mu0)), rho = 1.724e-8: 2.090 um at 1 GHz, 66.1 um at 1 MHz.
    assert tline.skin_depth(1e9) == pytest.approx(2.0897e-6, rel=1e-3)
    assert tline.skin_depth(1e6) == pytest.approx(66.08e-6, rel=1e-3)
    assert tline.surface_resistance(1e9) == pytest.approx(1.724e-8 / 2.0897e-6, rel=1e-3)


def test_wide_microstrip_tends_to_the_parallel_plate():
    # For w >> h: Z0 -> eta0 h / (w sqrt(er)), Er_eff -> er.
    z, ee = tline.microstrip_hj(1000.0, 1.0, 0.0, 4.0)
    assert ee == pytest.approx(4.0, rel=0.01)
    assert z == pytest.approx(tline.ETA0 / (1000.0 * 2.0), rel=0.02)


def test_narrow_microstrip_in_air_tends_to_the_wire_over_plane():
    # For w << h in air: Z0 -> 60 ln(8 h / w).
    z, ee = tline.microstrip_hj(0.01, 1.0, 0.0, 1.0)
    assert ee == pytest.approx(1.0)
    assert z == pytest.approx(tline.ETA0 / (2 * math.pi) * math.log(8 / 0.01), rel=0.001)


def test_microstrip_thickness_lowers_impedance_and_er_eff():
    z0, e0 = tline.microstrip_hj(1.0, 1.0, 0.0, 4.2)
    z1, e1 = tline.microstrip_hj(1.0, 1.0, 0.1, 4.2)
    assert z1 < z0 and e1 < e0
    assert 0.9 < z1 / z0 < 0.99


def test_dispersion_limits_and_monotonic():
    w, h, er = 0.3e-3, 0.2e-3, 4.2
    _, ee0 = tline.microstrip_hj(w, h, 0.0, er)
    assert tline.microstrip_dispersion(ee0, er, w, h, 0.0) == ee0
    assert tline.microstrip_dispersion(ee0, er, w, h, 1.0) == pytest.approx(ee0, rel=1e-6)
    last = ee0
    for f in (1e9, 5e9, 20e9, 80e9, 300e9):
        cur = tline.microstrip_dispersion(ee0, er, w, h, f)
        assert last < cur < er
        last = cur
    assert tline.microstrip_dispersion(ee0, er, w, h, 1e13) == pytest.approx(er, rel=0.01)


def test_stripline_wheeler_tends_to_cohn_as_thickness_vanishes():
    w, b, er = 0.3e-3, 1e-3, 4.2
    assert tline.stripline_wheeler(w, b, 1e-9, er) == pytest.approx(
        tline.stripline_cohn(w, b, er), rel=0.006)
    assert tline.stripline_wheeler(w, b, 35e-6, er) < tline.stripline_cohn(w, b, er)


def test_offset_stripline_reduces_to_the_centred_one():
    w, t, er = 0.15e-3, 35e-6, 4.2
    h = 0.2e-3
    assert tline.stripline_asym(w, h, h, t, er) == pytest.approx(
        tline.stripline_wheeler(w, 2 * h + t, t, er), rel=1e-12)
    assert tline.stripline_asym(w, h, 3 * h, t, er) > tline.stripline_asym(w, h, h, t, er)


def test_coupled_stripline_decouples_at_large_gap():
    w, b, er = 0.3e-3, 1e-3, 4.2
    ze, zo = tline.coupled_stripline_cohn(w, 20e-3, b, er)
    z0 = tline.stripline_cohn(w, b, er)
    assert ze == pytest.approx(z0, rel=1e-6) and zo == pytest.approx(z0, rel=1e-6)
    ze, zo = tline.coupled_stripline_cohn(w, 0.1e-3, b, er)
    assert zo < z0 < ze


def test_cpw_thick_substrate_limit():
    # h -> infinity: Er_eff -> (er + 1) / 2, exactly.
    z, ee = tline.cpw(0.2e-3, 0.1e-3, 1.0, 9.8, grounded=False)
    assert ee == pytest.approx((9.8 + 1) / 2, rel=1e-6)
    assert z == pytest.approx(tline.ETA0 / 4 / math.sqrt(ee) / tline.k_ratio(0.5), rel=1e-9)


def test_djordjevic_sarkar_reproduces_the_data_sheet_point_and_is_causal():
    er, td = tline.djordjevic_sarkar(4.3, 0.02, 1e9, 1e9)
    assert er == pytest.approx(4.3, rel=1e-9) and td == pytest.approx(0.02, rel=1e-9)
    # Er falls with frequency; the slope is tied to the loss:
    # d(er)/d(ln f) = -(2/pi) er tan d for a constant-tan d medium.
    e1, _ = tline.djordjevic_sarkar(4.3, 0.02, 1e9, 1e8)
    e2, _ = tline.djordjevic_sarkar(4.3, 0.02, 1e9, 1e10)
    assert e1 > 4.3 > e2
    slope = (e2 - e1) / math.log(100.0)
    assert slope == pytest.approx(-2 / math.pi * 4.3 * 0.02, rel=0.05)
    # tan d stays within 15 % of its value across four decades.
    for f in (1e7, 1e8, 1e10, 1e11):
        assert tline.djordjevic_sarkar(4.3, 0.02, 1e9, f)[1] == pytest.approx(0.02, rel=0.15)
    assert tline.djordjevic_sarkar(4.3, 0.0, 1e9, 1e10) == (4.3, 0.0)


def test_roughness_models_limits():
    assert tline.rough_hammerstad(1e3, 1e-6) == pytest.approx(1.0, abs=1e-3)
    assert tline.rough_hammerstad(1e12, 2e-6) == pytest.approx(2.0, abs=0.01)
    # Rq equal to the skin depth: 1 + (2/pi) atan(1.4) = 1.6055
    f = 1e9
    assert tline.rough_hammerstad(f, tline.skin_depth(f)) == pytest.approx(1.6055, abs=1e-3)
    assert tline.rough_huray(1e3, 0.5e-6, 2.0) == pytest.approx(1.0, abs=1e-3)
    assert tline.rough_huray(1e14, 0.5e-6, 2.0) == pytest.approx(1 + 1.5 * 2.0, rel=0.05)
    assert tline.rough_groisse(1e3, 1e-6) == pytest.approx(1.0, abs=1e-6)
    assert tline.rough_groisse(1e13, 1e-6) == pytest.approx(2.0, abs=0.02)
    assert tline.rough_factor(1e9, "none") == 1.0
    with pytest.raises(ValueError):
        tline.rough_factor(1e9, "smoothish")


def test_ac_resistance_factor_limits():
    w, t = 0.2e-3, 35e-6
    assert tline.ac_resistance_factor(0.0, w, t) == 1.0
    assert tline.ac_resistance_factor(10.0, w, t) == pytest.approx(1.0, abs=1e-3)
    f = 10e9
    d = tline.skin_depth(f)
    assert tline.ac_resistance_factor(f, w, t) == pytest.approx(w * t / (2 * (w + t) * d), rel=0.01)
