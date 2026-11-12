# SPDX-License-Identifier: Apache-2.0
"""Ohm's law, reactance, networks, dividers, pads and crystal loading."""
from __future__ import annotations

import math
import re

from .. import pcb_calc
from .core import Input, InputError, Result, calculator, need, to_si


@calculator(
    "ohms_law", "Ohm's law", "passives",
    "Any two of voltage, current, resistance and power give the other two.",
    [Input("voltage", "Voltage", "voltage", 5.0, "V", optional=True),
     Input("current", "Current", "current", None, "A", optional=True),
     Input("resistance", "Resistance", "resistance", 1000.0, "ohm", optional=True),
     Input("power", "Power", "power", None, "W", optional=True)])
def ohms_law(a):
    v, i, rr, p = a.get("voltage"), a.get("current"), a.get("resistance"), a.get("power")
    known = sum(x is not None for x in (v, i, rr, p))
    need(known == 2, "give exactly two of voltage, current, resistance and power")
    if v is not None and i is not None:
        rr, p = (v / i if i else math.inf), v * i
    elif v is not None and rr is not None:
        i = v / rr
        p = v * i
    elif v is not None and p is not None:
        i = p / v
        rr = v / i if i else math.inf
    elif i is not None and rr is not None:
        v = i * rr
        p = v * i
    elif i is not None and p is not None:
        v = p / i
        rr = v / i
    else:
        need(rr > 0, "resistance must be greater than zero")
        v = math.sqrt(p * rr)
        i = v / rr
    r = Result(model="V = I R, P = V I", status="exact")
    r.add("voltage", "Voltage", v, "voltage", primary=True)
    r.add("current", "Current", i, "current", primary=True)
    r.add("resistance", "Resistance", rr, "resistance", primary=True)
    r.add("power", "Power", p, "power", primary=True)
    return r


@calculator(
    "reactance", "Reactance and resonance", "passives",
    "Reactance of a capacitor and an inductor at a frequency, and their resonance.",
    [Input("frequency", "Frequency", "frequency", 10.0, "MHz", min=1e-9),
     Input("capacitance", "Capacitance", "capacitance", 100.0, "nF", min=0.0),
     Input("inductance", "Inductance", "inductance", 1.0, "uH", min=0.0)])
def reactance(a):
    f, c, L = a["frequency"], a["capacitance"], a["inductance"]
    w = 2 * math.pi * f
    r = Result(model="Xc = 1/(2 pi f C), Xl = 2 pi f L, f0 = 1/(2 pi sqrt(LC))", status="exact")
    if c > 0:
        r.add("xc", "Capacitive reactance", 1 / (w * c), "resistance", primary=True)
    if L > 0:
        r.add("xl", "Inductive reactance", w * L, "resistance", primary=True)
    if c > 0 and L > 0:
        r.add("f0", "Resonant frequency", 1 / (2 * math.pi * math.sqrt(L * c)), "frequency")
        r.add("z0", "Characteristic impedance sqrt(L/C)", math.sqrt(L / c), "resistance")
    return r


_SPLIT = re.compile(r"[,;\s]+")


def _values(text: str, kind: str, unit: str) -> list[float]:
    parts = [p for p in _SPLIT.split(str(text or "").strip()) if p]
    need(len(parts) >= 1, "enter at least one value")
    out = []
    for p in parts:
        # "4k7" and "4.7k" style values
        m = re.match(r"^(\d+)([pnumkKMR])(\d+)$", p)
        if m:
            p = f"{m.group(1)}.{m.group(3)}{'' if m.group(2) == 'R' else m.group(2)}"
        p = re.sub(r"(?<=[\d.])([pnumkKM])$", lambda mm: {"p": "e-12", "n": "e-9", "u": "e-6",
                                                         "m": "e-3", "k": "e3", "K": "e3",
                                                         "M": "e6"}[mm.group(1)], p)
        try:
            out.append(to_si(float(p), kind, unit))
        except (ValueError, InputError):
            raise InputError(f"'{p}' is not a value") from None
    return out


@calculator(
    "series_parallel", "Series and parallel R, L, C", "passives",
    "Combine resistors, inductors or capacitors in series and in parallel.",
    [Input("kind", "Component", "choice", "resistance",
           choices=[("resistance", "Resistors (ohm)"), ("inductance", "Inductors (H)"),
                    ("capacitance", "Capacitors (F)")]),
     Input("values", "Values, comma separated", "choice_text", "10k, 4k7, 2.2k",
           help="Engineering suffixes allowed: 4k7, 2.2k, 100n, 10u.")])
