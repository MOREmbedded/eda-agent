# SPDX-License-Identifier: Apache-2.0
"""S-parameters of uniform coupled transmission lines.

For n coupled lines with per-unit-length Z = R + jwL and Y = G + jwC,
the telegrapher's equations decouple in the modes of ZY:

    ZY = T diag(g^2) T^-1,     Yc = Z^-1 T diag(g) T^-1

and the 2n-port admittance matrix of a length l is exact:

    Y11 = Y22 =  Yc T coth(g l) T^-1
    Y12 = Y21 = -Yc T csch(g l) T^-1

(C. R. Paul, "Analysis of Multiconductor Transmission Lines", 2nd ed.,
Wiley 2008, ch. 7.) Ports 1..n are the near ends of lines 1..n and ports
n+1..2n their far ends, the order Touchstone files of coupled lines use.
S = (1 + Z0 Y)^-1 (1 - Z0 Y) for a reference impedance Z0.

Mixed-mode for a pair follows the usual definition: port pairs (1, 2)
and (3, 4) give differential d1, d2 and common c1, c2 with
v_d = (v_p - v_n) / sqrt 2.
"""
from __future__ import annotations

import math

import numpy as np

from .rlgc import RLGC, char_matrix


def _coth_csch(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """coth and csch, stable for large Re x (a long lossy line)."""
    e = np.exp(-2.0 * x)
    coth = (1 + e) / (1 - e)
    csch = 2.0 * np.exp(-x) / (1 - e)
    return coth, csch


def y_params(r: RLGC, length: float) -> np.ndarray:
    """(nf, 2n, 2n) admittance matrices of a line of ``length`` metres."""
    n = r.n
    out = np.empty((len(r.f), 2 * n, 2 * n), dtype=complex)
    for k in range(len(r.f)):
        Z, Y = r.zy(k)
        g, T, Yc = char_matrix(Z, Y)
        Ti = np.linalg.inv(T)
        coth, csch = _coth_csch(g * length)
        A = Yc @ T @ np.diag(coth) @ Ti
        B = -Yc @ T @ np.diag(csch) @ Ti
        out[k, :n, :n] = A
        out[k, n:, n:] = A
        out[k, :n, n:] = B
        out[k, n:, :n] = B
    return out


def s_params(r: RLGC, length: float, z_ref: float = 50.0) -> np.ndarray:
    """(nf, 2n, 2n) S-parameters referenced to ``z_ref`` ohms."""
    Yp = y_params(r, length)
    m = Yp.shape[1]
    eye = np.eye(m)
    out = np.empty_like(Yp)
    for k in range(len(Yp)):
        zy = z_ref * Yp[k]
        out[k] = np.linalg.solve(eye + zy, eye - zy)
    return out


def mixed_mode(s: np.ndarray) -> np.ndarray:
    """4-port single-ended S (ports 1,2 near P,N; 3,4 far P,N) to mixed
    mode, ordered (d1, d2, c1, c2): Sdd in the top left 2x2."""
    a = 1 / math.sqrt(2)
    M = np.array([[a, -a, 0, 0], [0, 0, a, -a], [a, a, 0, 0], [0, 0, a, a]])
    return np.einsum("ij,fjk,lk->fil", M, s, M)


def db(x) -> np.ndarray:
    return 20 * np.log10(np.maximum(np.abs(x), 1e-300))
