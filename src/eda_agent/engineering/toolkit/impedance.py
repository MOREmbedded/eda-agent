# SPDX-License-Identifier: Apache-2.0
"""Controlled-impedance calculators: single-ended and differential.

Every structure is solved by the 2D field solver (fdsolver.py), with the
grid refined twice and Richardson-extrapolated, then reported beside the
closed form where one exists. Loss is computed at the frequency asked:
dielectric loss from a causal Djordjevic-Sarkar Er(f), conductor loss
from the solver's surface current with skin effect and a chosen
roughness model.
"""
from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np
from scipy.linalg import sqrtm

from . import fdsolver as fd
from . import tline
from .core import INCH, MIL, OZ_M, Input, Result, calculator, need

NP_TO_DB = 20.0 / math.log(10.0)

REFS_SOLVER = [
    "2D quasi-static finite-difference solver (this project), "
    "Richardson-extrapolated over two grids",
    "Djordjevic, Sarkar et al., IEEE Trans. EMC 43(4), 2001 (causal Er(f))",
]

# ------------------------------------------------------------- shared inputs


def _geo(key, label, default, help_="", prefill=""):
    return Input(key, label, "length", default, "mil", min=0.0, help=help_, prefill=prefill)


def _common_tail(freq_default: float = 1.0) -> list[Input]:
    return [
        Input("er", "Dielectric constant Er", "ratio", 4.2, min=1.0, max=50.0,
              help="At the frequency below, from the laminate data sheet.",
              prefill="stackup.er"),
        Input("tand", "Loss tangent", "ratio", 0.02, min=0.0, max=0.2,
              prefill="stackup.tand"),
        Input("f_spec", "Er and tan d specified at", "frequency", 1.0, "GHz", min=0.0,
              help="The data-sheet frequency of the Er and tan d above."),
        Input("frequency", "Analysis frequency", "frequency", freq_default, "GHz", min=0.0,
              help="Impedance, loss and dispersion are evaluated here, with Er "
                   "and tan d moved to it by the causal Djordjevic-Sarkar model. "
                   "0 gives the quasi-static answer with the data-sheet Er."),
        Input("roughness", "Copper roughness model", "choice", "huray",
              choices=[("none", "Smooth"), ("hammerstad", "Hammerstad (Rq)"),
                       ("groisse", "Groisse (Rq)"), ("huray", "Huray snowball")]),
        Input("rq", "RMS roughness Rq", "length", 1.0, "um", min=0.0,
              show_if={"roughness": ["hammerstad", "groisse"]}),
        Input("huray_radius", "Huray nodule radius", "length", 0.5, "um", min=0.01,
              show_if={"roughness": ["huray"]}),
        Input("huray_sr", "Huray surface ratio", "ratio", 1.8, min=0.0, max=20.0,
              help="N*4*pi*r^2/A_flat. About 1-2 for low-profile foil, 3-6 "
                   "for standard ED foil; fabs and foil makers publish it.",
              show_if={"roughness": ["huray"]}),
        Input("method", "Engine", "choice", "solver",
              choices=[("solver", "Field solver (accurate)"),
                       ("closed", "Closed form (instant)")]),
    ]


def _copper(prefill="stackup.copper"):
    return Input("t", "Copper thickness", "length", 1.0, "oz", min=0.0,
                 help="1 oz = 35 um finished.", prefill=prefill)


def _etch():
    return Input("etch", "Etch: top narrower by", "length", 0.0, "mil", min=0.0,
                 help="Total width lost at the top of the trace (trapezoid). "
                      "0 is a rectangle; about 1 mil per oz is typical.")


def _mask():
    return [
        Input("mask_t", "Solder mask thickness", "length", 0.0, "mil", min=0.0,
              help="0 for none. About 0.5-1 mil over the trace.",
              prefill="stackup.mask_t"),
        Input("mask_er", "Solder mask Er", "ratio", 3.5, min=1.0, max=10.0),
    ]


# ------------------------------------------------------------- engine


def _metal(xc, w, y0, t, etch, net):
    top = max(w - etch, w * 0.05)
    return fd.Metal(xc - w / 2, xc + w / 2, y0, y0 + t, net,
                    xc - top / 2, xc + top / 2)


