# SPDX-License-Identifier: Apache-2.0
"""Conductor calculators: trace current and temperature, resistance, fusing.

The trace-current page solves the board's cross-section for heat
(thermal_fv.py) instead of reading a chart. IPC-2221's 1950s curve is
reported beside it for reference, because many specifications still
quote it, and its known bias (too generous for small traces on boards
without planes, too strict for wide traces and boards with planes) is
visible in the comparison.
"""
from __future__ import annotations

import math

from ...thermal import physics as tp
from .. import pcb_calc
from . import tline
from .core import MIL, OZ_M, Input, Result, calculator, need
from .thermal_fv import Board, Plane, current_for_rise, solve_trace, width_for_current


THERMAL_REFS = [
    "IPC-2152 (2009): current capacity depends on board, planes and environment",
    "D. Brooks, J. Adam, PCB Trace and Via Currents and Temperatures, 2nd ed., 2017",
    "Natural convection: Lloyd-Moran, Raithby-Hollands, Churchill-Chu (eda_agent.thermal.physics)",
]


def _board_inputs() -> list[Input]:
    return [
        Input("board_thickness", "Board thickness", "length", 1.6, "mm", min=0.05,
              prefill="board.thickness"),
        Input("laminate", "Laminate", "choice", "fr4",
              choices=[(k, d.name) for k, d in tp.DIELECTRICS.items()] + [("custom", "Custom")]),
        Input("k_xy", "Laminate k in-plane (custom)", "thermal_conductivity", 0.81, "W/mK",
              min=0.01, show_if={"laminate": ["custom"]}),
        Input("k_z", "Laminate k through-plane (custom)", "thermal_conductivity", 0.29, "W/mK",
              min=0.01, show_if={"laminate": ["custom"]}),
        Input("planes", "Copper planes", "choice", "none",
              choices=[("none", "None"), ("one", "One plane"), ("two", "Two planes")],
              prefill="board.planes"),
        Input("plane_distance", "Distance trace to nearest plane", "length", 10.0, "mil",
              min=0.5, show_if={"planes": ["one", "two"]}, prefill="stackup.dielectric_height"),
        Input("plane_copper", "Plane copper", "length", 1.0, "oz", min=0.1,
              show_if={"planes": ["one", "two"]}),
        Input("board_width", "Board size across the trace", "length", 100.0, "mm", min=5.0,
              prefill="board.width"),
        Input("board_length", "Board size along the trace", "length", 100.0, "mm", min=5.0,
              prefill="board.length"),
        Input("orientation", "Board orientation", "choice", "horizontal",
              choices=[("horizontal", "Horizontal"), ("vertical", "Vertical")]),
        Input("airflow", "Air velocity", "velocity", 0.0, "m/s", min=0.0, max=30.0),
        Input("ambient", "Ambient temperature", "temperature", 25.0, "°C", min=-55, max=200),
        Input("mask", "Solder mask on the faces", "bool", True),
    ]


def make_board(a, trace_depth: float, t: float) -> Board:
    lam = a["laminate"]
    if lam == "custom":
        kxy, kz = a["k_xy"], a["k_z"]
    else:
        d = tp.DIELECTRICS[lam]
        kxy, kz = d.k_xy, d.k_z
    H = a["board_thickness"]
    planes = []
    if a["planes"] in ("one", "two"):
        pd, pc = a["plane_distance"], a["plane_copper"]
        # The first plane sits below the trace at the given distance;
        # a second one mirrors it near the other face.
        d1 = trace_depth + t + pd
        need(d1 + pc <= H, "the plane does not fit inside the board at that distance")
        planes.append(Plane(d1, pc))
        if a["planes"] == "two":
            d2 = max(H - d1 - pc, 0.0) if trace_depth == 0 else max(trace_depth - pd - pc, 0.0)
            if abs(d2 - d1) > pc:
                planes.append(Plane(d2, pc))
    return Board(thickness=H, width=a["board_width"], length=a["board_length"],
                 k_xy=kxy, k_z=kz, planes=planes,
                 mask=tp.SOLDER_MASK_MILS * MIL if a["mask"] else 0.0,
                 emissivity=tp.EMISSIVITY_MASK if a["mask"] else 0.6,
                 orientation=a["orientation"], airflow=a["airflow"], ambient=a["ambient"])


def _ipc2221_current(w, t, rise, internal):
    k = 0.024 if internal else 0.048
    area = w * t / MIL ** 2
    return k * rise ** 0.44 * area ** 0.725


