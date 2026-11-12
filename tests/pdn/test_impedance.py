# SPDX-License-Identifier: Apache-2.0
"""AC PDN impedance against closed-form references."""

from __future__ import annotations

import math

import numpy as np
import pytest

from eda_agent.emc.stack import stack_from_stackup
from eda_agent.emc.touchstone import write_touchstone
from eda_agent.pdn import decap as dc
from eda_agent.pdn import impedance as im
from eda_agent.pdn import physics as ph

from .boards import STACKUP, plane_board


def modal_sum(a, b, d, eps, f, p, q, w, modes=160):
    """Impedance between square ports of side w at p and q on a
    rectangular plane pair with open edges: the double cosine series
    (Okoshi; Lei et al. 1999), written here independently of the solver
    under test."""
    a, b, d, w = (v * ph.MIL for v in (a, b, d, w))
    m = np.arange(modes)[:, None]
    n = np.arange(modes)[None, :]
    kx, ky = m * np.pi / a, n * np.pi / b
    chi = np.where(m == 0, 1, 2) * np.where(n == 0, 1, 2)

    def fac(pt):
        return (np.cos(kx * pt[0] * ph.MIL) * np.cos(ky * pt[1] * ph.MIL)
                * np.sinc(kx * w / 2 / np.pi) * np.sinc(ky * w / 2 / np.pi))
    ff = fac(p) * fac(q)
    out = []
    for fi, e in zip(f, eps):
        om = 2 * np.pi * fi
        k2 = om ** 2 * ph.MU0 * ph.EPS0 * e
        out.append(1j * om * ph.MU0 * d / (a * b) * np.sum(chi * ff / (kx ** 2 + ky ** 2 - k2)))
    return np.array(out)


# ---- physics --------------------------------------------------------------

def test_surface_impedance_limits():
    t = 35e-6
    rho = ph.RHO_CU_20C
    assert ph.slab_surface_impedance(0.0, t) == pytest.approx(rho / t)
    z = ph.slab_surface_impedance(100.0, t)
    assert z.real == pytest.approx(rho / t, rel=1e-6)
    assert z.imag == pytest.approx(2 * math.pi * 100.0 * ph.MU0 * t / 3, rel=1e-3)
    # skin limit: (1 + j) rho / delta
    f = 10e9
    delta = ph.skin_depth(f)
    z = ph.slab_surface_impedance(f, t)
    assert z.real == pytest.approx(rho / delta, rel=1e-6)
    assert z.imag == pytest.approx(rho / delta, rel=1e-6)
    # copper skin depth at 1 MHz is the handbook 65-66 um
    assert ph.skin_depth(1e6) == pytest.approx(66.1e-6, rel=0.01)
    # continuous through the crossover
    zs = [abs(ph.slab_surface_impedance(x, t)) for x in np.logspace(4, 9, 200)]
    assert all(b >= a * 0.999 for a, b in zip(zs, zs[1:]))


def test_via_pair_loop_inductance():
    # mu0 h / pi * acosh(s / 2r); 60 mil long, 40 mil apart, 5 mil radius
    L = ph.via_pair_loop_inductance(60, 40, 5)
    assert L == pytest.approx(4e-7 * 60 * 25.4e-6 * math.acosh(4.0), rel=1e-9)
    assert 1.2e-9 < L < 1.3e-9
    assert ph.via_pair_loop_inductance(0, 40, 5) == 0.0


def test_djordjevic_sarkar_hits_its_reference_and_is_causal():
    e = im.djordjevic_sarkar([1e9], 4.3, 0.02)[0]
    assert e.real == pytest.approx(4.3, rel=1e-9)
    assert -e.imag / e.real == pytest.approx(0.02, rel=1e-9)
    f = np.logspace(4, 10, 50)
    e = im.djordjevic_sarkar(f, 4.3, 0.02)
    assert np.all(np.diff(e.real) < 0)                 # e' falls with frequency
    tand = -e.imag / e.real
    assert tand.min() > 0.016 and tand.max() < 0.021   # near-constant loss
    # Kramers-Kronig slope: de'/dlog10(f) = -(2 / pi) ln(10) e''
    slope = np.gradient(e.real, np.log10(f))[25]
    assert slope == pytest.approx(-2 / math.pi * math.log(10) * (-e.imag[25]), rel=0.02)


# ---- capacitors ------------------------------------------------------------

@pytest.mark.parametrize("text,farads", [
    ("100nF", 100e-9), ("0.1uF", 100e-9), ("4u7", 4.7e-6), ("4.7µF", 4.7e-6),
    ("100n/16V X7R", 100e-9), ("10000pF", 10e-9), ("100NF", 100e-9),
    ("10uF 0805", 10e-6), ("2,2uF", 2.2e-6), ("CAP 22u", 22e-6), ("1mF", 1e-3),
])
def test_capacitance_is_read_from_a_comment(text, farads):
    assert dc.parse_capacitance(text) == pytest.approx(farads)