def _solve(build: Callable[[float], fd.CrossSection], f: float, materials, fast=False):
    """Solve on two grids, the second twice as fine everywhere, and
    extrapolate C, C0 and G to zero cell size.

    The error is set by the field singularity at the conductor edges. At
    the knife edge of a zero-thickness strip the capacitance error falls
    as h; at the 90 degree corner of a real trace as h^(4/3). Both
    orders were measured (tests/toolkit/test_fdsolver.py); extrapolating
    with them brings a 0.5 % grid error under 0.05 %.
    """
    xs = build(f)
    res = (30.0, 60.0) if fast else (40.0, 80.0)
    s1 = fd.solve(xs, res[0])
    s2 = fd.solve(xs, res[1])
    thin = any(m.net >= 0 and m.y1 - m.y0 <= 0 for m in xs.metals)
    k = 1.0 / (2.0 ** (1.0 if thin else 4.0 / 3.0) - 1.0)
    raw = s2.C.copy()
    s2.C = s2.C + (s2.C - s1.C) * k
    s2.G = s2.G + (s2.G - s1.G) * k
    s2.C0 = s2.C0 + (s2.C0 - s1.C0) * k
    s2.L = fd.MU0 * fd.EPS0 * np.linalg.inv(s2.C0)
    s2.extrapolation = float(np.max(np.abs(np.diag(s2.C - raw) / np.diag(s2.C))))
    s2.coarse = s1
    s2.xs = xs
    return s2


def _rs(a) -> float:
    f = a["frequency"]
    if f <= 0:
        return 0.0
    k = tline.rough_factor(f, a["roughness"], rq=a.get("rq") or 0.0,
                           radius=a.get("huray_radius") or 0.5e-6,
                           sr=a.get("huray_sr") or 0.0)
    return tline.surface_resistance(f) * k


def at_frequency(a: dict[str, Any]) -> dict[str, Any]:
    """Move Er and tan d from the data-sheet frequency to the analysis one.

    A laminate's Er falls and its tan d changes with frequency; holding
    the data-sheet figures fixed everywhere is the commonest silent error
    in impedance work. The causal Djordjevic-Sarkar model carries them.
    """
    f, fs = a.get("frequency") or 0.0, a.get("f_spec") or 0.0
    if f <= 0 or fs <= 0 or abs(f - fs) / fs < 1e-9:
        return a
    b = dict(a)
    b["er"], b["tand"] = tline.djordjevic_sarkar(a["er"], a["tand"], fs, f)
    if b.get("er2"):
        b["er2"], _ = tline.djordjevic_sarkar(a["er2"], a["tand"], fs, f)
    b["er_spec"] = a["er"]
    return b


def _mode_figures(sol, v, a, w_eff_t):
    """Z, Er_eff, delay, L, C, and loss for one drive pattern."""
    v = np.asarray(v, dtype=float)
    n = float(v @ v)
    c = float(v @ sol.C @ v) / n
    c0 = float(v @ sol.C0 @ v) / n
    g = float(v @ sol.G @ v) / n
    z = 1.0 / (fd.C0 * math.sqrt(c * c0))
    ee = c / c0
    f = a["frequency"]
    w = 2 * math.pi * f
    ad = 0.5 * w * g * z
    ac = 0.0
    if f > 0 and w_eff_t > 0:
        rs = _rs(a)
        ac2 = fd.conductor_loss_np_per_m(sol, v, z, rs)
        ac1 = fd.conductor_loss_np_per_m(sol.coarse, v, z, rs)
        # The surface current at a square corner goes as r^-1/3, so the
        # discrete loss integral converges as h^(1/3). Extrapolate with
        # that exponent (measured: within 1 % of Pozar/Wheeler stripline
        # loss, against 5-7 % for the finer grid alone).
        ac = ac2 + (ac2 - ac1) / (2.0 ** (1.0 / 3.0) - 1.0)
    return {"z": z, "ee": ee, "c": c, "l": z * z * c,
            "delay": math.sqrt(ee) / fd.C0, "ad": ad, "ac": ac}


def _report_line(r: Result, m: dict, prefix: str = "", label: str = "",
                 primary_z: bool = True) -> None:
    r.add(prefix + "z0", label or "Characteristic impedance Z0", m["z"], "resistance",
          primary=primary_z)
    r.add(prefix + "er_eff", "Effective Er", m["ee"], "ratio")
    r.add(prefix + "delay_per_in", "Propagation delay", m["delay"] * INCH, "time",
          note="per inch")
    r.add(prefix + "delay_per_mm", "Propagation delay per mm", m["delay"] * 1e-3, "time")
    r.add(prefix + "c_per_in", "Capacitance per inch", m["c"] * INCH, "capacitance")
    r.add(prefix + "l_per_in", "Inductance per inch", m["l"] * INCH, "inductance")
    if m.get("ac") is not None:
        r.add(prefix + "loss_conductor", "Conductor loss", m["ac"] * NP_TO_DB * INCH,
              "db", note="dB per inch")
        r.add(prefix + "loss_dielectric", "Dielectric loss", m["ad"] * NP_TO_DB * INCH,
              "db", note="dB per inch")
        r.add(prefix + "loss_total", "Total loss", (m["ac"] + m["ad"]) * NP_TO_DB * INCH,
              "db", note="dB per inch")


