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


def test_a_grid_node_has_the_peaceman_radius_absolutely():
    # Self minus transfer inductance to a node r away, deep inside a large
    # plane pair, is mu0 d / (2 pi) ln(r / r_eq): read r_eq back.
    cav = im.rectangular_cavity(12100, 12100, 4, 4.3, 0.0, cell_mils=100)
    f = np.array([1e5])
    c = int(cav.ids[60, 60])
    zs = im.solve_cavity(cav, f, [], [c], rho=1e-14)
    for k in (5, 8):
        zt = im.solve_cavity(cav, f, [], [c], rho=1e-14, observe_nodes=[int(cav.ids[60, 60 + k])])
        L = float(((zs - zt).imag / (2 * np.pi * f))[0])
        req = k * 100 / math.exp(L * 2 * math.pi / (ph.MU0 * 4 * ph.MIL))
        assert req == pytest.approx(ph.peaceman_radius(100.0), rel=0.01)
    assert ph.peaceman_radius(1.0) == pytest.approx(0.1985, abs=1e-4)


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
    # each fan-out: 5 mil of 12 mil track from the pad's edge to the land's,
    # then the land (20 mil across) to the barrel wall, 4 mil over the plane
    lt = 2 * ph.MU0 * 4 * ph.MIL * (5 / (12 + 8) + (10 - 5.79) / (20 + 8))
    assert m["traces_nh"] == pytest.approx(lt * 1e9, abs=2e-4)
    assert m["how"] == ["track"] and m["power_legs"] == 1 and m["ground_legs"] == 1
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


def test_bands_are_exactly_where_the_curve_is_over_the_target(result):
    r, b, st = result
    rows = open([f for f in r["files"] if f.endswith(".csv")][0]).read().splitlines()[1:]
    cols = [list(map(float, row.split(","))) for row in rows]
    target = r["target"]["target_mohm"] / 1e3
    assert all(c[5] == pytest.approx(target) for c in cols)
    for fhz, _re, _im, mag, _lump, _t in cols:
        inside = any(v["from_mhz"] * 1e6 * 0.9999 <= fhz <= v["to_mhz"] * 1e6 * 1.0001
                     for v in r["violations"])
        assert inside == (mag > target), fhz
    # some of the curve sits between 1x and 3x the target: a band there counts
    assert any(target < c[3] < 3 * target for c in cols)


def test_the_cavity_curve_carries_the_plane_resonance_the_lumped_one_lacks(result):
    r, b, st = result
    rows = open([f for f in r["files"] if f.endswith(".csv")][0]).read().splitlines()[1:]
    cols = np.array([list(map(float, row.split(","))) for row in rows])
    f, mag, lump = cols[:, 0], cols[:, 3], cols[:, 4]
    low = f < 1e5
    assert np.allclose(mag[low], lump[low], rtol=0.35)
    # the first plane mode (about 711 MHz) is a peak of the cavity curve only
    assert any(650 < p["mhz"] < 800 for p in r["anti_resonances"])
    band = (f > 600e6) & (f < 900e6)
    assert np.max(np.abs(mag[band] - lump[band]) / lump[band]) > 0.5


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


# --- mounting: each pin's own way to the planes --------------------------------

from eda_agent.layout.model import Component, Region, Track, Via   # noqa: E402
from eda_agent.pdn import mounting as mt                           # noqa: E402

from .boards import add_bga, add_cap, bare_board, smd, via        # noqa: E402


def _load_mount(b, ref="U2", reach=100.0):
    st = stack_from_stackup(STACKUP)
    cav = im.find_cavity(b, st, "+1V0", "GND", 1e9)
    pw = [p for p in b.pads if p.comp == ref and p.net == "+1V0"]
    gn = [p for p in b.pads if p.comp == ref and p.net == "GND"]
    return mt.mount(mt.Copper(b, st, 0.79), cav, "+1V0", "GND", pw, gn, reach)


