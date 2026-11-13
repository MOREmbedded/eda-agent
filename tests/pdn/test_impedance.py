# SPDX-License-Identifier: Apache-2.0
"""AC PDN impedance against closed-form references."""

from __future__ import annotations

import math

import numpy as np
import pytest

from eda_agent.emc.stack import stack_from_stackup
from eda_agent.emc.touchstone import write_touchstone
from eda_agent.layout.model import Component, Pad, PadCopper, Region, Track, Via
from eda_agent.pdn import decap as dc
from eda_agent.pdn import impedance as im
from eda_agent.pdn import mounting as mt
from eda_agent.pdn import physics as ph

from .boards import STACKUP, add_bga, add_cap, bare_board, plane_board, smd, via


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


@pytest.mark.parametrize("text", ["100nH", "10k", "", "LM1117", "0402", "1N4148", "1N5819",
                                  "2N7002", "4N35", "BAT54 1N5819", "4.7uH"])
def test_not_a_capacitance(text):
    assert dc.parse_capacitance(text) is None


def test_a_value_beside_a_part_number_is_still_read():
    assert dc.parse_capacitance("22uF 1N4148") == pytest.approx(22e-6)
    assert dc.parse_capacitance("10u0") == pytest.approx(10e-6)


@pytest.mark.parametrize("ref,is_cap", [
    ("C1", True), ("CB12", True), ("C_101", True), ("c5", True), ("CAP1", True),
    ("CN1", False), ("CON3", False), ("CR2", False), ("CRY1", False),
    ("D1", False), ("Q2", False), ("U1", False), ("", False),
])
def test_capacitor_designators(ref, is_cap):
    assert dc.is_capacitor_ref(ref) is is_cap


@pytest.mark.parametrize("text,ohms", [("100nF", 0.03), ("0.1uF", 0.03), ("100n", 0.03),
                                       ("1uF", 0.012), ("10nF", 0.08), ("10uF", 0.005)])
def test_default_esr_does_not_depend_on_how_the_value_is_written(text, ohms):
    # 100 * 1e-9 is not the float 100e-9: a bucket edge there gave the
    # commonest decoupling value the ESR of a part ten times larger
    assert dc.default_esr(dc.parse_capacitance(text)) == pytest.approx(ohms, rel=1e-6)


def test_default_esr_is_continuous_and_falls_with_value():
    c = np.logspace(-10, -3, 141)
    esr = np.array([dc.default_esr(v) for v in c])
    assert np.all(np.diff(esr) <= 1e-12)
    assert np.max(esr[:-1] / esr[1:]) < 1.08          # no step between neighbours
    # between the anchors it is a straight line on log axes
    assert dc.default_esr(math.sqrt(100e-9 * 1e-6)) == pytest.approx(math.sqrt(0.03 * 0.012))


@pytest.mark.parametrize("footprint,comment,kind", [
    ("CAPMP3528X21N", "", "tantalum"), ("EIA-7343", "", "tantalum"), ("CASE-D", "", "tantalum"),
    ("C1206", "47uF Tantalum", "tantalum"), ("TAJB106K016", "", "tantalum"),
    ("C_POLY_7343", "", "polymer"), ("POSCAP_D", "", "polymer"), ("C7343", "330uF SP-Cap", "polymer"),
    ("CAPAE530X550N", "", "electrolytic"), ("CAPPRD250W50D500H1100", "", "electrolytic"),
    ("RB5-10.5", "", "electrolytic"), ("CP_Elec_8x10", "", "electrolytic"),
    ("POLAR0.8", "", "electrolytic"),
    # ceramics and words that only look like the above
    ("CAPC1005X55N", "100nF", ""), ("C0805", "22uF", ""), ("C_0603_1608Metric", "100n", ""),
    ("CAP-0603", "10uF Polypropylene", ""), ("C0805", "Samsung Electro 22uF", ""),
    ("0402_CONSTANT", "", ""),
])
def test_bulk_kind_from_names(footprint, comment, kind):
    assert dc.bulk_kind(footprint, comment) == kind


def test_bulk_kind_without_a_telling_name():
    assert dc.bulk_kind("XYZ", "", 22e-6, through_hole=True) == "electrolytic"
    assert dc.bulk_kind("XYZ", "", 100e-6) == "electrolytic"
    assert dc.bulk_kind("XYZ", "", 22e-6) == ""
    assert dc.bulk_kind("C1210", "", 100e-6) == ""          # a real 100 uF chip
    assert dc.bulk_kind("C_Radial_D5.0", "", 100e-9) == ""  # a ceramic disc
    assert dc.bulk_kind("C_Radial_D5.0", "", 100e-6) == "electrolytic"