def _solver_note(r: Result, sol) -> None:
    r.status = "solver"
    r.accuracy = ("Field solver: Z0 within 0.1 % of the exact conformal-mapping "
                  "results (Cohn stripline and coupled stripline) and within "
                  "0.3 % of Hammerstad-Jensen microstrip in the tests; the "
                  f"extrapolation to zero cell size moved C by {sol.extrapolation * 100:.2f} %. "
                  "Conductor loss within 2 % of Wheeler's stripline "
                  "result for smooth copper; with roughness, the roughness "
                  "parameters dominate the error.")
    r.references = list(REFS_SOLVER)
    r.extra["solver_nodes"] = sol.nodes
    r.extra["section"] = _section(sol.xs)


def _section(xs: fd.CrossSection) -> dict[str, Any]:
    """The solved cross-section as plain shapes, for a drawing."""
    metals = []
    for m in xs.metals:
        t0, t1 = m.top()
        metals.append({"x0": m.x0, "x1": m.x1, "xt0": t0, "xt1": t1,
                       "y0": m.y0, "y1": m.y1, "net": m.net})
    return {"metals": metals,
            "dielectrics": [{"y0": d.y0, "y1": d.y1, "er": d.er} for d in xs.dielectrics],
            "coatings": [{"y": c.surface_y, "t": c.thickness, "er": c.er} for c in xs.coatings],
            "ground_bottom": xs.ground_bottom, "ground_top": xs.ground_top}


def _single(a: dict[str, Any], build, closed: Callable[[], tuple[float, float]] | None,
            closed_name: str, ms_dispersion: tuple | None = None) -> Result:
    r = Result()
    if a["method"] == "closed":
        need(closed is not None, "no closed form for this structure; use the field solver")
        z, ee = closed()
        f = a["frequency"]
        m = {"z": z, "ee": ee, "c": math.sqrt(ee) / (fd.C0 * z),
             "l": z * math.sqrt(ee) / fd.C0, "delay": math.sqrt(ee) / fd.C0,
             "ac": None, "ad": 0.0}
        if ms_dispersion and f > 0:
            w, h, er = ms_dispersion
            eef = tline.microstrip_dispersion(ee, er, w, h, f)
            m["z"] = tline.dispersive_z(z, ee, eef)
            m["ee"] = eef
            m["delay"] = math.sqrt(eef) / fd.C0
        _report_line(r, m)
        r.model = closed_name
        r.status = "published"
        r.accuracy = "See the model's stated range; use the field solver for real stackups."
        return r
    sol = _solve(build, a["frequency"], None)
    m = _mode_figures(sol, [1.0], a, a["t"])
    f = a["frequency"]
    if ms_dispersion and f > 0:
        w, h, er_ = ms_dispersion
        eef = tline.microstrip_dispersion(m["ee"], er_, w, h, f)
        m["z_static"] = m["z"]
        m["z"] = tline.dispersive_z(m["z"], m["ee"], eef)
        m["ee"] = eef
        m["delay"] = math.sqrt(eef) / fd.C0
    _report_line(r, m)
    if "z_static" in m:
        r.add("z0_static", "Z0 at DC (quasi-static)", m["z_static"], "resistance")
    r.model = "Field solver" + (" + Kirschning-Jansen dispersion" if ms_dispersion else "")
    _solver_note(r, sol)
    if ms_dispersion:
        r.references.append("Kirschning, Jansen, Electronics Letters 18(6), 1982")
    if closed is not None:
        try:
            zc, eec = closed()
            if ms_dispersion and f > 0:
                w, h, er_ = ms_dispersion
                eef = tline.microstrip_dispersion(eec, er_, w, h, f)
                zc = tline.dispersive_z(zc, eec, eef)
            r.check("Z0", m["z"], zc, closed_name)
        except (ValueError, ZeroDivisionError):
            pass
    return r


# ------------------------------------------------------------ microstrip

@calculator(
    "microstrip", "Microstrip", "impedance",
    "Trace on an outer layer over one reference plane, with optional solder mask.",
    [_geo("w", "Trace width", 6.0, prefill="trace.width"),
     _geo("h", "Dielectric height to plane", 4.0, prefill="stackup.dielectric_height"),
     _copper(), _etch(), *_mask(), *_common_tail()],
    status="solver")
