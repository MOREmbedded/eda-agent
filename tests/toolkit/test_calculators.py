# SPDX-License-Identifier: Apache-2.0
"""Every calculator runs, declares itself, and gives the right numbers."""
from __future__ import annotations

import math

import numpy as np
import pytest

from eda_agent.engineering import toolkit
from eda_agent.engineering.toolkit import core, plane, prefill, signal
from eda_agent.engineering.toolkit.fdsolver import C0, EPS0

MIL = 25.4e-6


def out(name, inputs=None):
    r = toolkit.run(name, inputs or {})
    assert r["ok"], r
    return {o["key"]: o["value"] for o in r["outputs"]}, r


# ------------------------------------------------------------ the registry

def test_the_toolkit_covers_every_saturn_tab():
    need = {
        # conductor
        "trace_current", "conductor_properties", "fusing_current", "conductor_spacing",
        # conductor impedance
        "microstrip", "embedded_microstrip", "stripline", "dual_stripline", "coplanar",
        "er_effective",
        # differential pairs
        "edge_coupled_microstrip", "edge_coupled_embedded", "edge_coupled_stripline",
        "broadside_coupled_stripline",
        # via, planes, crosstalk
        "via_properties", "via_pair", "plane_pair", "pdn_target", "crosstalk",
        # signal, thermal
        "wavelength", "rise_time_bandwidth", "ppm", "junction_temperature",
        # Ohm's law tab
        "ohms_law", "reactance", "series_parallel", "voltage_divider", "led_resistor",
        "attenuator", "crystal_load",
        # padstack, mechanical, conversions
        "padstack", "bga_land", "conductor_to_pad", "corner_to_corner",
        "embedded_resistor", "planar_inductor", "wire_gauge", "drill_chart",
        "unit_convert", "db_convert",
    }
    assert need <= set(toolkit.REGISTRY)


@pytest.mark.parametrize("name", sorted(toolkit.REGISTRY))
def test_every_calculator_runs_on_its_defaults_and_declares_itself(name):
    calc = toolkit.REGISTRY[name]
    r = calc.run({})
    assert r["ok"], r
    assert r["model"], "a result must name its model"
    assert r["status"] in ("exact", "solver", "published", "estimate")
    assert any(o["primary"] for o in r["outputs"])
    for o in r["outputs"]:
        assert o["value"] is None or math.isfinite(o["value"]) or o["kind"] in ("db", "power", "resistance")
        assert o["display"]
    if r["status"] in ("solver", "estimate"):
        assert r["accuracy"], "a solver or estimate must state its accuracy"
    spec = calc.spec()
    assert spec["group"] in {k for k, _ in core.GROUPS}
    keys = [i["key"] for i in spec["inputs"]]
    assert len(keys) == len(set(keys))
    for i in spec["inputs"]:
        for dep in (i.get("show_if") or {}):
            assert dep in keys


def test_catalog_lists_every_calculator_once():
    cat = toolkit.catalog()
    names = [c["name"] for g in cat["groups"] for c in g["calculators"]]
    assert sorted(names) == sorted(toolkit.REGISTRY) and cat["count"] == len(names)


def test_unknown_calculator_and_unknown_input_are_refused():
    assert not toolkit.run("nope")["ok"]
    r = toolkit.run("microstrip", {"width": 5})
    assert not r["ok"] and "width" in r["reason"]
    r = toolkit.run("microstrip", {"w": -1})
    assert not r["ok"]
    r = toolkit.run("microstrip", {"w": "wide"})
    assert not r["ok"] and "w:" in r["reason"]


# ------------------------------------------------------------ units