def test_n_dog_bones_are_one_nth_of_one():
    one, many = bare_board(), bare_board()
    add_bga(one, 2)
    add_bga(many, 8)
    m1, m8 = _load_mount(one), _load_mount(many)
    # 2 pairs against 32 pairs of the same dog-bone
    assert m8["via_pairs"] == 32 and m8["power_legs"] == 32 and m8["ground_legs"] == 32
    assert m8["total_nh"] == pytest.approx(m1["total_nh"] * 2 / 32, rel=0.02)
    assert m8["traces_nh"] == pytest.approx(m1["traces_nh"] * 2 / 32, rel=0.02)
    assert m8["how"] == ["track"] and m8["fanout_mils"] == [27.9, 27.9]
    # one dog-bone by hand: 27.86 mil centre to centre, 8 mil of it in the
    # ball, 9 in the land, then the land to the wall; half a via-pair loop
    # with the nearest other via 39.37 mil away; the power via crosses the
    # cavity (ground plane is the nearer)
    h, r = 4.0, 4.79
    fan = ph.MU0 * h * ph.MIL * ((27.86 - 8 - 9) / (6 + 2 * h) + (9 - r) / (18 + 2 * h))
    half_loop = 0.5 * ph.via_pair_loop_inductance(h, 39.37, r)
    cav = im.find_cavity(one, stack_from_stackup(STACKUP), "+1V0", "GND", 1e9)
    port = ph.cavity_port_inductance(cav.d_mils, r, cav.cell_mils)
    by_hand = (fan + half_loop + port) / 2 + (fan + half_loop) / 2
    assert m1["_l"] == pytest.approx(by_hand, rel=0.01)


def test_via_in_pad_has_no_fan_out():
    b = bare_board()
    add_bga(b, 4, dog=0.0)
    m = _load_mount(b)
    assert m["how"] == ["via in pad"] and m["traces_nh"] == 0.0 and m["via_pairs"] == 8


@pytest.mark.parametrize("pitch", [60.0, 40.0, 30.0])
def test_a_neighbours_via_is_not_this_capacitors_via(pitch):
    b = bare_board()
    for i in range(5):
        add_cap(b, f"C{10 + i}", 2000 + pitch * i, 1700)
    st = stack_from_stackup(STACKUP)
    caps, _ = im.build_decaps(b, st, im.find_cavity(b, st, "+1V0", "GND", 1e9),
                              "+1V0", "GND", None, 0.79)
    alone = bare_board()
    add_cap(alone, "C10", 2000, 1700)
    ref, _ = im.build_decaps(alone, st, im.find_cavity(alone, st, "+1V0", "GND", 1e9),
                             "+1V0", "GND", None, 0.79)
    for c in caps:
        assert c.mount_detail["via_pairs"] == 1
        assert c.mount_l == pytest.approx(ref[0].mount_l, rel=1e-6)
        # and it is attached where its vias are, not at its body
        assert (c.x, c.y) == pytest.approx((2000 + pitch * (int(c.ref[1:]) - 10), 1700))


def test_a_via_that_does_not_reach_the_plane_is_not_counted():
    b = bare_board()
    add_cap(b, "C10", 2000, 1700, power_via=False)
    # a blind via top to layer 2 (the ground plane): it never gets to +1V0 on layer 3
    b.vias.append(Via(2000, 1655, 20, 10, "TopLayer", "InternalPlane1", "+1V0"))
    b.tracks.append(Track("TopLayer", 2000, 1680, 2000, 1655, 12, "+1V0"))
    st = stack_from_stackup(STACKUP)
    caps, notes = im.build_decaps(b, st, im.find_cavity(b, st, "+1V0", "GND", 1e9),
                                  "+1V0", "GND", None, 0.79)
    assert caps[0].mount_detail["assumed"] and caps[0].mount_l == pytest.approx(1e-9)
    assert any("1 nH mounting assumed: C10" in n for n in notes)


