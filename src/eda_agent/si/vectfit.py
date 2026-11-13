# SPDX-License-Identifier: Apache-2.0
"""Rational macromodels of S-parameters: vector fitting and passivity.

A Touchstone file is a table: a time-domain simulator cannot use it as
it is. Vector fitting turns it into a sum of poles shared by every
entry,

    S(s) = D + sum_n R_n / (s - p_n),

which is causal and stable by construction (every pole in the left half
plane) and which SPICE can simulate with ordinary elements
(:mod:`.spice_export`).

The fit is relaxed vector fitting with common poles: B. Gustavsen and
A. Semlyen, "Rational approximation of frequency domain responses by
vector fitting", IEEE Trans. Power Delivery 14(3), 1999; B. Gustavsen,
"Improving the pole relocating properties of vector fitting", IEEE
Trans. Power Delivery 21(3), 2006; and the fast QR formulation of
D. Deschrijver et al., IEEE MWCL 18(6), 2008. Poles that stray to the
right half plane are flipped back.

A fitted model of a passive channel can still generate a little energy
between or beyond the samples. Passivity is checked on a dense grid
(the largest singular value of S must stay at or below 1) and enforced
by perturbing the residues, in the least-squares sense, by just what
pulls each violation under 1 while holding the in-band fit.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import numpy as np


@dataclass
class Model:
    poles: np.ndarray                  # (N,) complex; conjugate pairs adjacent
    residues: np.ndarray               # (N, P, P) complex
    d: np.ndarray                      # (P, P) real
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def ports(self) -> int:
        return self.d.shape[0]

    def eval(self, f: Sequence[float]) -> np.ndarray:
        s = 2j * math.pi * np.asarray(f, dtype=float)
        out = np.broadcast_to(self.d.astype(complex), (len(s),) + self.d.shape).copy()
        for p, r in zip(self.poles, self.residues):
            out += r[None] / (s[:, None, None] - p)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"poles": [[p.real, p.imag] for p in self.poles],
                "residues": [[[[x.real, x.imag] for x in row] for row in r]
                             for r in self.residues],
                "d": self.d.tolist(), "info": self.info}


def _is_pair(poles: np.ndarray, i: int) -> bool:
    return poles[i].imag > 0


def _basis(s: np.ndarray, poles: np.ndarray) -> np.ndarray:
    """Real-coefficient basis: one column per real pole, two per pair."""
    cols = []
    i = 0
    N = len(poles)
    while i < N:
        p = poles[i]
        if p.imag == 0:
            cols.append(1 / (s - p.real))
            i += 1
        else:
            a = 1 / (s - p)
            b = 1 / (s - np.conj(p))
            cols.append(a + b)
            cols.append(1j * a - 1j * b)
            i += 2
    return np.array(cols).T


def _initial_poles(w_min: float, w_max: float, n: int) -> np.ndarray:
    pairs = max(n // 2, 1)
    beta = np.linspace(max(w_min, w_max / 1000), w_max, pairs)
    out = []
    for b in beta:
        a = -b / 100
        out += [complex(a, b), complex(a, -b)]
    return np.array(out)


def _relocate(s: np.ndarray, H: np.ndarray, poles: np.ndarray) -> np.ndarray:
    """One pole-relocation step for responses H (K, M)."""
    K, M = H.shape
    Phi = _basis(s, poles)
    N = Phi.shape[1]
    rows = []
    for m in range(M):
        A = np.hstack([Phi, np.ones((K, 1)), -H[:, m:m + 1] * Phi, -H[:, m:m + 1]])
        Ar = np.vstack([A.real, A.imag])
        r = np.linalg.qr(Ar, mode="r")
        rows.append(r[N + 1:, N + 1:])
    # relaxation: the real sum of sigma over the samples is fixed
    sig = np.hstack([Phi, np.ones((K, 1))])
    relax = np.real(np.sum(sig, axis=0)) / K
    scale = np.linalg.norm(H) / K
    big = np.vstack(rows + [scale * relax[None, :]])
    b = np.zeros(big.shape[0])
    b[-1] = scale
    x, *_ = np.linalg.lstsq(big, b, rcond=None)
    c_t, d_t = x[:N], x[N]
    if abs(d_t) < 1e-8:
        d_t = 1e-8 if d_t >= 0 else -1e-8
    # zeros of sigma: eig(A - b c / d)
    Amat = np.zeros((N, N))
    bvec = np.zeros(N)
    i = j = 0
    while i < len(poles):
        p = poles[i]
        if p.imag == 0:
            Amat[j, j] = p.real
            bvec[j] = 1
            i += 1
            j += 1
        else:
            Amat[j:j + 2, j:j + 2] = [[p.real, p.imag], [-p.imag, p.real]]
            bvec[j:j + 2] = [2, 0]
            i += 2
            j += 2
    z = np.linalg.eigvals(Amat - np.outer(bvec, c_t) / d_t)
    z = np.where(z.real > 0, -z.real + 1j * z.imag, z)     # flip unstable poles
    return _order(z)


def _order(z: np.ndarray) -> np.ndarray:
    """Real poles first, then pairs (positive imaginary first), sorted."""
    real = sorted(float(x.real) for x in z if abs(x.imag) < 1e-9 * max(abs(x), 1.0))
    cplx = sorted((x for x in z if x.imag > 1e-9 * max(abs(x), 1.0)), key=lambda x: x.imag)
    out = [complex(r, 0) for r in real]
    for c in cplx:
        out += [c, np.conj(c)]
    return np.array(out, dtype=complex)


def _residues(s: np.ndarray, H: np.ndarray, poles: np.ndarray
              ) -> tuple[np.ndarray, np.ndarray]:
    """Least-squares residues (N, M) and constants (M,) for fixed poles."""
    Phi = _basis(s, poles)
    A = np.hstack([Phi, np.ones((len(s), 1))])
    Ar = np.vstack([A.real, A.imag])
    Br = np.vstack([H.real, H.imag])
    x, *_ = np.linalg.lstsq(Ar, Br, rcond=None)
    return _complex_residues(x[:-1], poles), x[-1]


def _complex_residues(x: np.ndarray, poles: np.ndarray) -> np.ndarray:
    out = np.zeros((len(poles),) + x.shape[1:], dtype=complex)
    i = j = 0
    while i < len(poles):
        if poles[i].imag == 0:
            out[i] = x[j]
            i += 1
            j += 1
        else:
            out[i] = x[j] + 1j * x[j + 1]
            out[i + 1] = x[j] - 1j * x[j + 1]
            i += 2
            j += 2
    return out


def _fit(f: np.ndarray, S: np.ndarray, n_poles: int, iterations: int) -> Model:
    P = S.shape[1]
    w0 = 2 * math.pi * float(f[-1])          # work in s / w0: columns of order 1
    s = 2j * math.pi * f / w0
    H = S.reshape(len(f), P * P)
    poles = _initial_poles(float(f[0] / f[-1]), 1.0, n_poles)
    for _ in range(iterations):
        poles = _relocate(s, H, poles)
    r, d = _residues(s, H, poles)
    return Model(poles * w0, r.reshape(len(poles), P, P) * w0, d.reshape(P, P).real)


def rms_error(model: Model, f: np.ndarray, S: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.abs(model.eval(f) - S) ** 2)))


def fit(f: Sequence[float], S: np.ndarray, n_poles: Optional[int] = None,
        tol: float = 2e-3, max_poles: int = 160, iterations: int = 12) -> Model:
    """Fit S (nf, P, P) at f (Hz). With no ``n_poles`` the order grows
    until the RMS error is under ``tol``. Reciprocal data stays
    reciprocal: S is symmetrised first."""
    f = np.asarray(f, dtype=float)
    S = np.asarray(S, dtype=complex)
    if f.ndim != 1 or S.shape[0] != len(f) or S.shape[1] != S.shape[2]:
        raise ValueError("S must be (frequencies, ports, ports)")
    if np.any(f <= 0):
        raise ValueError("vector fitting needs frequencies above 0")
    recip = float(np.max(np.abs(S - np.transpose(S, (0, 2, 1)))))
    if recip < 1e-6:
        S = 0.5 * (S + np.transpose(S, (0, 2, 1)))
    orders = [n_poles] if n_poles else list(range(8, max_poles + 1, 8))
    best: Optional[Model] = None
    for n in orders:
        m = _fit(f, S, n, iterations)
        m.info["rms_error"] = rms_error(m, f, S)
        m.info["n_poles"] = len(m.poles)
        if best is None or m.info["rms_error"] < best.info["rms_error"]:
            best = m
        if m.info["rms_error"] <= tol:
            break
    assert best is not None
    best.info["reciprocal"] = recip < 1e-6
    return best


# -- passivity ------------------------------------------------------------------------

def check_grid(f: np.ndarray) -> np.ndarray:
    """In-band samples, a log sweep below them and a stretch above."""
    lo = np.geomspace(max(f[0] / 1000, 1.0), f[0], 40, endpoint=False)
    mid = np.linspace(f[0], f[-1], 4 * len(f))
    hi = np.linspace(f[-1], 4 * f[-1], 200)[1:]
    return np.concatenate([lo, mid, hi])


def max_singular(model: Model, f: np.ndarray) -> tuple[float, float]:
    S = model.eval(f)
    sv = np.linalg.svd(S, compute_uv=False)[:, 0]
    k = int(np.argmax(sv))
    return float(sv[k]), float(f[k])


def enforce_passivity(model: Model, f_band: np.ndarray, S_band: np.ndarray,
                      margin: float = 1e-3, iterations: int = 30) -> Model:
    """Perturb residues and D until the largest singular value is at most
    1 - margin everywhere on the check grid, holding the band's fit."""
    grid = check_grid(np.asarray(f_band, dtype=float))
    P = model.ports
    poles = model.poles
    s_g = 2j * math.pi * grid
    s_b = 2j * math.pi * np.asarray(f_band, dtype=float)
    Phi_g = np.hstack([_basis(s_g, poles), np.ones((len(grid), 1))])
    Phi_b = np.hstack([_basis(s_b, poles), np.ones((len(s_b), 1))])
    # real coefficients of the current model, (Nr + 1, P*P)
    coeff = _real_coeffs(model)
    first = max_singular(model, grid)[0]
    for it in range(iterations):
        S = (Phi_g @ coeff).reshape(len(grid), P, P)
        U, sv, Vh = np.linalg.svd(S)
        bad = np.nonzero(sv[:, 0] > 1 - margin)[0]
        if not len(bad):
            break
        rows, rhs = [], []
        for k in bad:
            dS = np.zeros((P, P), dtype=complex)
            for i in range(P):
                if sv[k, i] > 1 - margin:
                    dS -= (sv[k, i] - (1 - margin * 1.5)) * np.outer(U[k, :, i], Vh[k, i, :])
            rows.append(Phi_g[k])
            rhs.append(dS.reshape(-1))
        A = np.array(rows)
        B = np.array(rhs)
        # hold the band: zero change there, lightly weighted
        wgt = 0.05
        A_all = np.vstack([A, wgt * Phi_b])
        B_all = np.vstack([B, np.zeros((len(Phi_b), P * P))])
        Ar = np.vstack([A_all.real, A_all.imag])
        Br = np.vstack([B_all.real, B_all.imag])
        dx, *_ = np.linalg.lstsq(Ar, Br, rcond=None)
        # keep reciprocity: symmetrise the change
        dx = 0.5 * (dx + dx.reshape(-1, P, P).transpose(0, 2, 1).reshape(dx.shape[0], -1))
        coeff = coeff + dx
    out = _from_real(poles, coeff, P)
    if max_singular(out, grid)[0] > first:
        # the perturbation made things worse (a poor fit far out of band):
        # keep the model as it was and let the caller's check report it
        out = _from_real(poles, _real_coeffs(model), P)
    out.info = dict(model.info)
    out.info["passivity_before"] = first
    out.info["passivity_after"] = max_singular(out, grid)[0]
    out.info["rms_error_after_enforcement"] = rms_error(out, np.asarray(f_band), S_band)
    out.info["enforcement_iterations"] = it + 1 if iterations else 0
    return out


def _real_coeffs(model: Model) -> np.ndarray:
    P = model.ports
    cols = []
    i = 0
    while i < len(model.poles):
        r = model.residues[i].reshape(-1)
        if model.poles[i].imag == 0:
            cols.append(r.real)
            i += 1
        else:
            cols.append(r.real)
            cols.append(r.imag)
            i += 2
    cols.append(model.d.reshape(-1))
    return np.array(cols).reshape(len(cols), P * P)


def _from_real(poles: np.ndarray, coeff: np.ndarray, P: int) -> Model:
    r = _complex_residues(coeff[:-1], poles)
    return Model(poles, r.reshape(len(poles), P, P), coeff[-1].reshape(P, P).real)


def macromodel(f: Sequence[float], S: np.ndarray, tol: float = 2e-3,
               n_poles: Optional[int] = None) -> Model:
    """Fit, then enforce passivity."""
    f = np.asarray(f, dtype=float)
    S = np.asarray(S, dtype=complex)
    m = fit(f, S, n_poles, tol)
    return enforce_passivity(m, f, S)