def test_units_parse_and_never_confuse_milli_with_mega():
    assert core.to_si("5 mil", "length", "mm") == pytest.approx(127e-6)
    assert core.to_si(5, "length", "mil") == pytest.approx(127e-6)
    assert core.to_si({"value": 0.2, "unit": "mm"}, "length", "mil") == pytest.approx(0.2e-3)
    assert core.to_si("1 oz", "length", "mil") == pytest.approx(35.0e-6, rel=1e-3)
    assert core.to_si("2.4GHz", "frequency", "Hz") == 2.4e9
    assert core.to_si("2.4 ghz", "frequency", "Hz") == 2.4e9
    assert core.to_si("10 mA", "current", "A") == pytest.approx(0.01)
    assert core.to_si("212 °F", "temperature", "°C") == pytest.approx(100.0)
    assert core.to_si("373.15 K", "temperature", "°C") == pytest.approx(100.0)
    for bad in ("5 MA", "3 MW", "1 Mv", "2 MF"):
        kind = {"A": "current", "W": "power", "v": "voltage", "F": "capacitance"}[bad[-1]]
        with pytest.raises(core.InputError):
            core.to_si(bad, kind, "")
    with pytest.raises(core.InputError):
        core.to_si("5 parsec", "length", "mm")
    with pytest.raises(core.InputError):
        core.to_si(True, "length", "mm")
    assert core.to_si("1 Mohm", "resistance", "ohm") == 1e6
    assert core.to_si("1 mohm", "resistance", "ohm") == 1e-3


def test_display_strings():
    assert core.fmt(0.000127, "length") == "0.1270 mm (5.000 mil)"
    assert core.fmt(4.7e-9, "capacitance") == "4.700 nF"
    assert core.fmt(50.0, "resistance") == "50.00 Ω"
    assert core.fmt(2.4e9, "frequency") == "2.400 GHz"
    assert core.fmt(float("nan"), "voltage") == "n/a"


# ------------------------------------------------------------ impedance

def test_laminate_er_moves_with_the_analysis_frequency():
    lo, _ = out("stripline", {"frequency": "100 MHz", "f_spec": "1 GHz"})
    hi, _ = out("stripline", {"frequency": "10 GHz", "f_spec": "1 GHz"})
    at, _ = out("stripline", {"frequency": "1 GHz", "f_spec": "1 GHz"})
    assert lo["er_eff"] > at["er_eff"] > hi["er_eff"]
    assert at["er_eff"] == pytest.approx(4.2, rel=1e-6)
    assert lo["z0"] < at["z0"] < hi["z0"]


def test_closed_form_engine_agrees_with_the_solver():
    for name in ("microstrip", "stripline"):
        s, _ = out(name, {"method": "solver", "frequency": 0})
        c, r = out(name, {"method": "closed", "frequency": 0})
        assert c["z0"] == pytest.approx(s["z0"], rel=0.005)
        assert r["status"] == "published"


def test_solver_result_carries_its_cross_check_and_section():
    _, r = out("microstrip")
    assert r["status"] == "solver" and r["section"]["metals"]
    chk = r["cross_checks"][0]
    assert "Hammerstad" in chk["model"] and abs(chk["difference_percent"]) < 0.5


def test_differential_pair_relations():
    v, _ = out("edge_coupled_microstrip", {"frequency": 0})
    assert v["zdiff"] == pytest.approx(2 * v["zodd"], rel=1e-9)
    assert v["zcomm"] == pytest.approx(v["zeven"] / 2, rel=1e-9)
    assert v["zodd"] < v["z0_single"] < v["zeven"]
    assert 0 < v["next_coeff"] < 0.25
    assert v["fext_coeff"] < 0           # microstrip: inductive coupling wins
    wide, _ = out("edge_coupled_microstrip", {"frequency": 0, "s": 60})
    assert wide["zdiff"] == pytest.approx(2 * wide["z0_single"], rel=0.005)
    assert wide["next_coeff"] < 0.002


def test_stripline_pair_matches_cohn_and_has_no_fext():
    v, r = out("edge_coupled_stripline", {"t": "0 mil", "frequency": 0})
    assert abs(r["cross_checks"][0]["difference_percent"]) < 0.2
    # Homogeneous dielectric: Lm/L = Cm/C, so the forward terms cancel.
    assert abs(v["fext_coeff"]) < 1e-3 * v["delay_odd"]
    assert v["next_coeff"] > 0.01


