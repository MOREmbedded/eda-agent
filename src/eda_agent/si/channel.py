# SPDX-License-Identifier: Apache-2.0
"""A channel as a cascade of blocks: lines, vias, stubs, lumped parts.

Every block is a 2n-port for n conductors (1 for a single net, 2 for a
pair), held as its ABCD (chain) matrix at each frequency:

    [V1]   [A B] [V2]
    [I1] = [C D] [I2]      I1 into port 1, I2 out of port 2.

Chain matrices multiply, so a channel is their product, and S follows at
the end for any reference impedance. A uniform line of per-unit-length
Z = R + jwL and Y = G + jwC, with Gamma = sqrt(ZY) = T diag(g) T^-1, is
exactly

    A = cosh(Gamma l)              B = sinh(Gamma l) Gamma^-1 Z
    C = Z^-1 Gamma sinh(Gamma l)   D = Z^-1 cosh(Gamma l) Z

(the matrix telegrapher's solution, C. R. Paul, "Analysis of
Multiconductor Transmission Lines", ch. 7). A stub hangs off the path as
a shunt admittance: an open-ended line's input admittance is C A^-1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from .rlgc import RLGC


def _fun(T: np.ndarray, Ti: np.ndarray, d: np.ndarray) -> np.ndarray:
    return T @ np.diag(d) @ Ti


def line_abcd(Z: np.ndarray, Y: np.ndarray, length: float) -> np.ndarray:
    """(2n, 2n) chain matrix of ``length`` metres of a uniform line."""
    n = Z.shape[0]
    lam, T = np.linalg.eig(Z @ Y)
    g = np.sqrt(lam.astype(complex))
    g = np.where(g.real < 0, -g, g)
    Ti = np.linalg.inv(T)
    gl = g * length
    ch = _fun(T, Ti, np.cosh(gl))
    # sinh(g l) / g, finite as g -> 0
    shg = np.where(np.abs(g) > 1e-30, np.sinh(gl) / np.where(g == 0, 1, g), length)
    sh_over = _fun(T, Ti, shg)
    gsh = _fun(T, Ti, g * np.sinh(gl))
    Zi = np.linalg.inv(Z)
    out = np.empty((2 * n, 2 * n), dtype=complex)
    out[:n, :n] = ch
    out[:n, n:] = sh_over @ Z
    out[n:, :n] = Zi @ gsh
    out[n:, n:] = Zi @ ch @ Z
    return out


def shunt(Ysh: np.ndarray) -> np.ndarray:
    n = Ysh.shape[0]
    out = np.eye(2 * n, dtype=complex)
    out[n:, :n] = Ysh
    return out


def series(Zse: np.ndarray) -> np.ndarray:
    n = Zse.shape[0]
    out = np.eye(2 * n, dtype=complex)
    out[:n, n:] = Zse
    return out


def open_stub_admittance(abcd: np.ndarray) -> np.ndarray:
    """Input admittance of a block left open at its far end: C A^-1."""
    n = abcd.shape[0] // 2
    return abcd[n:, :n] @ np.linalg.inv(abcd[:n, :n])


def per_conductor(blocks: Sequence[np.ndarray]) -> np.ndarray:
    """One 2-port chain matrix per conductor, joined as an uncoupled 2n-port."""
    n = len(blocks)
    out = np.zeros((2 * n, 2 * n), dtype=complex)
    for i, b in enumerate(blocks):
        out[i, i], out[i, n + i] = b[0, 0], b[0, 1]
        out[n + i, i], out[n + i, n + i] = b[1, 0], b[1, 1]
    return out


def abcd_to_s(abcd: np.ndarray, z_ref: float | Sequence[float] = 50.0) -> np.ndarray:
    """S of a 2n-port chain matrix; ports 1..n near end, n+1..2n far end.

    Solved from the waves directly, a = (V + Z I) / 2 sqrt Z and
    b = (V - Z I) / 2 sqrt Z, so a block with no series element (a shunt
    alone, whose B is 0) converts as well as any other."""
    n = abcd.shape[0] // 2
    A, B, C, D = abcd[:n, :n], abcd[:n, n:], abcd[n:, :n], abcd[n:, n:]
    z = np.broadcast_to(np.asarray(z_ref, dtype=float), (2 * n,))
    r1, r2 = np.diag(np.sqrt(z[:n])), np.diag(np.sqrt(z[n:]))
    i1, i2 = np.diag(1 / np.sqrt(z[:n])), np.diag(1 / np.sqrt(z[n:]))
    M = np.block([[r1, -(A @ r2 + B @ i2)], [-i1, -(C @ r2 + D @ i2)]])
    N = np.block([[-r1, A @ r2 - B @ i2], [-i1, C @ r2 - D @ i2]])
    return np.linalg.solve(M, N)


@dataclass
class Block:
    """One element of a channel. ``abcd(k)`` gives its chain matrix at
    frequency index ``k``; ``label`` and ``info`` say what it is."""

    kind: str
    label: str
    mats: np.ndarray                 # (nf, 2n, 2n)
    info: dict[str, Any] = field(default_factory=dict)


def line_block(r: RLGC, length: float, label: str = "", **info) -> Block:
    mats = np.stack([line_abcd(*r.zy(k), length) for k in range(len(r.f))])
    return Block("line", label, mats, {"length_m": length, **info})


def stub_block(r: RLGC, length: float, label: str = "", **info) -> Block:
    mats = np.stack([shunt(open_stub_admittance(line_abcd(*r.zy(k), length)))
                     for k in range(len(r.f))])
    return Block("stub", label, mats, {"length_m": length, **info})


@dataclass
class Channel:
    f: np.ndarray
    n: int
    blocks: list[Block] = field(default_factory=list)

    def add(self, block: Block) -> None:
        if block.mats.shape != (len(self.f), 2 * self.n, 2 * self.n):
            raise ValueError(f"block {block.label or block.kind} has shape "
                             f"{block.mats.shape}, the channel needs "
                             f"{(len(self.f), 2 * self.n, 2 * self.n)}")
        self.blocks.append(block)

    def abcd(self) -> np.ndarray:
        out = np.broadcast_to(np.eye(2 * self.n, dtype=complex),
                              (len(self.f), 2 * self.n, 2 * self.n)).copy()
        for b in self.blocks:
            out = out @ b.mats
        return out

    def s(self, z_ref: float | Sequence[float] = 50.0) -> np.ndarray:
        m = self.abcd()
        return np.stack([abcd_to_s(m[k], z_ref) for k in range(len(self.f))])

    def delay(self) -> float:
        """Sum of the blocks' own delays (s), where they state one."""
        return float(sum(b.info.get("delay_s", 0.0) for b in self.blocks))


