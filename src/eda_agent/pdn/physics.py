# SPDX-License-Identifier: Apache-2.0
"""Physics primitives for the PDN solvers, each a closed form from the
literature, stated with its range of validity.

Units: SI inside (metres, ohms, henries, siemens per metre); the helpers
that take board dimensions take mils and say so in the name.
"""

from __future__ import annotations

import cmath
import math

MU0 = 4e-7 * math.pi            # H/m (to 1e-10, the pre-2019 exact value)
EPS0 = 8.8541878128e-12         # F/m
C0 = 299_792_458.0              # m/s
MIL = 25.4e-6                   # m

#: Annealed copper (IACS) at 20 C, ohm*m, and its temperature coefficient
#: of resistance per kelvin at 20 C. Electrodeposited PCB copper is
#: typically 1-5 % above IACS; ``copper_resistivity`` takes a factor.
RHO_CU_20C = 1.7241e-8
ALPHA_CU = 0.00393

#: IPC-6012 minimum average barrel plating: class 2 is 20 um (0.79 mil),
#: class 3 is 25 um. 20 um is used unless the caller says otherwise.
DEFAULT_PLATING_MILS = 0.79


def copper_resistivity(temp_c: float = 20.0, factor: float = 1.0) -> float:
    """Copper resistivity at ``temp_c`` (linear in T, good to about 0.5 %
    from -50 C to 150 C)."""
    return RHO_CU_20C * factor * (1.0 + ALPHA_CU * (temp_c - 20.0))


def sheet_resistance(thickness_mils: float, rho: float = RHO_CU_20C) -> float:
    """DC sheet resistance (ohm per square) of a copper foil."""
    return rho / (thickness_mils * MIL)


def skin_depth(freq_hz: float, rho: float = RHO_CU_20C) -> float:
    """Skin depth in metres, sqrt(rho / (pi f mu0))."""
    if freq_hz <= 0:
        return math.inf
    return math.sqrt(rho / (math.pi * freq_hz * MU0))


def slab_surface_impedance(freq_hz: float, thickness_m: float,
                           rho: float = RHO_CU_20C) -> complex:
    """Internal impedance per square of a conducting slab of thickness t
    carrying current driven from one face (the plane of a plane pair, with
    its field on the cavity side): Zs = (gamma / sigma) coth(gamma t),
    gamma = (1 + j) / delta.

    It tends to the DC sheet resistance rho/t at low frequency (with the
    internal inductance mu0 t / 3 as the first imaginary term) and to the
    skin-effect surface impedance (1 + j) rho / delta once delta << t.
    """
    if freq_hz <= 0:
        return complex(rho / thickness_m, 0.0)
    delta = skin_depth(freq_hz, rho)
    gamma = (1 + 1j) / delta
    gt = gamma * thickness_m
    if abs(gt) < 1e-3:
        # coth(x) ~ 1/x + x/3
        return rho / thickness_m + 1j * 2 * math.pi * freq_hz * MU0 * thickness_m / 3
    if gt.real > 20:
        return gamma * rho
    return gamma * rho / cmath.tanh(gt)


def via_barrel_resistance(length_mils: float, hole_mils: float,
                          plating_mils: float = DEFAULT_PLATING_MILS,
                          rho: float = RHO_CU_20C) -> float:
    """DC resistance of a plated barrel: rho L / (pi ((r+t)^2 - r^2)),
    r the finished hole radius, t the plating."""
    r = hole_mils * MIL / 2.0
    t = plating_mils * MIL
    area = math.pi * ((r + t) ** 2 - r ** 2)
    return rho * (length_mils * MIL) / area


def barrel_area_m2(hole_mils: float, plating_mils: float = DEFAULT_PLATING_MILS) -> float:
    r = hole_mils * MIL / 2.0
    t = plating_mils * MIL
    return math.pi * ((r + t) ** 2 - r ** 2)


def via_pair_loop_inductance(height_mils: float, spacing_mils: float,
                             radius_mils: float) -> float:
    """Loop inductance of two parallel vias (current out on one, back on
    the other) of length h, centre spacing s, radius r:
    L = (mu0 h / pi) acosh(s / 2r), which is (mu0 h / pi) ln(s / r) for
    s >> r. Neglects the ends' fringing; exact for h >> s."""
    if height_mils <= 0 or radius_mils <= 0:
        return 0.0
    s = max(spacing_mils, 2.0 * radius_mils * 1.0001)
    return MU0 * height_mils * MIL / math.pi * math.acosh(s / (2.0 * radius_mils))


