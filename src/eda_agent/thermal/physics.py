# SPDX-License-Identifier: Apache-2.0
"""Heat transfer physics for boards: materials, air, convection, radiation.

Every formula here is a published correlation, named where it is used, so
a result can be checked by hand. SI units throughout (W, m, K) unless a
name says otherwise; temperatures are kelvin inside the correlations and
the public helpers take Celsius where they say so.

Sources (standard heat-transfer texts; formulas, not code, are reused):

* Air properties: ideal gas density; Sutherland's law for viscosity and
  conductivity (Sutherland 1893; constants from White, *Viscous Fluid
  Flow*). Within 1-2 % of tabulated air data from 250 K to 400 K.
* Natural convection, horizontal plate, hot side up: Lloyd & Moran (1974),
  Nu = 0.54 Ra^1/4 (1e4..1e7), 0.15 Ra^1/3 (1e7..1e11), characteristic
  length L = A / P. Below Ra 1e4 the laminar form is kept and blended
  with a pure-conduction floor (Nu >= 1).
* Hot side down: Raithby & Hollands, Nu = 0.52 Ra^1/5 (1e4..1e9) for the
  same L. (The older 0.27 Ra^1/4 underestimates small plates.)
* Vertical plate: Churchill & Chu (1975), full range,
  Nu = (0.825 + 0.387 Ra^1/6 / (1 + (0.492/Pr)^9/16)^8/27)^2, L = height.
* Forced flow along a plate: average laminar Nu = 0.664 Re^1/2 Pr^1/3
  (Re < 5e5), mixed laminar-turbulent Nu = (0.037 Re^4/5 - 871) Pr^1/3.
* Natural plus forced: Churchill's combination Nu^3 = Nu_F^3 + Nu_N^3
  (assisting / transverse flow).
* Radiation to large surroundings at ambient: q = eps sigma (T^4 - Ta^4),
  linearised per node as h_r = eps sigma (T^2 + Ta^2)(T + Ta) and iterated.
* Plated barrel: a copper annulus, R = L / (k A), A = pi/4 (D^2 - d^2),
  with an optional fill (epoxy, conductive or copper) in parallel.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

#: Stefan-Boltzmann constant, W/m^2/K^4.
SIGMA = 5.670374419e-8
#: 0 degC in kelvin.
T0 = 273.15
#: Metres per mil.
MIL = 25.4e-6

# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------

#: Electrodeposited / rolled PCB copper, W/m/K. Pure annealed copper is
#: 401; plated and foil copper measure 380-395.
K_COPPER = 385.0
#: Copper resistivity at 20 degC, ohm m (IACS annealed 1.7241e-8; PCB
#: foil runs a few percent higher).
RHO_COPPER_20C = 1.724e-8
#: Copper resistivity temperature coefficient, 1/K, at 20 degC.
ALPHA_COPPER = 0.00393


def rho_copper(temp_c: float) -> float:
    """Copper resistivity at ``temp_c``, ohm m (linear in T, good to ~200 C)."""
    return RHO_COPPER_20C * (1.0 + ALPHA_COPPER * (temp_c - 20.0))


@dataclass(frozen=True)
class Dielectric:
    name: str
    k_xy: float     # in-plane conductivity, W/m/K
    k_z: float      # through-plane conductivity, W/m/K


#: FR-4 class laminates (glass weave + epoxy). Through-plane 0.29 W/mK is
#: the commonly measured value (ASTM D5470 / laser flash); in-plane is
#: larger because of the glass, ~0.8 W/mK.
DIELECTRICS = {
    "fr4": Dielectric("FR-4", 0.81, 0.29),
    "high_tg_fr4": Dielectric("High-Tg FR-4", 0.85, 0.32),
    "polyimide": Dielectric("Polyimide", 0.35, 0.20),
    "rogers_4350b": Dielectric("RO4350B", 0.69, 0.69),
    "rogers_4003c": Dielectric("RO4003C", 0.71, 0.71),
    "ptfe": Dielectric("PTFE", 0.25, 0.25),
    "imsl": Dielectric("IMS dielectric", 2.2, 2.2),
}
DEFAULT_DIELECTRIC = "fr4"

#: Liquid photo-imageable solder mask: about 20 um (0.8 mil), k ~0.25.
K_SOLDER_MASK = 0.25
SOLDER_MASK_MILS = 0.8
#: Emissivity of green solder mask (matte, ~0.9) and of bare copper
#: (oxidised ~0.6, bright ~0.05). The board's outer surface is mask.
EMISSIVITY_MASK = 0.9

#: Barrel plating, m: IPC-6012 class 2 average minimum 20 um; 25 um is
#: the common fab default.
PLATING_M = 25e-6
#: Via fill conductivity, W/m/K.
VIA_FILL_K = {"none": 0.0, "epoxy": 0.3, "conductive": 3.5, "silver": 8.0, "copper": K_COPPER}

# ---------------------------------------------------------------------------
# Air
# ---------------------------------------------------------------------------

R_AIR = 287.05          # J/kg/K
CP_AIR = 1006.0         # J/kg/K (1005-1009 from 250 to 400 K)
P_ATM = 101325.0        # Pa
G = 9.80665             # m/s^2


@dataclass(frozen=True)
class Air:
    """Dry air properties; every field may be a float or a numpy array."""

    T: Any          # K
    rho: Any        # kg/m^3
    mu: Any         # Pa s
    k: Any          # W/m/K
    cp: float       # J/kg/K

    @property
    def nu(self):
        return self.mu / self.rho

    @property
    def alpha(self):
        return self.k / (self.rho * self.cp)

    @property
    def pr(self):
        return self.mu * self.cp / self.k


def air(T, pressure_pa: float = P_ATM) -> Air:
    """Dry air at ``T`` kelvin (float or array). Sutherland for mu and k."""
    T = np.maximum(T, 150.0)
    mu = 1.716e-5 * (T / T0) ** 1.5 * (T0 + 110.4) / (T + 110.4)
    k = 0.02414 * (T / T0) ** 1.5 * (T0 + 194.0) / (T + 194.0)
    rho = pressure_pa / (R_AIR * T)
    return Air(T, rho, mu, k, CP_AIR)


def rayleigh(t_surf_k, t_amb_k: float, length_m, pressure_pa: float = P_ATM):
    """Ra at the film temperature, and the film air."""
    film = air(0.5 * (t_surf_k + t_amb_k), pressure_pa)
    beta = 1.0 / t_amb_k  # ideal gas, evaluated at ambient (common practice)
    dT = np.abs(t_surf_k - t_amb_k)
    ra = G * beta * dT * length_m ** 3 / (film.nu * film.alpha)
    return ra, film


# ---------------------------------------------------------------------------
# Convection
# ---------------------------------------------------------------------------

ORIENTATIONS = ("up", "down", "vertical")
#: Laminar to turbulent transition of a flat plate in parallel flow.
RE_TRANSITION = 5.0e5


def nusselt_natural(ra, pr, orientation: str):
    """Average Nusselt number of an isothermal plate in still air.

    ``orientation`` is the way the HOT face looks: ``up``, ``down`` or
    ``vertical``. ``ra`` and ``pr`` may be arrays."""
    ra = np.maximum(ra, 0.0)
    if orientation == "vertical":
        return (0.825 + 0.387 * ra ** (1 / 6)
                / (1 + (0.492 / pr) ** (9 / 16)) ** (8 / 27)) ** 2
    if orientation == "up":
        nu = np.where(ra < 1e7, 0.54 * ra ** 0.25, 0.15 * ra ** (1 / 3))
    elif orientation == "down":
        nu = 0.52 * ra ** 0.2
    else:
        raise ValueError(f"orientation must be one of {ORIENTATIONS}")
    nu = np.maximum(nu, 1.0)
    return float(nu) if np.ndim(nu) == 0 else nu


def nusselt_forced(re, pr):
    """Average Nusselt number of a plate in parallel flow, from its
    leading edge: laminar to Re 5e5, then the mixed boundary layer."""
    re = np.maximum(re, 0.0)
    lam = 0.664 * re ** 0.5 * pr ** (1 / 3)
    turb = (0.037 * re ** 0.8 - 871.0) * pr ** (1 / 3)
    nu = np.where(re < RE_TRANSITION, lam, np.maximum(turb, lam * 0.0 + 0.664 * RE_TRANSITION ** 0.5
                                                      * pr ** (1 / 3)))
    return float(nu) if np.ndim(nu) == 0 else nu


def h_natural(t_surf_c, t_amb_c: float, length_m, orientation: str = "up",
              pressure_pa: float = P_ATM):
    """Natural convection coefficient, W/m^2/K, of a face at ``t_surf_c``.

    ``orientation`` says which way the face looks. A face colder than the
    air swaps ``up`` and ``down`` (a cold plate facing up is stably
    stratified, like a hot plate facing down). Arrays are accepted for the
    temperature and the length."""
    if orientation not in ORIENTATIONS:
        raise ValueError(f"orientation must be one of {ORIENTATIONS}")
    ts, ta = np.asarray(t_surf_c, dtype=float) + T0, t_amb_c + T0
    ra, film = rayleigh(ts, ta, length_m, pressure_pa)
    if orientation == "vertical":
        nu = nusselt_natural(ra, film.pr, "vertical")
    else:
        hot = ts >= ta
        same = nusselt_natural(ra, film.pr, orientation)
        other = nusselt_natural(ra, film.pr, "down" if orientation == "up" else "up")
        nu = np.where(hot, same, other)
    h = nu * film.k / length_m
    return float(h) if np.ndim(h) == 0 else h


def h_forced(t_surf_c, t_amb_c: float, velocity_ms: float, flow_length_m,
             pressure_pa: float = P_ATM):
    """Average forced-convection coefficient over the first
    ``flow_length_m`` of a plate from its leading edge, W/m^2/K."""
    ts, ta = np.asarray(t_surf_c, dtype=float) + T0, t_amb_c + T0
    film = air(0.5 * (ts + ta), pressure_pa)
    lf = np.maximum(flow_length_m, 1e-9)
    re = velocity_ms * lf / film.nu
    h = nusselt_forced(re, film.pr) * film.k / lf
    return float(h) if np.ndim(h) == 0 else h


def h_mixed(h_nat, h_for):
    """Churchill's rule for natural and forced convection acting together."""
    return (np.abs(h_nat) ** 3 + np.abs(h_for) ** 3) ** (1 / 3)


