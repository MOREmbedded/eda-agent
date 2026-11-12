# SPDX-License-Identifier: Apache-2.0
"""Signal, timing, crosstalk and thermal-junction calculators."""
from __future__ import annotations

import math

from .core import REGISTRY, Input, Result, calculator, need
from .fdsolver import C0


@calculator(
    "wavelength", "Wavelength", "signal",
    "Wavelength of a signal on a line with a given effective Er, and the usual fractions.",
    [Input("frequency", "Frequency", "frequency", 1.0, "GHz", min=1e-12),
     Input("er_eff", "Effective Er", "ratio", 3.0, min=1.0,
           help="From the impedance calculators; Er itself for stripline.")])
def wavelength(a):
    lam = C0 / (a["frequency"] * math.sqrt(a["er_eff"]))
    r = Result(model="lambda = c / (f sqrt(Er_eff))", status="exact")
    r.add("lambda", "Wavelength", lam, "length", primary=True)
    r.add("lambda_2", "Half wave", lam / 2, "length")
    r.add("lambda_4", "Quarter wave", lam / 4, "length")
    r.add("lambda_10", "Tenth wave (lumped-element limit)", lam / 10, "length")
    r.add("lambda_free", "Free-space wavelength", C0 / a["frequency"], "length")
    return r


@calculator(
    "rise_time_bandwidth", "Bandwidth, rise time and critical length", "signal",
    "Signal bandwidth from rise time, and the trace length beyond which it needs "
    "to be treated as a transmission line.",
    [Input("t_rise", "Rise time (10-90 %)", "time", 1.0, "ns", min=1e-6),
     Input("er_eff", "Effective Er", "ratio", 3.0, min=1.0),
     Input("fraction", "Critical when one-way delay exceeds", "choice", "1/6",
           choices=[("1/2", "1/2 of rise time (round trip = rise)"),
                    ("1/4", "1/4 of rise time"), ("1/6", "1/6 of rise time (conservative)")])])
def rise_time_bandwidth(a):
    tr = a["t_rise"]
    tpd = math.sqrt(a["er_eff"]) / C0
    frac = {"1/2": 0.5, "1/4": 0.25, "1/6": 1 / 6}[a["fraction"]]
    r = Result(model="BW = 0.35/tr (Gaussian edge); knee 0.5/tr; l_crit = frac*tr/tpd",
               status="published")
    r.add("l_crit", "Critical length", frac * tr / tpd, "length", primary=True)
    r.add("bw", "Bandwidth (0.35 / tr)", 0.35 / tr, "frequency")
    r.add("f_knee", "Knee frequency (0.5 / tr)", 0.5 / tr, "frequency")
    r.add("tpd", "Delay per inch", tpd * 0.0254, "time")
    r.add("edge_length", "Length of the rising edge on the board", tr / tpd, "length")
    r.accuracy = ("0.35 holds for a single-pole (Gaussian-like) edge; 0.5 (Johnson's "
                  "knee) bounds the significant spectrum of a digital edge. IPC-2251's "
                  "printed version of this rule has a known error; this uses the "
                  "derivation, not the printed constant.")
    r.references = ["H. Johnson, M. Graham, High-Speed Digital Design, 1993, ch. 1"]
    return r


@calculator(
    "ppm", "PPM and frequency tolerance", "signal",
    "Frequency error of a crystal or oscillator in ppm and Hz, both directions.",
    [Input("nominal", "Nominal frequency", "frequency", 25.0, "MHz", min=1e-9),
     Input("ppm", "Tolerance", "ppm", 20.0, "ppm", min=0.0),
     Input("measured", "Measured frequency (optional)", "frequency", None, "MHz",
           optional=True)])
def ppm(a):
    f0, p = a["nominal"], a["ppm"]
    r = Result(model="df = f0 * ppm / 1e6", status="exact")
    r.add("delta", "Deviation", f0 * p * 1e-6, "frequency", primary=True)
    r.add("f_min", "Minimum frequency", f0 * (1 - p * 1e-6), "frequency")
    r.add("f_max", "Maximum frequency", f0 * (1 + p * 1e-6), "frequency")
    r.add("drift_day", "Clock drift per day at that error", 86400.0 * p * 1e-6, "time")
    if a.get("measured"):
        r.add("measured_ppm", "Measured error", (a["measured"] / f0 - 1) * 1e6, "ppm")
    return r


@calculator(
    "crosstalk", "Crosstalk between two traces", "crosstalk",
    "Near- and far-end crosstalk between two parallel traces, from the field "
    "solver's L and C matrices.",
    [Input("structure", "Layer", "choice", "microstrip",
           choices=[("microstrip", "Outer layer (microstrip)"),
                    ("stripline", "Inner layer (stripline)")]),
     Input("w", "Trace width", "length", 5.0, "mil", min=0.1, prefill="trace.width"),
     Input("s", "Gap (edge to edge)", "length", 10.0, "mil", min=0.1),
     Input("h", "Height to plane (stripline: below)", "length", 4.0, "mil", min=0.1,
           prefill="stackup.dielectric_height"),
     Input("h2", "Stripline: height to plane above", "length", 5.0, "mil", min=0.1,
           show_if={"structure": ["stripline"]}),
     Input("t", "Copper thickness", "length", 1.0, "oz", min=0.0, prefill="stackup.copper"),
     Input("er", "Dielectric constant Er", "ratio", 4.2, min=1.0, prefill="stackup.er"),
     Input("length", "Coupled length", "length", 50.0, "mm", min=0.0),
     Input("t_rise", "Aggressor rise time", "time", 1.0, "ns", min=1e-6),
     Input("voltage", "Aggressor swing", "voltage", 3.3, "V", min=0.0)],
    status="solver")