def microstrip(a):
    a = at_frequency(a)
    w, h, t = a["w"], a["h"], a["t"]
    need(w > 0 and h > 0, "width and height must be greater than zero")
    er, tand = a["er"], a["tand"]

    def build(_f):
        xs = fd.CrossSection(metals=[_metal(0.0, w, h, t, a["etch"], 0)],
                             dielectrics=[fd.Dielectric(0.0, h, er, tand)],
                             ground_bottom=0.0)
        if a["mask_t"] > 0:
            xs.coatings.append(fd.Conformal(h, a["mask_t"], a["mask_er"], 0.02))
        return xs
    r = _single(a, build, lambda: tline.microstrip_hj(w, h, t, er),
                "Hammerstad-Jensen 1980" + (" (no solder mask)" if a["mask_t"] > 0 else ""),
                ms_dispersion=(w, h, er))
    if a["mask_t"] > 0 and r.checks:
        r.checks[-1]["note"] = "the closed form ignores solder mask"
    return r


@calculator(
    "embedded_microstrip", "Embedded microstrip", "impedance",
    "Outer-layer style trace buried under more dielectric (coated, or under a prepreg with no plane above).",
    [_geo("w", "Trace width", 6.0, prefill="trace.width"),
     _geo("h", "Dielectric height to plane", 4.0, prefill="stackup.dielectric_height"),
     _geo("cover", "Dielectric above trace bottom", 6.0,
          help_="From the trace's bottom to the top of the covering dielectric."),
     _copper(), _etch(), *_common_tail()],
    status="solver")
def embedded_microstrip(a):
    a = at_frequency(a)
    w, h, t, er, tand = a["w"], a["h"], a["t"], a["er"], a["tand"]
    need(a["cover"] >= t, "the covering dielectric must be at least as thick as the copper")

    def build(_f):
        return fd.CrossSection(metals=[_metal(0.0, w, h, t, a["etch"], 0)],
                               dielectrics=[fd.Dielectric(0.0, h + a["cover"], er, tand)],
                               ground_bottom=0.0)

    def closed():
        # Brooks' buried-microstrip correction of Er_eff (an estimate).
        z, ee = tline.microstrip_hj(w, h, t, er)
        k = 1.0 - math.exp(-1.55 * (h + a["cover"]) / h)
        ee2 = ee * (1.0 - k) + er * k if a["cover"] > 0 else ee
        return z * math.sqrt(ee / ee2), ee2
    return _single(a, build, closed, "Hammerstad-Jensen with buried-Er correction (estimate)")


# ------------------------------------------------------------ stripline

@calculator(
    "stripline", "Stripline (symmetric or offset)", "impedance",
    "Inner-layer trace between two planes. Unequal heights give the offset (asymmetric) case.",
    [_geo("w", "Trace width", 5.0, prefill="trace.width"),
     _geo("h1", "Height: plane below to trace", 5.0, prefill="stackup.h_below"),
     _geo("h2", "Height: trace top to plane above", 5.0, prefill="stackup.h_above"),
     _copper(), _etch(),
     Input("er2", "Er above the trace (0 = same)", "ratio", 0.0, min=0.0, max=50.0,
           help="Prepreg and core often differ; 0 uses the same Er both sides."),
     *_common_tail()],
    status="solver")
def stripline(a):
    a = at_frequency(a)
    w, h1, h2, t, er, tand = a["w"], a["h1"], a["h2"], a["t"], a["er"], a["tand"]
    er2 = a["er2"] or er
    b = h1 + t + h2

    def build(_f):
        return fd.CrossSection(metals=[_metal(0.0, w, h1, t, a["etch"], 0)],
                               dielectrics=[fd.Dielectric(0.0, h1 + t, er, tand),
                                            fd.Dielectric(h1 + t, b, er2, tand)],
                               ground_bottom=0.0, ground_top=b)
    sym = abs(h1 - h2) < 1e-12 and er2 == er

    def closed():
        er_mix = er if er2 == er else (er * h1 + er2 * h2) / (h1 + h2)
        z = (tline.stripline_wheeler(w, b, t, er_mix) if sym
             else tline.stripline_asym(w, h1, h2, t, er_mix))
        return z, er_mix
    name = ("Wheeler 1978" if t > 0 else "Cohn 1954 (exact)") if sym else \
        "Cohn parallel-line offset stripline"
    return _single(a, build, closed, name)


@calculator(
    "dual_stripline", "Dual stripline", "impedance",
    "Two signal layers between two planes; the trace is on one of them.",
    [_geo("w", "Trace width", 5.0, prefill="trace.width"),
     _geo("h1", "Plane to this trace", 5.0),
     _geo("h_layer", "This trace to the other signal layer", 5.0),
     _geo("h2", "Other signal layer to far plane", 5.0),
     _copper(), _etch(), *_common_tail()],
    status="solver")
def dual_stripline(a):
    # The other layer's traces are taken as orthogonal and absent from
    # this cross-section, as IPC-2141 does; only its copper thickness
    # adds to the spacing.
    t = a["t"]
    above = a["h_layer"] + t + a["h2"]
    b = dict(a, h2=above, er2=0.0)
    r = stripline(b)
    r.model = "Dual stripline as offset stripline: " + r.model
    return r