def test_broadside_pair_is_symmetric_and_tighter_than_edge():
    b, _ = out("broadside_coupled_stripline", {"frequency": 0})
    assert b["zdiff"] == pytest.approx(2 * b["zodd"], rel=1e-6)
    assert b["next_coeff"] > 0.1
    off, _ = out("broadside_coupled_stripline", {"frequency": 0, "offset": 20})
    assert off["zdiff"] > b["zdiff"]


def test_width_for_impedance_round_trips():
    w, _ = out("width_for_impedance", {"structure": "microstrip", "target": 50})
    back, _ = out("microstrip", {"w": {"value": w["w"], "unit": "m"}, "frequency": 0})
    assert back["z0"] == pytest.approx(50.0, rel=1e-3)
    d, _ = out("width_for_impedance", {"structure": "edge_coupled_stripline", "target": 100,
                                       "s": 6, "h1": 6, "h2": 6})
    assert d["achieved"] == pytest.approx(100.0, rel=1e-3)
    assert not toolkit.run("width_for_impedance", {"structure": "stripline", "target": 400})["ok"]


def test_loss_grows_with_frequency_and_roughness():
    a, _ = out("microstrip", {"frequency": "1 GHz", "roughness": "none"})
    b, _ = out("microstrip", {"frequency": "10 GHz", "roughness": "none"})
    c, _ = out("microstrip", {"frequency": "10 GHz", "roughness": "huray"})
    assert b["loss_conductor"] == pytest.approx(a["loss_conductor"] * math.sqrt(10), rel=0.06)
    assert c["loss_conductor"] > 1.3 * b["loss_conductor"]
    assert b["loss_dielectric"] > 8 * a["loss_dielectric"]
    assert c["loss_total"] == pytest.approx(c["loss_conductor"] + c["loss_dielectric"])


# ------------------------------------------------------------ conductor, via

def test_conductor_resistance_exact():
    v, _ = out("conductor_properties", {"w": "10 mil", "t": "35 um", "length": "100 mm",
                                        "temperature": 20, "current": 2})
    r = 1.724e-8 * 0.1 / (254e-6 * 35e-6)
    assert v["r_dc"] == pytest.approx(r, rel=1e-6)
    assert v["v_drop"] == pytest.approx(2 * r, rel=1e-6)
    assert v["power"] == pytest.approx(4 * r, rel=1e-6)
    hot, _ = out("conductor_properties", {"w": "10 mil", "t": "35 um", "length": "100 mm",
                                          "temperature": 120})
    assert hot["r_dc"] == pytest.approx(r * (1 + 0.00393 * 100), rel=1e-6)
    ac, _ = out("conductor_properties", {"frequency": "1 GHz"})
    assert ac["r_ac"] > 3 * ac["r_dc"] and ac["skin_depth"] == pytest.approx(2.09e-6, rel=0.02)


def test_trace_current_modes_agree_and_planes_matter():
    r, _ = out("trace_current", {"solve_for": "rise", "current": 3, "w": 30})
    c, _ = out("trace_current", {"solve_for": "current", "w": 30,
                                 "rise": {"value": r["rise"], "unit": "°C"}})
    assert c["current"] == pytest.approx(3.0, rel=0.01)
    w, _ = out("trace_current", {"solve_for": "width", "current": 3,
                                 "rise": {"value": r["rise"], "unit": "°C"}})
    assert w["w"] == pytest.approx(30 * MIL, rel=0.02)
    p, _ = out("trace_current", {"solve_for": "rise", "current": 3, "w": 30, "planes": "one",
                                 "plane_distance": 5})
    assert p["rise"] < 0.6 * r["rise"]
    assert r["v_drop"] == pytest.approx(3 * r["r_hot"])
    _, hot = out("trace_current", {"solve_for": "rise", "current": 12, "w": 10})
    assert any("130" in w_ or "runaway" in w_.lower() for w_ in hot["warnings"])


