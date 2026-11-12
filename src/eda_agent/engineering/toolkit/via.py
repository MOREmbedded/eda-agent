# SPDX-License-Identifier: Apache-2.0
"""Via calculators: current, resistance, thermal resistance, parasitics."""
from __future__ import annotations

import math

from ...thermal import physics as tp
from .. import pcb_calc
from .conductor import _board_inputs, make_board
from .core import Input, Result, calculator, need
from .fdsolver import C0, EPS0, MU0
from .thermal_fv import current_for_rise, solve_trace

MIL = 25.4e-6


@calculator(
    "via_properties", "Via current and properties", "via",
    "Barrel resistance, current for a temperature rise, thermal resistance, "
    "inductance, capacitance, impedance and resonance of a plated via.",
    [Input("hole", "Finished hole diameter", "length", 10.0, "mil", min=0.5,
           prefill="via.hole"),
     Input("plating", "Barrel plating thickness", "length", 25.0, "um", min=1.0,
           help="IPC-6012 class 2: 20 um average minimum; 25 um is a common default."),
     Input("length", "Via length (layers spanned)", "length", 1.6, "mm", min=0.01,
           prefill="board.thickness"),
     Input("pad", "Pad diameter", "length", 20.0, "mil", min=0.0, prefill="via.pad"),
     Input("antipad", "Antipad (clearance) diameter", "length", 30.0, "mil", min=0.0),
     Input("er", "Dielectric constant Er", "ratio", 4.2, min=1.0, prefill="stackup.er"),
     Input("current", "Current", "current", 1.0, "A", min=0.0),
     Input("rise", "Allowed temperature rise", "delta_t", 10.0, "°C", min=0.1),
     Input("count", "Number of vias in parallel", "count", 1, min=1, max=10000),
     Input("fill", "Fill", "choice", "none",
           choices=[(k, k.capitalize()) for k in tp.VIA_FILL_K]),
     Input("plane_spacing", "Return plane spacing (loop L)", "length", 0.0, "mil", min=0.0,
           help="Distance between the two planes the via current returns through; "
                "0 skips the loop-inductance figure."),
     Input("z0", "Line impedance (for rise-time impact)", "resistance", 50.0, "ohm", min=1.0),
     *[i for i in _board_inputs() if i.key not in ("board_thickness",)]],
    status="solver")