def series_parallel(a):
    kind = a["kind"]
    vals = _values(a["values"], kind, {"resistance": "ohm", "inductance": "H",
                                       "capacitance": "F"}[kind])
    need(all(v > 0 for v in vals), "every value must be greater than zero")
    ssum = sum(vals)
    psum = 1.0 / sum(1.0 / v for v in vals)
    if kind == "capacitance":
        ssum, psum = psum, ssum
    r = Result(model="Series adds, parallel adds reciprocals (C the other way round)",
               status="exact")
    r.add("series", "Series", ssum, kind, primary=True)
    r.add("parallel", "Parallel", psum, kind, primary=True)
    return r


@calculator(
    "voltage_divider", "Voltage divider", "passives",
    "Output voltage, current and resistor power of a two-resistor divider, loaded or not.",
    [Input("vin", "Input voltage", "voltage", 5.0, "V"),
     Input("r1", "Top resistor R1", "resistance", 10e3, "ohm", min=0.0),
     Input("r2", "Bottom resistor R2", "resistance", 10e3, "ohm", min=0.0),
     Input("load", "Load resistance (0 = none)", "resistance", 0.0, "ohm", min=0.0)])
def voltage_divider(a):
    r1, r2, rl = a["r1"], a["r2"], a["load"]
    r2e = r2 * rl / (r2 + rl) if rl > 0 else r2
    need(r1 + r2e > 0, "the divider has no resistance")
    i = a["vin"] / (r1 + r2e)
    vout = i * r2e
    r = Result(model="Vout = Vin R2' / (R1 + R2'), R2' = R2 || Rload", status="exact")
    r.add("vout", "Output voltage", vout, "voltage", primary=True)
    r.add("ratio", "Ratio", vout / a["vin"] if a["vin"] else 0.0, "ratio")
    r.add("current", "Divider current", i, "current")
    r.add("p_r1", "Power in R1", i * i * r1, "power")
    r.add("p_r2", "Power in R2", vout * vout / r2 if r2 > 0 else 0.0, "power")
    r.add("r_out", "Output (Thevenin) resistance", r1 * r2 / (r1 + r2) if r1 + r2 > 0 else 0.0,
          "resistance")
    return r


@calculator(
    "led_resistor", "LED series resistor", "passives",
    "Series resistor for an LED, its power, and the nearest E-series values.",
    [Input("vs", "Supply voltage", "voltage", 5.0, "V", min=0.0),
     Input("vf", "LED forward voltage", "voltage", 2.0, "V", min=0.0),
     Input("current", "LED current", "current", 10.0, "mA", min=1e-6),
     Input("count", "LEDs in series", "count", 1, min=1, max=100)])
def led_resistor(a):
    vdrop = a["vs"] - a["vf"] * int(a["count"])
    need(vdrop > 0, "the supply is below the LEDs' total forward voltage")
    rr = vdrop / a["current"]
    r = Result(model="R = (Vs - n Vf) / I", status="exact")
    r.add("r", "Resistor", rr, "resistance", primary=True)
    r.add("p", "Resistor power", vdrop * a["current"], "power")
    for series in ("E24", "E96"):
        near = _nearest_e(rr, series, up=True)
        r.add(f"r_{series.lower()}", f"Next {series} value up", near, "resistance")
        r.add(f"i_{series.lower()}", f"  current with it", vdrop / near, "current")
    return r


_E = {
    "E24": [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9,
            4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1],
}


def _e96() -> list[float]:
    return [round(10 ** (i / 96.0), 2) for i in range(96)]


def _nearest_e(value: float, series: str, up: bool = False) -> float:
    base = _E["E24"] if series == "E24" else _e96()
    dec = 10 ** math.floor(math.log10(value))
    cands = [b * dec for b in base] + [b * dec * 10 for b in base[:2]]
    if up:
        cands = [c for c in cands if c >= value * (1 - 1e-9)]
        return min(cands)
    return min(cands, key=lambda c: abs(math.log(c / value)))


@calculator(
    "attenuator", "Pi and T attenuator pads", "passives",
    "Resistor values for a matched Pi or T attenuator.",
    [Input("db", "Attenuation", "db", 6.0, "dB", min=0.01, max=120),
     Input("z0", "System impedance", "resistance", 50.0, "ohm", min=0.1)])
def attenuator(a):
    k = 10 ** (a["db"] / 20.0)
    z = a["z0"]
    r = Result(model="Matched symmetric pads, K = 10^(dB/20)", status="exact")
    r.add("pi_shunt", "Pi: shunt resistors (each)", z * (k + 1) / (k - 1), "resistance", primary=True)
    r.add("pi_series", "Pi: series resistor", z * (k * k - 1) / (2 * k), "resistance", primary=True)
    r.add("t_series", "T: series resistors (each)", z * (k - 1) / (k + 1), "resistance")
    r.add("t_shunt", "T: shunt resistor", z * 2 * k / (k * k - 1), "resistance")
    r.references = ["Standard matched-pad design equations (e.g. ARRL Handbook)"]
    return r