def test_bulk_defaults_are_not_a_ceramics():
    c = 100e-6
    mlcc = dc.default_esr(c)
    assert dc.default_esr(c, "tantalum") > 50 * mlcc
    assert dc.default_esr(c, "electrolytic") > 50 * mlcc
    assert mlcc < dc.default_esr(c, "polymer") < dc.default_esr(c, "tantalum")
    assert dc.default_esl("", "tantalum") == pytest.approx(2e-9)
    assert dc.default_esl("", "electrolytic", through_hole=True) == pytest.approx(10e-9)
    assert dc.default_esl("0402") == pytest.approx(0.35e-9)


def _caps(b, decaps=None):
    st = stack_from_stackup(STACKUP)
    return im.build_decaps(b, st, im.find_cavity(b, st, "+1V0", "GND", 1e9),
                           "+1V0", "GND", decaps, 0.79)


def test_a_tantalum_on_the_board_gets_tantalum_values_and_says_so():
    b = bare_board()
    add_cap(b, "C10", 2000, 1700)
    b.components.append(Component("C20", "CAPMP7343X31N", 1000, 1500, comment="220uF 6.3V"))
    b.pads += [smd("C20", "1", 880, 1500, "+1V0", 90, 100), smd("C20", "2", 1120, 1500, "GND", 90, 100)]
    b.vias += [via(880, 1580, "+1V0"), via(1120, 1580, "GND")]
    caps, _ = _caps(b)
    by = {c.ref: c for c in caps}
    assert by["C20"].kind == "tantalum" and by["C20"].esr == pytest.approx(0.2)
    assert by["C20"].esl == pytest.approx(2e-9)
    assert any("tantalum part on typical values" in n for n in by["C20"].notes)
    assert by["C10"].kind == "mlcc" and by["C10"].esr == pytest.approx(0.03) and not by["C10"].notes
    # the data sheet's numbers take the note away; kind can be said outright
    caps, _ = _caps(b, [{"ref": "C20", "esr_ohms": 0.045, "esl_nh": 1.6}])
    c20 = next(c for c in caps if c.ref == "C20")
    assert c20.esr == pytest.approx(0.045) and not any("typical" in n for n in c20.notes)
    caps, _ = _caps(b, [{"ref": "C20", "kind": "polymer"}])
    assert next(c for c in caps if c.ref == "C20").esr == pytest.approx(0.025)
    with pytest.raises(im.PDNError, match="kind"):
        _caps(b, [{"ref": "C20", "kind": "paper"}])


def test_a_through_hole_electrolytic_is_found_by_its_holes():
    b = bare_board()
    b.components.append(Component("C30", "XYZ", 1000, 1500, comment="100uF"))
    for name, dx, net in (("1", -50, "+1V0"), ("2", 50, "GND")):
        b.pads.append(Pad("C30", name, 1000 + dx, 1500, 0.0, net,
                          [PadCopper(l, "round", 60, 60) for l in ("TopLayer", "BottomLayer")],
                          hole=32.0))
    caps, _ = _caps(b)
    assert caps[0].kind == "electrolytic" and caps[0].esl == pytest.approx(10e-9)
    assert caps[0].esr == pytest.approx(0.35)
    assert caps[0].mount_detail["how"] == ["through-hole pin"]


def test_a_diode_or_a_connector_across_the_rail_is_not_a_capacitor():
    b = bare_board()
    add_cap(b, "C10", 2000, 1700)
    for ref, comment in (("D1", "1N4148"), ("CN1", "HDR-2"), ("CR3", "1N5819"), ("Q4", "2N7002")):
        add_cap(b, ref, 1000 + 100 * len(b.components), 1000, value=comment)
    caps, notes = _caps(b)
    assert [c.ref for c in caps] == ["C10"]
    assert not any("D1" in n or "CN1" in n for n in notes)


def test_an_array_is_named_not_dropped_and_a_doubled_part_is_taken():
    b = bare_board()
    # CA1: four capacitors in one body, two of them on other rails
    b.components.append(Component("CA1", "CAPCAV4", 1000, 1000, comment="100nF"))
    for k, net in enumerate(("+1V0", "+3V3", "+1V8", "+1V0")):
        b.pads += [smd("CA1", str(k + 1), 1000 + 30 * k, 980, net, 14, 20),
                   smd("CA1", str(8 - k), 1000 + 30 * k, 1020, "GND", 14, 20)]
    # C40: a low-inductance part, two terminals a side, nothing else on it
    b.components.append(Component("C40", "C0612", 1500, 1000, comment="1uF"))
    for k in range(2):
        b.pads += [smd("C40", f"P{k}", 1500 + 40 * k, 980, "+1V0", 20, 20),
                   smd("C40", f"G{k}", 1500 + 40 * k, 1020, "GND", 20, 20)]
    caps, notes = _caps(b)
    assert [c.ref for c in caps] == ["C40"]
    note = next(n for n in notes if n.startswith("CA1:"))
    assert "8 pads" in note and "+1V8" in note and "decaps" in note
    # asked for by name, the array's share on this rail is counted
    caps, notes = _caps(b, [{"ref": "CA1", "c_uf": 0.2}])
    assert sorted(c.ref for c in caps) == ["C40", "CA1"]
    assert not any(n.startswith("CA1:") for n in notes)


