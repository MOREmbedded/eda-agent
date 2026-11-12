# SPDX-License-Identifier: Apache-2.0
"""Padstack, BGA, breakout, embedded resistor, wire gauge, conversions."""
from __future__ import annotations

import math

from .. import pcb_calc
from .core import INCH, UNITS, UNIT_CHOICES, Input, Result, calculator, from_si, need, to_si


@calculator(
    "padstack", "Padstack from drill", "layout",
    "Pad diameters for a plated hole from its drill, the required annular ring "
    "and the fab's registration allowance (IPC-2221 / IPC-2222).",
    [Input("hole", "Finished hole", "length", 12.0, "mil", min=0.5),
     Input("ring", "Minimum annular ring", "length", 2.0, "mil", min=0.0,
           help="IPC-6012 class 2: 2 mil external breakout tangency is allowed; "
                "class 3: 2 mil external, 1 mil internal."),
     Input("allowance", "Fabrication allowance", "length", 8.0, "mil", min=0.0,
           help="IPC-2221 Table 9-1: level A 16 mil, B 10 mil, C 8 mil."),
     Input("plating", "Hole plating", "length", 1.0, "mil", min=0.0),
     Input("clearance", "Plane clearance (each side)", "length", 10.0, "mil", min=0.0)])
def padstack(a):
    drill = a["hole"] + 2 * a["plating"]
    pad = a["hole"] + 2 * a["ring"] + a["allowance"]
    r = Result(model="Pad = hole + 2 x annular ring + fab allowance (IPC-2221 9.1.1)",
               status="exact")
    r.add("pad_outer", "Pad diameter, outer layers", pad, "length", primary=True)
    r.add("pad_inner", "Pad diameter, inner layers", pad, "length")
    r.add("drill", "Drill size (hole + plating)", drill, "length")
    r.add("antipad", "Plane clearance (antipad) diameter", pad + 2 * a["clearance"], "length")
    r.add("thermal_od", "Thermal relief outer diameter (typ.)", pad + 2 * a["clearance"],
          "length")
    r.references = ["IPC-2221B 9.1.1, IPC-2222 9.1"]
    return r


@calculator(
    "bga_land", "BGA land and breakout", "layout",
    "Land size for a BGA ball (IPC-7351 reduction), and the widest trace that "
    "fits between two lands.",
    [Input("ball", "Ball diameter", "length", 0.5, "mm", min=0.05),
     Input("pitch", "Ball pitch", "length", 0.8, "mm", min=0.1),
     Input("collapsing", "Collapsing balls (SAC/eutectic)", "bool", True),
     Input("space", "Minimum trace-to-land spacing", "length", 4.0, "mil", min=0.0),
     Input("traces", "Traces between two lands", "count", 1, min=1, max=4),
     Input("solder_mask_defined", "Solder-mask-defined lands", "bool", False)])
def bga_land(a):
    ball = a["ball"]
    if a["collapsing"]:
        red = 0.25 if ball >= 0.55e-3 else 0.20
    else:
        red = -0.05
    land = ball * (1 - red)
    n = int(a["traces"])
    room = a["pitch"] - land - (n + 1) * a["space"]
    w = room / n
    r = Result(model="IPC-7351B BGA land reduction; breakout geometry", status="published")
    r.add("land", "Land diameter (nominal)", land, "length", primary=True)
    r.add("reduction", "Reduction from ball", red * 100, "percent")
    r.add("trace_max", "Widest trace between lands", max(w, 0.0), "length", primary=True)
    r.add("via_pad_max", "Largest dog-bone pad at the diagonal",
          a["pitch"] * math.sqrt(2) - land - 2 * a["space"], "length")
    if w <= 0:
        r.warnings.append("No trace fits between the lands at that spacing.")
    if a["solder_mask_defined"]:
        r.warnings.append("SMD lands: the copper is larger than the mask opening; "
                          "size the opening to the land above.")
    r.accuracy = ("IPC-7351B: 25 % reduction for collapsing balls >= 0.55 mm, 20 % below; "
                  "non-collapsing lands about 5 % larger than the ball. Follow the "
                  "package data sheet where it gives a land.")
    r.references = ["IPC-7351B, BGA land pattern section"]
    return r