def h_convection(t_surf_c, t_amb_c: float, length_m, orientation: str = "up",
                 velocity_ms: float = 0.0, flow_length_m: float = 0.0,
                 pressure_pa: float = P_ATM):
    """Average convection coefficient, W/m^2/K, of one board face.

    ``length_m``: the natural-convection length (A/P for a horizontal
    face, the height for a vertical one). ``velocity_ms`` > 0 adds forced
    flow along ``flow_length_m`` (default ``length_m``), combined with the
    natural part by Churchill's cube rule."""
    if np.any(np.asarray(length_m) <= 0):
        raise ValueError("length_m must be > 0")
    h_n = h_natural(t_surf_c, t_amb_c, length_m, orientation, pressure_pa)
    if velocity_ms <= 0:
        return h_n
    h_f = h_forced(t_surf_c, t_amb_c, velocity_ms, flow_length_m or length_m, pressure_pa)
    h = h_mixed(h_n, h_f)
    return float(h) if np.ndim(h) == 0 else h


def h_radiation(t_surf_c, t_amb_c: float, emissivity: float = EMISSIVITY_MASK):
    """Linearised radiation coefficient to large surroundings, W/m^2/K:
    ``h_r (Ts - Ta)`` is exactly ``eps sigma (Ts^4 - Ta^4)``."""
    ts, ta = np.asarray(t_surf_c, dtype=float) + T0, t_amb_c + T0
    h = emissivity * SIGMA * (ts * ts + ta * ta) * (ts + ta)
    return float(h) if np.ndim(h) == 0 else h