@pytest.mark.parametrize("text", ["100nH", "10k", "", "LM1117", "0402"])
def test_not_a_capacitance(text):
    assert dc.parse_capacitance(text) is None


def test_package_from_footprint():
    assert dc.package_of("CAPC1005X55N") == "0402"
    assert dc.package_of("C_0603_1608Metric") == "0603"
    assert dc.package_of("C0402") == "0402"
    assert dc.package_of("SOT23") == ""


def test_decap_series_resonance():
    d = dc.Decap("C1", 100e-9, 0.02, 0.4e-9, mount_l=0.6e-9)
    assert d.srf_hz == pytest.approx(1 / (2 * math.pi * math.sqrt(1e-9 * 100e-9)))
    z = d.z(np.array([d.srf_hz]))[0]
    assert z.real == pytest.approx(0.02) and abs(z.imag) < 1e-9


def test_touchstone_shunt_capacitor_round_trip(tmp_path):
    f = np.logspace(5, 9, 81)
    d = dc.Decap("C1", 1e-6, 0.01, 0.5e-9)
    z = d.z_part(f)
    s21 = 2 * z / (2 * z + 50.0)           # shunt-thru
    s11 = -50.0 / (2 * z + 50.0)
    s = [[[s11[k], s21[k]], [s21[k], s11[k]]] for k in range(len(f))]
    path = tmp_path / "cap.s2p"
    write_touchstone(path, list(f), s)
    ft, zt = dc.touchstone_impedance(str(path), "shunt")
    assert np.allclose(zt, z, rtol=1e-4, atol=1e-7)


# ---- lumped ------------------------------------------------------------------

def test_lumped_parallel_anti_resonance():
    # two capacitors: the peak between their SRFs sits at
    # 1 / (2 pi sqrt((L1 + L2) * C1 C2 / (C1 + C2)))
    f = np.logspace(5, 9, 4001)
    a = dc.Decap("A", 10e-6, 0.005, 1e-9)
    b = dc.Decap("B", 100e-9, 0.005, 1e-9)
    z = np.abs(im.lumped(f, [a.z(f), b.z(f)]))
    peak = f[np.argmax(np.where((f > a.srf_hz) & (f < b.srf_hz), z, 0))]
    cs = 10e-6 * 100e-9 / (10e-6 + 100e-9)
    assert peak == pytest.approx(1 / (2 * math.pi * math.sqrt(2e-9 * cs)), rel=0.01)


# ---- cavity ---------------------------------------------------------------------

def _port(cav, x, y):
    node, _ = cav.node_near(x, y)
    iy, ix = np.argwhere(cav.ids == node)[0]
    return node, ((ix + 0.5) * cav.cell_mils, (iy + 0.5) * cav.cell_mils)


def test_cavity_is_the_plate_capacitor_at_low_frequency():
    cav = im.rectangular_cavity(4000, 3000, 4, 4.3, 0.02, cell_mils=100)
    c0 = ph.EPS0 * 4.3 * 12e6 * ph.MIL ** 2 / (4 * ph.MIL)
    assert cav.capacitance_f == pytest.approx(c0, rel=1e-9)
    f = np.array([1e5])
    node, _ = _port(cav, 1000, 1000)
    z = im.solve_cavity(cav, f, [], [node])[0]
    eps = im.djordjevic_sarkar(f, 4.3, 0.02)[0]
    assert abs(z) == pytest.approx(1 / (2 * math.pi * 1e5 * c0 * abs(eps) / 4.3), rel=2e-3)


def test_transfer_impedance_matches_the_modal_series_to_1p5_ghz():
    # Complex Z between two ports, through eleven resonances: the solver
    # against the analytic series, point for point.
    a, b, d = 4000.0, 3000.0, 4.0
    cav = im.rectangular_cavity(a, b, d, 4.3, 0.02, cell_mils=50)
    n1, p = _port(cav, 1010, 1010)
    n2, q = _port(cav, 2810, 1910)
    f = np.logspace(6, 9.2, 60)
    z = im.solve_cavity(cav, f, [], [n1], rho=1e-14, observe_nodes=[n2])
    ref = modal_sum(a, b, d, im.djordjevic_sarkar(f, 4.3, 0.02), f, p, q, cav.cell_mils)
    err = np.abs(z - ref) / np.abs(ref)
    assert np.median(err) < 1e-4
    assert np.percentile(err, 90) < 0.005
    assert err.max() < 0.05


