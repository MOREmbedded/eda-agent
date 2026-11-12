# SPDX-License-Identifier: Apache-2.0
"""Decoupling capacitors as the PDN sees them: value, ESR, ESL, and the
inductance of their mounting on this board.

A capacitor is a series R-L-C: Z = ESR + j w ESL + 1 / (j w C). Values
come from the part's comment ("100nF", "4u7 16V X7R"), the package from
the footprint name (imperial "0402", IPC "CAPC1005"). When the data sheet
or a vendor model is at hand, pass the real numbers, or a Touchstone file
of the part, and they replace the defaults.

Defaults (a capacitor's own ESL is mostly its package; ESR its value):

=========  ==========  ====================================
package    ESL (nH)    source
=========  ==========  ====================================
0201       0.20        typical MLCC data (Murata, TDK, KEMET
0402       0.35        published curves, mounted-free value)
0603       0.50
0805       0.60
1206       0.90
1210       1.00
1812/2220  1.20
=========  ==========  ====================================

MLCC capacitance falls with DC bias and temperature (X5R/X7R lose 30-80 %
in small cases at rated voltage). The defaults use the nominal value and
say so; pass ``c_farads`` with the derated value from the maker's tool for
a sign-off number.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from . import physics as ph

PACKAGE_ESL_NH = {"0201": 0.20, "0402": 0.35, "0603": 0.50, "0805": 0.60,
                  "1206": 0.90, "1210": 1.00, "1812": 1.20, "2220": 1.20,
                  "2512": 1.30}
METRIC_TO_IMPERIAL = {"0603": "0201", "1005": "0402", "1608": "0603", "2012": "0805",
                      "3216": "1206", "3225": "1210", "4532": "1812", "5750": "2220"}
_BULK_WORDS = ("TANT", "ELEC", "CPOL", "POLY", "CAPAE", "CAPPM", "CASE_", "SMD_CASE",
               "RADIAL", "CP_")

_MULT = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3}


def parse_capacitance(text: str) -> Optional[float]:
    """Farads from a value string: 100nF, 0.1uF, 4u7, 4.7µ, 100n, 1uF/16V,
    10000pF, 1F, 100NF. None when no capacitance is found (100nH is not)."""
    if not text:
        return None
    s = str(text).strip()
    m = re.search(r"(?<![\w.])(\d+)([pnuµμ])(\d+)(?![\w.])", s, re.I)
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * _MULT[m.group(2).lower()]
    m = re.search(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*(?:(mF)|([pnuµμ])F?|(F))(?![a-zA-Z])", s, re.I)
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    if m.group(2):
        return v * 1e-3
    if m.group(3):
        return v * _MULT[m.group(3).lower()]
    return v


def package_of(footprint: str) -> str:
    """Imperial case code from a footprint name, or ''."""
    f = (footprint or "").upper()
    m = re.search(r"CAPC(\d{4})", f)
    if m and m.group(1) in METRIC_TO_IMPERIAL:
        return METRIC_TO_IMPERIAL[m.group(1)]
    m = re.search(r"(?<!\d)(0201|0402|0603|0805|1206|1210|1812|2220|2512)(?!\d)", f)
    if m:
        return m.group(1)
    m = re.search(r"(?<!\d)(1005|1608|2012|3216|3225|4532|5750)(?!\d)", f)
    if m:
        return METRIC_TO_IMPERIAL[m.group(1)]
    return ""


def is_bulk(footprint: str, comment: str = "") -> bool:
    f = (footprint or "").upper() + " " + (comment or "").upper()
    return any(w in f for w in _BULK_WORDS)


def default_esr(c: float, bulk: bool = False) -> float:
    """Typical ESR at the series resonance for an MLCC of this value
    (a fit to published X7R/X5R curves, within about a factor of two), or
    a polymer/tantalum bulk part."""
    if bulk:
        return 0.05 if c >= 100e-6 else 0.1
    if c <= 1e-9:
        return 0.25
    if c <= 10e-9:
        return 0.08
    if c <= 100e-9:
        return 0.03
    if c <= 1e-6:
        return 0.012
    if c <= 10e-6:
        return 0.005
    return 0.003


def default_esl(package: str, bulk: bool = False) -> float:
    if bulk:
        return 2.0e-9
    return PACKAGE_ESL_NH.get(package, 0.5) * 1e-9


@dataclass
class Decap:
    ref: str
    c: float
    esr: float
    esl: float
    package: str = ""
    mount_l: float = 0.0          # board mounting inductance, H
    x: float = 0.0
    y: float = 0.0
    layer: str = ""
    source: str = "defaults"      # defaults | user | touchstone
    notes: list[str] = field(default_factory=list)
    touchstone: Optional[tuple[np.ndarray, np.ndarray]] = None   # f, Z
    mount_detail: dict = field(default_factory=dict)

    def z_part(self, f: np.ndarray) -> np.ndarray:
        f = np.asarray(f, dtype=float)
        if self.touchstone is not None:
            ft, zt = self.touchstone
            re_ = np.interp(np.log(f), np.log(ft), zt.real)
            im_ = np.interp(np.log(f), np.log(ft), zt.imag)
            return re_ + 1j * im_
        w = 2 * np.pi * f
        return self.esr + 1j * w * self.esl + 1.0 / (1j * w * self.c)

    def z(self, f: np.ndarray) -> np.ndarray:
        return self.z_part(f) + 1j * 2 * np.pi * np.asarray(f) * self.mount_l

    @property
    def srf_hz(self) -> float:
        return 1.0 / (2 * math.pi * math.sqrt((self.esl + self.mount_l) * self.c))

    def to_dict(self) -> dict:
        return {"ref": self.ref, "c_uf": round(self.c * 1e6, 6), "esr_mohm": round(self.esr * 1e3, 3),
                "esl_nh": round(self.esl * 1e9, 4), "mount_nh": round(self.mount_l * 1e9, 4),
                "package": self.package, "srf_mhz": round(self.srf_hz / 1e6, 4),
                "at": [round(self.x, 1), round(self.y, 1)], "layer": self.layer,
                "model": self.source, "mount": self.mount_detail,
                **({"notes": self.notes} if self.notes else {})}


def touchstone_impedance(path: str, config: str = "shunt", z0: float = 50.0):
    """(f, Z) of a two-port capacitor measurement. ``shunt``: the part
    from the line to ground (shunt-thru), Z = Z0 S21 / (2 (1 - S21));
    ``series``: in series with the line, Z = 2 Z0 (1 - S21) / S21; a
    one-port file gives Z = Z0 (1 + S11) / (1 - S11)."""
    from ..emc.touchstone import read_touchstone
    data = read_touchstone(path)
    f = np.asarray(data["freq_hz"], dtype=float)
    s = np.asarray(data["s"], dtype=complex)
    z0 = float(data.get("z0", z0) or z0)
    if s.ndim == 1 or s.shape[-1] == 1:
        s11 = s.reshape(len(f), -1)[:, 0]
        return f, z0 * (1 + s11) / (1 - s11)
    s21 = s[:, 1, 0]
    if config == "series":
        return f, 2 * z0 * (1 - s21) / s21
    return f, z0 * s21 / (2 * (1 - s21))


def mounting_inductance(*, cap_z_mils: float, plane_z_mils: float, cavity_mils: float,
                        via_spacing_mils: float, via_radius_mils: float,
                        n_vias_each: int, trace_len_mils: float, trace_w_mils: float,
                        ref_h_mils: float, cell_mils: float) -> dict:
    """Inductance between the capacitor's pads and the plane cavity.

    * vias: the loop of the power and ground vias from the capacitor's
      layer down to the nearer plane of the pair, (mu0 h / pi)
      acosh(s / 2r), divided among parallel via pairs;
    * fan-out traces: the pad-to-via traces over their reference plane,
      mu0 h l / w per trace, with w widened by 2h for fringing (Wheeler);
    * the via through the cavity: the part a grid node of the cavity
      model does not hold, mu0 d / (2 pi) ln(r_eq / r) (Peaceman).
    """
    h = abs(cap_z_mils - plane_z_mils)
    lv = ph.via_pair_loop_inductance(h, via_spacing_mils, via_radius_mils) / max(1, n_vias_each)
    lt = 0.0
    if trace_len_mils > 0 and trace_w_mils > 0:
        lt = ph.MU0 * ref_h_mils * trace_len_mils * ph.MIL / (trace_w_mils + 2 * ref_h_mils)
    lc = ph.cavity_port_inductance(cavity_mils, via_radius_mils, cell_mils) / max(1, n_vias_each)
    return {"vias_nh": round(lv * 1e9, 4), "traces_nh": round(lt * 1e9, 4),
            "cavity_nh": round(lc * 1e9, 4), "total_nh": round((lv + lt + lc) * 1e9, 4),
            "_total": lv + lt + lc}
