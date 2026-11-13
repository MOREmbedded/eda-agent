# SPDX-License-Identifier: Apache-2.0
"""Laminates and copper for the line models.

A laminate data sheet gives Er and the loss tangent at one frequency.
Holding them constant at every frequency is not causal, and a channel
model built that way rings in the time domain. The Djordjevic-Sarkar
wideband Debye model is the causal extension most field solvers use: a
continuous spread of relaxation frequencies between ``f_lo`` and
``f_hi`` with a constant weight, fitted to pass through the data-sheet
point. (A. R. Djordjevic et al., IEEE Trans. EMC 43(4), 2001.)

Copper loss rises above the smooth-conductor value when the skin depth
falls to the size of the foil's surface texture. The Huray "snowball"
model treats the texture as copper spheres of radius ``a`` covering the
flat area with ratio ``sr`` = N 4 pi a^2 / A_flat (P. G. Huray et al.,
DesignCon 2010); Hammerstad-Bekkadal and Groisse use the RMS roughness.
"""
from __future__ import annotations

import cmath
import math
from dataclasses import dataclass
from typing import Any

from ..engineering.toolkit import tline

RHO_CU = tline.RHO_CU
F_LO = 1e3
F_HI = 1e12


@dataclass(frozen=True)
class Laminate:
    """A dielectric: Er and tan d at ``f_spec`` (Hz)."""

    er: float
    tand: float = 0.0
    f_spec: float = 1e9
    name: str = ""

    def eps(self, f: float) -> complex:
        """Complex relative permittivity Er' - j Er'' at ``f`` (Hz)."""
        return eps_ds(self.er, self.tand, self.f_spec, f)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "er": self.er, "tand": self.tand, "f_spec_hz": self.f_spec}


def eps_ds(er: float, tand: float, f_spec: float, f: float) -> complex:
    """Djordjevic-Sarkar complex Er through (er, tand) at ``f_spec``.

    Er(f) = Er_inf + dEr / ln(f_hi/f_lo) * ln((f_hi + j f) / (f_lo + j f)).
    At ``f_spec`` it gives exactly ``er`` and ``tand``. With no loss it is
    a constant (a lossless dielectric is causal as it is).
    """
    if tand <= 0 or f_spec <= 0:
        return complex(er, 0.0)
    span = math.log(F_HI / F_LO)

    def lg(fr: float) -> complex:
        return cmath.log((F_HI + 1j * fr) / (F_LO + 1j * fr))

    l0 = lg(f_spec)
    d = -er * tand * span / l0.imag
    einf = er - d * l0.real / span
    return einf + d * lg(max(f, 0.0)) / span


@dataclass(frozen=True)
class Copper:
    """Conductor: resistivity and surface roughness.

    ``roughness`` is none, huray, hammerstad or groisse. Huray takes the
    sphere radius ``radius`` (m) and surface ratio ``sr``; the other two
    take the RMS roughness ``rq`` (m).
    """

    rho: float = RHO_CU
    roughness: str = "none"
    rq: float = 0.0
    radius: float = 0.5e-6
    sr: float = 0.0

    @property
    def sigma(self) -> float:
        return 1.0 / self.rho

    def rough(self, f: float) -> float:
        """Loss factor of the rough surface over a smooth one at ``f``."""
        if self.roughness == "none" or f <= 0:
            return 1.0
        d = tline.skin_depth(f, self.rho)
        if self.roughness == "huray":
            if self.sr <= 0:
                return 1.0
            a = self.radius
            return 1.0 + 1.5 * self.sr / (1.0 + d / a + d * d / (2.0 * a * a))
        if self.roughness == "hammerstad":
            return 1.0 + 2.0 / math.pi * math.atan(1.4 * (self.rq / d) ** 2) if self.rq > 0 else 1.0
        if self.roughness == "groisse":
            return 1.0 + math.exp(-((d / (2.0 * self.rq)) ** 1.6)) if self.rq > 0 else 1.0
        raise ValueError(f"unknown roughness model {self.roughness!r}; "
                         "use none, huray, hammerstad or groisse")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"rho_ohm_m": self.rho, "roughness": self.roughness}
        if self.roughness == "huray":
            out.update(radius_m=self.radius, surface_ratio=self.sr)
        elif self.roughness in ("hammerstad", "groisse"):
            out["rq_m"] = self.rq
        return out


#: Generic laminates. Values are typical data-sheet figures; a fab's own
#: numbers for the chosen glass style and resin content are better.
LAMINATES: dict[str, Laminate] = {
    "fr4": Laminate(4.3, 0.02, 1e9, "Generic FR-4"),
    "fr4-mid-loss": Laminate(3.9, 0.01, 1e9, "Mid-loss FR-4 class"),
    "low-loss": Laminate(3.6, 0.005, 1e9, "Low-loss class"),
    "ultra-low-loss": Laminate(3.4, 0.002, 1e9, "Ultra-low-loss class"),
    "ro4350b": Laminate(3.66, 0.0037, 10e9, "RO4350B (design Dk 3.66, Df at 10 GHz)"),
}

#: Copper foil classes for the Huray model: (sphere radius m, surface
#: ratio). Typical published fits; a fab's measured values are better.
FOILS: dict[str, Copper] = {
    "smooth": Copper(),
    "std": Copper(roughness="huray", radius=0.5e-6, sr=2.9),
    "rtf": Copper(roughness="huray", radius=0.5e-6, sr=1.6),
    "vlp": Copper(roughness="huray", radius=0.4e-6, sr=0.9),
    "hvlp": Copper(roughness="huray", radius=0.3e-6, sr=0.5),
}


def laminate(value: Any, er: float | None = None, tand: float | None = None,
             f_spec: float | None = None) -> Laminate:
    """A laminate from a preset name, a dict or explicit numbers."""
    if isinstance(value, Laminate):
        return value
    if isinstance(value, str) and value:
        key = value.strip().lower()
        if key not in LAMINATES:
            raise ValueError(f"unknown laminate {value!r}; presets: {', '.join(LAMINATES)}")
        base = LAMINATES[key]
        return Laminate(er if er is not None else base.er,
                        tand if tand is not None else base.tand,
                        f_spec if f_spec is not None else base.f_spec, base.name)
    if isinstance(value, dict):
        return Laminate(float(value.get("er", 4.3)), float(value.get("tand", 0.0)),
                        float(value.get("f_spec_hz", value.get("f_spec", 1e9))),
                        str(value.get("name", "")))
    return Laminate(er if er is not None else 4.3, tand if tand is not None else 0.0,
                    f_spec if f_spec is not None else 1e9)


def copper(value: Any) -> Copper:
    """Copper from a foil preset name or a dict."""
    if isinstance(value, Copper):
        return value
    if value is None or value == "":
        return Copper()
    if isinstance(value, str):
        key = value.strip().lower()
        if key not in FOILS:
            raise ValueError(f"unknown copper foil {value!r}; presets: {', '.join(FOILS)}")
        return FOILS[key]
    if isinstance(value, dict):
        c = Copper(float(value.get("rho_ohm_m", RHO_CU)), str(value.get("roughness", "none")),
                   float(value.get("rq_m", 0.0)), float(value.get("radius_m", 0.5e-6)),
                   float(value.get("surface_ratio", 0.0)))
        c.rough(1e9)   # validates the model name
        return c
    raise ValueError("copper is a foil preset name or a dict")