def test_via_barrel_resistance_and_parallel_vias():
    one, _ = out("via_properties", {"hole": "0.3 mm", "plating": "25 um", "length": "1.6 mm",
                                    "ambient": 20})
    area = math.pi / 4 * (0.35e-3 ** 2 - 0.3e-3 ** 2)
    assert one["r_dc"] == pytest.approx(1.724e-8 * 1.6e-3 / area, rel=1e-6)
    assert one["rth"] == pytest.approx(1.6e-3 / (385.0 * area), rel=1e-6)
    four, _ = out("via_properties", {"hole": "0.3 mm", "plating": "25 um", "length": "1.6 mm",
                                     "ambient": 20, "count": 4})
    assert four["r_dc"] == pytest.approx(one["r_dc"] / 4)
    assert four["i_max"] == pytest.approx(one["i_max"] * 4)
    assert one["f_res"] == pytest.approx(
        1 / (2 * math.pi * math.sqrt(one["l_self"] * one["c_pad"])))
    _, deep = out("via_properties", {"hole": "0.1 mm", "length": "3 mm"})
    assert any("spect" in w for w in deep["warnings"])


def test_via_pair_is_the_two_wire_line():
    v, _ = out("via_pair", {"hole": "10 mil", "plating": "1 um", "pitch": "40 mil", "er": 1.0})
    # (eta0 / pi) acosh(pitch / diameter), exact for two parallel cylinders.
    assert v["zdiff"] == pytest.approx(376.7303 / math.pi * math.acosh(1016.0 / 256.0), rel=1e-6)


def test_fusing_and_spacing_reuse_the_existing_engine():
    from eda_agent.engineering import pcb_calc
    v, r = out("fusing_current", {"w": 100, "t": "1 oz", "time": 1})
    assert v["i_fuse"] == pytest.approx(pcb_calc.fusing_current(100, 1.0, 1.0)["fusing_current_a"])
    assert r["warnings"]
    s, _ = out("conductor_spacing", {"voltage": 100, "environment": "B2"})
    assert s["spacing"] == pytest.approx(pcb_calc.ipc2221_spacing(100, "B2")["spacing_mm"] * 1e-3)


# ------------------------------------------------------------ planes

def test_plane_capacitance_and_resonances():
    v, r = out("plane_pair", {"a": "100 mm", "b": "80 mm", "d": "0.1 mm", "er": 4.0})
    assert v["c"] == pytest.approx(EPS0 * 4.0 * 0.1 * 0.08 / 0.1e-3, rel=1e-9)
    assert v["f_10"] == pytest.approx(C0 / 2 / 0.1 / 2, rel=1e-9)
    assert v["f_01"] == pytest.approx(C0 / 2 / 0.08 / 2, rel=1e-9)
    assert len(r["curve"]["x"]) == len(r["curve"]["y"]) > 100


def test_cavity_impedance_limits():
    a, b, d, er = 0.1, 0.08, 0.1e-3, 4.0
    c = EPS0 * er * a * b / d
    port = (0.03, 0.02, 1e-3, 1e-3)
    z = plane.cavity_impedance(a, b, d, er, 0.0, [1e5], [port], sigma=1e30)[0, 0, 0]
    # Far below the first mode the pair is its capacitance (plus the
    # port's spreading inductance, negligible at 100 kHz).
    assert z.imag == pytest.approx(-1 / (2 * math.pi * 1e5 * c), rel=1e-3)
    assert abs(z.real) < 1e-6 * abs(z.imag)
    # A port on the nodal line of TM10 does not see that resonance.
    f10 = C0 / (2 * a * math.sqrt(er))
    fs = np.array([f10 * 0.999, f10, f10 * 1.001])
    on = np.abs(plane.cavity_impedance(a, b, d, er, 0.002, fs, [(0.0, 0.04, 1e-3, 1e-3)]))[:, 0, 0]
    off = np.abs(plane.cavity_impedance(a, b, d, er, 0.002, fs, [(a / 2, 0.04, 1e-3, 1e-3)]))[:, 0, 0]
    assert on[1] > 20 * off[1]
    # Reciprocity of the transfer impedance.
    z2 = plane.cavity_impedance(a, b, d, er, 0.02, [3e8], [port, (0.07, 0.05, 1e-3, 1e-3)])[0]
    assert z2[0, 1] == pytest.approx(z2[1, 0])
    # Loss damps the peak.
    lossy = np.abs(plane.cavity_impedance(a, b, d, er, 0.05, [f10], [(0.0, 0.04, 1e-3, 1e-3)]))[0, 0, 0]
    assert lossy < on[1]