# ------------------------------------------------------------ coplanar

@calculator(
    "coplanar", "Coplanar waveguide", "impedance",
    "Trace with ground copper either side on the same layer, with or without a plane below.",
    [_geo("w", "Trace width", 10.0, prefill="trace.width"),
     _geo("g", "Gap to side ground", 6.0),
     _geo("h", "Dielectric height", 8.0, prefill="stackup.dielectric_height"),
     _geo("gw", "Side ground width", 40.0),
     Input("grounded", "Plane below (grounded CPW)", "bool", True),
     _copper(), _etch(), *_mask(), *_common_tail()],
    status="solver")
def coplanar(a):
    a = at_frequency(a)
    w, g, h, t, er, tand = a["w"], a["g"], a["h"], a["t"], a["er"], a["tand"]
    need(g > 0, "the gap must be greater than zero")
    gw = max(a["gw"], w)

    def build(_f):
        ms = [_metal(0.0, w, h, t, a["etch"], 0),
              _metal(-(w / 2 + g + gw / 2), gw, h, t, a["etch"], -1),
              _metal(+(w / 2 + g + gw / 2), gw, h, t, a["etch"], -1)]
        xs = fd.CrossSection(metals=ms, dielectrics=[fd.Dielectric(0.0, h, er, tand)],
                             ground_bottom=0.0 if a["grounded"] else None,
                             feature=min(w, g, t if t > 0 else w))
        if a["mask_t"] > 0:
            xs.coatings.append(fd.Conformal(h, a["mask_t"], a["mask_er"], 0.02))
        return xs
    r = _single(a, build, lambda: tline.cpw(w, g, h, er, a["grounded"]),
                "Conformal mapping, Simons 2001 (zero thickness, infinite grounds)")
    return r


# ------------------------------------------------------------ pairs


def _pair_report(a, sol, r: Result) -> None:
    sym = abs(sol.C[0, 0] - sol.C[1, 1]) / sol.C[0, 0] < 1e-3
    odd = _mode_figures(sol, [1.0, -1.0], a, a.get("t", 0.0))
    even = _mode_figures(sol, [1.0, 1.0], a, a.get("t", 0.0))
    single = _mode_figures(sol, [1.0, 0.0], a, a.get("t", 0.0))
    if sym:
        zdiff, zcomm = 2.0 * odd["z"], even["z"] / 2.0
    else:
        # Unequal lines: the characteristic impedance matrix Zc =
        # (LC)^(1/2) C^-1, then V/I for a pure differential current.
        Zc = np.real(sqrtm(sol.L @ sol.C)) @ np.linalg.inv(sol.C)
        zdiff = float(Zc[0, 0] + Zc[1, 1] - 2 * Zc[0, 1])
        zcomm = float(Zc[0, 0] + Zc[1, 1] + 2 * Zc[0, 1]) / 4.0
    r.add("zdiff", "Differential impedance Zdiff", zdiff, "resistance", primary=True)
    r.add("zodd", "Odd-mode impedance", odd["z"], "resistance")
    r.add("zeven", "Even-mode impedance", even["z"], "resistance")
    r.add("zcomm", "Common-mode impedance", zcomm, "resistance")
    r.add("z0_single", "Single-ended Z0 (other line at 0 V)", single["z"], "resistance")
    r.add("er_eff_odd", "Effective Er, odd mode", odd["ee"], "ratio")
    r.add("er_eff_even", "Effective Er, even mode", even["ee"], "ratio")
    r.add("delay_odd", "Delay, odd mode", odd["delay"] * INCH, "time", note="per inch")
    r.add("delay_even", "Delay, even mode", even["delay"] * INCH, "time", note="per inch")
    c11, c12 = sol.C[0, 0], sol.C[0, 1]
    l11, l12 = sol.L[0, 0], sol.L[0, 1]
    kb = 0.25 * (-c12 / c11 + l12 / l11)
    r.add("next_coeff", "Backward (NEXT) coefficient Kb", kb, "ratio")
    kf = 0.5 * (-c12 / c11 - l12 / l11) * math.sqrt(c11 * l11)      # s/m
    r.add("fext_coeff", "Forward (FEXT) coefficient", kf * INCH, "time",
          note="per inch: V_fext / V = this x length / rise time")
    f = a["frequency"]
    if f > 0:
        r.add("loss_diff", "Differential loss",
              (odd["ac"] + odd["ad"]) * NP_TO_DB * INCH, "db", note="dB per inch")
        r.add("loss_diff_cond", "  of which conductor", odd["ac"] * NP_TO_DB * INCH, "db")
        r.add("loss_diff_diel", "  of which dielectric", odd["ad"] * NP_TO_DB * INCH, "db")
    r.extra["matrices"] = {
        "C_pF_per_in": (sol.C * INCH * 1e12).round(5).tolist(),
        "L_nH_per_in": (sol.L * INCH * 1e9).round(5).tolist(),
    }
    _solver_note(r, sol)


