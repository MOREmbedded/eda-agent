# SPDX-License-Identifier: Apache-2.0
"""Transmission-line models: closed forms, dielectric and copper loss.

The closed forms are the published ones, implemented from the papers.
They are used two ways: as a fast answer, and as an independent
cross-check on the field solver in fdsolver.py. Each function names its
source and the accuracy its authors claim.

Sources:

* E. Hammerstad, O. Jensen, "Accurate models for microstrip
  computer-aided design", IEEE MTT-S Digest, 1980. Static microstrip
  Z0 and Er_eff, with the strip-thickness correction. Stated accuracy:
  Z0 in air within 0.01 % for w/h <= 1 and 0.03 % for w/h <= 1000;
  Er_eff within 0.2 % for Er <= 128 and 0.01 <= w/h <= 100.
* M. Kirschning, R. H. Jansen, "Accurate model for effective dielectric
  constant of microstrip with validity up to millimetre-wave
  frequencies", Electronics Letters 18(6), 1982. Dispersion, within
  0.6 % for 0.1 <= w/h <= 100, 1 <= Er <= 20, f*h <= 25 GHz*mm.
* S. B. Cohn, "Characteristic impedance of the shielded-strip
  transmission line", IRE Trans. MTT, 1954, and "Shielded coupled-strip
  transmission line", 1955. Zero-thickness stripline, single and edge
  coupled: exact (conformal mapping).
* H. A. Wheeler, "Transmission-line properties of a strip line between
  parallel planes", IEEE Trans. MTT 26(11), 1978. Finite thickness
  stripline, within 0.5 % for w/(b-t) < 10.
* R. N. Simons, "Coplanar Waveguide Circuits, Components, and Systems",
  Wiley 2001, ch. 2. CPW and conductor-backed CPW by conformal mapping.
* A. R. Djordjevic, R. M. Biljic, V. D. Likar-Smiljanic, T. K. Sarkar,
  "Wideband frequency-domain characterization of FR-4 and time-domain
  causality", IEEE Trans. EMC 43(4), 2001. Causal Er(f), tan d(f).
* E. Hammerstad, O. Bekkadal, "Microstrip Handbook", 1975: roughness
  factor from RMS roughness. P. G. Huray et al., "Impact of copper
  surface texture on loss: a model that works", DesignCon 2010: the
  snowball model.
"""
from __future__ import annotations

import cmath
import math

from scipy.special import ellipk

from .fdsolver import C0, EPS0, MU0

ETA0 = MU0 * C0          # impedance of free space, ~376.73 ohm
RHO_CU = 1.724e-8        # IACS annealed copper at 20 C, ohm*m
ALPHA_CU = 0.00393       # 1/K


def k_ratio(k: float) -> float:
    """K(k)/K(k'), the complete elliptic integral ratio (scipy uses m=k^2)."""
    k = min(max(k, 1e-300), 1.0 - 1e-16)
    kp = math.sqrt(1.0 - k * k)
    return float(ellipk(k * k) / ellipk(kp * kp))


# ------------------------------------------------------------ microstrip

def _hj_z01(u: float) -> float:
    f = 6.0 + (2.0 * math.pi - 6.0) * math.exp(-((30.666 / u) ** 0.7528))
    return ETA0 / (2.0 * math.pi) * math.log(f / u + math.sqrt(1.0 + (2.0 / u) ** 2))