def test_pdn_target():
    v, _ = out("pdn_target", {"voltage": 1.0, "ripple": 5, "current": 10, "t_rise": "1 ns"})
    assert v["z_target"] == pytest.approx(0.005)
    assert v["f_max"] == pytest.approx(350e6)


# ------------------------------------------------------------ signal

def test_signal_calculators_exact():
    v, _ = out("wavelength", {"frequency": "1 GHz", "er_eff": 4.0})
    assert v["lambda"] == pytest.approx(C0 / 1e9 / 2) and v["lambda_4"] == pytest.approx(v["lambda"] / 4)
    v, _ = out("rise_time_bandwidth", {"t_rise": "1 ns", "er_eff": 4.0, "fraction": "1/6"})
    assert v["bw"] == pytest.approx(350e6) and v["f_knee"] == pytest.approx(500e6)
    assert v["l_crit"] == pytest.approx(1e-9 / 6 * C0 / 2)
    v, _ = out("ppm", {"nominal": "32.768 kHz", "ppm": 20, "measured": "32.7683 kHz"})
    assert v["delta"] == pytest.approx(0.65536) and v["drift_day"] == pytest.approx(1.728)
    assert v["measured_ppm"] == pytest.approx(9.155, rel=1e-3)


def test_crosstalk_falls_with_spacing_and_saturates_with_length():
    near, _ = out("crosstalk", {"s": 5})
    far, _ = out("crosstalk", {"s": 20})
    assert far["next"] < 0.35 * near["next"]
    short, _ = out("crosstalk", {"length": "5 mm"})
    long_, _ = out("crosstalk", {"length": "500 mm"})
    assert long_["next"] == pytest.approx(long_["kb"] * 3.3, rel=1e-6)
    assert short["next"] < long_["next"] and abs(long_["fext"]) > abs(short["fext"])
    strip, _ = out("crosstalk", {"structure": "stripline"})
    assert abs(strip["fext"]) < 0.02 * abs(near["fext"])


def test_junction_temperature():
    v, r = out("junction_temperature", {"power": 2, "ambient": 40, "theta_ja": 30})
    assert v["tj"] == 100.0 and v["p_max"] == pytest.approx(85 / 30) and not r["warnings"]
    v, r = out("junction_temperature", {"power": 5, "ambient": 40, "path": "chain",
                                        "theta_jc": 2, "theta_cs": 0.5, "theta_sa": 20})
    assert v["tj"] == pytest.approx(152.5) and r["warnings"]
    assert signal.junction_temperature(1.0, 25.0, theta_jc=1.0, theta_sa=9.0) == 35.0
    with pytest.raises(core.InputError):
        signal.junction_temperature(1.0, 25.0)


# ------------------------------------------------------------ passives

def test_ohms_law_every_pair():
    for given in ({"voltage": 12, "current": 2}, {"voltage": 12, "resistance": 6},
                  {"voltage": 12, "power": 24}, {"current": 2, "resistance": 6},
                  {"current": 2, "power": 24}, {"resistance": 6, "power": 24}):
        full = {"voltage": None, "current": None, "resistance": None, "power": None}
        full.update(given)
        v, _ = out("ohms_law", full)
        assert (v["voltage"], v["current"], v["resistance"], v["power"]) == pytest.approx((12, 2, 6, 24))
    assert not toolkit.run("ohms_law", {"voltage": 1, "current": 1, "resistance": 1})["ok"]


