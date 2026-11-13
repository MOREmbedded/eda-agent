# SPDX-License-Identifier: Apache-2.0
"""A signal via as a two-port, from its geometry and the stack.

The barrel between the planes it passes through is a coaxial line: the
barrel inside the antipad (clearance hole) of each plane, with
impedance (60 / sqrt Er) ln(D_antipad / d_barrel) and the dielectric's
speed. The current enters at one layer and leaves at another, so the
barrel between them is in series with the path, and any barrel beyond
either end (to the board surface, or to the end of a blind via) is an
open stub hanging off it. That stub is what puts a notch in the
insertion loss at c / (4 l sqrt Er), the reason vias are back-drilled.

Each pad is a small capacitor to the planes beside it. Johnson and
Graham's estimate of a via's pad capacitance (High-Speed Digital
Design, 1993, ch. 7),

    C = 1.41 Er T D1 / (D2 - D1)  pF  (T, D1, D2 in inches),

is used per pad with T the distance to the nearest plane, D1 the pad and
D2 the antipad diameter.

The return current has to cross from one reference plane to the other.
When the two references differ, the loop it takes through the nearest
return via (a stitching via, or a ground pin) is a series inductance,
(mu0 h / pi) acosh(s / 2r) for barrels a distance s apart over the
height h between the planes (the via-pair loop formula of the PDN
module). With no return via within reach, it is flagged rather than
guessed.

This is an engineering model, good to a few GHz for a typical via and
to the first stub resonance in shape. Above that, or for a via field
that matters, model the via in 3D (the em-3d job, openEMS) and cascade
its S-parameters instead.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import numpy as np

from ..pdn import physics as pdnp
from ..units import MM_PER_MIL
from .materials import RHO_CU

MIL = MM_PER_MIL / 1000.0
C_LIGHT = 299792458.0
MU0 = 4e-7 * math.pi
#: Antipad when the board states none: pad plus 2 x 8 mil clearance.
DEFAULT_CLEARANCE_MILS = 8.0


@dataclass
class ViaGeometry:
    """Lengths in mils, heights measured upwards from the board bottom."""

    drill_mils: float
    pad_mils: float
    antipad_mils: float
    z_entry: float                  # height of the layer the signal arrives on
    z_exit: float                   # height of the layer it leaves on
    z_low: float                    # lowest end of the barrel
    z_high: float                   # highest end of the barrel
    er: float = 4.3
    tand: float = 0.02
    #: Heights of the planes the barrel passes (for the pad capacitance).
    planes: Sequence[float] = ()
    #: Return loop: height between the two references, distance to the
    #: nearest return via and its drill; 0 height = same reference.
    return_height_mils: float = 0.0
    return_spacing_mils: float = 0.0
    return_drill_mils: float = 0.0
    backdrill_to: Optional[float] = None   # stub cut back to this height
    notes: list[str] = field(default_factory=list)

    def plated_ends(self) -> tuple[float, float]:
        lo, hi = self.z_low, self.z_high
        if self.backdrill_to is not None:
            if self.backdrill_to <= min(self.z_entry, self.z_exit):
                lo = max(lo, self.backdrill_to)
            else:
                hi = min(hi, self.backdrill_to)
        return lo, hi


def barrel_impedance(g: ViaGeometry) -> float:
    ratio = max(g.antipad_mils / max(g.drill_mils, 1e-6), 1.05)
    return 60.0 / math.sqrt(g.er) * math.log(ratio)


def pad_capacitance(g: ViaGeometry, z: float) -> float:
    """F, one pad at height ``z``."""
    if not g.planes:
        return 0.0
    t_mils = min(abs(z - p) for p in g.planes)
    if t_mils <= 0:
        return 0.0
    d1, d2 = g.pad_mils, max(g.antipad_mils, g.pad_mils * 1.05)
    t_in = t_mils / 1000.0
    return 1.41 * g.er * t_in * (d1 / 1000.0) / ((d2 - d1) / 1000.0) * 1e-12


def _coax_abcd(z0: float, length_m: float, er: float, tand: float, f: float,
               r_per_m: float) -> np.ndarray:
    w = 2 * math.pi * f
    l = z0 * math.sqrt(er) / C_LIGHT
    c = math.sqrt(er) / (C_LIGHT * z0)
    zs = r_per_m * math.sqrt(max(f, 1.0)) + 1j * w * l
    ys = w * c * tand + 1j * w * c
    gam = np.sqrt(zs * ys)
    zc = np.sqrt(zs / ys)
    gl = gam * length_m
    return np.array([[np.cosh(gl), zc * np.sinh(gl)], [np.sinh(gl) / zc, np.cosh(gl)]])


def two_port(g: ViaGeometry, freqs: Sequence[float]) -> tuple[np.ndarray, dict[str, Any]]:
    """(nf, 2, 2) chain matrices of the via, and what went into them."""
    z0 = barrel_impedance(g)
    lo, hi = g.plated_ends()
    z_in, z_out = g.z_entry, g.z_exit
    through = abs(z_out - z_in) * MIL
    up = z_out > z_in
    # stubs: barrel beyond the exit (away from the entry) and beyond the entry
    stub_exit = max((hi - z_out) if up else (z_out - lo), 0.0) * MIL
    stub_entry = max((z_in - lo) if up else (hi - z_in), 0.0) * MIL
    drill_r = max(g.drill_mils, 1e-3) * MIL / 2
    # Skin resistance per metre and per sqrt(Hz): barrel plus return wall.
    rs_coef = math.sqrt(math.pi * MU0 * RHO_CU)
    r_per_m = rs_coef / (2 * math.pi) * (1 / drill_r + 2 / (g.antipad_mils * MIL))
    c_in = pad_capacitance(g, z_in)
    c_out = pad_capacitance(g, z_out)
    l_ret = 0.0
    if g.return_height_mils > 0:
        if g.return_spacing_mils > 0:
            l_ret = pdnp.via_pair_loop_inductance(g.return_height_mils, g.return_spacing_mils,
                                                  max(g.return_drill_mils, g.drill_mils) / 2)
    mats = np.empty((len(freqs), 2, 2), dtype=complex)
    for k, f in enumerate(freqs):
        w = 2 * math.pi * f
        m = np.eye(2, dtype=complex)
        if stub_entry > 0:
            s = _coax_abcd(z0, stub_entry, g.er, g.tand, f, r_per_m)
            m = m @ np.array([[1, 0], [s[1, 0] / s[0, 0], 1]])
        m = m @ np.array([[1, 0], [1j * w * c_in, 1]])
        if through > 0:
            m = m @ _coax_abcd(z0, through, g.er, g.tand, f, r_per_m)
        if l_ret > 0:
            m = m @ np.array([[1, 1j * w * l_ret], [0, 1]])
        m = m @ np.array([[1, 0], [1j * w * c_out, 1]])
        if stub_exit > 0:
            s = _coax_abcd(z0, stub_exit, g.er, g.tand, f, r_per_m)
            m = m @ np.array([[1, 0], [s[1, 0] / s[0, 0], 1]])
        mats[k] = m
    stub = max(stub_entry, stub_exit)
    info = {
        "z_barrel_ohms": z0, "through_mils": through / MIL,
        "stub_mils": stub / MIL, "stub_entry_mils": stub_entry / MIL,
        "stub_exit_mils": stub_exit / MIL,
        "stub_resonance_hz": (C_LIGHT / (4 * stub * math.sqrt(g.er)) if stub > 0 else None),
        "pad_c_f": [c_in, c_out], "return_l_h": l_ret,
        "delay_s": through * math.sqrt(g.er) / C_LIGHT,
        "notes": list(g.notes),
    }
    return mats, info


__all__ = ["ViaGeometry", "barrel_impedance", "pad_capacitance", "two_port",
           "DEFAULT_CLEARANCE_MILS", "MIL"]