def _hj_eeff(u: float, er: float) -> float:
    a = (1.0 + math.log((u ** 4 + (u / 52.0) ** 2) / (u ** 4 + 0.432)) / 49.0
         + math.log(1.0 + (u / 18.1) ** 3) / 18.7)
    b = 0.564 * ((er - 0.9) / (er + 3.0)) ** 0.053
    return (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * (1.0 + 10.0 / u) ** (-a * b)


def microstrip_hj(w: float, h: float, t: float, er: float) -> tuple[float, float]:
    """Static microstrip (Z0, Er_eff), Hammerstad-Jensen 1980 with thickness."""
    u = w / h
    if t > 0:
        tn = t / h
        coth = 1.0 / math.tanh(math.sqrt(6.517 * u))
        du1 = tn / math.pi * math.log(1.0 + 4.0 * math.e / (tn * coth * coth))
        dur = 0.5 * (1.0 + 1.0 / math.cosh(math.sqrt(max(er - 1.0, 0.0)))) * du1
        u1, ur = u + du1, u + dur
    else:
        u1 = ur = u
    ee_r = _hj_eeff(ur, er)
    z = _hj_z01(ur) / math.sqrt(ee_r)
    ee = ee_r * (_hj_z01(u1) / _hj_z01(ur)) ** 2
    return z, ee


def microstrip_dispersion(eeff0: float, er: float, w: float, h: float, f: float) -> float:
    """Er_eff(f), Kirschning-Jansen 1982."""
    if f <= 0:
        return eeff0
    u = w / h
    fn = f * 1e-9 * h * 1e3       # GHz * mm
    p1 = (0.27488 + (0.6315 + 0.525 / (1.0 + 0.0157 * fn) ** 20) * u
          - 0.065683 * math.exp(-8.7513 * u))
    p2 = 0.33622 * (1.0 - math.exp(-0.03442 * er))
    p3 = 0.0363 * math.exp(-4.6 * u) * (1.0 - math.exp(-((fn / 38.7) ** 4.97)))
    p4 = 1.0 + 2.751 * (1.0 - math.exp(-((er / 15.916) ** 8)))
    p = p1 * p2 * ((0.1844 + p3 * p4) * fn) ** 1.5763
    return er - (er - eeff0) / (1.0 + p)


def dispersive_z(z0: float, eeff0: float, eefff: float) -> float:
    """Z0(f) from the dispersed Er_eff, power-current form (Bianco et al.).

    Accurate to a few percent for the usual PCB band; a full-wave
    solver is the reference above that.
    """
    if abs(eeff0 - 1.0) < 1e-9:
        return z0
    return z0 * (eefff - 1.0) / (eeff0 - 1.0) * math.sqrt(eeff0 / eefff)


# ------------------------------------------------------------- stripline

def stripline_cohn(w: float, b: float, er: float) -> float:
    """Zero-thickness centred stripline, exact (Cohn 1954)."""
    k = 1.0 / math.cosh(math.pi * w / (2.0 * b))
    return ETA0 / (4.0 * math.sqrt(er)) * k_ratio(k)


def stripline_wheeler(w: float, b: float, t: float, er: float) -> float:
    """Centred stripline with finite thickness, Wheeler 1978 (0.5 %)."""
    if t <= 0:
        return stripline_cohn(w, b, er)
    x = t / b
    m = 2.0 / (1.0 + 2.0 / 3.0 * x / (1.0 - x))
    dw = (x / (math.pi * (1.0 - x)) *
          (1.0 - 0.5 * math.log((x / (2.0 - x)) ** 2 +
                                (0.0796 * x / (w / b + 1.1 * x)) ** m)))
    wp = w / (b - t) + dw
    a = 8.0 / (math.pi * wp)
    return 30.0 / math.sqrt(er) * math.log(1.0 + 4.0 / (math.pi * wp) *
                                           (a + math.sqrt(a * a + 6.27)))


def stripline_asym(w: float, h1: float, h2: float, t: float, er: float) -> float:
    """Off-centre stripline: parallel combination of two centred lines.

    Each half sees a centred line of spacing 2h+t. Within ~2 % while the
    offset is moderate (h2/h1 < 3); the solver is the reference.
    """
    z1 = stripline_wheeler(w, 2.0 * h1 + t, t, er)
    z2 = stripline_wheeler(w, 2.0 * h2 + t, t, er)
    return 2.0 * z1 * z2 / (z1 + z2)


def coupled_stripline_cohn(w: float, s: float, b: float, er: float) -> tuple[float, float]:
    """Edge-coupled centred stripline, zero thickness: (Z_even, Z_odd), exact."""
    a = math.pi * w / (2.0 * b)
    c = math.pi * (w + s) / (2.0 * b)
    ke = math.tanh(a) * math.tanh(c)
    ko = math.tanh(a) / math.tanh(c)
    pref = ETA0 / (4.0 * math.sqrt(er))
    return pref / k_ratio(ke), pref / k_ratio(ko)


# --------------------------------------------------------------- coplanar

def cpw(w: float, g: float, h: float, er: float, grounded: bool) -> tuple[float, float]:
    """CPW (Z0, Er_eff), zero thickness, infinite grounds (Simons ch. 2)."""
    k0 = w / (w + 2.0 * g)
    q0 = k_ratio(k0)                      # K(k0)/K(k0')
    if not grounded:
        k1 = math.sinh(math.pi * w / (4.0 * h)) / math.sinh(math.pi * (w + 2.0 * g) / (4.0 * h))
        q1 = k_ratio(k1)
        ee = 1.0 + (er - 1.0) / 2.0 * q1 / q0
        return ETA0 / (4.0 * math.sqrt(ee)) / q0, ee
    k3 = math.tanh(math.pi * w / (4.0 * h)) / math.tanh(math.pi * (w + 2.0 * g) / (4.0 * h))
    q3 = k_ratio(k3)
    ee = (1.0 + er * q3 / q0) / (1.0 + q3 / q0)
    return ETA0 / (2.0 * math.sqrt(ee)) / (q0 + q3), ee


# --------------------------------------------------------------- materials

def djordjevic_sarkar(er_spec: float, tand_spec: float, f_spec: float, f: float,
                      f_lo: float = 1e3, f_hi: float = 1e12) -> tuple[float, float]:
    """Causal wideband Debye model: (Er'(f), tan d(f)) through one point.

    A laminate data sheet gives Er and tan d at one frequency. Holding them
    constant at every frequency is not causal; the Djordjevic-Sarkar model
    is the industry-standard causal extension (most field solvers use it).
    """
    if tand_spec <= 0 or f <= 0 or f_spec <= 0:
        return er_spec, tand_spec

    def lg(fr: float) -> complex:
        return cmath.log((f_hi + 1j * fr) / (f_lo + 1j * fr))

    span = math.log(f_hi / f_lo)
    l0 = lg(f_spec)
    # eps = eps_inf + d * lg(f) / span. Im part sets d from the loss.
    d = -er_spec * tand_spec * span / l0.imag
    einf = er_spec - d * l0.real / span
    e = einf + d * lg(f) / span
    return e.real, -e.imag / e.real


def skin_depth(f: float, rho: float = RHO_CU, mu_r: float = 1.0) -> float:
    if f <= 0:
        return math.inf
    return math.sqrt(rho / (math.pi * f * MU0 * mu_r))


def surface_resistance(f: float, rho: float = RHO_CU) -> float:
    return math.sqrt(math.pi * f * MU0 * rho)


def rough_hammerstad(f: float, rq: float, rho: float = RHO_CU) -> float:
    """Hammerstad-Bekkadal roughness factor from RMS roughness. Saturates at 2."""
    d = skin_depth(f, rho)
    return 1.0 + 2.0 / math.pi * math.atan(1.4 * (rq / d) ** 2)


def rough_groisse(f: float, rq: float, rho: float = RHO_CU) -> float:
    d = skin_depth(f, rho)
    return 1.0 + math.exp(-((d / (2.0 * rq)) ** 1.6))


def rough_huray(f: float, radius: float, sr: float, rho: float = RHO_CU) -> float:
    """Huray snowball factor. ``sr`` is the surface ratio N*4*pi*a^2/A_flat."""
    d = skin_depth(f, rho)
    return 1.0 + 1.5 * sr / (1.0 + d / radius + d * d / (2.0 * radius * radius))


def rough_factor(f: float, model: str, rq: float = 0.0, radius: float = 0.5e-6,
                 sr: float = 0.0) -> float:
    if model == "none" or f <= 0:
        return 1.0
    if model == "hammerstad":
        return rough_hammerstad(f, rq) if rq > 0 else 1.0
    if model == "groisse":
        return rough_groisse(f, rq) if rq > 0 else 1.0
    if model == "huray":
        return rough_huray(f, radius, sr) if sr > 0 else 1.0
    raise ValueError(f"unknown roughness model {model}")


def ac_resistance_factor(f: float, w: float, t: float, rho: float = RHO_CU) -> float:
    """Ratio R_ac/R_dc for a rectangular bar, both sides conducting.

    Smooth transition sqrt(1 + (Rs_ac/Rdc)^2): exact limits at DC and at
    high frequency, within ~10 % at the knee. Used only for the
    conductor-current page; the impedance page uses the solver loss.
    """
    if f <= 0:
        return 1.0
    d = skin_depth(f, rho)
    rdc = rho / (w * t)
    rac = rho / (d * 2.0 * (w + t))
    return math.sqrt(1.0 + (rac / rdc) ** 2)