def test_two_vias_per_pad_halve_the_way_down():
    one, two = bare_board(), bare_board()
    add_cap(one, "C10", 2000, 1700)
    add_cap(two, "C10", 2000, 1700)
    # second via on each pad, the other side of it, on its own track
    two.vias += [via(2030, 1680, "+1V0"), via(2030, 1720, "GND")]
    two.tracks += [Track("TopLayer", 2000, 1680, 2030, 1680, 12, "+1V0"),
                   Track("TopLayer", 2000, 1720, 2030, 1720, 12, "GND")]
    st = stack_from_stackup(STACKUP)

    def m(b):
        return im.build_decaps(b, st, im.find_cavity(b, st, "+1V0", "GND", 1e9),
                               "+1V0", "GND", None, 0.79)[0][0]
    a, c = m(one), m(two)
    assert c.mount_detail["via_pairs"] == 2
    assert 0.4 * a.mount_l < c.mount_l < 0.75 * a.mount_l


def _pour_board():
    """+1V0 as a 2500 x 2000 mil pour on the top layer over the layer 2 ground plane."""
    b = bare_board(ground_l3=True)
    b.regions.append(Region("TopLayer", [(500, 500), (3000, 500), (3000, 2500), (500, 2500)],
                            [], "+1V0", "pour", source="polygon:P"))
    b.regions.append(Region("TopLayer", [(400, 400), (3100, 400), (3100, 2600), (400, 2600)],
                            [], "+1V0", "pour_boundary", source="polygon:P"))
    for i in range(3):
        add_cap(b, f"C{10 + i}", 1500 + 100 * i, 1500, power_via=False)
    b.components.append(Component("U2", "QFN", 2500, 1500))
    b.pads += [smd("U2", "1", 2500, 1500, "+1V0"), smd("U2", "2", 2550, 1500, "GND")]
    b.vias.append(via(2550, 1540, "GND"))
    b.tracks.append(Track("TopLayer", 2550, 1500, 2550, 1540, 12, "GND"))
    return b


def test_rail_on_an_outer_pour_needs_no_power_via():
    b = _pour_board()
    r = im.analyse(b, stack_from_stackup(STACKUP), "+1V0", "GND", load={"ref": "U2"},
                   target_ohms=0.05, points=31)
    cav = r["cavity"]
    assert (cav["power_layer"], cav["ground_layer"]) == ("TopLayer", "InternalPlane1")
    assert cav["area_in2"] == pytest.approx(5.0, rel=0.01)      # the pour, not its outline
    for d in r["decaps"]:
        m = d["mount"]
        assert m["how"] == ["in the plane copper", "track"] and "assumed" not in m
        assert m["height_mils"] == 0.0 and m["vias_nh"] == 0.0
        # the ground via through the 4 mil cavity and its fan-out: about 0.07 nH, not 1
        assert 0.03 < m["total_nh"] < 0.15
    assert r["load"]["mount"]["how"] == ["in the plane copper", "track"]
    assert not any("assumed" in n for n in r["notes"])
    # first resonance of the pour itself, not of the board: c / (2 a sqrt(er))
    f1 = ph.C0 / (2 * 2500 * ph.MIL * math.sqrt(cav["er_1ghz"])) / 1e6
    assert cav["first_mode_mhz"] == pytest.approx(f1, rel=0.01)
    assert cav["extent_mils"] == pytest.approx([2500, 2000], abs=90)


def _split_board(track: bool):
    """+1V0 plane only right of x = 2000; the load at x = 300 on a long track."""
    b = bare_board()
    b.regions.append(Region("InternalPlane2", [(2000, 0), (4000, 0), (4000, 3000), (2000, 3000)],
                            [], "+1V0", "copper"))
    add_cap(b, "C10", 3000, 1700)
    b.components.append(Component("U3", "SOT23", 300, 1500))
    b.pads += [smd("U3", "1", 300, 1500, "+1V0"), smd("U3", "2", 300, 1560, "GND")]
    b.vias.append(via(300, 1600, "GND"))
    b.tracks.append(Track("TopLayer", 300, 1560, 300, 1600, 12, "GND"))
    if track:
        b.tracks.append(Track("TopLayer", 300, 1500, 2100, 1500, 10, "+1V0"))
        b.vias.append(via(2100, 1500, "+1V0"))
    return b


