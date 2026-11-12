# SPDX-License-Identifier: Apache-2.0
"""Stack heights and the emission arithmetic, against hand numbers."""

from __future__ import annotations

import math

import pytest

from eda_agent.emc.emission import (
    Drive, db_uv, limit_at, loop_field, spectrum, trapezoid_harmonic,
)
from eda_agent.emc.stack import assumed_stack, layer_id_name, stack_from_stackup

from .boards import LAYERS, STACKUP


def test_layer_ids_map_to_layout_names():
    assert layer_id_name(1) == "TopLayer"
    assert layer_id_name(2) == "MidLayer1"
    assert layer_id_name(32) == "BottomLayer"
    assert layer_id_name(39) == "InternalPlane1"
    assert layer_id_name(70) == ""


def test_stack_heights_add_up_from_the_bottom():
    s = stack_from_stackup(STACKUP, LAYERS)
    assert [l.name for l in s.layers] == ["TopLayer", "MidLayer1", "MidLayer2", "BottomLayer"]
    assert [l.z_mils for l in s.layers] == [56.0, 48.0, 8.0, 0.0]
    assert s.thickness_mils == 56.0
    assert s.gap_mils("TopLayer", "MidLayer1") == 8.0
    assert s.er_between("TopLayer", "MidLayer1") == pytest.approx(4.2)
    # 8 mil of 4.2 and 40 of 4.6 between top and layer 3
    assert s.er_between("TopLayer", "MidLayer2") == pytest.approx((8 * 4.2 + 40 * 4.6) / 48)
    assert not s.assumed


def test_neighbours_are_nearest_first():
    s = stack_from_stackup(STACKUP, LAYERS)
    assert [l.name for l in s.neighbours("MidLayer1")] == ["TopLayer", "MidLayer2"]
    assert [l.name for l in s.neighbours("TopLayer")] == ["MidLayer1"]


def test_without_a_stackup_the_stack_is_assumed_and_says_so():
    s = assumed_stack(LAYERS)
    assert s.assumed and s.notes
    assert s.thickness_mils == pytest.approx(62.99)
    assert s.gap_mils("TopLayer", "MidLayer1") == pytest.approx(8.0)
    assert assumed_stack(LAYERS[:3]).thickness_mils == pytest.approx(62.99)


def test_ott_loop_field_hand_number():
    # 1 cm^2, 100 MHz, 10 mA, 3 m: 263e-16 * 1e16 * 1e-4 * 1e-2 / 3 V/m
    e = loop_field(100e6, 1e-4, 0.01, 3.0)
    assert e == pytest.approx(263e-6 / 3)
    assert db_uv(e) == pytest.approx(20 * math.log10(263 / 3), abs=1e-9)


def test_square_wave_harmonics_are_the_fourier_series():
    # 50 % duty, zero rise: odd harmonics 2A/(n pi), even ones vanish
    for n in (1, 3, 5):
        assert trapezoid_harmonic(n, 1.0, 0.5, 0.0, 1.0) == pytest.approx(2 / (n * math.pi))
    assert trapezoid_harmonic(2, 1.0, 0.5, 0.0, 1.0) == pytest.approx(0.0, abs=1e-12)


def test_rise_time_rolls_the_spectrum_off():
    slow = trapezoid_harmonic(21, 1.0, 0.5, 2e-9, 40e-9)
    fast = trapezoid_harmonic(21, 1.0, 0.5, 0.2e-9, 40e-9)
    assert slow < fast


def test_limit_lines():
    assert limit_at("cispr32_b_3m", 100e6) == 40.0
    assert limit_at("cispr32_b_3m", 500e6) == 47.0
    assert limit_at("cispr32_b_10m", 2e9) is None
    assert limit_at("fcc15_b_3m", 100e6) == 43.5


def test_spectrum_worst_harmonic_and_margin():
    spec = spectrum(1e-4, Drive(freq_hz=25e6, rise_s=1e-9, volts=3.3, z_ohms=50))
    worst = spec["worst"]
    assert worst["margin_db"] == min(h["margin_db"] for h in spec["harmonics"])
    # every harmonic listed is inside 30 MHz .. 6 GHz and on 25 MHz
    assert all(h["f_mhz"] % 25 == 0 for h in spec["harmonics"])
    assert min(h["f_mhz"] for h in spec["harmonics"]) >= 30


def test_drive_from_dict_in_mhz():
    d = Drive.from_dict({"freq_mhz": 50, "rise_ns": 2})
    assert d.freq_hz == 50e6 and d.rise_s == pytest.approx(2e-9)
    assert Drive(freq_hz=1e6).rise() == 1e-9