def test_a_feedthrough_capacitor_is_counted_for_its_value_to_ground():
    b = bare_board()
    # in on the rail, out on another rail, ground twice
    b.components.append(Component("C12", "CAPFT0603", 1000, 1000,
                                  comment="CAP FEEDTHRU 1UF 20% 6.3V 0603"))
    b.pads += [smd("C12", "1", 970, 1000, "+1V0", 14, 30), smd("C12", "3", 1030, 1000, "VDD_ESP", 14, 30),
               smd("C12", "2", 1000, 985, "GND", 20, 10), smd("C12", "4", 1000, 1015, "GND", 20, 10)]
    caps, notes = _caps(b)
    assert [c.ref for c in caps] == ["C12"] and caps[0].c == pytest.approx(1e-6)
    assert any("feedthrough" in n and "VDD_ESP is not modelled" in n for n in caps[0].notes)
    assert not any(n.startswith("C12:") for n in notes)
    # not that shape: two pads on the other net, or one ground pad of five, stays an array
    for extra in (smd("C12", "5", 1060, 1000, "VDD_ESP", 14, 30), smd("C12", "5", 1060, 1000, "+3V3", 14, 30)):
        b.pads.append(extra)
        caps, notes = _caps(b)
        assert caps == [] and any(n.startswith("C12: 5 pads") for n in notes)
        b.pads.pop()
    # four pads with two other nets is an array too
    b.pads[-1] = smd("C12", "4", 1000, 1015, "+3V3", 20, 10)
    caps, notes = _caps(b)
    assert caps == [] and any(n.startswith("C12: 4 pads") for n in notes)
    # and a doubled two-terminal part gets no feedthrough note
    b = bare_board()
    b.components.append(Component("C40", "C0612", 1500, 1000, comment="1uF"))
    for k in range(2):
        b.pads += [smd("C40", f"P{k}", 1500 + 40 * k, 980, "+1V0", 20, 20),
                   smd("C40", f"G{k}", 1500 + 40 * k, 1020, "GND", 20, 20)]
    caps, _ = _caps(b)
    assert [c.ref for c in caps] == ["C40"] and not any("feedthrough" in n for n in caps[0].notes)


def test_a_part_given_by_ref_is_taken_whatever_it_is_called():
    b = bare_board()
    add_cap(b, "X7", 2000, 1700, value="")          # a capacitor by neither name nor comment
    caps, _ = _caps(b)
    assert caps == []
    caps, _ = _caps(b, [{"ref": "X7", "c_uf": 0.1}])
    assert caps[0].ref == "X7" and caps[0].mount_detail["via_pairs"] == 1
    # one given that is on the board but not across the rail is an added part, and says so
    b.components.append(Component("R1", "R0402", 500, 500, comment="10k"))
    b.pads += [smd("R1", "1", 500, 480, "+3V3"), smd("R1", "2", 500, 520, "GND")]
    caps, _ = _caps(b, [{"ref": "R1", "c_uf": 1.0}])
    assert any("not across +1V0 and GND" in n for n in caps[0].notes)
    _, notes = _caps(b, [{"ref": "R1", "esr_ohms": 0.01}])
    assert any("R1 is not a part across" in n for n in notes)


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
        # Two real vias of radius r, k cells apart, one carrying the
        # current down and the other back: the loop between the planes is
        # the two-wire formula (mu0 d / pi) acosh(D / 2r). The grid gives
        # 2 (Ls - Lt) for vias of the node's radius; the port term makes
        # up the rest for each via, whatever the cell.
        for r in (5.0, 12.0):
            loop = 2 * L + 2 * ph.cavity_port_inductance(4, r, 100.0)
            assert loop == pytest.approx(ph.via_pair_loop_inductance(4, k * 100.0, r), rel=0.01)
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