def test_self_impedance_matches_the_modal_series_at_low_frequency():
    a, b, d = 4000.0, 3000.0, 4.0
    cav = im.rectangular_cavity(a, b, d, 4.3, 0.02, cell_mils=50)
    node, p = _port(cav, 1010, 1010)
    # (higher up the port's own inductance shows, and a square port of
    # the series is not the grid node's Peaceman circle)
    f = np.logspace(6, 7.5, 30)
    z = im.solve_cavity(cav, f, [], [node], rho=1e-14)
    ref = modal_sum(a, b, d, im.djordjevic_sarkar(f, 4.3, 0.02), f, p, p, cav.cell_mils)
    assert np.max(np.abs(np.abs(z) - np.abs(ref)) / np.abs(ref)) < 0.005


def test_grid_node_is_a_via_of_the_peaceman_radius():
    # Refining the grid three times narrows the node's equivalent via
    # three times: the self-inductance rises by mu0 d / (2 pi) ln 3. That
    # is what cavity_port_inductance adds back, so the answer for a real
    # via does not depend on the grid.
    f = np.array([3e7, 1e8])
    z = {}
    for cell in (150.0, 50.0):
        cav = im.rectangular_cavity(4050, 3150, 4, 4.3, 0.02, cell_mils=cell)
        node, p = _port(cav, 1125, 1125)
        assert p == (1125.0, 1125.0)
        z[cell] = im.solve_cavity(cav, f, [], [node], rho=1e-14)
    dl = (z[50.0] - z[150.0]).imag / (2 * np.pi * f)
    want = ph.MU0 * 4 * ph.MIL / (2 * math.pi) * math.log(3)
    assert dl == pytest.approx(want, rel=1e-3)
    r = 6.0   # a via of 6 mil radius, on either grid
    total = [ph.cavity_port_inductance(4, r, c) for c in (150.0, 50.0)]
    assert total[0] - total[1] == pytest.approx(want, rel=1e-9)
    assert ph.cavity_port_inductance(4, 40.0, 50.0) == 0.0


def test_cavity_resonances_are_where_the_cavity_has_them():
    a, b, d = 4000.0, 3000.0, 4.0
    cav = im.rectangular_cavity(a, b, d, 4.3, 0.02, cell_mils=50)
    node, p = _port(cav, 1010, 1010)
    f = np.linspace(600e6, 1300e6, 1401)
    z = np.abs(im.solve_cavity(cav, f, [], [node], rho=1e-14))
    from scipy.signal import find_peaks
    got = f[find_peaks(z)[0]]
    er = im.djordjevic_sarkar(f, 4.3, 0.02).real
    assert len(got) >= 3
    # (1,0) and (0,1): f = c / (2 L sqrt(er(f)))
    for g, side in zip(got[:2], (a, b)):
        fr = 1e9
        for _ in range(20):
            fr = ph.C0 / (2 * side * ph.MIL * math.sqrt(np.interp(fr, f, er)))
        assert g == pytest.approx(fr, rel=0.003)
    # and all three where the series puts its (lossy, overlapping) peaks
    ref = np.abs(modal_sum(a, b, d, im.djordjevic_sarkar(f, 4.3, 0.02), f, p, p, cav.cell_mils))
    want = f[find_peaks(ref)[0]]
    assert got[:3] == pytest.approx(want[:3], rel=0.002)


def test_copper_loss_damps_the_resonance():
    cav = im.rectangular_cavity(4000, 3000, 4, 4.3, 0.001, cell_mils=100)
    node, _ = _port(cav, 1010, 1010)
    f = np.linspace(690e6, 730e6, 201)
    ideal = np.abs(im.solve_cavity(cav, f, [], [node], rho=1e-14)).max()
    real = np.abs(im.solve_cavity(cav, f, [], [node])).max()
    assert real < 0.6 * ideal
    # Q of a thin cavity from conductor loss is d / delta: here 4 mil over
    # 2.5 um, about 41; with tan_d 0.001 the total is near 39. The peak
    # scales with Q.
    delta = ph.skin_depth(710e6)
    q_c = 4 * ph.MIL / delta
    q = 1 / (1 / q_c + 0.001)
    assert real / ideal == pytest.approx(q / (1 / 0.001), rel=0.25)


# ---- whole analysis ----------------------------------------------------------------

@pytest.fixture(scope="module")
def result(tmp_path_factory):
    b = plane_board()
    st = stack_from_stackup(STACKUP, b.layers)
    out = tmp_path_factory.mktemp("pdn")
    return im.analyse(b, st, "+1V0", "GND", load={"ref": "U2"}, volts=1.0, ripple_pct=3,
                      step_amps=2, out_dir=out), b, st