def test_a_load_fed_by_a_track_carries_the_track():
    st = stack_from_stackup(STACKUP)
    r = im.analyse(_split_board(True), st, "+1V0", "GND", load={"ref": "U3"},
                   target_ohms=0.05, points=31)
    assert r["cavity"]["area_in2"] == pytest.approx(6.0, rel=0.02)
    m = r["load"]["mount"]
    # 1800 mil of 10 mil track 4 mil over the ground plane, less the pad and the land
    track = ph.MU0 * 4 * (1800 - 12 - 10) * ph.MIL / (10 + 8)
    assert m["traces_nh"] == pytest.approx(track * 1e9, rel=0.02)
    assert m["fanout_mils"][0] == pytest.approx(1800, abs=1)
    assert r["load"]["mount_nh_included"] > 12.0
    # the curve has it: at the top of the sweep the load is that inductance
    top = r["curve"][-1]
    assert top["mohm"] / 1e3 == pytest.approx(2 * math.pi * top["hz"] * m["total_nh"] * 1e-9,
                                              rel=0.05)


def test_a_load_off_the_plane_pair_with_no_copper_to_it_is_said():
    st = stack_from_stackup(STACKUP)
    r = im.analyse(_split_board(False), st, "+1V0", "GND", load={"ref": "U3"},
                   target_ohms=0.05, points=31)
    notes = " | ".join(r["notes"])
    assert "no power via reached" in notes                     # 1 nH assumed, said at the top
    assert "the load is 1" in notes and "off the plane pair" in notes
    assert r["load"]["off_plane_mils"] > 1600
    # the distance is in the number: far more than the 1 nH assumed for the vias
    assert r["load"]["mount_nh_included"] > 8.0


def test_perforated_plane_under_a_bga_stays_and_does_not_depend_on_the_grid():
    st = stack_from_stackup(STACKUP)
    z, area = [], []
    for shift in (0.0, 14.0, 28.0, 41.0):
        b = bare_board()
        add_bga(b, 20, 1600 + shift, 1100 + 0.6 * shift, dog=0.0, land=18, hole=12)
        cav = im.find_cavity(b, st, "+1V0", "GND", 1e9)
        # every cell under the part is still a node: copper with holes, not a hole
        ix0, iy0 = int((1600 + shift - cav.x0) / cav.cell_mils), int((1100 - cav.y0) / cav.cell_mils)
        assert cav.cells[iy0 + 1:iy0 + 8, ix0 + 1:ix0 + 8].all()
        assert 0.3 < cav.fill[iy0 + 3, ix0 + 3] < 0.95
        area.append(cav.area_mils2)
        r = im.analyse(b, st, "+1V0", "GND", load={"ref": "U2"}, target_ohms=0.05,
                       f_min_hz=2e8, f_max_hz=2.2e8, points=16, vrm={"disable": True})
        z.append(r["curve"][0]["mohm"])
    assert max(area) / min(area) - 1 < 0.002
    assert max(z) / min(z) - 1 < 0.04


def test_a_slot_through_one_plane_opens_the_cavity_there():
    st = stack_from_stackup(STACKUP)
    b = bare_board()
    # the +1V0 plane as two regions with a 6 mil slot between: far narrower than a cell
    b.regions += [Region("InternalPlane2", [(0, 0), (1997, 0), (1997, 3000), (0, 3000)], [], "+1V0", "copper"),
                  Region("InternalPlane2", [(2003, 0), (4000, 0), (4000, 3000), (2003, 3000)], [], "+1V0", "copper")]
    add_cap(b, "C10", 1000, 1500)
    add_cap(b, "C11", 3000, 1500)
    b.components.append(Component("U2", "X", 3200, 1500))
    b.pads += [smd("U2", "1", 3200, 1500, "+1V0"), smd("U2", "2", 3250, 1500, "GND")]
    b.vias += [via(3200, 1500, "+1V0"), via(3250, 1500, "GND")]
    r = im.analyse(b, st, "+1V0", "GND", load={"ref": "U2"}, target_ohms=0.05, points=31)
    assert any("2 separate islands; the one under the load" in n or "2 separate islands; the largest" in n
               for n in r["notes"])
    assert r["cavity"]["area_in2"] == pytest.approx(6.0, rel=0.02)   # the load's half only
    assert any(d["ref"] == "C10" and any("off the plane pair" in n for n in d.get("notes", []))
               for d in r["decaps"])