def test_passives_exact():
    v, _ = out("reactance", {"frequency": "1 MHz", "capacitance": "1 nF", "inductance": "1 uH"})
    assert v["xc"] == pytest.approx(159.1549, rel=1e-6) and v["xl"] == pytest.approx(6.283185, rel=1e-6)
    assert v["f0"] == pytest.approx(5.03292e6, rel=1e-5)
    v, _ = out("series_parallel", {"kind": "resistance", "values": "10k, 4k7, 2.2k"})
    assert v["series"] == pytest.approx(16900) and v["parallel"] == pytest.approx(1303.2518, rel=1e-6)
    v, _ = out("series_parallel", {"kind": "capacitance", "values": "100n 100n"})
    assert v["series"] == pytest.approx(50e-9) and v["parallel"] == pytest.approx(200e-9)
    assert not toolkit.run("series_parallel", {"values": "10k, abc"})["ok"]
    v, _ = out("voltage_divider", {"vin": 12, "r1": 9000, "r2": 3000})
    assert v["vout"] == pytest.approx(3.0) and v["r_out"] == pytest.approx(2250)
    v, _ = out("voltage_divider", {"vin": 12, "r1": 9000, "r2": 3000, "load": 3000})
    assert v["vout"] == pytest.approx(12 * 1500 / 10500)
    v, _ = out("led_resistor", {"vs": 5, "vf": 2, "current": "20 mA"})
    assert v["r"] == pytest.approx(150) and v["r_e24"] == pytest.approx(150) and v["p"] == pytest.approx(0.06)
    v, _ = out("led_resistor", {"vs": 5, "vf": 2.1, "current": "20 mA"})
    assert v["r"] == pytest.approx(145) and v["r_e24"] == pytest.approx(150) and v["r_e96"] == pytest.approx(147)
    assert not toolkit.run("led_resistor", {"vs": 3, "vf": 3.2})["ok"]
    # 6 dB, 50 ohm: the standard table values.
    v, _ = out("attenuator", {"db": 6, "z0": 50})
    assert v["pi_shunt"] == pytest.approx(150.48, abs=0.01) and v["pi_series"] == pytest.approx(37.35, abs=0.01)
    assert v["t_series"] == pytest.approx(16.61, abs=0.01) and v["t_shunt"] == pytest.approx(66.93, abs=0.01)
    v, _ = out("crystal_load", {"cl": "12.5 pF", "stray": "3 pF"})
    assert v["c"] == pytest.approx(19e-12) and v["e12"] == pytest.approx(18e-12)
    v, _ = out("rc_time", {"resistance": 1000, "capacitance": "1 uF"})
    assert v["tau_rc"] == pytest.approx(1e-3) and v["fc_rc"] == pytest.approx(159.1549, rel=1e-6)


def test_planar_inductor_two_published_expressions_agree():
    v, r = out("planar_inductor", {"shape": "square", "turns": 5, "d_out": "10 mm", "d_in": "5 mm"})
    assert abs(r["cross_checks"][0]["difference_percent"]) < 4
    # mu0 n^2 d_avg c1/2 (ln(c2/rho) + c3 rho + c4 rho^2), by hand.
    rho = 1 / 3
    by_hand = 4e-7 * math.pi * 25 * 7.5e-3 * 1.27 / 2 * (math.log(2.07 / rho) + 0.18 * rho + 0.13 * rho ** 2)
    assert v["l"] == pytest.approx(by_hand, rel=1e-9)
    c, _ = out("planar_inductor", {"shape": "circular", "turns": 5, "d_out": "10 mm", "d_in": "5 mm"})
    assert c["l"] < v["l"]


# ------------------------------------------------------------ layout, reference