def test_analysis_finds_the_plane_pair_and_the_capacitors(result):
    r, b, st = result
    assert r["model"] == "cavity"
    cav = r["cavity"]
    assert {cav["power_layer"], cav["ground_layer"]} == {"InternalPlane1", "InternalPlane2"}
    assert cav["separation_mils"] == 4.0
    # 12 in2 at 4 mil, er 4.3 (antipads take a little off)
    assert cav["capacitance_nf"] == pytest.approx(2.90, rel=0.02)
    refs = {d["ref"]: d for d in r["decaps"]}
    assert set(refs) == {"C1", "C10", "C11", "C12", "C13"}
    assert refs["C1"]["c_uf"] == 22.0 and refs["C1"]["package"] == "0805"
    assert refs["C10"]["c_uf"] == 0.1 and refs["C10"]["esl_nh"] == 0.35
    assert r["target"]["target_mohm"] == 15.0


def test_mounting_inductance_is_the_via_loop_plus_fanout(result):
    r, b, st = result
    m = {d["ref"]: d for d in r["decaps"]}["C10"]["mount"]
    # vias 90 mil apart, 10 mil hole + plating, 4 mil from top to the GND plane
    assert m["via_spacing_mils"] == 90.0 and m["height_mils"] == 4.0
    lv = ph.via_pair_loop_inductance(4.0, 90.0, 5.79)
    assert m["vias_nh"] == pytest.approx(lv * 1e9, abs=2e-4)
    # two 13 mil fan-outs, 12 mil wide, 4 mil over the plane
    lt = 2 * ph.MU0 * 4 * 13 * ph.MIL / (12 + 8)
    assert m["traces_nh"] == pytest.approx(lt * 1e9, abs=2e-4)
    assert m["total_nh"] == pytest.approx(m["vias_nh"] + m["traces_nh"] + m["cavity_nh"], abs=3e-4)


def test_analysis_reports_bands_peaks_and_files(result):
    r, b, st = result
    assert not r["pass"] and r["violations"]
    for v in r["violations"]:
        assert v["peak_mohm"] > 15.0 and v["from_mhz"] <= v["peak_mhz"] <= v["to_mhz"]
    assert len(r["suggestions"]) == len(r["violations"])
    # bulk-to-MLCC anti-resonance is between their mounted SRFs
    d = {x["ref"]: x for x in r["decaps"]}
    assert any(d["C1"]["srf_mhz"] < p["mhz"] < d["C10"]["srf_mhz"] for p in r["anti_resonances"])
    names = [f.rsplit(".", 1)[-1] for f in r["files"]]
    assert "csv" in names and "cir" in names
    cir = open([f for f in r["files"] if f.endswith(".cir")][0]).read()
    assert "I1 0 load AC 1" in cir and cir.count("\nC") == 6   # 5 parts + plane


def test_cavity_and_lumped_agree_below_the_plane_effects(result):
    r, b, st = result
    lum = im.analyse(b, st, "+1V0", "GND", load={"ref": "U2"}, target_ohms=0.015, mode="lumped")
    assert lum["model"] == "lumped" and lum["cavity"] is None
    zc = {p["hz"]: p["mohm"] for p in r["curve"]}
    zl = {p["hz"]: p["mohm"] for p in lum["curve"]}
    for hz in zc:
        if 3e4 < hz < 3e5:
            # the cavity adds the planes' DC resistance between VRM and load
            assert zc[hz] == pytest.approx(zl[hz], rel=0.12)


def test_removing_capacitors_raises_the_impedance(result):
    r, b, st = result
    fewer = im.analyse(b, st, "+1V0", "GND", load={"ref": "U2"}, target_ohms=0.015,
                       exclude=["C11", "C12", "C13"], f_min_hz=1e7, f_max_hz=5e7, points=41)
    full = im.analyse(b, st, "+1V0", "GND", load={"ref": "U2"}, target_ohms=0.015,
                      f_min_hz=1e7, f_max_hz=5e7, points=41)
    assert len(fewer["decaps"]) == 2
    assert fewer["minimum"]["mohm"] > 2.0 * full["minimum"]["mohm"]


def test_a_met_target_passes():
    f = im.analyse(None, None, "V", "GND", load={"mount_nh": 0.0}, target_ohms=0.5,
                   decaps=[{"ref": "CX", "c_uf": 10.0, "esr_ohms": 0.01, "esl_nh": 0.5,
                            "mount_nh": 0.5, "count": 4}],
                   vrm={"r_mohm": 5, "l_nh": 100}, f_min_hz=1e3, f_max_hz=1e8, mode="lumped")
    assert f["pass"] and not f["violations"] and f["worst"]["over_target_x"] < 1.0


def test_bad_inputs():
    with pytest.raises(im.PDNError, match="target"):
        im.analyse(None, None, "V", "GND", load={})
    with pytest.raises(im.PDNError, match="mode"):
        im.analyse(None, None, "V", "GND", load={}, target_ohms=1, mode="x")