def uniform_band(f_max: float, points: int) -> np.ndarray:
    """``points`` frequencies df, 2 df, ... f_max: the grid a TDR needs."""
    if f_max <= 0 or points < 2:
        raise ValueError("f_max must be > 0 and points >= 2")
    return np.linspace(f_max / points, f_max, points)


def nyquist_index(f: np.ndarray, f_target: float) -> int:
    return int(np.argmin(np.abs(f - f_target)))


def passivity(s: np.ndarray) -> float:
    """Largest singular value of S over the band (<= 1 for a passive block)."""
    return float(max(np.linalg.svd(s[k], compute_uv=False)[0] for k in range(len(s))))


def reciprocity(s: np.ndarray) -> float:
    return float(np.max(np.abs(s - np.transpose(s, (0, 2, 1)))))


def length_m(blocks: Sequence[Block]) -> float:
    return float(sum(b.info.get("length_m", 0.0) for b in blocks if b.kind == "line"))


def db(x) -> np.ndarray:
    return 20 * np.log10(np.maximum(np.abs(x), 1e-300))


__all__ = ["line_abcd", "shunt", "series", "open_stub_admittance", "per_conductor",
           "abcd_to_s", "Block", "line_block", "stub_block", "Channel", "uniform_band",
           "nyquist_index", "passivity", "reciprocity", "length_m", "db"]
