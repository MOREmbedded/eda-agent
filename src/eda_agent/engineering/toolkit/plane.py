# SPDX-License-Identifier: Apache-2.0
"""Plane-pair and PDN calculators.

``cavity_impedance`` is the rectangular plane-pair modal sum (Lei,
Techentin, Gilbert, "High-frequency characterization of power/ground-
plane structures", IEEE Trans. MTT 47(5), 1999; the same form as
Hayes/Novak), with each port's finite size entering through sinc
factors and the loss through a complex wavenumber built from the
dielectric loss tangent and the copper's skin depth. It is exact for
rectangular planes as the mode count grows; 40 x 40 modes is converged
to well under 1 % below the 10th resonance. Shared with the board-level
PDN solver (eda_agent.pdn), which uses it to validate its FDFD.
"""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np

from .. import pcb_calc
from .core import INCH, Input, Result, calculator, need
from .fdsolver import C0, EPS0, MU0

SIGMA_CU = 5.8e7


def cavity_impedance(a_m: float, b_m: float, d_m: float, er: float, tand: float,
                     freqs_hz: Sequence[float], ports: Sequence[tuple],
                     sigma: float = SIGMA_CU, m_max: int = 40,
                     n_max: int = 40) -> np.ndarray:
    """Port impedance matrix of a rectangular plane pair, [nf, np, np].

    ``ports`` are (x_m, y_m, wx_m, wy_m): centre and size of each port.
    """
    f = np.asarray(freqs_hz, dtype=float)
    P = len(ports)
    m = np.arange(m_max + 1)
    n = np.arange(n_max + 1)
    km = m * math.pi / a_m
    kn = n * math.pi / b_m
    chi_m = np.where(m == 0, 1.0, 2.0)
    chi_n = np.where(n == 0, 1.0, 2.0)

    def sinc(x):
        return np.sinc(x / math.pi)

    # Port shape factors: [P, m] and [P, n]
    fx = np.array([np.cos(km * x) * sinc(km * wx / 2.0) for (x, y, wx, wy) in ports])
    fy = np.array([np.cos(kn * y) * sinc(kn * wy / 2.0) for (x, y, wx, wy) in ports])
    kmn2 = (km[:, None] ** 2 + kn[None, :] ** 2)
    weight = chi_m[:, None] * chi_n[None, :]
    out = np.zeros((len(f), P, P), dtype=complex)
    eps = EPS0 * er
    for i, fi in enumerate(f):
        w = 2.0 * math.pi * fi
        delta = math.sqrt(2.0 / (w * MU0 * sigma)) if fi > 0 else 0.0
        k2 = w * w * MU0 * eps * (1.0 - 1j * (tand + delta / d_m))
        denom = kmn2 - k2
        G = weight / denom
        pref = 1j * w * MU0 * d_m / (a_m * b_m)
        for p in range(P):
            for q in range(p, P):
                val = pref * np.sum(G * np.outer(fx[p] * fx[q], fy[p] * fy[q]))
                out[i, p, q] = val
                out[i, q, p] = val
    return out


def resonances(a_m: float, b_m: float, er: float, count: int = 8) -> list[tuple[int, int, float]]:
    v = C0 / math.sqrt(er)
    modes = []
    for m in range(0, 12):
        for n in range(0, 12):
            if m == n == 0:
                continue
            modes.append((m, n, v / 2.0 * math.sqrt((m / a_m) ** 2 + (n / b_m) ** 2)))
    modes.sort(key=lambda t: t[2])
    return modes[:count]