def _pair_inputs(extra_geo: list[Input], tail=True) -> list[Input]:
    return [_geo("w", "Trace width", 5.0, prefill="trace.width"),
            _geo("s", "Gap between traces (edge to edge)", 5.0, prefill="trace.gap"),
            *extra_geo, _copper(), _etch(), *(_common_tail() if tail else [])]


@calculator(
    "edge_coupled_microstrip", "Edge-coupled microstrip", "pairs",
    "Differential pair on an outer layer over a plane, with optional solder mask.",
    _pair_inputs([_geo("h", "Dielectric height to plane", 4.0, prefill="stackup.dielectric_height"),
                  *_mask()]),
    status="solver")
def edge_coupled_microstrip(a):
    a = at_frequency(a)
    w, s, h, t, er, tand = a["w"], a["s"], a["h"], a["t"], a["er"], a["tand"]
    need(s > 0, "the gap must be greater than zero")

    def build(_f):
        xs = fd.CrossSection(metals=[_metal(-(s + w) / 2, w, h, t, a["etch"], 0),
                                     _metal(+(s + w) / 2, w, h, t, a["etch"], 1)],
                             dielectrics=[fd.Dielectric(0.0, h, er, tand)],
                             ground_bottom=0.0, feature=min(w, s, h))
        if a["mask_t"] > 0:
            xs.coatings.append(fd.Conformal(h, a["mask_t"], a["mask_er"], 0.02))
        return xs
    r = Result(model="Field solver, edge-coupled microstrip (quasi-static)")
    _pair_report(a, _solve(build, a["frequency"], None), r)
    return r


@calculator(
    "edge_coupled_embedded", "Edge-coupled embedded microstrip", "pairs",
    "Differential pair buried under a dielectric with no plane above.",
    _pair_inputs([_geo("h", "Dielectric height to plane", 4.0, prefill="stackup.dielectric_height"),
                  _geo("cover", "Dielectric above trace bottom", 6.0)]),
    status="solver")
def edge_coupled_embedded(a):
    a = at_frequency(a)
    w, s, h, t, er, tand = a["w"], a["s"], a["h"], a["t"], a["er"], a["tand"]

    def build(_f):
        return fd.CrossSection(metals=[_metal(-(s + w) / 2, w, h, t, a["etch"], 0),
                                       _metal(+(s + w) / 2, w, h, t, a["etch"], 1)],
                               dielectrics=[fd.Dielectric(0.0, h + a["cover"], er, tand)],
                               ground_bottom=0.0, feature=min(w, s, h))
    r = Result(model="Field solver, embedded edge-coupled microstrip")
    _pair_report(a, _solve(build, a["frequency"], None), r)
    return r


@calculator(
    "edge_coupled_stripline", "Edge-coupled stripline", "pairs",
    "Differential pair on an inner layer between two planes, centred or offset.",
    _pair_inputs([_geo("h1", "Height: plane below to traces", 5.0, prefill="stackup.h_below"),
                  _geo("h2", "Height: trace top to plane above", 5.0, prefill="stackup.h_above"),
                  Input("er2", "Er above the traces (0 = same)", "ratio", 0.0, min=0.0)]),
    status="solver")
def edge_coupled_stripline(a):
    a = at_frequency(a)
    w, s, h1, h2, t, er, tand = a["w"], a["s"], a["h1"], a["h2"], a["t"], a["er"], a["tand"]
    er2 = a["er2"] or er
    b = h1 + t + h2

    def build(_f):
        return fd.CrossSection(metals=[_metal(-(s + w) / 2, w, h1, t, a["etch"], 0),
                                       _metal(+(s + w) / 2, w, h1, t, a["etch"], 1)],
                               dielectrics=[fd.Dielectric(0.0, h1 + t, er, tand),
                                            fd.Dielectric(h1 + t, b, er2, tand)],
                               ground_bottom=0.0, ground_top=b, feature=min(w, s, h1, h2))
    r = Result(model="Field solver, edge-coupled stripline")
    sol = _solve(build, a["frequency"], None)
    _pair_report(a, sol, r)
    if abs(h1 - h2) < 1e-12 and t == 0 and er2 == er:
        ze, zo = tline.coupled_stripline_cohn(w, s, b, er)
        r.check("Zdiff", r.value("zdiff"), 2 * zo, "Cohn 1955 (exact, zero thickness)")
    return r