@calculator(
    "trace_current", "Trace current and temperature rise", "conductor",
    "Current, width or temperature rise of a trace, solved for your board's "
    "thickness, planes, laminate and air (the physics IPC-2152 measured).",
    [Input("solve_for", "Solve for", "choice", "rise",
           choices=[("rise", "Temperature rise (given current and width)"),
                    ("width", "Width (given current and allowed rise)"),
                    ("current", "Current (given width and allowed rise)")]),
     Input("current", "Current", "current", 2.0, "A", min=0.0,
           show_if={"solve_for": ["rise", "width"]}),
     Input("w", "Trace width", "length", 20.0, "mil", min=0.1,
           show_if={"solve_for": ["rise", "current"]}, prefill="trace.width"),
     Input("rise", "Allowed temperature rise", "delta_t", 10.0, "°C", min=0.1, max=500,
           show_if={"solve_for": ["width", "current"]}),
     Input("t", "Copper thickness", "length", 1.0, "oz", min=0.05, prefill="stackup.copper"),
     Input("layer", "Layer", "choice", "external",
           choices=[("external", "Outer layer"), ("internal", "Inner layer")]),
     Input("depth", "Inner layer: depth below the top face", "length", 10.0, "mil", min=0.0,
           show_if={"layer": ["internal"]}),
     Input("length", "Trace length (for R, V, P)", "length", 50.0, "mm", min=0.0),
     *_board_inputs()],
    status="solver")
def trace_current(a):
    t = a["t"]
    depth = a["depth"] if a["layer"] == "internal" else 0.0
    board = make_board(a, depth, t)
    mode = a["solve_for"]
    if mode == "rise":
        I, w = a["current"], a["w"]
        res = solve_trace(I, w, t, depth, board)
    elif mode == "width":
        I = a["current"]
        need(I > 0, "current must be greater than zero")
        w = width_for_current(I, a["rise"], t, depth, board)
        res = solve_trace(I, w, t, depth, board)
    else:
        w = a["w"]
        I = current_for_rise(a["rise"], w, t, depth, board)
        res = solve_trace(I, w, t, depth, board)
    r = Result(model="2D thermal cross-section of the board (finite volume), "
               "rho(T), convection + radiation", status="solver")
    r.add("w", "Trace width", w, "length", primary=mode == "width")
    r.add("current", "Current", I, "current", primary=mode == "current")
    r.add("rise", "Temperature rise", res.rise, "delta_t", primary=mode == "rise")
    r.add("t_trace", "Trace temperature", res.t_trace, "temperature")
    L = a["length"]
    r.add("r_hot", "Resistance at temperature", res.r_per_m * L, "resistance")
    r.add("r_20", "Resistance at 20 °C", tp.RHO_COPPER_20C / (w * t) * L, "resistance")
    r.add("v_drop", "Voltage drop", I * res.r_per_m * L, "voltage")
    r.add("power", "Power dissipated", res.power_per_m * L, "power")
    r.add("density", "Current density", I / (w * t) * 1e-6, "ratio", note="A/mm²")
    i2221 = _ipc2221_current(w, t, max(res.rise, 0.1), a["layer"] == "internal")
    r.add("ipc2221_current", "IPC-2221 current for the same rise (legacy)", i2221, "current")
    r.check("current", I, i2221, "IPC-2221 chart (1950s data, no board effects)", "current")
    if res.runaway:
        r.warnings.append("Thermal runaway: the trace heats faster than it can shed "
                          "heat at this current. It will fuse.")
    if res.t_trace > 130:
        r.warnings.append("Above 130 °C: beyond FR-4's Tg and most laminates' MOT.")
    r.accuracy = ("Physics model, no chart. Board, plane and air inputs dominate: "
                  "a plane at 10 mil can halve the rise. IPC-2152's Figure 5-1 is "
                  "a conservative envelope (polyimide, no planes, still air), so "
                  "it reads 10-50 % hotter than a real FR-4 board.")
    r.references = list(THERMAL_REFS)
    return r


@calculator(
    "conductor_properties", "Conductor resistance, skin effect, voltage drop", "conductor",
    "DC and AC resistance of a trace at a temperature and frequency.",
    [Input("w", "Trace width", "length", 10.0, "mil", min=0.01, prefill="trace.width"),
     Input("t", "Copper thickness", "length", 1.0, "oz", min=0.01, prefill="stackup.copper"),
     Input("length", "Length", "length", 50.0, "mm", min=0.0),
     Input("temperature", "Copper temperature", "temperature", 25.0, "°C", min=-200, max=500),
     Input("current", "Current", "current", 1.0, "A", min=0.0),
     Input("frequency", "Frequency (0 = DC)", "frequency", 0.0, "MHz", min=0.0),
     Input("plating", "Plating on top (outer layer)", "length", 0.0, "um", min=0.0,
           help="Extra copper plated onto outer-layer traces; 0 if none.")])
