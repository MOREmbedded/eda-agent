# SPDX-License-Identifier: Apache-2.0
"""Frequency-dependent RLGC matrices of a cross-section.

* C(f) and G(f): the electrostatic field solve with each dielectric's
  complex permittivity from the Djordjevic-Sarkar model, so the shunt
  admittance G + jwC is causal and the loss of every material, the solder
  mask included, is weighted by the field that is really in it. It is
  solved at a few frequencies (the model is smooth in log f) on two grids
  and extrapolated to zero cell size, as the impedance calculators do.
* R(f) and L(f): the eddy-current solve (:mod:`.eddy`), with its external
  inductance replaced by the electrostatic one, L = mu0 eps0 C0^-1, which
  is the more accurate of the two (both are reported, and they agree
  within the grid error). Copper roughness multiplies the internal
  (frequency-dependent) part of the impedance.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import splu

from ..engineering.toolkit import fdsolver as fd
from ..units import MM_PER_INCH
from . import eddy
from ._threads import single_blas_thread
from .section import Section

EPS0 = fd.EPS0
MU0 = fd.MU0
C_LIGHT = fd.C0
M_PER_IN = MM_PER_INCH / 1000.0


@dataclass
class RLGC:
    f: np.ndarray                 # Hz
    R: np.ndarray                 # (nf, n, n) ohm/m
    L: np.ndarray                 # H/m
    G: np.ndarray                 # S/m
    C: np.ndarray                 # F/m
    C0: np.ndarray                # (n, n) F/m in vacuum
    info: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return self.R.shape[1]

    def zy(self, k: int) -> tuple[np.ndarray, np.ndarray]:
        w = 2 * math.pi * self.f[k]
        return self.R[k] + 1j * w * self.L[k], self.G[k] + 1j * w * self.C[k]


# -- C(f), G(f) -----------------------------------------------------------------

def _operator_c(X, Y, eps):
    """``fdsolver._operator`` for a complex permittivity."""
    nx, ny = len(X), len(Y)
    dx = np.diff(X)
    dy = np.diff(Y)
    epad = np.zeros((nx + 1, ny + 1), dtype=complex)
    epad[1:-1, 1:-1] = eps
    dypad = np.concatenate([[0.0], dy, [0.0]])
    dxpad = np.concatenate([[0.0], dx, [0.0]])
    cH = (epad[1:nx, 0:ny] * dypad[0:ny] * 0.5 +
          epad[1:nx, 1:ny + 1] * dypad[1:ny + 1] * 0.5) / dx[:, None]
    cV = (epad[0:nx, 1:ny] * dxpad[0:nx, None] * 0.5 +
          epad[1:nx + 1, 1:ny] * dxpad[1:nx + 1, None] * 0.5) / dy[None, :]
    idx = np.arange(nx * ny).reshape(nx, ny)
    ia = np.concatenate([idx[:-1, :].ravel(), idx[:, :-1].ravel()])
    ib = np.concatenate([idx[1:, :].ravel(), idx[:, 1:].ravel()])
    cc = np.concatenate([cH.ravel(), cV.ravel()])
    rows = np.concatenate([ia, ib, ia, ib])
    cols = np.concatenate([ia, ib, ib, ia])
    vals = np.concatenate([cc, cc, -cc, -cc])
    n = nx * ny
    return coo_matrix((vals, (rows, cols)), shape=(n, n)).tocsr()


def _material_map(section: Section, X, Y) -> np.ndarray:
    """Per cell: 0 for air, k + 1 for laminate k of ``section.laminates()``."""
    lams = section.laminates()
    coded = section.to_fd(eps=[k + 2.0 for k in range(len(lams))], tand=[0.0] * len(lams))
    er, _ = fd._materials(coded, X, Y)
    return np.rint(er).astype(int) - 1


def capacitance(section: Section, freqs: Sequence[float], resolution: float = 40.0
                ) -> tuple[np.ndarray, np.ndarray, int]:
    """Complex C*(f) (nf, n, n) with eps = eps0 (Er' - j Er''), and C0.

    jw C* = G + jwC, so C = Re C* and G = -w Im C*."""
    xs = section.to_fd()
    X, Y = fd._mesh(xs, resolution)
    nx, ny = len(X), len(Y)
    if nx * ny > fd.MAX_NODES:
        raise ValueError(f"the cross-section needs {nx * ny} grid nodes; check the dimensions")
    mat = _material_map(section, X, Y)
    PX, PY = np.meshgrid(X, Y, indexing="ij")
    tol = 1e-9 * (X[-1] - X[0])
    owner = np.full((nx, ny), -2, dtype=int)
    owner[0, :] = owner[-1, :] = owner[:, 0] = owner[:, -1] = -1
    for m in xs.metals:
        owner[m.contains(PX, PY, tol)] = m.net
    flat = owner.ravel()
    free = np.where(flat == -2)[0]
    fixed = np.where(flat != -2)[0]
    nets = list(range(section.n))
    lams = section.laminates()

    def solve(eps):
        A = _operator_c(X, Y, eps)
        Auu = A[free][:, free].tocsc()
        Auf = A[free][:, fixed]
        lu = splu(Auu, permc_spec="MMD_AT_PLUS_A", options={"SymmetricMode": True})
        P = []
        for k in nets:
            pf = (flat[fixed] == k).astype(complex)
            phi = np.zeros(nx * ny, dtype=complex)
            phi[fixed] = pf
            phi[free] = lu.solve(-(Auf @ pf))
            P.append(phi)
        P = np.array(P)
        Cm = P @ (A @ P.T)
        return 0.5 * (Cm + Cm.T)

    out = []
    with single_blas_thread():
        for f in freqs:
            eps = np.ones(mat.shape, dtype=complex)
            for k, lam in enumerate(lams):
                eps[mat == k + 1] = lam.eps(f)
            out.append(solve(eps * EPS0))
        c0 = solve(np.full(mat.shape, EPS0, dtype=complex)).real
    return np.array(out), c0, nx * ny


def _extrapolated(section: Section, freqs, resolution: float, thin: bool):
    """C* and C0 on two grids, extrapolated to zero cell size."""
    c1, v1, _ = capacitance(section, freqs, resolution)
    c2, v2, nodes = capacitance(section, freqs, 2 * resolution)
    k = 1.0 / (2.0 ** (1.0 if thin else 4.0 / 3.0) - 1.0)
    return c2 + (c2 - c1) * k, v2 + (v2 - v1) * k, nodes, float(
        np.max(np.abs(np.diag((c2 - c1)[-1].real) * k / np.diag(c2[-1].real))))


# -- assembly -------------------------------------------------------------------

def _interp(fs: np.ndarray, vals: np.ndarray, f: np.ndarray) -> np.ndarray:
    """Pchip in log f, element by element, clamped at the ends."""
    lf = np.log(fs)
    out = np.empty((len(f),) + vals.shape[1:], dtype=vals.dtype)
    lq = np.log(np.clip(f, fs[0], fs[-1]))
    for idx in np.ndindex(*vals.shape[1:]):
        col = vals[(slice(None),) + idx]
        if np.iscomplexobj(col):
            out[(slice(None),) + idx] = (PchipInterpolator(lf, col.real)(lq)
                                         + 1j * PchipInterpolator(lf, col.imag)(lq))
        else:
            out[(slice(None),) + idx] = PchipInterpolator(lf, col)(lq)
    return out


def compute(section: Section, freqs: Sequence[float], resolution: float = 30.0,
            eddy_opts: Optional[dict[str, Any]] = None) -> RLGC:
    """RLGC at ``freqs`` (Hz, > 0)."""
    f = np.asarray(sorted(float(x) for x in freqs), dtype=float)
    if len(f) == 0 or f[0] <= 0:
        raise ValueError("frequencies must be more than 0")
    f_lo, f_hi = float(f[0]), float(f[-1])
    if f_hi / f_lo > 1.01:
        samples = np.geomspace(f_lo, f_hi, 7)
    else:
        samples = np.array([f_lo, f_lo * 1.001])
    lossless = all(lam.tand <= 0 for lam in section.laminates())
    if lossless:
        samples = samples[:2]
    cstar, c0, es_nodes, ext = _extrapolated(section, samples, resolution, thin=False)
    cs = _interp(samples, cstar, f)
    w = 2 * math.pi * f
    C = cs.real
    G = -cs.imag * w[:, None, None]
    L_es = MU0 * EPS0 * np.linalg.inv(c0)

    opts = {"f_max": max(f_hi, 1e6)}
    opts.update(eddy_opts or {})
    ed = eddy.solve(section, **opts)
    R, L = eddy.evaluate(ed, f, l_inf=L_es)
    # Roughness on the frequency-dependent (internal) part.
    r_dc = ed.Z[0].real
    rough = np.array([section.copper.rough(x) for x in f])
    if np.any(rough != 1.0):
        extra = (rough - 1.0)[:, None, None] * np.maximum(R - r_dc[None], 0.0)
        R = R + extra
        L = L + extra / w[:, None, None]
    info = {
        "es_nodes": es_nodes, "es_extrapolation": ext,
        "eddy_nodes": ed.nodes, "eddy_f_top_hz": ed.f_top,
        "eddy_l_inf_vs_electrostatic": float(np.max(np.abs(
            np.diag(ed.asymptote["l_inf"]) / np.diag(L_es) - 1.0))),
        "r_dc_ohm_per_m": np.diag(r_dc).tolist(),
    }
    return RLGC(f, R, L, G, C, c0, info)


# -- figures --------------------------------------------------------------------

def char_matrix(Z: np.ndarray, Y: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Modal decomposition: ZY = T diag(g^2) T^-1, Re g > 0.

    Returns (gamma (n,), T (n, n), Yc), Yc = Z^-1 T diag(g) T^-1 the
    characteristic admittance matrix."""
    lam, T = np.linalg.eig(Z @ Y)
    g = np.sqrt(lam.astype(complex))
    g = np.where(g.real < 0, -g, g)
    Yc = np.linalg.solve(Z, T @ np.diag(g) @ np.linalg.inv(T))
    return g, T, Yc


def figures(r: RLGC, k: int) -> dict[str, Any]:
    """Impedances, Er_eff, delay and loss at frequency index ``k``."""
    Z, Y = r.zy(k)
    f = r.f[k]
    w = 2 * math.pi * f
    out: dict[str, Any] = {"f_hz": f}
    C, C0 = r.C[k], r.C0
    # Quasi-static (lossless) figures from C(f) and C0: the impedance a
    # fab, Altium's stack manager and the closed forms quote. The full
    # figures below add the conductor's internal inductance and the loss.
    if r.n == 1:
        out.update(z0_qs=float(1 / (C_LIGHT * math.sqrt(C[0, 0] * C0[0, 0]))),
                   er_eff_qs=float(C[0, 0] / C0[0, 0]))
    elif r.n == 2:
        co, c0o = C[0, 0] - C[0, 1], C0[0, 0] - C0[0, 1]
        ce, c0e = C[0, 0] + C[0, 1], C0[0, 0] + C0[0, 1]
        zo = 1 / (C_LIGHT * math.sqrt(co * c0o))
        ze = 1 / (C_LIGHT * math.sqrt(ce * c0e))
        out.update(z_odd_qs=float(zo), z_even_qs=float(ze), z_diff_qs=float(2 * zo),
                   z_common_qs=float(ze / 2))
    if r.n == 1:
        z0 = np.sqrt(Z[0, 0] / Y[0, 0])
        g = np.sqrt(Z[0, 0] * Y[0, 0])
        beta = g.imag
        out.update(z0=float(z0.real), z0_imag=float(z0.imag),
                   er_eff=float((beta / (w / C_LIGHT)) ** 2),
                   delay_ps_per_in=float(beta / w * M_PER_IN * 1e12),
                   loss_db_per_in=float(8.685889638 * g.real * M_PER_IN))
        return out
    g, T, Yc = char_matrix(Z, Y)
    Zc = np.linalg.inv(Yc)
    out["zc_matrix"] = Zc.real.tolist()
    if r.n == 2:
        zodd = Zc[0, 0] - Zc[0, 1]
        zeven = Zc[0, 0] + Zc[0, 1]
        # Odd and even modes from the modal vectors (largest |v1 - v2|).
        order = np.argsort([abs(T[0, i] - T[1, i]) / (abs(T[0, i]) + abs(T[1, i])) for i in range(2)])
        ge, go = g[order[0]], g[order[1]]
        out.update(
            z_odd=float(zodd.real), z_even=float(zeven.real),
            z_diff=float(2 * zodd.real), z_common=float(zeven.real / 2),
            z_se=float(Zc[0, 0].real),
            er_eff_odd=float((go.imag / (w / C_LIGHT)) ** 2),
            er_eff_even=float((ge.imag / (w / C_LIGHT)) ** 2),
            delay_odd_ps_per_in=float(go.imag / w * M_PER_IN * 1e12),
            delay_even_ps_per_in=float(ge.imag / w * M_PER_IN * 1e12),
            loss_diff_db_per_in=float(8.685889638 * go.real * M_PER_IN),
            loss_common_db_per_in=float(8.685889638 * ge.real * M_PER_IN),
        )
    else:
        out["mode_delay_ps_per_in"] = sorted(float(x.imag / w * M_PER_IN * 1e12) for x in g)
        out["mode_loss_db_per_in"] = sorted(float(8.685889638 * x.real * M_PER_IN) for x in g)
        out["z_se"] = [float(Zc[i, i].real) for i in range(r.n)]
    return out


def to_dict(r: RLGC, every: int = 1) -> dict[str, Any]:
    """Per-unit-length matrices, for a file or a reply (``every``-th point)."""
    idx = list(range(0, len(r.f), every))
    if idx[-1] != len(r.f) - 1:
        idx.append(len(r.f) - 1)
    return {"unit": "per metre", "f_hz": [float(r.f[i]) for i in idx],
            "R_ohm": [r.R[i].tolist() for i in idx], "L_h": [r.L[i].tolist() for i in idx],
            "G_s": [r.G[i].tolist() for i in idx], "C_f": [r.C[i].tolist() for i in idx]}