def test_layout_calculators_exact():
    v, _ = out("padstack", {"hole": 12, "ring": 2, "allowance": 8, "plating": 1, "clearance": 10})
    assert v["pad_outer"] == pytest.approx(24 * MIL) and v["drill"] == pytest.approx(14 * MIL)
    assert v["antipad"] == pytest.approx(44 * MIL)
    v, _ = out("bga_land", {"ball": "0.6 mm", "pitch": "1 mm", "space": "0.1 mm", "traces": 1})
    assert v["land"] == pytest.approx(0.45e-3) and v["trace_max"] == pytest.approx(0.35e-3)
    v, _ = out("bga_land", {"ball": "0.3 mm", "pitch": "0.5 mm", "space": "0.1 mm"})
    assert v["land"] == pytest.approx(0.24e-3)
    v, _ = out("conductor_to_pad", {"pitch": 100, "pad": 60, "space": 6, "traces": 2})
    assert v["w_max"] == pytest.approx(11 * MIL)
    v, _ = out("corner_to_corner", {"x": "3 mm", "y": "4 mm"})
    assert v["diag"] == pytest.approx(5e-3)
    v, _ = out("embedded_resistor", {"sheet": 100, "length": 60, "width": 20})
    assert v["r"] == pytest.approx(300) and v["squares"] == pytest.approx(3)


def test_reference_calculators():
    v, _ = out("wire_gauge", {"awg": 24})
    assert v["diameter"] == pytest.approx(0.5106e-3, rel=1e-3)
    v, _ = out("wire_gauge", {"awg": 10})
    assert v["diameter"] == pytest.approx(2.588e-3, rel=1e-3)
    v, _ = out("unit_convert", {"kind": "length", "value": "1 in"})
    assert v["in_mil"] == pytest.approx(1000) and v["in_mm"] == pytest.approx(25.4)
    v, _ = out("unit_convert", {"kind": "temperature", "value": "100 °C"})
    assert v["in_f"] == pytest.approx(212) and v["in_k"] == pytest.approx(373.15)
    v, _ = out("db_convert", {"dbm": 30, "z0": 50, "gain_db": 20})
    assert v["watts"] == pytest.approx(1.0) and v["vrms"] == pytest.approx(math.sqrt(50))
    assert v["voltage_ratio"] == pytest.approx(10) and v["power_ratio"] == pytest.approx(100)
    v, _ = out("drill_chart", {"hole": "0.83 mm"})
    assert v["metric"] == pytest.approx(0.85e-3) and v["metric_up"] == pytest.approx(0.85e-3)
    v, _ = out("er_effective", {"frequency": 0})
    assert 2.5 < v["er_eff"] == v["er_eff_static"] < 4.2
    v, _ = out("laminate_er", {"er": 4.3, "tand": 0.02, "f_spec": "1 GHz", "frequency": "1 GHz"})
    assert v["er"] == pytest.approx(4.3) and v["tand"] == pytest.approx(0.02)


# ------------------------------------------------------------ board prefill

STACK = {"board_name": "demo.PcbDoc", "layers": [
    {"name": "Top", "layer_id": 1, "copper_thickness_mils": 1.4, "dielectric_type": "PrePreg",
     "dielectric_height_mils": 4.0, "dielectric_constant": 4.1},
    {"name": "GND", "layer_id": 39, "copper_thickness_mils": 1.2, "dielectric_type": "Core",
     "dielectric_height_mils": 47.0, "dielectric_constant": 4.5},
    {"name": "PWR", "layer_id": 40, "copper_thickness_mils": 1.2, "dielectric_type": "PrePreg",
     "dielectric_height_mils": 4.0, "dielectric_constant": 4.1},
    {"name": "Bottom", "layer_id": 32, "copper_thickness_mils": 1.4, "dielectric_type": "none",
     "dielectric_height_mils": 0, "dielectric_constant": 0},
]}


