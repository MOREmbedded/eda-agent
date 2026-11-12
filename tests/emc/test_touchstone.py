# SPDX-License-Identifier: Apache-2.0
"""Touchstone read / write / summary."""

from __future__ import annotations

import cmath
import math

import pytest

from eda_agent.emc.touchstone import read_touchstone, summarize, write_touchstone


def test_a_two_port_round_trips_in_the_two_port_order(tmp_path):
    f = [1e9, 2e9]
    s = [[[0.1 + 0.2j, 0.7 - 0.1j], [0.8 + 0.0j, 0.05j]],
         [[0.2 + 0.0j, 0.6 + 0.0j], [0.9 - 0.3j, 0.1 + 0.1j]]]
    path = write_touchstone(tmp_path / "line", f, s)
    assert path.suffix == ".s2p"
    first = path.read_text().splitlines()[1].split()
    # S11 S21 S12 S22: the second pair is S21 = s[0][1][0]
    assert float(first[3]) == pytest.approx(0.8)
    back = read_touchstone(path)
    assert back["ports"] == 2 and back["freq_hz"] == f
    for k in range(2):
        for i in range(2):
            for j in range(2):
                assert back["s"][k][i][j] == pytest.approx(s[k][i][j])


def test_a_three_port_wraps_after_four_pairs(tmp_path):
    s = [[[complex(i, j) for j in range(3)] for i in range(3)]]
    path = write_touchstone(tmp_path / "x", [1e6], s)
    assert path.suffix == ".s3p"
    assert read_touchstone(path)["s"][0] == s[0]


def test_db_and_ma_formats_and_units(tmp_path):
    p = tmp_path / "ant.s1p"
    p.write_text("! vendor antenna\n# MHz S DB R 50\n2400 -20 90\n2450 -6.0206 0\n")
    d = read_touchstone(p)
    assert d["freq_hz"] == [2.4e9, 2.45e9]
    assert d["s"][0][0][0] == pytest.approx(0.1j, abs=1e-9)
    assert abs(d["s"][1][0][0]) == pytest.approx(0.5, abs=1e-4)
    p2 = tmp_path / "m.s1p"
    p2.write_text("# GHz S MA R 75\n1 0.5 180\n")
    d2 = read_touchstone(p2)
    assert d2["z0"] == 75 and d2["s"][0][0][0] == pytest.approx(-0.5)


def test_summary_finds_the_antenna_dip_and_its_impedance(tmp_path):
    f = [2.3e9 + k * 1e7 for k in range(21)]
    # return loss deepest at 2.4 GHz, matched to 50 there
    s = [[[complex(0.05 + abs(k - 10) * 0.04, 0)]] for k in range(21)]
    out = summarize({"ports": 1, "z0": 50.0, "freq_hz": f, "s": s}, at_hz=[2.4e9])
    port = out["ports_detail"][0]
    assert port["best_at_mhz"] == pytest.approx(2400)
    assert port["best_s_db"] == pytest.approx(20 * math.log10(0.05), abs=0.01)
    assert port["zin_at_best_ohms"][0] == pytest.approx(50 * 1.05 / 0.95, abs=0.01)
    assert out["at"][0]["f_mhz"] == pytest.approx(2400)


def test_bad_files_are_refused(tmp_path):
    p = tmp_path / "x.s2p"
    p.write_text("# GHz S RI R 50\n1 0 0 0\n")
    with pytest.raises(ValueError):
        read_touchstone(p)
    p3 = tmp_path / "noext.txt"
    p3.write_text("# GHz S RI R 50\n1 0 0\n")
    with pytest.raises(ValueError):
        read_touchstone(p3)
    p4 = tmp_path / "y.s1p"
    p4.write_text("# GHz Z RI R 50\n1 0 0\n")
    with pytest.raises(ValueError):
        read_touchstone(p4)
