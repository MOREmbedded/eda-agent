# SPDX-License-Identifier: Apache-2.0
"""A 2D quasi-static field solver for PCB transmission-line cross-sections.

Closed-form impedance formulas are curve fits. Each is good inside the
band its author fitted and drifts outside it, and none of them handles a
real stackup: solder mask over a microstrip, a prepreg with a different
Er from the core, an asymmetric stripline, a trapezoidal etch. A field
solver does, so it is the reference engine here and the closed forms are
reported beside it as a cross-check.

METHOD. Finite differences on a non-uniform tensor grid. The grid is fine
at every conductor edge and dielectric interface and grows geometrically
away from them, so the far boundaries can sit 40-60 substrate heights out
for little cost. The node equation is the integral form of Gauss's law
over the dual cell, with each quarter of the dual cell taking the
permittivity of the material it lies in. That makes dielectric
interfaces exact on grid lines.

For an N-conductor line it solves N excitations, each with one conductor
at 1 V and the others at 0 V, twice: once with the real dielectrics and
once in vacuum. That gives the Maxwell capacitance matrices C and C0.
Then:

    L = mu0 eps0 C0^-1            (inductance matrix, quasi-TEM)
    Z0 = 1 / (c sqrt(C C0))       (one conductor)
    odd/even mode C from C11 -/+ C12, and the same for C0

The dielectric loss conductance G comes from the same solution, weighted
by each cell's loss tangent (a perturbation, exact to first order in
tan d). The conductor loss comes from the vacuum solution's surface
charge. In a TEM line the surface current density is the surface charge
times the speed of light, so its square, integrated round every metal
surface including the return planes, gives the loss with current
crowding at the edges included (Wheeler's incremental-inductance rule in
discrete form). Surface roughness multiplies that.

ACCURACY. Checked in tests against the exact conformal-mapping result for
zero-thickness stripline (Cohn) and against Hammerstad-Jensen microstrip,
which its authors give as within 0.2 % for Er_eff and 0.03 % for the
vacuum impedance. Converged grids agree with both to better than 0.5 %.
The quasi-static assumption holds while the cross-section is small next
to a wavelength. Microstrip dispersion is added separately with the
Kirschning-Jansen model.

numpy and scipy only. Both are already core dependencies.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import splu

#: Grid growth per unit distance from the nearest edge, at the reference
#: resolution; it scales down with the resolution so that a refinement
#: refines the whole grid, not only the edges. Without that the answer
#: converges to a value offset by the fixed coarse grid (measured: 0.2 %).
GROWTH = 0.12
FAR_GROWTH = 0.30
REF_RESOLUTION = 60.0
EPS0 = 8.8541878128e-12
MU0 = 1.25663706212e-6
C0 = 299_792_458.0


@dataclass
class Dielectric:
    """A horizontal slab from ``y0`` to ``y1``, spanning the full width."""

    y0: float
    y1: float
    er: float
    tand: float = 0.0


@dataclass
class Conformal:
    """A coating of ``thickness`` over everything below ``top_of``.

    Solder mask is conformal: it covers the substrate between traces and
    wraps over each trace. Cells within ``thickness`` above the substrate
    top or within ``thickness`` of a conductor on that surface take the
    coating's Er.
    """

    surface_y: float
    thickness: float
    er: float
    tand: float = 0.0
    direction: int = 1  # +1 coats upward, -1 coats downward (bottom side)


@dataclass
class Metal:
    """A trapezoidal conductor, or a ground body.

    ``net`` is the conductor index (0, 1, ...) for a signal line, or -1
    for ground. The bottom edge runs x0..x1 at y0 and the top edge
    xt0..xt1 at y1. With y0 == y1 it is a zero-thickness strip.
    """

    x0: float
    x1: float
    y0: float
    y1: float
    net: int = 0
    xt0: Optional[float] = None
    xt1: Optional[float] = None

    def top(self) -> tuple[float, float]:
        return (self.x0 if self.xt0 is None else self.xt0,
                self.x1 if self.xt1 is None else self.xt1)

    def contains(self, x: np.ndarray, y: np.ndarray, tol: float) -> np.ndarray:
        t0, t1 = self.top()
        inside_y = (y >= self.y0 - tol) & (y <= self.y1 + tol)
        if self.y1 - self.y0 <= tol:
            frac = np.zeros_like(y)
        else:
            frac = np.clip((y - self.y0) / (self.y1 - self.y0), 0.0, 1.0)
        left = self.x0 + (t0 - self.x0) * frac
        right = self.x1 + (t1 - self.x1) * frac
        return inside_y & (x >= left - tol) & (x <= right + tol)


@dataclass
class CrossSection:
    """Everything the solver needs. Lengths in metres."""

    metals: list[Metal]
    dielectrics: list[Dielectric] = field(default_factory=list)
    coatings: list[Conformal] = field(default_factory=list)
    ground_bottom: Optional[float] = 0.0   # y of a bottom plane, or None
    ground_top: Optional[float] = None     # y of a top plane, or None
    feature: float = 0.0                   # smallest feature, for meshing

    @property
    def n_lines(self) -> int:
        nets = {m.net for m in self.metals if m.net >= 0}
        return len(nets)


@dataclass
class Solution:
    C: np.ndarray        # F/m, Maxwell capacitance matrix
    C0: np.ndarray       # F/m, in vacuum
    G: np.ndarray        # S/m per (rad/s), multiply by omega
    L: np.ndarray        # H/m
    surface: list        # per excitation: (fluxes, lengths) of the vacuum solve
    nodes: int
    nets: list[int]

    def mode(self, v: Sequence[float]) -> dict:
        """Impedance and Er_eff for a voltage pattern across the lines."""
        v = np.asarray(v, dtype=float)
        c = float(v @ self.C @ v)
        c0 = float(v @ self.C0 @ v)
        z = 1.0 / (C0 * math.sqrt(c * c0))
        # A pattern drives every line it touches; the impedance quoted
        # is per line, so divide by the energy-weighted line count.
        return {"c": c, "c0": c0, "z_total": z,
                "er_eff": c / c0}


def _axis(keys: Sequence[float], lo: float, hi: float, h0: float,
          hmax: float, near: tuple[float, float], scale: float = 1.0) -> np.ndarray:
    """Grid lines on [lo, hi] through every key, from a size field.

    The local cell size is h0 at each key line and grows linearly with
    the distance to the nearest key (GROWTH per unit distance, times
    ``scale``). Outside the near zone it grows faster (FAR_GROWTH), so
    far boundaries cost few lines. Each interval between keys is marched with that size and
    then stretched to land exactly on the next key.
    """
    pts = np.array(sorted({float(k) for k in keys if lo <= k <= hi} | {lo, hi}))
    pts = pts[np.concatenate([[True], np.diff(pts) > 1e-15])]
    nlo, nhi = near

    def size(x: float) -> float:
        d = float(np.min(np.abs(pts - x)))
        hs = h0 + GROWTH * scale * d
        out = max(nlo - x, x - nhi, 0.0)
        hs += (FAR_GROWTH - GROWTH) * scale * out
        return min(hs, hmax)

    out = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        xs, x = [], a
        while True:
            x = x + size(x)
            if x >= b - 0.3 * size(b):
                break
            xs.append(x)
        if xs:
            # Stretch so the march ends on b with the last step sized
            # like its neighbour.
            span = xs[-1] - a
            target = b - a - size(b) if b - a > size(b) * 1.5 else span
            scale = target / span if span > 0 else 1.0
            out.extend(a + (np.array(xs) - a) * scale)
        out.append(b)
    return np.unique(np.array(out))


def _mesh(xs: CrossSection, resolution: float):
    """Build the grid: key lines at every edge, graded spacing between."""
    feat = xs.feature
    if feat <= 0:
        cands = []
        for m in xs.metals:
            if m.net >= 0:
                cands.append(m.x1 - m.x0)
                if m.y1 > m.y0:
                    cands.append(m.y1 - m.y0)
        for d in xs.dielectrics:
            cands.append(d.y1 - d.y0)
        feat = min(c for c in cands if c > 0)
    h0 = feat / resolution
    sig = [m for m in xs.metals if m.net >= 0]
    xmin = min(m.x0 for m in sig)
    xmax = max(m.x1 for m in sig)
    ys = [d.y0 for d in xs.dielectrics] + [d.y1 for d in xs.dielectrics]
    ys += [m.y0 for m in xs.metals] + [m.y1 for m in xs.metals]
    for c in xs.coatings:
        ys.append(c.surface_y + c.direction * c.thickness)
        for m in xs.metals:
            if abs(m.y0 - c.surface_y) < 1e-12 or abs(m.y1 - c.surface_y) < 1e-12:
                ys.append((m.y1 if c.direction > 0 else m.y0) + c.direction * c.thickness)
    struct_lo = min(ys) if xs.ground_bottom is None else xs.ground_bottom
    struct_hi = max(ys) if xs.ground_top is None else xs.ground_top
    height = max(struct_hi - struct_lo, feat)
    # Open boundaries are pushed far out. With a plane on both sides the
    # field decays exponentially sideways, so a few heights suffice.
    shielded = xs.ground_bottom is not None and xs.ground_top is not None
    side = (8.0 if shielded else 40.0) * height + (xmax - xmin)
    ylo = xs.ground_bottom if xs.ground_bottom is not None else struct_lo - 40.0 * height
    yhi = xs.ground_top if xs.ground_top is not None else struct_hi + 40.0 * height
    # Key lines two heights out bound the near field; only beyond them
    # does the grid grow at the far ratio.
    xkeys = [xmin - side, xmax + side]
    for m in xs.metals:
        t0, t1 = m.top()
        xkeys += [m.x0, m.x1, t0, t1]
        for c in xs.coatings:
            xkeys += [min(m.x0, t0) - c.thickness, max(m.x1, t1) + c.thickness]
    ykeys = ys + [ylo, yhi]
    scale = min(REF_RESOLUTION / resolution, 2.5)
    hmax = 4.0 * height * scale
    X = _axis(xkeys, xmin - side, xmax + side, h0, hmax,
              (xmin - 2.0 * height, xmax + 2.0 * height), scale)
    Y = _axis(ykeys, ylo, yhi, h0, hmax,
              (struct_lo - 2.0 * height, struct_hi + 2.0 * height), scale)
    return X, Y


def _materials(xs: CrossSection, X: np.ndarray, Y: np.ndarray):
    """Er and tan d per cell (cell centres)."""
    cx = 0.5 * (X[:-1] + X[1:])
    cy = 0.5 * (Y[:-1] + Y[1:])
    CX, CY = np.meshgrid(cx, cy, indexing="ij")
    er = np.ones_like(CX)
    td = np.zeros_like(CX)
    for d in xs.dielectrics:
        sel = (CY > d.y0) & (CY < d.y1)
        er[sel] = d.er
        td[sel] = d.tand
    for c in xs.coatings:
        if c.direction > 0:
            sel = (CY > c.surface_y) & (CY < c.surface_y + c.thickness)
        else:
            sel = (CY < c.surface_y) & (CY > c.surface_y - c.thickness)
        for m in xs.metals:
            on = abs(m.y0 - c.surface_y) < 1e-12 if c.direction > 0 else abs(m.y1 - c.surface_y) < 1e-12
            if not on:
                continue
            t0, t1 = m.top()
            lo, hi = min(m.x0, t0) - c.thickness, max(m.x1, t1) + c.thickness
            if c.direction > 0:
                band = (CX > lo) & (CX < hi) & (CY > m.y0) & (CY < m.y1 + c.thickness)
            else:
                band = (CX > lo) & (CX < hi) & (CY < m.y1) & (CY > m.y0 - c.thickness)
            sel = sel | band
        er[sel] = c.er
        td[sel] = c.tand
    return er, td


def _operator(X, Y, eps):
    """Sparse 5-point operator for -div(eps grad) on the node grid.

    Returns (rows, cols, vals, nx, ny); the per-edge coupling
    coefficients are also returned for the surface-flux integral.
    """
    nx, ny = len(X), len(Y)
    dx = np.diff(X)
    dy = np.diff(Y)
    # Horizontal edges (i,j)-(i+1,j): dual length is half of the cell
    # below plus half of the cell above, each with its own eps.
    epad = np.zeros((nx + 1, ny + 1))
    epad[1:-1, 1:-1] = eps
    dypad = np.concatenate([[0.0], dy, [0.0]])
    dxpad = np.concatenate([[0.0], dx, [0.0]])
    # cH[i, j] couples node (i,j) and (i+1,j), i in 0..nx-2, j in 0..ny-1
    cH = (epad[1:nx, 0:ny] * dypad[0:ny] * 0.5 +
          epad[1:nx, 1:ny + 1] * dypad[1:ny + 1] * 0.5) / dx[:, None]
    lenH = 0.5 * (dypad[0:ny] + dypad[1:ny + 1])[None, :].repeat(nx - 1, 0)
    # cV[i, j] couples node (i,j) and (i,j+1)
    cV = (epad[0:nx, 1:ny] * dxpad[0:nx, None] * 0.5 +
          epad[1:nx + 1, 1:ny] * dxpad[1:nx + 1, None] * 0.5) / dy[None, :]
    lenV = 0.5 * (dxpad[0:nx] + dxpad[1:nx + 1])[:, None].repeat(ny - 1, 1)
    return cH, cV, lenH, lenV


def _assemble(cH, cV, nx, ny):
    idx = np.arange(nx * ny).reshape(nx, ny)
    a = idx[:-1, :].ravel()
    b = idx[1:, :].ravel()
    c = cH.ravel()
    a2 = idx[:, :-1].ravel()
    b2 = idx[:, 1:].ravel()
    c2 = cV.ravel()
    ia = np.concatenate([a, a2])
    ib = np.concatenate([b, b2])
    cc = np.concatenate([c, c2])
    rows = np.concatenate([ia, ib, ia, ib])
    cols = np.concatenate([ia, ib, ib, ia])
    vals = np.concatenate([cc, cc, -cc, -cc])
    n = nx * ny
    return coo_matrix((vals, (rows, cols)), shape=(n, n)).tocsr()


def solve(xs: CrossSection, resolution: float = 10.0) -> Solution:
    """Solve the cross-section. ``resolution`` is cells per smallest feature."""
    X, Y = _mesh(xs, resolution)
    nx, ny = len(X), len(Y)
    er, td = _materials(xs, X, Y)
    PX, PY = np.meshgrid(X, Y, indexing="ij")
    tol = 1e-9 * (X[-1] - X[0])
    owner = np.full((nx, ny), -2, dtype=int)   # -2 free, -1 ground, k line
    # Box boundary is ground: either a real plane or the far boundary.
    owner[0, :] = -1
    owner[-1, :] = -1
    owner[:, 0] = -1
    owner[:, -1] = -1
    for m in xs.metals:
        sel = m.contains(PX, PY, tol)
        owner[sel] = m.net if m.net >= 0 else -1
    nets = sorted({m.net for m in xs.metals if m.net >= 0})
    flat = owner.ravel()
    free = np.where(flat == -2)[0]
    fixed = np.where(flat != -2)[0]

    results = {}
    surfaces = []
    for label, eps in (("diel", er * EPS0), ("vac", np.full_like(er, EPS0))):
        cH, cV, lenH, lenV = _operator(X, Y, eps)
        A = _assemble(cH, cV, nx, ny)
        Auu = A[free][:, free].tocsc()
        Auf = A[free][:, fixed]
        lu = splu(Auu, permc_spec="MMD_AT_PLUS_A",
                  options={"SymmetricMode": True})
        phis = []
        for k in nets:
            pf = (flat[fixed] == k).astype(float)
            phi = np.zeros(nx * ny)
            phi[fixed] = pf
            phi[free] = lu.solve(-(Auf @ pf))
            phis.append(phi)
        P = np.array(phis)
        AP = (A @ P.T)
        Cm = P @ AP
        results[label] = 0.5 * (Cm + Cm.T)
        if label == "diel":
            cHt, cVt, _, _ = _operator(X, Y, er * td * EPS0)
            At = _assemble(cHt, cVt, nx, ny)
            G = P @ (At @ P.T)
            results["G"] = 0.5 * (G + G.T)
        else:
            # Surface flux on every metal-to-free edge, for conductor loss.
            for phi in phis:
                ph = phi.reshape(nx, ny)
                fl, ln = [], []
                for coef, length, axis in ((cH, lenH, 0), (cV, lenV, 1)):
                    if axis == 0:
                        o1, o2 = owner[:-1, :], owner[1:, :]
                        d = ph[:-1, :] - ph[1:, :]
                    else:
                        o1, o2 = owner[:, :-1], owner[:, 1:]
                        d = ph[:, :-1] - ph[:, 1:]
                    # Only true metal surfaces: a metal node next to a
                    # free node. The far box is not metal, so edges into
                    # its boundary rows are excluded unless a real plane.
                    metal1 = o1 != -2
                    metal2 = o2 != -2
                    edge = metal1 ^ metal2
                    fl.append((coef * d)[edge])
                    ln.append(length[edge])
                surfaces.append((np.concatenate(fl), np.concatenate(ln)))
    C = results["diel"]
    Cv = results["vac"]
    L = MU0 * EPS0 * np.linalg.inv(Cv)
    return Solution(C=C, C0=Cv, G=results["G"], L=L, surface=surfaces,
                    nodes=nx * ny, nets=nets)


def conductor_loss_np_per_m(sol: Solution, v: Sequence[float], z_mode: float,
                            rs_ohm: float) -> float:
    """Conductor attenuation (Np/m) for a drive pattern ``v``.

    The vacuum solution's surface charge sets the current distribution.
    It is scaled so the line currents match the mode current V/Z, and the
    loss is Rs times |J|^2 integrated over all metal, the return planes
    included. Requires finite-thickness conductors: a zero-thickness
    edge has a non-integrable current singularity.
    """
    v = np.asarray(v, dtype=float)
    # Superpose the per-excitation vacuum solutions for this pattern.
    fl = sum(vk * sol.surface[k][0] for k, vk in enumerate(v) if vk)
    ln = sol.surface[0][1]
    q0 = sol.C0 @ v                   # vacuum charge per line
    i_vac = q0 * C0                   # TEM current in vacuum
    k = int(np.argmax(np.abs(v)))
    i_mode = v[k] / z_mode
    scale = i_mode / i_vac[k]
    j = fl * C0 * scale / ln           # surface current density, A/m
    p_loss = rs_ohm * float(np.sum(j * j * ln))
    p_t = float(np.sum(v * (i_vac * scale)))
    return p_loss / (2.0 * p_t)