def _sheet_conductance(fill):
    """Conductance from the left edge to the right of a sheet with the
    copper share ``fill`` per sample (rows across, columns along), sides
    closed, in squares: a plain five-point solution, written here
    independently of the model under test."""
    import scipy.sparse as sp
    from scipy.sparse.linalg import spsolve
    ny, nx = fill.shape
    idx = np.arange(ny * nx).reshape(ny, nx)
    gx = 2 * fill[:, :-1] * fill[:, 1:] / np.maximum(fill[:, :-1] + fill[:, 1:], 1e-12)
    gy = 2 * fill[:-1, :] * fill[1:, :] / np.maximum(fill[:-1, :] + fill[1:, :], 1e-12)
    a = np.r_[idx[:, :-1].ravel(), idx[:-1, :].ravel()]
    b = np.r_[idx[:, 1:].ravel(), idx[1:, :].ravel()]
    g = np.r_[gx.ravel(), gy.ravel()]
    A = sp.coo_matrix((np.r_[-g, -g], (np.r_[a, b], np.r_[b, a])), shape=(ny * nx,) * 2).tocsr()
    diag = -np.asarray(A.sum(axis=1)).ravel()
    left, right = 2 * fill[:, 0], 2 * fill[:, -1]
    diag[idx[:, 0]] += left
    diag[idx[:, -1]] += right
    diag[diag == 0] = 1.0
    rhs = np.zeros(ny * nx)
    rhs[idx[:, 0]] = left
    u = spsolve((A + sp.diags(diag)).tocsc(), rhs)
    return float((left * (1 - u[idx[:, 0]])).sum()) * nx / ny


def _samples(length, width, h):
    x = (np.arange(int(round(length / h))) + 0.5) * h
    y = (np.arange(int(round(width / h))) + 0.5) * h
    return np.meshgrid(x, y)


def test_a_sheet_with_round_holes_conducts_as_the_series_says():
    assert ph.holed_sheet_conductance(0.0) == 1.0
    assert ph.holed_sheet_conductance(0.2) == pytest.approx(0.6665, abs=2e-4)
    # holes that touch: nothing more to take away
    assert ph.holed_sheet_conductance(0.9) == ph.holed_sheet_conductance(math.pi / 4) < 0.05
    got = ph.holed_sheet_conductance(np.array([0.0, 0.5]))
    assert got.shape == (2,) and got[1] == pytest.approx(0.3247, abs=2e-4)
    # one cell of a square array, solved finely: the dilute formula
    # (1 - v) / (1 + v) is 8 % out at v = 0.6, the series is not
    for v in (0.2, 0.385, 0.6, 0.684):
        X, Y = _samples(1.0, 1.0, 1.0 / 240)
        r = math.sqrt(v / math.pi)
        fill = ((X - 0.5) ** 2 + (Y - 0.5) ** 2 > r * r).astype(float)
        assert ph.holed_sheet_conductance(v) == pytest.approx(_sheet_conductance(fill), rel=0.02)


def _via_field(kind, pitch=40.0, hole=8.0):
    """A plane pair pierced all over by vias on a square pitch: of the
    ground net (antipads in the power plane only), of both nets in a
    chequerboard (every other antipad in the other plane), or of a signal
    (the same antipad in both)."""
    b = bare_board()
    w, h = round(2000 / pitch) * pitch, round(1600 / pitch) * pitch
    b.outline = [(0, 0), (w, 0), (w, h), (0, h)]
    for i in range(int(round(w / pitch))):
        for j in range(int(round(h / pitch))):
            net = {"one": "GND", "cheq": "+1V0" if (i + j) % 2 == 0 else "GND",
                   "same": "SIG"}.get(kind)
            if net:
                b.vias.append(Via((i + 0.5) * pitch, (j + 0.5) * pitch, hole + 8, hole,
                                  "TopLayer", "BottomLayer", net))
    return b


@pytest.mark.parametrize("cell", [0.0, 80.0])
def test_a_field_of_antipads_slows_the_plane_pair_as_a_holed_sheet(cell):
    # The planes are 4 mil apart and an antipad is 28 mil across: the field
    # is only where both planes have copper, so the pair is one sheet with
    # the holes of both. Its first mode falls by sqrt(k / (1 - v)): k the
    # sheet conductance left by the holes, 1 - v the capacitance left.
    st = stack_from_stackup(STACKUP)
    solid = im.find_cavity(_via_field("none"), st, "+1V0", "GND", 1e9, cell)
    v = math.pi * (8 / 2 + 10) ** 2 / 40.0 ** 2          # hole radius + plane clearance
    want = solid.first_mode_hz() * math.sqrt(ph.holed_sheet_conductance(v) / (1 - v))
    assert 0.80 < want / solid.first_mode_hz() < 0.90
    for kind in ("one", "cheq", "same"):
        cav = im.find_cavity(_via_field(kind), st, "+1V0", "GND", 1e9, cell)
        assert (cav.cells == solid.cells).all() and not cav.notes
        assert cav.capacitance_f == pytest.approx(solid.capacitance_f * (1 - v), rel=0.01)
        assert cav.first_mode_hz() == pytest.approx(want, rel=0.01)