@calculator(
    "crystal_load", "Crystal load capacitors", "passives",
    "Load capacitor values for a Pierce oscillator from the crystal's specified load.",
    [Input("cl", "Crystal load capacitance CL", "capacitance", 12.0, "pF", min=0.1),
     Input("stray", "Stray (pins + traces)", "capacitance", 3.0, "pF", min=0.0)])
def crystal_load(a):
    c = 2.0 * (a["cl"] - a["stray"])
    need(c > 0, "the stray capacitance already exceeds the load")
    r = Result(model="C1 = C2 = 2 (CL - Cstray)", status="exact")
    r.add("c", "Each load capacitor", c, "capacitance", primary=True)
    r.add("e12", "Nearest standard (E12)", _nearest_e12(c), "capacitance")
    r.accuracy = "Stray is typically 2-5 pF; trim against a measured frequency."
    return r


def _nearest_e12(v: float) -> float:
    base = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2, 10.0]
    dec = 10 ** math.floor(math.log10(v))
    return min((b * dec for b in base), key=lambda c: abs(math.log(c / v)))


@calculator(
    "rc_time", "RC / RL time constant and cutoff", "passives",
    "Time constant and -3 dB frequency of a first-order RC or RL network.",
    [Input("resistance", "Resistance", "resistance", 10e3, "ohm", min=1e-9),
     Input("capacitance", "Capacitance", "capacitance", 100.0, "nF", min=0.0),
     Input("inductance", "Inductance (RL)", "inductance", 0.0, "uH", min=0.0)])
def rc_time(a):
    R = a["resistance"]
    r = Result(model="tau = RC or L/R, fc = 1/(2 pi tau)", status="exact")
    if a["capacitance"] > 0:
        tau = R * a["capacitance"]
        r.add("tau_rc", "RC time constant", tau, "time", primary=True)
        r.add("fc_rc", "RC cutoff", 1 / (2 * math.pi * tau), "frequency")
        r.add("t_rise_rc", "10-90 % rise (2.2 tau)", 2.2 * tau, "time")
    if a["inductance"] > 0:
        tau = a["inductance"] / R
        r.add("tau_rl", "RL time constant", tau, "time", primary=True)
        r.add("fc_rl", "RL cutoff", 1 / (2 * math.pi * tau), "frequency")
    return r


@calculator(
    "planar_inductor", "Planar spiral inductor", "passives",
    "Inductance of a square, hexagonal, octagonal or circular spiral on a PCB.",
    [Input("shape", "Shape", "choice", "square",
           choices=[("square", "Square"), ("hexagonal", "Hexagonal"),
                    ("octagonal", "Octagonal"), ("circular", "Circular")]),
     Input("turns", "Turns", "ratio", 5, min=0.5),
     Input("d_out", "Outer diameter", "length", 400.0, "mil", min=1.0),
     Input("d_in", "Inner diameter", "length", 200.0, "mil", min=0.1)])
def planar_inductor(a):
    n, do, di = a["turns"], a["d_out"], a["d_in"]
    need(di < do, "the inner diameter must be smaller than the outer")
    davg = (do + di) / 2
    rho = (do - di) / (do + di)
    mu0 = 4e-7 * math.pi
    # Current-sheet expression, Mohan et al. 1999, Table II.
    cs = {"square": (1.27, 2.07, 0.18, 0.13), "hexagonal": (1.09, 2.23, 0.0, 0.17),
          "octagonal": (1.07, 2.29, 0.0, 0.19), "circular": (1.0, 2.46, 0.0, 0.2)}
    c1, c2, c3, c4 = cs[a["shape"]]
    l_cs = mu0 * n * n * davg * c1 / 2 * (math.log(c2 / rho) + c3 * rho + c4 * rho * rho)
    r = Result(model="Mohan et al. 1999: current-sheet and modified Wheeler", status="published")
    r.add("l", "Inductance (current sheet)", l_cs, "inductance", primary=True)
    if a["shape"] != "circular":
        d = pcb_calc.spiral_inductor(n, do / 25.4e-6, di / 25.4e-6, a["shape"])
        if d.get("ok"):
            r.add("l_wheeler", "Inductance (modified Wheeler)", d["inductance_nh"] * 1e-9,
                  "inductance")
            r.check("L", l_cs, d["inductance_nh"] * 1e-9, "Modified Wheeler", "inductance")
    r.add("fill", "Fill ratio", rho, "ratio")
    r.accuracy = ("Mohan et al. report 2-3 % typical error against field solvers "
                  "for spirals with no ground plane close beneath.")
    r.references = ["S. S. Mohan et al., 'Simple accurate expressions for planar spiral "
                    "inductances', IEEE JSSC 34(10), 1999"]
    return r