@calculator(
    "plane_pair", "Plane pair: capacitance, resonances, impedance", "plane",
    "Capacitance of a power/ground plane pair, its cavity resonances, and the "
    "impedance seen at a port across frequency.",
    [Input("a", "Plane length", "length", 100.0, "mm", min=1.0, prefill="board.width"),
     Input("b", "Plane width", "length", 80.0, "mm", min=1.0, prefill="board.length"),
     Input("d", "Dielectric between the planes", "length", 4.0, "mil", min=0.1,
           prefill="stackup.plane_gap"),
     Input("er", "Dielectric constant Er", "ratio", 4.2, min=1.0, prefill="stackup.er"),
     Input("tand", "Loss tangent", "ratio", 0.02, min=0.0),
     Input("px", "Port position x", "length", 25.0, "mm", min=0.0),
     Input("py", "Port position y", "length", 20.0, "mm", min=0.0),
     Input("port_size", "Port size (via/pad footprint)", "length", 1.0, "mm", min=0.01),
     Input("f_max", "Sweep up to", "frequency", 3.0, "GHz", min=0.01)])
def plane_pair(a):
    A, B, d, er = a["a"], a["b"], a["d"], a["er"]
    need(a["px"] <= A and a["py"] <= B, "the port must be on the plane")
    c = EPS0 * er * A * B / d
    r = Result(model="Parallel-plate capacitance; cavity modal sum (Lei et al. 1999)",
               status="exact")
    r.add("c", "Plane capacitance", c, "capacitance", primary=True)
    r.add("c_per_area", "Capacitance per square inch", EPS0 * er / d * INCH ** 2,
          "capacitance")
    res = resonances(A, B, er, 6)
    for (m, n, fr) in res[:4]:
        r.add(f"f_{m}{n}", f"Resonance TM{m}{n}", fr, "frequency")
    fs = np.geomspace(1e6, a["f_max"], 240)
    port = (a["px"], a["py"], a["port_size"], a["port_size"])
    z = np.abs(cavity_impedance(A, B, d, er, a["tand"], fs, [port])[:, 0, 0])
    r.extra["curve"] = {"x": fs.tolist(), "y": z.tolist(), "x_kind": "frequency",
                        "y_kind": "resistance", "log_x": True, "log_y": True,
                        "label": "|Z| at the port"}
    i_min = int(np.argmin(z[: max(1, int(np.searchsorted(fs, res[0][2])))]))
    r.add("z_min", "Lowest |Z| below the first resonance", float(z[i_min]), "resistance")
    r.accuracy = ("Capacitance exact (fringing < 1 % for planes much larger than "
                  "their spacing). Impedance: exact for rectangular planes, 40x40 "
                  "modes; no decaps or VRM (use the PDN analysis for a real board).")
    r.references = ["Lei, Techentin, Gilbert, IEEE Trans. MTT 47(5), 1999",
                    "I. Novak, Power Distribution Network Design Methodologies, 2008"]
    return r


@calculator(
    "pdn_target", "PDN target impedance", "plane",
    "Target impedance of a supply from its voltage, ripple budget and transient current.",
    [Input("voltage", "Supply voltage", "voltage", 1.0, "V", min=0.0),
     Input("ripple", "Allowed ripple", "percent", 3.0, "%", min=0.01, max=50),
     Input("current", "Transient current step", "current", 2.0, "A", min=1e-6),
     Input("t_rise", "Current edge rise time", "time", 1.0, "ns", min=1e-6)])
def pdn_target(a):
    d = pcb_calc.pdn_target_impedance(a["voltage"], a["ripple"], a["current"])
    need(d.get("ok", False), d.get("reason", "cannot compute"))
    z = a["voltage"] * a["ripple"] / 100.0 / a["current"]
    r = Result(model="Z_target = V * ripple / I_step (Smith, Novak)", status="published")
    r.add("z_target", "Target impedance", z, "resistance", primary=True)
    r.add("f_max", "Hold it up to (0.35 / t_rise)", 0.35 / a["t_rise"], "frequency")
    r.accuracy = ("A flat-target first pass. Real loads are not a single step; "
                  "where the current spectrum is known, a frequency-dependent "
                  "target is better (Novak, 'Frequency-dependent target impedance', "
                  "DesignCon 2017).")
    return r