def crosstalk(a):
    name = "edge_coupled_microstrip" if a["structure"] == "microstrip" else "edge_coupled_stripline"
    calc = REGISTRY[name]
    args = calc.parse({})
    args.update({"w": a["w"], "s": a["s"], "t": a["t"], "er": a["er"], "tand": 0.0,
                 "frequency": 0.0, "roughness": "none"})
    if name == "edge_coupled_microstrip":
        args["h"] = a["h"]
    else:
        args["h1"], args["h2"] = a["h"], a["h2"]
    res = calc.fn(args)
    kb = res.value("next_coeff")
    kf_per_in = res.value("fext_coeff")
    td = a["length"] * math.sqrt(res.value("er_eff_even") * 0.5 + res.value("er_eff_odd") * 0.5) / C0
    sat = min(1.0, 2.0 * td / a["t_rise"])
    v = a["voltage"]
    next_v = kb * v * sat
    fext_v = kf_per_in * (a["length"] / 0.0254) / a["t_rise"] * v
    r = Result(model="Field-solver L/C matrices, weak-coupling crosstalk", status="solver")
    r.add("next", "Near-end crosstalk (NEXT)", next_v, "voltage", primary=True)
    r.add("next_pct", "NEXT, % of swing", 100 * next_v / v if v else 0.0, "percent")
    r.add("fext", "Far-end crosstalk (FEXT)", fext_v, "voltage")
    r.add("fext_pct", "FEXT, % of swing", 100 * fext_v / v if v else 0.0, "percent")
    r.add("kb", "Backward coefficient Kb", kb, "ratio")
    r.add("l_sat", "NEXT saturation length", a["t_rise"] / 2.0 * C0 /
          math.sqrt(res.value("er_eff_odd")), "length")
    r.add("zdiff", "Pair impedance if driven differentially", res.value("zdiff"), "resistance")
    if a["structure"] == "stripline":
        r.warnings.append("Homogeneous stripline has almost no FEXT: inductive and "
                          "capacitive coupling cancel. A residual here is the stack's Er mix.")
    r.accuracy = ("Coupling coefficients from the solver are within ~1 %. The NEXT/FEXT "
                  "voltages assume weak coupling, matched terminations and a linear "
                  "ramp, and are typically within 10 % of a circuit simulation.")
    r.references = ["E. Bogatin, Signal and Power Integrity Simplified, 3rd ed., ch. 10",
                    "H. Johnson, M. Graham, High-Speed Digital Design, ch. 5"]
    return r


@calculator(
    "junction_temperature", "Junction temperature", "thermal",
    "Device junction temperature from power and thermal resistances, and the "
    "power allowed for a junction limit.",
    [Input("power", "Power dissipated", "power", 1.0, "W", min=0.0),
     Input("ambient", "Ambient", "temperature", 25.0, "°C", min=-55, max=200),
     Input("path", "Thermal path", "choice", "ja",
           choices=[("ja", "Junction to ambient (theta-JA)"),
                    ("chain", "Junction-case + case-sink + sink-ambient")]),
     Input("theta_ja", "theta-JA", "thermal_resistance", 50.0, "°C/W", min=0.0,
           show_if={"path": ["ja"]}),
     Input("theta_jc", "theta-JC", "thermal_resistance", 5.0, "°C/W", min=0.0,
           show_if={"path": ["chain"]}),
     Input("theta_cs", "theta-CS (interface)", "thermal_resistance", 0.5, "°C/W", min=0.0,
           show_if={"path": ["chain"]}),
     Input("theta_sa", "theta-SA (heatsink or board)", "thermal_resistance", 20.0, "°C/W",
           min=0.0, show_if={"path": ["chain"]}),
     Input("tj_max", "Maximum junction temperature", "temperature", 125.0, "°C", min=0)])
def junction_temperature_calc(a):
    th = (a["theta_ja"] if a["path"] == "ja"
          else a["theta_jc"] + a["theta_cs"] + a["theta_sa"])
    tj = junction_temperature(a["power"], a["ambient"], th)
    r = Result(model="Tj = Ta + P * theta (series thermal resistances)", status="exact")
    r.add("tj", "Junction temperature", tj, "temperature", primary=True)
    r.add("theta_total", "Total thermal resistance", th, "thermal_resistance")
    r.add("p_max", "Power allowed at Tj max", (a["tj_max"] - a["ambient"]) / th if th > 0 else math.inf, "power")
    r.add("margin", "Margin to Tj max", a["tj_max"] - tj, "delta_t")
    if tj > a["tj_max"]:
        r.warnings.append("The junction exceeds its maximum.")
    r.accuracy = ("Exact for the theta values given. Data-sheet theta-JA is measured on "
                  "a JEDEC JESD51 test board and rarely matches yours; a board-level "
                  "thermal analysis gives the real figure.")
    r.references = ["JEDEC JESD51-2A, JESD51-12"]
    return r


def junction_temperature(power_w: float, ambient_c: float, theta_ja: float | None = None,
                         theta_jc: float | None = None, theta_cs: float | None = None,
                         theta_sa: float | None = None) -> float:
    """Tj from power and a theta-JA, or from a JC + CS + SA chain."""
    if theta_ja is not None:
        th = theta_ja
    else:
        need(None not in (theta_jc, theta_sa), "give theta_ja, or theta_jc and theta_sa")
        th = theta_jc + (theta_cs or 0.0) + theta_sa
    return ambient_c + power_w * th