def pressure_at_altitude(altitude_m: float) -> float:
    """Standard-atmosphere pressure, Pa, up to 11 km (ISO 2533)."""
    h = min(max(float(altitude_m), -500.0), 11_000.0)
    return P_ATM * (1.0 - 2.25577e-5 * h) ** 5.25588


def plate_length(area_m2: float, perimeter_m: float) -> float:
    """Characteristic length A/P of a horizontal plate."""
    return area_m2 / perimeter_m if perimeter_m > 0 else math.sqrt(max(area_m2, 0.0))


# ---------------------------------------------------------------------------
# Vias
# ---------------------------------------------------------------------------

def barrel_conductance(hole_m: float, length_m: float, plating_m: float = PLATING_M,
                       fill: str = "none") -> float:
    """Thermal conductance, W/K, of one plated hole over ``length_m``.

    ``hole_m`` is the finished (drilled) hole diameter; the plating is
    inside it. A plating thicker than the radius means a solid copper
    plug."""
    if hole_m <= 0 or length_m <= 0:
        return 0.0
    r_o = hole_m / 2.0
    r_i = max(r_o - plating_m, 0.0)
    a_cu = math.pi * (r_o * r_o - r_i * r_i)
    k_fill = VIA_FILL_K.get(fill, 0.0) if isinstance(fill, str) else float(fill)
    a_fill = math.pi * r_i * r_i
    return (K_COPPER * a_cu + k_fill * a_fill) / length_m


def barrel_resistance(hole_m: float, length_m: float, plating_m: float = PLATING_M,
                      fill: str = "none") -> float:
    """Thermal resistance, K/W, of one plated hole (inf for no hole)."""
    g = barrel_conductance(hole_m, length_m, plating_m, fill)
    return 1.0 / g if g > 0 else math.inf