def conductor_properties(a):
    w, t = a["w"], a["t"] + a["plating"]
    rho = tp.rho_copper(a["temperature"])
    rdc = rho * a["length"] / (w * t)
    f = a["frequency"]
    k = tline.ac_resistance_factor(f, w, t, rho) if f > 0 else 1.0
    delta = tline.skin_depth(f, rho) if f > 0 else math.inf
    r = Result(model="R = rho(T) L / A; skin effect from the bar's surface", status="exact")
    r.add("r_dc", "DC resistance", rdc, "resistance", primary=True)
    r.add("r_ac", "AC resistance at f", rdc * k, "resistance")
    if f > 0:
        r.add("skin_depth", "Skin depth", delta, "length")
        r.add("skin_ratio", "Skin depth / copper thickness", delta / t * 100, "percent")
    r.add("v_drop", "Voltage drop", a["current"] * rdc * k, "voltage")
    r.add("power", "Power dissipated", a["current"] ** 2 * rdc * k, "power")
    r.add("rho", "Resistivity at T", rho, "resistivity")
    r.add("sheet", "Sheet resistance", rho / t * 1e3, "ratio", note="mΩ per square")
    r.accuracy = ("DC exact for the stated copper (IACS 1.724e-8 ohm*m, alpha 0.00393/K); "
                  "real foil runs 2-5 % higher. AC uses a smooth DC-to-skin "
                  "transition, within ~10 % at the knee and exact at both ends.")
    r.references = ["IEC 60028 (IACS copper)", "Wheeler, Proc. IRE 1942 (skin effect)"]
    return r


@calculator(
    "fusing_current", "Fusing current (Onderdonk)", "conductor",
    "The current that melts a trace in a given time. A destruction limit, not a rating.",
    [Input("w", "Trace width", "length", 10.0, "mil", min=0.01, prefill="trace.width"),
     Input("t", "Copper thickness", "length", 1.0, "oz", min=0.01, prefill="stackup.copper"),
     Input("time", "Time", "time", 1.0, "s", min=1e-6),
     Input("ambient", "Ambient", "temperature", 25.0, "°C", min=-55, max=500),
     Input("etch_factor", "Etch: top narrower by", "length", 0.0, "mil", min=0.0)])
def fusing_current(a):
    w_eff = a["w"] - a["etch_factor"] / 2.0
    need(w_eff > 0, "the etch removes the whole trace")
    oz = a["t"] / OZ_M
    d = pcb_calc.fusing_current(w_eff / MIL, oz, a["time"], a["ambient"])
    need(d.get("ok", False), d.get("reason", "cannot compute"))
    r = Result(model="Onderdonk's equation (adiabatic)", status="published")
    r.add("i_fuse", "Fusing current", d["fusing_current_a"], "current", primary=True)
    r.add("area", "Cross-section used", w_eff * a["t"], "area")
    r.warnings.append(d["warning"])
    if d.get("out_of_range"):
        r.warnings.append(d["out_of_range"])
    r.accuracy = "Adiabatic: trustworthy below about 1 s; longer times read low."
    r.references = ["I. M. Onderdonk, via Brooks, 'Fusing current: when traces melt', 2001"]
    return r


@calculator(
    "conductor_spacing", "Minimum conductor spacing (IPC-2221)", "conductor",
    "Electrical clearance for a peak voltage between two conductors.",
    [Input("voltage", "Peak voltage between conductors (DC or AC peak)", "voltage", 50.0,
           "V", min=0.0),
     Input("environment", "Environment", "choice", "B2",
           choices=[(k, f"{k}: {v}") for k, v in pcb_calc.IPC2221_ENVIRONMENTS.items()])])
def conductor_spacing(a):
    d = pcb_calc.ipc2221_spacing(max(a["voltage"], 0.001), a["environment"])
    need(d.get("ok", False), d.get("reason", "cannot compute"))
    r = Result(model="IPC-2221 Table 6-1 (A/B revision classes)", status="published")
    r.add("spacing", "Minimum spacing", d["spacing_mm"] * 1e-3, "length", primary=True)
    if d.get("disputed"):
        r.warnings.append("Sources disagree on this cell; the larger value is shown: "
                          f"{d['disputed']}")
    r.warnings.append("IPC-2221C redefined the B/A classes; match the class by its "
                      "description, and check safety standards (IEC 62368-1, IEC 60664) "
                      "where they apply: they usually ask for more.")
    r.accuracy = "Table values transcribed from secondary sources; confirm against your copy of IPC-2221."
    return r