@calculator(
    "conductor_to_pad", "Traces between pads", "layout",
    "Widest trace that fits between two pads, or how many traces of a width fit.",
    [Input("pitch", "Pad pitch (centre to centre)", "length", 100.0, "mil", min=1.0),
     Input("pad", "Pad diameter or width", "length", 60.0, "mil", min=0.0),
     Input("space", "Minimum spacing", "length", 6.0, "mil", min=0.0),
     Input("traces", "Number of traces", "count", 1, min=1, max=20)])
def conductor_to_pad(a):
    n = int(a["traces"])
    room = a["pitch"] - a["pad"] - (n + 1) * a["space"]
    r = Result(model="Free gap = pitch - pad - (n + 1) spacing, shared by n traces",
               status="exact")
    r.add("w_max", "Widest trace", max(room / n, 0.0), "length", primary=True)
    r.add("gap", "Copper-free gap between pads", a["pitch"] - a["pad"], "length")
    if room <= 0:
        r.warnings.append("That many traces do not fit at this spacing.")
    return r


@calculator(
    "corner_to_corner", "Corner-to-corner (diagonal)", "layout",
    "Diagonal of a rectangular or square pad, aperture or keep-out.",
    [Input("x", "Width", "length", 1.0, "mm", min=0.0),
     Input("y", "Height", "length", 1.0, "mm", min=0.0),
     Input("radius", "Corner radius", "length", 0.0, "mm", min=0.0)])
def corner_to_corner(a):
    x, y, rr = a["x"], a["y"], a["radius"]
    need(2 * rr <= min(x, y), "the corner radius is larger than half the shorter side")
    diag = math.hypot(x, y) - 2 * rr * (math.sqrt(2) - 1)
    r = Result(model="Pythagoras, rounded corners pulled in", status="exact")
    r.add("diag", "Corner-to-corner", diag, "length", primary=True)
    r.add("area", "Area", x * y - (4 - math.pi) * rr * rr, "area")
    return r


@calculator(
    "embedded_resistor", "Embedded (planar) resistor", "layout",
    "Resistance of a printed or buried thin-film resistor from its sheet resistance and shape.",
    [Input("sheet", "Sheet resistance", "resistance", 25.0, "ohm", min=0.001,
           help="Ohms per square of the resistive foil (e.g. 25, 50, 100, 250)."),
     Input("length", "Length (current direction)", "length", 40.0, "mil", min=0.01),
     Input("width", "Width", "length", 20.0, "mil", min=0.01),
     Input("tcr", "TCR", "ppm", 50.0, "ppm", help="ppm per kelvin"),
     Input("delta_t", "Temperature change", "delta_t", 0.0, "°C")])
def embedded_resistor(a):
    sq = a["length"] / a["width"]
    rr = a["sheet"] * sq * (1 + a["tcr"] * 1e-6 * a["delta_t"])
    r = Result(model="R = Rs x squares x (1 + TCR dT)", status="exact")
    r.add("r", "Resistance", rr, "resistance", primary=True)
    r.add("squares", "Squares", sq, "ratio")
    r.accuracy = ("Exact for a rectangle; terminations add roughly 0.1-0.3 squares in "
                  "practice, and etch tolerance on narrow resistors is the main error.")
    return r


@calculator(
    "wire_gauge", "Wire gauge (AWG)", "reference",
    "Diameter, area and resistance of an AWG wire.",
    [Input("awg", "AWG", "ratio", 24, min=-3, max=46),
     Input("length", "Length", "length", 1000.0, "mm", min=0.0),
     Input("temperature", "Temperature", "temperature", 20.0, "°C")])