def test_a_part_cell_at_the_copper_edge_stays_joined_to_the_plane():
    # 1600 mil is 38.4 cells of 41.67: the last row holds a strip of copper
    # 0.4 of a cell wide and its centre is off the board. The strip is the
    # plane's own copper, not an island.
    st = stack_from_stackup(STACKUP)
    cav = im.find_cavity(_via_field("none"), st, "+1V0", "GND", 1e9)
    assert cav.cell_mils == pytest.approx(2000 / 48) and cav.cells.shape == (39, 48)
    assert cav.cells.all() and not cav.notes
    assert cav.fill[-1, 5] == pytest.approx(0.4, abs=0.01)
    assert cav.area_mils2 == pytest.approx(2000 * 1600, rel=1e-3)
    assert cav.first_mode_hz() == pytest.approx(
        ph.C0 / (2 * 2000 * ph.MIL * math.sqrt(cav.er)), rel=0.005)
    # a strip across a real gap is still apart: 30 mil of nothing, then copper
    b = _via_field("none")
    b.regions += [Region("InternalPlane2", [(0, 0), (2000, 0), (2000, 1560), (0, 1560)], [], "+1V0", "copper"),
                  Region("InternalPlane2", [(0, 1590), (2000, 1590), (2000, 1600), (0, 1600)], [], "+1V0", "copper")]
    cav = im.find_cavity(b, st, "+1V0", "GND", 1e9)
    assert cav.area_mils2 == pytest.approx(2000 * 1560, rel=2e-3)
    # the same at the other side of a cell: a plane that starts 30 mil in
    b = _via_field("none")
    b.regions.append(Region("InternalPlane2", [(0, 30), (2000, 30), (2000, 1600), (0, 1600)],
                            [], "+1V0", "copper"))
    cav = im.find_cavity(b, st, "+1V0", "GND", 1e9)
    assert cav.cells.all() and not cav.notes
    assert cav.fill[0, 5] == pytest.approx(0.28, abs=0.01)
    assert cav.area_mils2 == pytest.approx(2000 * 1570, rel=1e-3)


def test_antipads_four_mils_apart_still_leave_the_plane_whole():
    st = stack_from_stackup(STACKUP)
    solid = im.find_cavity(_via_field("none", 32.0), st, "+1V0", "GND", 1e9, 80.0)
    cav = im.find_cavity(_via_field("one", 32.0), st, "+1V0", "GND", 1e9, 80.0)
    v = math.pi * 14 ** 2 / 32.0 ** 2                    # 0.60: webs of 4 mil
    assert cav.cells.all() and not cav.notes
    assert cav.first_mode_hz() == pytest.approx(
        solid.first_mode_hz() * math.sqrt(ph.holed_sheet_conductance(v) / (1 - v)), rel=0.015)


def test_a_large_board_is_sampled_as_finely_as_memory_allows_and_says_so(monkeypatch):
    st = stack_from_stackup(STACKUP)
    fine = im.find_cavity(_via_field("one"), st, "+1V0", "GND", 1e9, 80.0)
    monkeypatch.setattr(im, "MAX_SAMPLES", 100_000)      # 2000 x 1600 mil: 5.7 mil samples
    cav = im.find_cavity(_via_field("one"), st, "+1V0", "GND", 1e9, 80.0)
    assert any("sampled every" in n and "may read as cut" in n for n in cav.notes)
    assert cav.first_mode_hz() == pytest.approx(fine.first_mode_hz(), rel=0.03)
    # and the fine grid itself stays inside the raster's limit
    from eda_agent.layout import copper_raster
    monkeypatch.setattr(copper_raster, "MAX_CELLS", 20_000)   # 500 cells x 8 x 8 would be 32 000
    cav = im.find_cavity(_via_field("one"), st, "+1V0", "GND", 1e9, 80.0)
    assert cav.first_mode_hz() == pytest.approx(fine.first_mode_hz(), rel=0.03)


def _row_of_vias(pitch):
    b = bare_board()
    b.outline = [(0, 0), (2000, 0), (2000, 1600), (0, 1600)]
    for j in range(int(round(1600 / pitch))):
        b.vias.append(Via(800.0, (j + 0.5) * pitch, 16, 8, "TopLayer", "BottomLayer", "GND"))
    return b