def test_prefill_reads_each_layer_from_the_stack():
    p = prefill.from_stackup(STACK)
    assert p["ok"] and p["layer_count"] == 4
    assert p["thickness_mils"] == pytest.approx(1.4 + 4 + 1.2 + 47 + 1.2 + 4 + 1.4)
    top, gnd, pwr, bot = p["layers"]
    assert (top["kind"], gnd["kind"], bot["kind"]) == ("outer", "plane", "outer")
    assert top["structure"] == "microstrip" and gnd["structure"] == "stripline"
    assert top["values"]["stackup.dielectric_height"] == {"value": 4.0, "unit": "mil"}
    assert top["values"]["stackup.er"]["value"] == 4.1
    assert top["values"]["stackup.copper"] == {"value": 1.4, "unit": "mil"}
    assert bot["values"]["stackup.dielectric_height"]["value"] == 4.0      # the prepreg above it
    assert bot["values"]["stackup.er"]["value"] == 4.1
    assert gnd["values"]["stackup.h_below"]["value"] == 47.0
    assert gnd["values"]["stackup.h_above"]["value"] == 4.0
    assert top["values"]["board.planes"]["value"] == "two"
    assert top["values"]["stackup.plane_gap"]["value"] == 47.0
    assert not prefill.from_stackup({})["ok"]
    assert not prefill.from_stackup({"layers": []})["ok"]


#: BoosterTest.PcbDoc as the bridge really reports it (read live on
#: 2026-11-12): two copper layers and no dielectric at all.
BARE_STACK = {"board_name": "BoosterTest.PcbDoc", "layer_count": 2, "layers": [
    {"name": "Top Layer", "order": 1, "layer_id": 1, "copper_thickness_mils": 1.4,
     "dielectric_type": "none", "dielectric_height_mils": 0, "dielectric_constant": 0},
    {"name": "Bottom Layer", "order": 2, "layer_id": 32, "copper_thickness_mils": 1.4,
     "dielectric_type": "none", "dielectric_height_mils": 0, "dielectric_constant": 0},
]}


def test_a_stack_without_dielectrics_fills_copper_only_and_says_so():
    """Copper added up is not a board thickness: 2.8 mil was offered."""
    p = prefill.from_stackup(BARE_STACK)
    assert p["ok"] and p["layer_count"] == 2
    assert p["thickness_mils"] is None
    assert len(p["notes"]) == 1 and "no dielectric" in p["notes"][0]
    for layer in p["layers"]:
        assert set(layer["values"]) == {"stackup.copper", "board.planes"}
        assert layer["values"]["stackup.copper"] == {"value": 1.4, "unit": "mil"}
    # A complete stack carries no note, and its thickness is the whole board.
    full = prefill.from_stackup(STACK)
    assert full["notes"] == []
    assert full["layers"][0]["values"]["board.thickness"]["value"] == pytest.approx(60.2)


def test_a_dielectric_without_er_is_named():
    rows = [dict(r) for r in STACK["layers"]]
    rows[0]["dielectric_constant"] = 0
    p = prefill.from_stackup({"layers": rows})
    assert len(p["notes"]) == 1 and "no Er" in p["notes"][0]
    assert "stackup.er" not in p["layers"][0]["values"]
    assert p["layers"][0]["values"]["stackup.dielectric_height"]["value"] == 4.0
    assert p["thickness_mils"] == pytest.approx(60.2)


def test_prefill_keys_exist_on_the_calculators_that_name_them():
    known = {"stackup.copper", "stackup.dielectric_height", "stackup.h_below", "stackup.h_above",
             "stackup.er", "stackup.plane_gap", "board.thickness", "board.planes"}
    later = {"trace.width", "trace.gap", "via.hole", "via.pad", "stackup.tand", "stackup.mask_t",
             "board.width", "board.length"}
    used = {i.prefill for c in toolkit.REGISTRY.values() for i in c.inputs if i.prefill}
    assert used <= known | later
    assert known <= used
    # Every value the stack gives parses as the input it fills.
    top = prefill.from_stackup(STACK)["layers"][0]["values"]
    r = toolkit.run("microstrip", {"h": top["stackup.dielectric_height"],
                                   "t": top["stackup.copper"], "er": top["stackup.er"]["value"]})
    assert r["ok"]