def wire_gauge(a):
    d = pcb_calc.awg_properties(a["awg"], a["length"], a["temperature"])
    need(d.get("ok", False), d.get("reason", "cannot compute"))
    r = Result(model="ASTM B258 diameter law; copper resistivity at T", status="exact")
    r.add("diameter", "Diameter", d["diameter_mm"] * 1e-3, "length", primary=True)
    r.add("area", "Area", math.pi / 4 * (d["diameter_mm"] * 1e-3) ** 2, "area")
    r.add("r", "Resistance", d["resistance_ohm"], "resistance")
    r.add("r_per_m", "Resistance per metre", d["resistance_ohm_per_m"], "resistance")
    return r


@calculator(
    "drill_chart", "Drill sizes", "reference",
    "Nearest standard metric and fractional-inch drills for a hole.",
    [Input("hole", "Hole diameter", "length", 0.8, "mm", min=0.05, max=50)])
def drill_chart(a):
    d = a["hole"]
    metric = round(d * 1e3 / 0.05) * 0.05e-3
    frac64 = round(d / INCH * 64)
    r = Result(model="Metric drills in 0.05 mm steps; fractional inch in 1/64", status="exact")
    r.add("metric", "Nearest metric drill", metric, "length", primary=True)
    r.add("metric_up", "Next metric drill up", math.ceil(d * 1e3 / 0.05 - 1e-9) * 0.05e-3, "length")
    r.add("fraction", f"Nearest fractional: {frac64}/64 in", frac64 / 64 * INCH, "length")
    return r


@calculator(
    "unit_convert", "Unit conversion", "reference",
    "Convert a value between units of the same kind.",
    [Input("kind", "Quantity", "choice", "length",
           choices=[(k, k.replace("_", " ").capitalize()) for k in
                    ("length", "frequency", "time", "current", "voltage", "resistance",
                     "capacitance", "inductance", "power", "temperature", "area",
                     "velocity", "thermal_conductivity")]),
     Input("value", "Value with unit", "choice_text", "10 mil",
           help="For example 10 mil, 2.4 GHz, 85 F, 0.5 oz.")])
def unit_convert(a):
    kind = a["kind"]
    text = str(a["value"]).strip()
    si = to_si(text, kind, next(iter(UNITS[kind])))
    r = Result(model="Exact unit definitions", status="exact")
    for u in UNIT_CHOICES.get(kind) or list(UNITS[kind]):
        if u in ("°F", "K") and kind == "temperature":
            continue
        r.add(f"in_{u}", u, from_si(si, kind, u), "ratio", digits=6)
    if kind == "temperature":
        r.add("in_f", "°F", si * 9 / 5 + 32, "ratio", digits=6)
        r.add("in_k", "K", si + 273.15, "ratio", digits=6)
    if r.outputs:
        r.outputs[0]["primary"] = True
    return r


@calculator(
    "db_convert", "dB, dBm, volts and watts", "reference",
    "Convert between dBm, watts and RMS volts in a system impedance, and dB ratios.",
    [Input("dbm", "Power", "dbm", 0.0, "dBm"),
     Input("z0", "System impedance", "resistance", 50.0, "ohm", min=0.001),
     Input("gain_db", "Gain or loss", "db", 6.0, "dB")])
def db_convert(a):
    p = 1e-3 * 10 ** (a["dbm"] / 10)
    v = math.sqrt(p * a["z0"])
    r = Result(model="P = 1 mW x 10^(dBm/10), V = sqrt(P Z)", status="exact")
    r.add("watts", "Power", p, "power", primary=True)
    r.add("vrms", "RMS voltage", v, "voltage")
    r.add("vpp", "Peak-to-peak (sine)", v * 2 * math.sqrt(2), "voltage")
    r.add("dbuv", "dBuV", 20 * math.log10(v / 1e-6) if v > 0 else -math.inf, "db")
    r.add("power_ratio", "Power ratio of the gain", 10 ** (a["gain_db"] / 10), "ratio")
    r.add("voltage_ratio", "Voltage ratio of the gain", 10 ** (a["gain_db"] / 20), "ratio")
    return r