@pytest.mark.parametrize("pitch", [32.0, 40.0])
def test_a_row_of_antipads_across_the_way_is_a_neck_not_a_field(pitch):
    # A fence of ground vias from edge to edge, on the line between two
    # columns of cells: its antipads leave webs of 4 mil (12 mil) in the
    # power plane. For its share of the cell's area a field of holes would
    # leave the link at 0.6 and more; the webs are what the current has to
    # get through. Against a fine solution of the same 80 mil of plane.
    st = stack_from_stackup(STACKUP)
    cav = im.find_cavity(_row_of_vias(pitch), st, "+1V0", "GND", 1e9, 80.0)
    X, Y = _samples(80.0, pitch, 0.25)
    want = _sheet_conductance(((X - 40) ** 2 + (Y - pitch / 2) ** 2 > 14 ** 2).astype(float))
    got = cav.link_x[4:-4, 9]
    assert want < 0.62 and got.max() - got.min() < 0.02 * want
    assert float(got.mean()) == pytest.approx(want, rel=0.12)
    # nothing else on the board is touched: the next links are whole, and
    # along the fence the cells lose the scallops only
    assert (cav.link_x[4:-4, 7] == 1.0).all() and (cav.link_x[4:-4, 11] == 1.0).all()
    assert 0.7 < float(cav.link_y[4:-4, 9].mean()) < 0.9
    assert cav.cells.all() and not cav.notes              # one piece


def test_a_split_with_a_bridge_conducts_through_the_bridge():
    st = stack_from_stackup(STACKUP)
    b = bare_board()
    b.outline = [(0, 0), (2000, 0), (2000, 1600), (0, 1600)]
    # the power plane in two halves 6 mil apart, joined by 20 mil of copper
    b.regions += [Region("InternalPlane2", [(0, 0), (797, 0), (797, 1600), (0, 1600)], [], "+1V0", "copper"),
                  Region("InternalPlane2", [(803, 0), (2000, 0), (2000, 1600), (803, 1600)], [], "+1V0", "copper"),
                  Region("InternalPlane2", [(790, 750), (810, 750), (810, 770), (790, 770)], [], "+1V0", "copper")]
    cav = im.find_cavity(b, st, "+1V0", "GND", 1e9, 80.0)
    lx = cav.link_x[:, 9]
    assert (np.delete(lx, 9) == 0).all()                  # the slot, everywhere else
    X, Y = _samples(80.0, 80.0, 0.25)
    want = _sheet_conductance(np.where((abs(X - 40) < 3) & (abs(Y - 40) > 10), 0.0, 1.0))
    assert 0.5 < want < 0.6
    assert lx[9] == pytest.approx(want, rel=0.2)
    assert cav.cells.all() and not cav.notes              # one piece, through the bridge
    # and without the bridge the two halves are apart
    b.regions.pop()
    cav = im.find_cavity(b, st, "+1V0", "GND", 1e9, 80.0, near=[(1500, 800)])
    assert any("2 separate islands" in n for n in cav.notes)
    assert cav.area_mils2 == pytest.approx(1197 * 1600, rel=0.01)


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


def test_a_pad_joined_to_its_via_through_a_pour():
    # The power pad sits in a small top-layer pour with its via 80 mil up
    # the pour and no track; the ground pad has its via in the pad. The
    # way through the pour is a strip between two round contacts over the
    # ground plane 4 mil below: (mu0 h / pi) acosh(d / 2a), a the geometric
    # mean of the pad's half width and the barrel's wall radius.
    b = bare_board()
    add_cap(b, "C10", 2000, 1700, power_via=False, ground_via=False)
    b.vias += [via(2000, 1720, "GND"), via(2000, 1600, "+1V0")]
    b.regions.append(Region("TopLayer", [(1950, 1560), (2050, 1560), (2050, 1692), (1950, 1692)],
                            [], "+1V0", "pour", source="polygon:P"))
    caps, notes = _caps(b)
    m = caps[0].mount_detail
    assert sorted(m["how"]) == ["through a pour", "via in pad"] and "assumed" not in m
    a = math.sqrt(10.0 * (5.0 + 0.79))
    want = ph.MU0 * 4.0 * ph.MIL * math.acosh(80.0 / (2 * a)) / math.pi
    assert m["traces_nh"] == pytest.approx(want * 1e9, rel=0.02)
    assert m["fanout_mils"] == [80.0, 0.0] or sorted(m["fanout_mils"]) == [0.0, 80.0]