def via_properties(a):
    d, tpl, L, n = a["hole"], a["plating"], a["length"], int(a["count"])
    drill = d + 2.0 * tpl
    area = math.pi / 4.0 * (drill ** 2 - d ** 2)
    rho = tp.rho_copper(a["ambient"])
    r_one = rho * L / area
    rth_one = tp.barrel_resistance(drill, L, tpl, a["fill"])
    # Current: IPC-2152 treats a via barrel as a conductor of the same
    # cross-section. Its unrolled barrel is a trace of width pi*(d+t)
    # and thickness t; solve that trace on this board.
    a2 = dict(a, board_thickness=max(L, 0.2e-3))
    board = make_board(a2, 0.0, tpl)
    w_eq = math.pi * (d + tpl)
    i_one = current_for_rise(a["rise"], w_eq, tpl, 0.0, board)
    rise_at_i = solve_trace(a["current"] / n, w_eq, tpl, 0.0, board).rise
    r = Result(model="Barrel as an unrolled conductor on the board (IPC-2152 "
               "equivalence), parasitics from Johnson & Graham", status="solver")
    r.add("i_max", "Current for the allowed rise (all vias)", i_one * n, "current", primary=True)
    r.add("rise_at_i", "Rise at the given current", rise_at_i, "delta_t")
    r.add("r_dc", "Barrel resistance (all vias)", r_one / n, "resistance")
    r.add("v_drop", "Voltage drop at the current", a["current"] * r_one / n, "voltage")
    r.add("power", "Power at the current", a["current"] ** 2 * r_one / n, "power")
    r.add("rth", "Thermal resistance (all vias)", rth_one / n, "thermal_resistance")
    r.add("aspect", "Aspect ratio (length / drill)", L / drill, "ratio")
    r.add("area", "Barrel copper cross-section", area, "area")
    h_in, d_in = L / 0.0254, d / 0.0254
    l_nh = 5.08 * h_in * (math.log(4.0 * h_in / d_in) + 1.0)
    r.add("l_self", "Inductance (partial, Johnson)", l_nh * 1e-9, "inductance")
    if a["pad"] > 0 and a["antipad"] > a["pad"]:
        c_pf = 1.41 * a["er"] * h_in * (a["pad"] / 0.0254) / ((a["antipad"] - a["pad"]) / 0.0254)
        c = c_pf * 1e-12
        r.add("c_pad", "Capacitance (pad to plane, Johnson)", c, "capacitance")
        lv = l_nh * 1e-9
        r.add("z_via", "Via impedance sqrt(L/C)", math.sqrt(lv / c), "resistance")
        r.add("f_res", "Self-resonance", 1.0 / (2 * math.pi * math.sqrt(lv * c)), "frequency")
        r.add("t_rise_c", "10-90 % rise added by its capacitance", 2.2 * c * a["z0"] / 2.0, "time")
        r.add("z_coax", "Barrel-in-antipad impedance (coaxial)",
              60.0 / math.sqrt(a["er"]) * math.log(a["antipad"] / drill), "resistance")
    if a["plane_spacing"] > 0:
        lp = pcb_calc.via_loop_inductance(a["plane_spacing"] / MIL, drill / MIL)
        if lp.get("ok"):
            r.add("l_loop", "Loop inductance between the planes", lp["inductance_nh"] * 1e-9,
                  "inductance")
    if L / drill > 10:
        r.warnings.append(f"Aspect ratio {L / drill:.1f}:1 is above what most fabs plate "
                          "reliably (8-10:1 for through vias).")
    r.accuracy = ("Resistance exact for the stated plating. Current: the IPC-2152 "
                  "equivalent-conductor method on a physics model of the board; real "
                  "vias usually run cooler, because the pads and traces sink heat. "
                  "L and C: Johnson's rules of thumb, +/-20-30 %; use a 3D solver for "
                  "signal-integrity sign-off.")
    r.references = ["IPC-2152 (2009), section on vias",
                    "H. Johnson, M. Graham, High-Speed Digital Design, 1993, ch. 7"]
    return r


@calculator(
    "via_pair", "Differential via pair", "via",
    "Differential and single impedance of a via pair through a plane stack (two-wire and coaxial models).",
    [Input("hole", "Finished hole diameter", "length", 10.0, "mil", min=0.5),
     Input("plating", "Plating", "length", 25.0, "um", min=1.0),
     Input("pitch", "Via pitch (centre to centre)", "length", 40.0, "mil", min=1.0),
     Input("antipad", "Antipad diameter (each)", "length", 30.0, "mil", min=1.0),
     Input("length", "Via length", "length", 1.6, "mm", min=0.01),
     Input("er", "Dielectric constant Er", "ratio", 4.2, min=1.0)],
    status="estimate")
def via_pair(a):
    d = a["hole"] + 2 * a["plating"]
    s = a["pitch"]
    need(s > d, "the pitch must exceed the barrel diameter")
    er = a["er"]
    eta = math.sqrt(MU0 / (EPS0 * er))
    zdiff = eta / math.pi * math.acosh(s / d)
    zse = eta / (2 * math.pi) * math.log(a["antipad"] / d)
    r = Result(model="Two-wire line (exact for parallel cylinders in a uniform dielectric)",
               status="estimate")
    r.add("zdiff", "Differential impedance (two-wire)", zdiff, "resistance", primary=True)
    r.add("z_coax", "Single via in its antipad (coaxial)", zse, "resistance")
    r.add("delay", "Delay through the via", a["length"] * math.sqrt(er) / C0, "time")
    r.accuracy = ("Ignores the pads and the plane stack's fringing: typically "
                  "within 10-15 % for vias longer than their pitch. A 3D solver "
                  "is the reference.")
    return r
