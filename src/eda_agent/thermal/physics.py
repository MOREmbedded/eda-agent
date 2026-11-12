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
    T: float        # K
    rho: float      # kg/m^3
    mu: float       # Pa s
    k: float        # W/m/K
    cp: float       # J/kg/K

    @property
    def nu(self) -> float:
        return self.mu / self.rho

    @property
    def alpha(self) -> float:
        return self.k / (self.rho * self.cp)

    @property
    def pr(self) -> float:
        return self.mu * self.cp / self.k


def air(T: float, pressure_pa: float = P_ATM) -> Air:
    """Dry air at ``T`` kelvin. Sutherland for mu and k."""
    T = max(T, 150.0)
    mu = 1.716e-5 * (T / T0) ** 1.5 * (T0 + 110.4) / (T + 110.4)
    k = 0.02414 * (T / T0) ** 1.5 * (T0 + 194.0) / (T + 194.0)
    rho = pressure_pa / (R_AIR * T)
    return Air(T, rho, mu, k, CP_AIR)


def rayleigh(t_surf_k: float, t_amb_k: float, length_m: float,
             pressure_pa: float = P_ATM) -> tuple[float, Air]:
    """Ra at the film temperature, and the film air."""
    film = air(0.5 * (t_surf_k + t_amb_k), pressure_pa)
    beta = 1.0 / t_amb_k  # ideal gas, evaluated at ambient (common practice)
    dT = abs(t_surf_k - t_amb_k)
    ra = G * beta * dT * length_m ** 3 / (film.nu * film.alpha)
    return ra, film


# ---------------------------------------------------------------------------
# Convection
# ---------------------------------------------------------------------------

ORIENTATIONS = ("up", "down", "vertical")


def nusselt_natural(ra: float, pr: float, orientation: str) -> float:
    """Average Nusselt number of an isothermal plate in still air."""
    ra = max(ra, 0.0)
    if orientation == "vertical":
        return (0.825 + 0.387 * ra ** (1 / 6)
                / (1 + (0.492 / pr) ** (9 / 16)) ** (8 / 27)) ** 2
    if orientation == "up":
        nu = 0.54 * ra ** 0.25 if ra < 1e7 else 0.15 * ra ** (1 / 3)
    elif orientation == "down":
        nu = 0.52 * ra ** 0.2
    else:
        raise ValueError(f"orientation must be one of {ORIENTATIONS}")
    return max(nu, 1.0)


def nusselt_forced(re: float, pr: float) -> float:
    """Average Nusselt number of a plate in parallel flow."""
    if re <= 0:
        return 0.0
    if re < 5e5:
        return 0.664 * re ** 0.5 * pr ** (1 / 3)
    return max((0.037 * re ** 0.8 - 871.0) * pr ** (1 / 3), 0.664 * 5e5 ** 0.5 * pr ** (1 / 3))


def h_convection(t_surf_c: float, t_amb_c: float, length_m: float,
                 orientation: str = "up", velocity_ms: float = 0.0,
                 flow_length_m: float = 0.0, pressure_pa: float = P_ATM) -> float:
    """Average convection coefficient, W/m^2/K, of one board face.

    ``length_m``: the natural-convection length (A/P for a horizontal
    face, the height for a vertical one). ``velocity_ms`` > 0 adds forced
    flow along ``flow_length_m`` (default ``length_m``), combined with the
    natural part by Churchill's cube rule."""
    ts, ta = t_surf_c + T0, t_amb_c + T0
    if length_m <= 0:
        raise ValueError("length_m must be > 0")
    ra, film = rayleigh(ts, ta, length_m, pressure_pa)
    nu_n = nusselt_natural(ra, film.pr, orientation)
    h_n = nu_n * film.k / length_m
    if velocity_ms <= 0:
        return h_n
    lf = flow_length_m or length_m
    re = velocity_ms * lf / film.nu
    h_f = nusselt_forced(re, film.pr) * film.k / lf
    return (h_n ** 3 + h_f ** 3) ** (1 / 3)


def h_radiation(t_surf_c: float, t_amb_c: float, emissivity: float = EMISSIVITY_MASK) -> float:
    """Linearised radiation coefficient to large surroundings, W/m^2/K."""
    ts, ta = t_surf_c + T0, t_amb_c + T0
    return emissivity * SIGMA * (ts * ts + ta * ta) * (ts + ta)


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