@calculator(
    "broadside_coupled_stripline", "Broadside-coupled stripline", "pairs",
    "Pair stacked on two adjacent layers, with planes above and below (shielded) or without.",
    [_geo("w", "Trace width", 5.0, prefill="trace.width"),
     _geo("s", "Vertical gap between the traces", 5.0,
          help_="Dielectric between the two trace layers."),
     _geo("offset", "Horizontal offset", 0.0),
     _geo("h1", "Plane below to lower trace", 8.0),
     _geo("h2", "Upper trace to plane above", 8.0),
     Input("shielded", "Planes above and below", "bool", True),
     _copper(), _etch(), *_common_tail()],
    status="solver")
def broadside_coupled_stripline(a):
    a = at_frequency(a)
    w, s, h1, h2, t, er, tand = a["w"], a["s"], a["h1"], a["h2"], a["t"], a["er"], a["tand"]
    need(s > 0, "the vertical gap must be greater than zero")
    y_low = h1
    y_up = h1 + t + s
    top = y_up + t + h2

    def build(_f):
        return fd.CrossSection(
            metals=[_metal(0.0, w, y_low, t, a["etch"], 0),
                    _metal(a["offset"], w, y_up, t, a["etch"], 1)],
            dielectrics=[fd.Dielectric(0.0, top, er, tand)],
            ground_bottom=0.0 if a["shielded"] else None,
            ground_top=top if a["shielded"] else None,
            feature=min(w, s, t if t > 0 else s))
    r = Result(model="Field solver, broadside-coupled "
               + ("shielded stripline" if a["shielded"] else "lines, no planes"))
    _pair_report(a, _solve(build, a["frequency"], None), r)
    return r


@calculator(
    "coplanar_diff", "Coplanar differential pair", "pairs",
    "Differential pair with ground copper either side, with or without a plane below.",
    _pair_inputs([_geo("g", "Gap to side ground", 6.0),
                  _geo("h", "Dielectric height", 8.0, prefill="stackup.dielectric_height"),
                  _geo("gw", "Side ground width", 40.0),
                  Input("grounded", "Plane below", "bool", True), *_mask()]),
    status="solver")
def coplanar_diff(a):
    a = at_frequency(a)
    w, s, g, h, t, er, tand = a["w"], a["s"], a["g"], a["h"], a["t"], a["er"], a["tand"]
    gw = a["gw"]
    xo = s / 2 + w + g + gw / 2

    def build(_f):
        ms = [_metal(-(s + w) / 2, w, h, t, a["etch"], 0),
              _metal(+(s + w) / 2, w, h, t, a["etch"], 1),
              _metal(-xo, gw, h, t, a["etch"], -1),
              _metal(+xo, gw, h, t, a["etch"], -1)]
        xs = fd.CrossSection(metals=ms, dielectrics=[fd.Dielectric(0.0, h, er, tand)],
                             ground_bottom=0.0 if a["grounded"] else None,
                             feature=min(w, s, g, h))
        if a["mask_t"] > 0:
            xs.coatings.append(fd.Conformal(h, a["mask_t"], a["mask_er"], 0.02))
        return xs
    r = Result(model="Field solver, coplanar differential pair")
    _pair_report(a, _solve(build, a["frequency"], None), r)
    return r


# ------------------------------------------------------- effective Er etc.

@calculator(
    "er_effective", "Effective dielectric constant", "impedance",
    "Er_eff of a microstrip at a frequency, Hammerstad-Jensen with Kirschning-Jansen dispersion.",
    [_geo("w", "Trace width", 6.0), _geo("h", "Dielectric height", 4.0),
     _copper(), Input("er", "Dielectric constant Er", "ratio", 4.2, min=1.0),
     Input("frequency", "Frequency", "frequency", 1.0, "GHz", min=0.0)])
def er_effective(a):
    z, ee = tline.microstrip_hj(a["w"], a["h"], a["t"], a["er"])
    eef = tline.microstrip_dispersion(ee, a["er"], a["w"], a["h"], a["frequency"])
    r = Result(model="Hammerstad-Jensen 1980, Kirschning-Jansen 1982")
    r.add("er_eff", "Effective Er at f", eef, "ratio", primary=True)
    r.add("er_eff_static", "Effective Er at DC", ee, "ratio")
    r.add("velocity", "Phase velocity", fd.C0 / math.sqrt(eef), "velocity")
    r.add("velocity_factor", "Velocity factor", 1 / math.sqrt(eef), "ratio")
    r.accuracy = "Er_eff within 0.2 % static and 0.6 % dispersive inside the models' range."
    return r