#: exp(-gamma) / 2^(3/2): the lattice Green's function of the square
#: five-point Laplacian tends to (1 / 2 pi) ln(r / (PEACEMAN * h)) far from
#: its source (gamma is Euler's constant).
PEACEMAN = math.exp(-0.5772156649015329) / 2.0 ** 1.5      # 0.19851


def peaceman_radius(cell_mils: float) -> float:
    """Equivalent radius of a node of a square five-point grid: a round
    conductor of this radius in a continuous sheet sees the same potential
    as the grid node (Peaceman 1978; 0.1985 h for a square grid)."""
    return PEACEMAN * cell_mils


_LATTICE: dict[tuple[int, int], float] = {}


def lattice_potential(m: int, n: int) -> float:
    """G(0,0) - G(m,n) of the square five-point Laplacian with unit links:
    the potential drop from a node carrying 1 A out of an infinite grid of
    1 ohm links to the node (m, n) away. Half the two-point resistance
    (Cserti 2000): (1 / 2 pi) int_0^pi (1 - exp(-|m| s) cos(n t)) / sinh(s) dt,
    cosh(s) = 2 - cos(t). 1/4 at (1, 0), 1/pi at (1, 1), and
    (1 / 2 pi) ln(r / PEACEMAN) far away."""
    m, n = abs(int(m)), abs(int(n))
    if m < n:
        m, n = n, m
    key = (m, n)
    if key not in _LATTICE:
        if m == 0:
            _LATTICE[key] = 0.0
        else:
            import numpy as np
            x, w = np.polynomial.legendre.leggauss(400)
            t = (x + 1.0) * (math.pi / 2.0)
            s = np.arccosh(2.0 - np.cos(t))
            f = (1.0 - np.exp(-m * s) * np.cos(n * t)) / np.sinh(s)
            _LATTICE[key] = float((w * f).sum() * (math.pi / 2.0) / (2.0 * math.pi))
    return _LATTICE[key]


def lattice_capacity_radius(cells) -> float:
    """Radius, in cells, of the round conductor in a continuous sheet that
    an equipotential set of grid nodes stands for: far from it the grid
    potential is that of a disc of this radius (the set's logarithmic
    capacity on the lattice). ``cells`` are (i, j) offsets. One node gives
    PEACEMAN; a large round set of radius R about R - 0.2."""
    import numpy as np
    pts = sorted({(int(i), int(j)) for i, j in cells})
    k = len(pts)
    if k == 0:
        raise ValueError("no cells")
    if k == 1:
        return PEACEMAN
    a = np.zeros((k + 1, k + 1))
    for u in range(k):
        for v in range(u + 1, k):
            a[u, v] = a[v, u] = lattice_potential(pts[u][0] - pts[v][0], pts[u][1] - pts[v][1])
    a[:k, k] = -1.0
    a[k, :k] = 1.0
    rhs = np.zeros(k + 1)
    rhs[k] = 1.0
    u0 = float(np.linalg.solve(a, rhs)[k])
    return PEACEMAN * math.exp(2.0 * math.pi * u0)


def cavity_port_inductance(cavity_mils: float, via_radius_mils: float,
                           cell_mils: float) -> float:
    """The inductance a via of radius r adds inside a plane cavity of
    separation d over what a grid node of pitch h already models:
    mu0 d / (2 pi) ln(r_eq / r), r_eq the Peaceman radius. Zero when the via
    is wider than the node."""
    req = peaceman_radius(cell_mils)
    if via_radius_mils <= 0 or req <= via_radius_mils:
        return 0.0
    return MU0 * cavity_mils * MIL / (2 * math.pi) * math.log(req / via_radius_mils)


def ipc2221_current_capacity(area_mils2: float, temp_rise_c: float,
                             external: bool = True) -> float:
    """IPC-2221 conductor current capacity: I = k dT^0.44 A^0.725, A in
    square mils, k = 0.048 external, 0.024 internal. Conservative
    (IPC-2152 allows more on most boards); used as a flag, not a limit."""
    if area_mils2 <= 0 or temp_rise_c <= 0:
        return 0.0
    k = 0.048 if external else 0.024
    return k * temp_rise_c ** 0.44 * area_mils2 ** 0.725