def test_mounting_resistance_is_the_two_barrels_each_to_its_own_plane():
    # Both vias in the pads, so there is no fan-out: what is left is the
    # ground barrel down to the ground plane and the power barrel down to
    # the power plane, which lies the cavity's height further.
    b = bare_board()
    add_cap(b, "C10", 2000, 1700, power_via=False, ground_via=False)
    b.vias += [via(2000, 1680, "+1V0"), via(2000, 1720, "GND")]
    st = stack_from_stackup(STACKUP)
    cav = im.find_cavity(b, st, "+1V0", "GND", 1e9)
    caps, _ = im.build_decaps(b, st, cav, "+1V0", "GND", None, 0.79)
    m = caps[0].mount_detail
    assert m["how"] == ["via in pad"] and m["traces_nh"] == 0.0
    h, d = m["height_mils"], cav.d_mils
    assert h > 0 and d > 0
    want = ph.via_barrel_resistance(h, 10.0, 0.79) + ph.via_barrel_resistance(h + d, 10.0, 0.79)
    assert m["resistance_mohm"] == pytest.approx(want * 1e3, rel=0.01)
    assert caps[0].mount_r == pytest.approx(want, rel=0.01)
    assert ph.via_barrel_resistance(h + d, 10.0, 0.79) > 1.5 * ph.via_barrel_resistance(h, 10.0, 0.79)


def test_a_blind_via_in_the_pour_that_stops_short_of_the_plane_is_not_a_way_down():
    b = bare_board()
    add_cap(b, "C10", 2000, 1700, power_via=False, ground_via=False)
    b.vias.append(via(2000, 1720, "GND"))
    # top to layer 2 only: it never reaches +1V0 on layer 3
    b.vias.append(Via(2000, 1600, 20, 10, "TopLayer", "InternalPlane1", "+1V0"))
    b.regions.append(Region("TopLayer", [(1950, 1560), (2050, 1560), (2050, 1692), (1950, 1692)],
                            [], "+1V0", "pour", source="polygon:P"))
    caps, notes = _caps(b)
    m = caps[0].mount_detail
    assert m.get("assumed") and "through a pour" not in m.get("how", [])
    assert any("1 nH mounting assumed: C10" in n for n in notes)


def test_a_via_by_the_pad_with_no_copper_drawn_to_it_is_taken_and_said():
    b = bare_board()
    add_cap(b, "C10", 2000, 1700, power_via=False, ground_via=False)
    b.vias += [via(2000, 1645, "+1V0"), via(2000, 1755, "GND")]     # 25 mil off each pad, no track
    caps, notes = _caps(b)
    m = caps[0].mount_detail
    assert m["how"] == ["nearest via"] and "assumed" not in m and m["via_pairs"] == 1
    assert any("C10.1: no copper path to a via found; the nearest +1V0 via (35 mil)" in n
               for n in m["notes"])
    assert any("no copper drawn from a pad to a via" in n and n.endswith("C10") for n in notes)


def test_the_nearest_via_is_not_taken_when_it_is_a_neighbours():
    # C10 has no via of its own; C11, 60 mil along, has both of its. Its
    # vias are C11's way down, not C10's: C10 is on the assumed 1 nH.
    b = bare_board()
    add_cap(b, "C10", 2000, 1700, power_via=False, ground_via=False)
    add_cap(b, "C11", 2060, 1700)
    caps, notes = _caps(b)
    by = {c.ref: c for c in caps}
    assert by["C10"].mount_detail.get("assumed") and by["C10"].mount_l == pytest.approx(1e-9)
    assert by["C11"].mount_detail["via_pairs"] == 1 and "assumed" not in by["C11"].mount_detail
    assert any("1 nH mounting assumed: C10" in n for n in notes)


def test_fan_out_resistance_is_added_to_the_barrels():
    # The same capacitor with its vias in the pads and with each via 25 mil
    # off the pad on a 12 mil track: the tracks add their own resistance,
    # 5 mil of track from the pad's edge to the land's, then the land (20
    # mil wide) in to the barrel wall, on each side.
    def mount_r(in_pad):
        b = bare_board()
        if in_pad:
            add_cap(b, "C10", 2000, 1700, power_via=False, ground_via=False)
            b.vias += [via(2000, 1680, "+1V0"), via(2000, 1720, "GND")]
        else:
            add_cap(b, "C10", 2000, 1700)
        return _caps(b)[0][0].mount_r
    rs = ph.sheet_resistance(1.4)
    want = 2 * rs * (5.0 / 12.0 + (10.0 - 5.79) / 20.0)
    assert mount_r(False) - mount_r(True) == pytest.approx(want, rel=0.03)


def test_files_that_cannot_be_written_cost_the_files_not_the_numbers(tmp_path):
    st = stack_from_stackup(STACKUP)
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    r = im.analyse(plane_board(), st, "+1V0", "GND", load={"ref": "U2"}, target_ohms=0.02,
                   points=31, out_dir=blocker / "runs")
    assert r["worst"]["mohm"] > 0 and r["curve"]
    assert r["files"] == [] and r["files_error"].split(":")[0] in (
        "NotADirectoryError", "FileExistsError", "FileNotFoundError")