@calculator(
    "laminate_er", "Laminate Er(f) and tan d(f)", "impedance",
    "Causal frequency dependence of a laminate from one data-sheet point (Djordjevic-Sarkar).",
    [Input("er", "Er at the data-sheet frequency", "ratio", 4.3, min=1.0),
     Input("tand", "tan d at the data-sheet frequency", "ratio", 0.02, min=0.0),
     Input("f_spec", "Data-sheet frequency", "frequency", 1.0, "GHz", min=1e-6),
     Input("frequency", "Evaluate at", "frequency", 10.0, "GHz", min=1e-6)])
def laminate_er(a):
    er, td = tline.djordjevic_sarkar(a["er"], a["tand"], a["f_spec"], a["frequency"])
    r = Result(model="Wideband Debye (Djordjevic-Sarkar 2001)", status="published")
    r.add("er", "Er at f", er, "ratio", primary=True)
    r.add("tand", "tan d at f", td, "ratio")
    r.accuracy = ("Causal model fitted to one point; typically within 2 % of "
                  "measured FR-4 class laminates from 1 MHz to 20 GHz.")
    r.references = ["Djordjevic, Biljic, Likar-Smiljanic, Sarkar, IEEE Trans. EMC 43(4), 2001"]
    return r


def solve_width_for_z(calc_name: str, args: dict[str, Any], target: float,
                      key_out: str = "z0", key_in: str = "w") -> float:
    """Width for a target impedance: secant on ln(Z) against ln(w).

    Z is close to linear in ln(w) for every structure here, so the secant
    converges in 4-6 solves; it falls back to bisection if it ever
    leaves the bracket.
    """
    from .core import REGISTRY
    calc = REGISTRY[calc_name]

    def z_of(w):
        return calc.fn(dict(args, **{key_in: w})).value(key_out)
    x0 = math.log(args.get(key_in) or 1.5e-4)
    x1 = x0 + 0.3
    f0 = math.log(z_of(math.exp(x0)) / target)
    f1 = math.log(z_of(math.exp(x1)) / target)
    for _ in range(25):
        if abs(f1) < 2e-5:
            return math.exp(x1)
        if f1 == f0:
            break
        x2 = x1 - f1 * (x1 - x0) / (f1 - f0)
        x2 = min(max(x2, x1 - 2.0), x1 + 2.0)
        need(-14.0 < x2 < -1.0, "the target impedance is out of reach for this stackup")
        x0, f0 = x1, f1
        x1 = x2
        f1 = math.log(z_of(math.exp(x1)) / target)
    need(abs(f1) < 1e-3, "no width converges to that impedance")
    return math.exp(x1)


@calculator(
    "width_for_impedance", "Width for a target impedance", "impedance",
    "Trace width that gives a target Z0 or Zdiff, for any structure, solved with the field solver.",
    [Input("structure", "Structure", "choice", "microstrip",
           choices=[("microstrip", "Microstrip"), ("stripline", "Stripline"),
                    ("embedded_microstrip", "Embedded microstrip"),
                    ("coplanar", "Coplanar waveguide"),
                    ("edge_coupled_microstrip", "Edge-coupled microstrip (Zdiff)"),
                    ("edge_coupled_stripline", "Edge-coupled stripline (Zdiff)")]),
     Input("target", "Target impedance", "resistance", 50.0, "ohm", min=1.0),
     _geo("s", "Pair gap / CPW gap", 5.0),
     _geo("h", "Height to plane (microstrip, CPW)", 4.0, prefill="stackup.dielectric_height"),
     _geo("h1", "Stripline: plane below to trace", 5.0, prefill="stackup.h_below"),
     _geo("h2", "Stripline: trace top to plane above", 5.0, prefill="stackup.h_above"),
     _copper(), Input("er", "Dielectric constant Er", "ratio", 4.2, min=1.0, prefill="stackup.er")],
    status="solver")
def width_for_impedance(a):
    st = a["structure"]
    from .core import REGISTRY
    calc = REGISTRY[st]
    base = calc.parse({})
    base.update({"t": a["t"], "er": a["er"], "frequency": 0.0, "roughness": "none",
                 "method": "solver"})
    for k in ("h", "h1", "h2"):
        if k in base:
            base[k] = a[k]
    if "s" in base:
        base["s"] = a["s"]
    if "g" in base:
        base["g"] = a["s"]
    base["w"] = 6 * MIL
    key = "zdiff" if st.startswith("edge_coupled") else "z0"
    w = solve_width_for_z(st, base, a["target"], key_out=key)
    final = calc.fn(dict(base, w=w))
    r = Result(model=f"Field solver, {calc.title}, bisection on width", status="solver")
    r.add("w", "Trace width", w, "length", primary=True)
    r.add("achieved", "Impedance achieved", final.value(key), "resistance")
    r.accuracy = "Width within 0.05 % of the solver's answer; solver within 0.5 %."
    return r