def test_load_current_is_split_by_pins_not_by_grid_nodes():
    cav = im.rectangular_cavity(4000, 3000, 4, 4.3, 0.02, cell_mils=100)
    f = np.array([3e8])
    a, _ = cav.node_near(1050, 1550)
    far, _ = cav.node_near(3050, 550)
    za = im.solve_cavity(cav, f, [], [a])[0]
    zf = im.solve_cavity(cav, f, [], [far])[0]
    even = im.solve_cavity(cav, f, [], [a, far])[0]
    pins = im.solve_cavity(cav, f, [], [a, far], load_weights=[30, 1])[0]
    # thirty pins at one node and one stray: the stray carries 1/31, not half
    assert abs(pins - za) < 0.2 * abs(even - za)
    # weights of one are the old equal split
    assert im.solve_cavity(cav, f, [], [a, far], load_weights=[1, 1])[0] == pytest.approx(even)
    with pytest.raises(im.PDNError):
        im.solve_cavity(cav, f, [], [a, far], load_weights=[1])
    assert zf != za


def test_impedance_inputs_are_checked_and_unknown_keys_are_said():
    b = plane_board()
    st = stack_from_stackup(STACKUP)
    kw = dict(load={"ref": "U2"}, target_ohms=0.02, points=31)
    bad = [
        (dict(kw, decaps=[{"ref": "C10", "c_uf": "big"}]), "c_uf must be a number"),
        (dict(kw, decaps=[{"ref": "C10", "esr_ohms": -1}]), "esr_ohms must be 0 or more"),
        (dict(kw, decaps=[{"c_uf": 0}]), "c_uf must be above 0"),
        (dict(kw, decaps=[{"esr_ohms": 0.01}]), "needs a ref"),
        (dict(kw, decaps="C10"), "list of dicts"),
        (dict(kw, decaps=["C10"]), "is a dict"),
        (dict(kw, vrm={"r_mohm": "x"}), "r_mohm must be a number"),
        (dict(kw, vrm={"r_mohm": 0, "l_nh": 0}), "is a short"),
        (dict(kw, f_min_hz="a"), "are numbers"),
        (dict(kw, plating_mils=0), "plating"),
    ]
    for args, msg in bad:
        with pytest.raises(im.PDNError, match=msg):
            im.analyse(b, st, "+1V0", "GND", **args)
    with pytest.raises(im.PDNError, match="same net"):
        im.analyse(b, st, "GND", "GND", **kw)
    with pytest.raises(im.PDNError, match="not on the board"):
        im.analyse(b, st, "+1V0", "AGND", **kw)
    r = im.analyse(b, st, "+1V0", "GND", load={"ref": "U2", "amps": 2}, target_ohms=0.02,
                   points=5000, decaps=[{"c_uf": 10, "esr": 0.01}], vrm={"ref": "U99"})
    notes = " | ".join(r["notes"])
    assert "load: ['amps'] not known and ignored" in notes
    assert "decaps[1]: ['esr'] not known and ignored" in notes
    assert "points 5000 taken as 2001" in notes
    assert "vrm: U99 has no pad on +1V0" in notes
    assert any(d["ref"] == "ADDED1" for d in r["decaps"])       # a value with no ref is added


def test_many_capacitors_are_listed_by_kind_and_all_are_in_the_file(tmp_path):
    import json
    b = plane_board(n_caps=4)
    for i in range(40):
        add_cap(b, f"C{100 + i}", 1000 + 45 * i, 2300, "1uF" if i % 2 else "100nF")
    r = im.analyse(b, stack_from_stackup(STACKUP), "+1V0", "GND", load={"ref": "U2"},
                   target_ohms=0.02, points=31, out_dir=tmp_path)
    assert "decaps" not in r and r["decap_count"] == 45
    assert sum(g["count"] for g in r["decap_groups"]) == 45
    assert len(json.dumps(r)) < 12000
    rows = (tmp_path / "+1V0_pdn_decaps.csv").read_text().splitlines()
    assert len(rows) == 46 and rows[0].startswith("ref,c_uf,")
    assert any(f.endswith("_pdn_impedance.png") for f in r["files"])