def test_mounting_resistance_is_in_series_with_the_capacitor_and_in_the_deck(tmp_path):
    d = dc.Decap("C1", 100e-9, 0.02, 0.4e-9, mount_l=0.6e-9, mount_r=0.005)
    srf = 1 / (2 * math.pi * math.sqrt(d.c * (d.esl + d.mount_l)))
    z = d.z(np.array([srf]))[0]
    assert z.real == pytest.approx(0.025, rel=1e-6) and abs(z.imag) < 1e-6
    deck = tmp_path / "p.cir"
    im.write_spice(deck, np.array([1e5, 1e8]), [d], None, 1e-9, 0.0, "t")
    assert "R1 pdn n1a 0.025" in deck.read_text()
    # a value with more than two digits after the letter is not that notation
    assert dc.parse_capacitance("100n5000") == pytest.approx(100e-9)


def test_a_hairline_slot_on_a_cell_centre_line_still_parts_the_plane():
    # 4 mil of nothing, narrower than a fine cell, exactly where the cell
    # centres are: copper on both sides of the centre is no reason to step
    # over what lies between
    st = stack_from_stackup(STACKUP)
    b = bare_board()
    b.outline = [(0, 0), (2000, 0), (2000, 1600), (0, 1600)]
    b.regions += [Region("InternalPlane2", [(0, 0), (838, 0), (838, 1600), (0, 1600)], [], "+1V0", "copper"),
                  Region("InternalPlane2", [(842, 0), (2000, 0), (2000, 1600), (842, 1600)], [], "+1V0", "copper")]
    cav = im.find_cavity(b, st, "+1V0", "GND", 1e9, 80.0, near=[(1500, 800)])
    assert any("2 separate islands" in n for n in cav.notes)
    assert cav.area_mils2 < 0.62 * 2000 * 1600


def test_a_capacitor_value_is_read_from_its_parameters_when_the_comment_has_none():
    f = dc.capacitance_from_parameters
    assert f({"Value": "100nF", "Voltage": "16V"}) == (pytest.approx(100e-9), "Value")
    assert f([{"name": "Tolerance", "value": "10%"}, {"name": "capacitance", "value": "4u7"}]) \
        == (pytest.approx(4.7e-6), "capacitance")
    # a parameter named as the value comes before one that only mentions it
    assert f({"Value (alt)": "1uF", "Val": "22n"}) == (pytest.approx(22e-9), "Val")
    assert f({"Component Value": "10 uF"}) == (pytest.approx(10e-6), "Component Value")
    # not a value: a voltage, a part number, nothing
    assert f({"Value": "16V", "Manufacturer Part Number": "GRM155R71C104KA88D"}) is None
    assert f({}) is None and f(None) is None and f({"Value": ""}) is None

    st = stack_from_stackup(STACKUP)
    b = plane_board()
    for c in b.components:
        if c.ref in ("C10", "C11", "C12", "C13"):
            c.comment = "CAP CER SMD 0402"
    kw = dict(load={"ref": "U2"}, target_ohms=0.02, points=31)
    assert sorted(im.decaps_without_a_value(b, "+1V0", "GND")) == ["C10", "C11", "C12", "C13"]
    assert im.decaps_without_a_value(b, "+1V0", "GND", [{"ref": "C10", "c_uf": 1.0}]) == \
        ["C11", "C12", "C13"]
    bare = im.analyse(b, st, "+1V0", "GND", **kw)
    assert [d["ref"] for d in bare["decaps"]] == ["C1"] and sum("no capacitance in its comment" in n for n in bare["notes"]) == 4
    vals = {r: (100e-9, "the parameter 'Value'") for r in ("C10", "C11", "C12")}
    r = im.analyse(b, st, "+1V0", "GND", cap_values=vals, **kw)
    assert sorted(d["ref"] for d in r["decaps"]) == ["C1", "C10", "C11", "C12"]
    assert all(d["c_uf"] == pytest.approx(0.1) and "value from the parameter 'Value'" in d["notes"]
               for d in r["decaps"] if d["ref"] != "C1")
    assert not any("value from" in n for d in r["decaps"] if d["ref"] == "C1" for n in d.get("notes", []))
    assert any("3 capacitor(s) with no value in the comment" in n for n in r["notes"])
    assert sum("C13: no capacitance in its comment" in n for n in r["notes"]) == 1
    # what the caller gives wins, and a comment that is a value is not asked about
    r = im.analyse(b, st, "+1V0", "GND", cap_values=vals,
                   decaps=[{"ref": "C10", "c_uf": 2.2}], **kw)
    assert next(d for d in r["decaps"] if d["ref"] == "C10")["c_uf"] == pytest.approx(2.2)
    assert im.decaps_without_a_value(plane_board(), "+1V0", "GND") == []
