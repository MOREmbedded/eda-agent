# SPDX-License-Identifier: Apache-2.0
"""2D eddy-current solver: series impedance R(f) + jwL(f) of a section.

The usual shortcut for conductor loss takes the high-frequency current
distribution and multiplies by the surface resistance. That is right
once the skin depth is far below the copper thickness and wrong below
it, where most of a board's copper lives for the first few hundred MHz:
the current neither fills the copper uniformly nor sits on its surface.
This solver does not assume either. It solves the magneto-quasistatic
field across every conductor, the reference planes included, so the
skin effect, the proximity effect between traces and the crowding of
the return current under the trace come out of the field equation.

EQUATION. For a 2D problem the only field unknown is the axial vector
potential A_z. In copper the current density is

    J = sigma (u_c - j w A)

where u_c is the voltage drop per metre along conductor c, the same over
its whole cross-section. Ampere's law gives

    -div(1/mu grad A) + j w sigma A = sigma u_c

and each conductor's current is the integral of J over it. Discretised
by finite differences on a tensor grid (the dual-cell form used by the
electrostatic solver in ``engineering/toolkit/fdsolver.py``), with
K the stiffness matrix and S the conductivity-weighted cell areas:

    (K + j w S) A = S P u,      I = P^T S (P u - j w A)

Eliminating A leaves a small matrix Y with I = Y u, one row per
conductor. Every reference conductor is tied into one return
conductor, and with E mapping the signal currents onto all conductors
(the return carries minus their sum), the loop impedance per metre is

    Z = E^T Y^-1 E,     R(f) = Re Z,  L(f) = Im Z / w.

At low frequency this gives the DC resistance of each trace plus the
plane's, and the low-frequency inductance with current filling the
copper; at high frequency it gives the skin-effect resistance with
current crowding at the trace edges and in the plane under the trace.

GRID. Cells at every copper surface are a fraction of the skin depth at
the highest frequency solved, growing away from it. Above that
frequency the impedance follows the skin-effect asymptote fitted to
the last solved points (R = a sqrt(f) + b, internal reactance a sqrt(f)),
which is the exact form of the surface impedance of a good conductor.

The outer box is a perfect conductor (A = 0). Every current returns
inside the model, so the field falls off like a dipole's and the box is
placed far enough out not to matter.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
from scipy.sparse import coo_matrix, diags
from scipy.sparse.linalg import splu

from ..engineering.toolkit import fdsolver as fd
from ._threads import single_blas_thread
from .section import Section

MU0 = fd.MU0
MAX_NODES = 600_000


@dataclass
class Conductor:
    """A trapezoid of copper, its group: a trace index or ``-1`` for return."""

    metal: fd.Metal
    group: int
    sigma: float


@dataclass
class EddyResult:
    freqs: np.ndarray            # Hz, the solved frequencies
    Z: np.ndarray                # (nf, n, n) complex loop impedance per metre
    nodes: int
    f_top: float                 # highest frequency solved
    asymptote: dict              # a, b (n x n) of the sqrt(f) fit above f_top

    @property
    def R(self) -> np.ndarray:
        return self.Z.real

    @property
    def L(self) -> np.ndarray:
        w = 2 * np.pi * self.freqs
        return self.Z.imag / w[:, None, None]


def skin_depth(f: float, sigma: float) -> float:
    return math.sqrt(1.0 / (math.pi * f * MU0 * sigma)) if f > 0 else math.inf


def freq_for_depth(depth: float, sigma: float) -> float:
    return 1.0 / (math.pi * MU0 * sigma * depth * depth)


def conductors(section: Section) -> list[Conductor]:
    """Traces and the reference planes as copper bodies."""
    sig = section.copper.sigma
    out = [Conductor(t.metal(i), i, sig) for i, t in enumerate(section.traces)]
    xl = min(t.x - t.width / 2 for t in section.traces)
    xr = max(t.x + t.width / 2 for t in section.traces)
    h = section.height()
    reach = 25.0 * h + 0.5 * (xr - xl)
    xc = 0.5 * (xl + xr)
    tp = section.plane_thickness
    if section.plane_below is not None:
        y = section.plane_below
        out.append(Conductor(fd.Metal(xc - reach, xc + reach, y - tp, y, -1), -1, sig))
    if section.plane_above is not None:
        y = section.plane_above
        out.append(Conductor(fd.Metal(xc - reach, xc + reach, y, y + tp, -1), -1, sig))
    return out


def _grid(cond: Sequence[Conductor], h0: float, height: float, open_top: bool,
          growth: float = 1.0):
    xs = [c.metal.x0 for c in cond] + [c.metal.x1 for c in cond]
    xs += [v for c in cond for v in c.metal.top()]
    ys = [c.metal.y0 for c in cond] + [c.metal.y1 for c in cond]
    sig = [c for c in cond if c.group >= 0]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    pad = 6.0 * height
    xlo, xhi = xmin - pad, xmax + pad
    ylo = ymin - pad
    yhi = ymax + (30.0 * height if open_top else pad)
    sx0 = min(c.metal.x0 for c in sig) - 3 * height
    sx1 = max(c.metal.x1 for c in sig) + 3 * height
    hmax = 3.0 * height
    X = fd._axis(xs + [xlo, xhi], xlo, xhi, h0, hmax, (sx0, sx1), growth)
    Y = fd._axis(ys + [ylo, yhi], ylo, yhi, h0, hmax, (ymin - height, ymax + height), growth)
    return X, Y


@dataclass
class _Model:
    X: np.ndarray
    Y: np.ndarray
    K: object
    S: np.ndarray
    P: object
    free: np.ndarray
    ngroups: int


def _model(section: Section, h0: float, growth: float = 1.0) -> _Model:
    cond = conductors(section)
    open_top = section.plane_above is None
    X, Y = _grid(cond, h0, section.height(), open_top, growth)
    nx, ny = len(X), len(Y)
    if nx * ny > MAX_NODES:
        raise ValueError(f"the eddy-current grid would have {nx * ny} nodes "
                         f"(limit {MAX_NODES}); lower f_max or the resolution")
    cx = 0.5 * (X[:-1] + X[1:])
    cy = 0.5 * (Y[:-1] + Y[1:])
    CX, CY = np.meshgrid(cx, cy, indexing="ij")
    sigma = np.zeros_like(CX)
    group = np.full(CX.shape, -9, dtype=int)
    n = section.n
    for c in cond:
        sel = c.metal.contains(CX, CY, 0.0)
        sigma[sel] = c.sigma
        group[sel] = c.group if c.group >= 0 else n     # all returns are one group
    area = np.outer(np.diff(X), np.diff(Y))
    w = sigma * area / 4.0
    S = np.zeros((nx, ny))
    G = np.full((nx, ny), -9, dtype=int)
    for di in (0, 1):
        for dj in (0, 1):
            S[di:nx - 1 + di, dj:ny - 1 + dj] += w
            sl = (slice(di, nx - 1 + di), slice(dj, ny - 1 + dj))
            G[sl] = np.where(group >= 0, group, G[sl])
    cH, cV, _, _ = fd._operator(X, Y, np.full_like(CX, 1.0 / MU0))
    K = fd._assemble(cH, cV, nx, ny)
    boundary = np.zeros((nx, ny), dtype=bool)
    boundary[0, :] = boundary[-1, :] = True
    boundary[:, 0] = boundary[:, -1] = True
    free = np.where(~boundary.ravel())[0]
    Sf = S.ravel()[free]
    Gf = G.ravel()[free]
    rows = np.where(Gf >= 0)[0]
    P = coo_matrix((np.ones(len(rows)), (rows, Gf[rows])), shape=(len(free), n + 1)).tocsc()
    Kf = K[free][:, free].tocsc()
    return _Model(X, Y, Kf, Sf, P, free, n + 1)


def _loop_z(m: _Model, f: float, n: int) -> np.ndarray:
    w = 2 * math.pi * f
    A = (m.K + diags(1j * w * m.S)).tocsc()
    lu = splu(A, permc_spec="MMD_AT_PLUS_A")
    SPs = (diags(m.S) @ m.P).tocsc()
    Xs = lu.solve(SPs.toarray().astype(complex))
    PtSP = (m.P.T @ SPs).toarray()
    Yc = PtSP - 1j * w * np.asarray(SPs.T @ Xs)
    E = np.vstack([np.eye(n), -np.ones((1, n))])
    Zc = np.linalg.inv(Yc)
    return E.T @ Zc @ E


def solve(section: Section, f_max: float = 20e9, points: int = 10,
          depth_ratio: float = 20.0, cells_per_depth: float = 5.0,
          f_low: float = 1e3, growth: float = 1.0) -> EddyResult:
    """Loop impedance per metre from ``f_low`` up to the frequency where
    the skin depth is the thinnest copper / ``depth_ratio``; above that
    the skin-effect asymptote is fitted (``asymptote``)."""
    sigma = section.copper.sigma
    t_min = min([t.thickness for t in section.traces] + [section.plane_thickness])
    t_max = max([t.thickness for t in section.traces] + [section.plane_thickness])
    f_top = min(f_max, freq_for_depth(t_min / depth_ratio, sigma))
    f_onset = min(freq_for_depth(2.0 * t_max, sigma), f_top / 10.0)
    h0 = skin_depth(f_top, sigma) / cells_per_depth
    n = section.n
    freqs = np.concatenate([[f_low], np.geomspace(f_onset, f_top, max(points, 3))])
    with single_blas_thread():
        model = _model(section, h0, growth)
        Z = np.array([_loop_z(model, f, n) for f in freqs])
    # sqrt(f) asymptote from the last two points: R = a sqrt f + b.
    f1, f2 = freqs[-2], freqs[-1]
    r1, r2 = Z[-2].real, Z[-1].real
    a = (r2 - r1) / (math.sqrt(f2) - math.sqrt(f1))
    b = r2 - a * math.sqrt(f2)
    l_inf = Z[-1].imag / (2 * math.pi * f2) - a * math.sqrt(f2) / (2 * math.pi * f2)
    return EddyResult(freqs, Z, int(len(model.X) * len(model.Y)), float(f_top),
                      {"a": a, "b": b, "l_inf": l_inf})


def evaluate(res: EddyResult, f: np.ndarray, l_inf: Optional[np.ndarray] = None
             ) -> tuple[np.ndarray, np.ndarray]:
    """R(f) and L(f) (each (nf, n, n)) at any frequencies: interpolated
    in log f between solved points (R and L separately, both smooth),
    the asymptote above. ``l_inf`` replaces the external inductance the
    eddy grid gives with a better one (the electrostatic solve's)."""
    f = np.asarray(f, dtype=float)
    fs = res.freqs
    n = res.Z.shape[1]
    R = np.empty((len(f), n, n))
    L = np.empty((len(f), n, n))
    lf = np.log(np.maximum(fs, 1e-30))
    a, b, linf = res.asymptote["a"], res.asymptote["b"], res.asymptote["l_inf"]
    shift = (l_inf - linf) if l_inf is not None else np.zeros((n, n))
    from scipy.interpolate import PchipInterpolator
    for i in range(n):
        for j in range(n):
            pr = PchipInterpolator(lf, res.R[:, i, j])
            pl = PchipInterpolator(lf, res.L[:, i, j])
            for k, fk in enumerate(f):
                if fk <= fs[0]:
                    R[k, i, j] = res.R[0, i, j]
                    L[k, i, j] = res.L[0, i, j]
                elif fk <= fs[-1]:
                    R[k, i, j] = pr(math.log(fk))
                    L[k, i, j] = pl(math.log(fk))
                else:
                    sq = math.sqrt(fk)
                    R[k, i, j] = a[i, j] * sq + b[i, j]
                    L[k, i, j] = linf[i, j] + a[i, j] * sq / (2 * math.pi * fk)
    return R, L + shift[None, :, :]
