# SPDX-License-Identifier: Apache-2.0
"""Steady temperature of a current-carrying trace: a 2D board cross-section.

IPC-2152 replaced the 1950s IPC-2221 chart with measurements that showed
what the old chart hid: a trace's temperature depends far more on the
board around it (thickness, planes, their distance, the laminate's
conductivity, air) than on the trace. The standard gives charts and
modifier charts for that; no closed formula. This module computes the
same physics directly, the way Brooks and Adam did to reproduce the
IPC-2152 data (D. Brooks, J. Adam, "PCB Trace and Via Currents and
Temperatures: The Complete Analysis", 2nd ed., 2017):

* the trace's Joule heat I^2 rho(T) / A per unit length, with copper's
  resistivity rising with its own temperature (iterated);
* conduction through an anisotropic laminate (in-plane and through-plane
  conductivity differ about 3:1 in FR-4), through copper planes at their
  depth (a plane is a 385 W/mK sheet and is the biggest single lever),
  and through solder mask;
* convection from both faces by the natural- and forced-convection
  correlations in eda_agent.thermal.physics, and radiation, each
  evaluated at the local surface temperature (iterated).

The trace is taken as long, so its cross-section is the whole problem
(IPC-2152's test traces were long for the same reason). Finite volumes,
cell-centred, on a graded grid; the nonlinear terms converge by Picard
iteration in 5-15 steps.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import spsolve

from ...thermal import physics as tp
from .fdsolver import _axis


@dataclass
class Plane:
    depth: float            # from the top surface to the plane's top, m
    thickness: float        # m
    coverage: float = 1.0   # copper fraction of the layer


@dataclass
class Board:
    thickness: float = 1.6e-3
    width: float = 0.1             # board extent across the trace, m
    length: float = 0.1            # board extent along the trace, m
    k_xy: float = 0.81
    k_z: float = 0.29
    planes: list[Plane] = field(default_factory=list)
    mask: float = 20e-6            # solder mask thickness on both faces
    emissivity: float = 0.9
    orientation: str = "horizontal"   # or "vertical"
    airflow: float = 0.0           # m/s
    ambient: float = 25.0          # C


@dataclass
class TraceResult:
    t_trace: float          # mean trace temperature, C
    rise: float             # over ambient, K
    power_per_m: float      # W/m
    r_per_m: float          # ohm/m at temperature
    iterations: int
    runaway: bool = False


def solve_trace(current: float, w: float, t: float, depth: float,
                board: Board, resolution: float = 4.0,
                max_iter: int = 60) -> TraceResult:
    """Temperature of one trace of width ``w``, copper ``t``, whose top is
    ``depth`` below the board's top face (0 = outer layer)."""
    H = board.thickness
    half = board.width / 2.0
    y_top = H                       # board spans y in [0, H]
    tr_y1 = y_top - depth
    tr_y0 = tr_y1 - t
    if tr_y0 < -1e-12:
        raise ValueError("the trace lies below the bottom of the board")
    feat = min(w, t, H / 8.0)
    h0 = feat / resolution
    xkeys = [-half, half, -w / 2, w / 2]
    ykeys = [0.0, H, tr_y0, tr_y1]
    for p in board.planes:
        ykeys += [y_top - p.depth, y_top - p.depth - p.thickness]
    X = _axis(xkeys, -half, half, h0, board.width / 20.0, (-4 * H - w, 4 * H + w))
    Y = _axis(ykeys, 0.0, H, h0, H / 6.0, (0.0, H))
    nx, ny = len(X) - 1, len(Y) - 1
    cx = 0.5 * (X[:-1] + X[1:])
    cy = 0.5 * (Y[:-1] + Y[1:])
    dx = np.diff(X)
    dy = np.diff(Y)
    CX, CY = np.meshgrid(cx, cy, indexing="ij")
    kx = np.full((nx, ny), board.k_xy)
    ky = np.full((nx, ny), board.k_z)
    for p in board.planes:
        y1 = y_top - p.depth
        y0 = y1 - p.thickness
        sel = (CY > y0) & (CY < y1)
        # A partly filled plane: copper and laminate in parallel along
        # the plane, in series-ish across it (taken as parallel too; the
        # plane is thin, so its through-plane resistance is negligible).
        kx[sel] = tp.K_COPPER * p.coverage + board.k_xy * (1 - p.coverage)
        ky[sel] = tp.K_COPPER * p.coverage + board.k_z * (1 - p.coverage)
    trace = (np.abs(CX) < w / 2) & (CY > tr_y0) & (CY < tr_y1)
    kx[trace] = tp.K_COPPER
    ky[trace] = tp.K_COPPER
    area = float(np.sum((dx[:, None] * dy[None, :])[trace]))
    if area <= 0:
        raise ValueError("the trace has no cross-section on the grid")

    idx = np.arange(nx * ny).reshape(nx, ny)
    # Interior conductances (harmonic mean of half-cells).
    gx = dy[None, :] / (dx[:-1, None] / (2 * kx[:-1, :]) + dx[1:, None] / (2 * kx[1:, :]))
    gy = dx[:, None] / (dy[None, :-1] / (2 * ky[:, :-1]) + dy[None, 1:] / (2 * ky[:, 1:]))
    rows = [idx[:-1, :].ravel(), idx[:, :-1].ravel()]
    cols = [idx[1:, :].ravel(), idx[:, 1:].ravel()]
    vals = [gx.ravel(), gy.ravel()]
    ia = np.concatenate(rows)
    ib = np.concatenate(cols)
    gv = np.concatenate(vals)
    n = nx * ny
    base = coo_matrix((np.concatenate([gv, gv, -gv, -gv]),
                       (np.concatenate([ia, ib, ia, ib]),
                        np.concatenate([ia, ib, ib, ia]))), shape=(n, n)).tocsr()

    # Surface: the mask adds a series resistance per face.
    r_mask = board.mask / tp.K_SOLDER_MASK if board.mask > 0 else 0.0
    top_cells = idx[:, -1]
    bot_cells = idx[:, 0]
    plate_area = board.width * board.length
    lc = (tp.plate_length(plate_area, 2 * (board.width + board.length))
          if board.orientation == "horizontal" else board.length)
    T = np.full(n, board.ambient + 10.0)
    ta = board.ambient
    runaway = False
    t_trace = ta
    it = 0
    for it in range(1, max_iter + 1):
        t_top = float(np.mean(T[top_cells]))
        t_bot = float(np.mean(T[bot_cells]))
        orient_top = "up" if board.orientation == "horizontal" else "vertical"
        orient_bot = "down" if board.orientation == "horizontal" else "vertical"
        h_t = (tp.h_convection(max(t_top, ta + 0.01), ta, lc, orient_top, board.airflow, board.length)
               + tp.h_radiation(t_top, ta, board.emissivity))
        h_b = (tp.h_convection(max(t_bot, ta + 0.01), ta, lc, orient_bot, board.airflow, board.length)
               + tp.h_radiation(t_bot, ta, board.emissivity))
        # Face conductance per cell: half-cell, mask, film.
        g_top = dx / (dy[-1] / (2 * ky[:, -1]) + r_mask + 1.0 / h_t)
        g_bot = dx / (dy[0] / (2 * ky[:, 0]) + r_mask + 1.0 / h_b)
        diag = np.zeros(n)
        diag[top_cells] += g_top
        diag[bot_cells] += g_bot
        A = base + coo_matrix((diag, (np.arange(n), np.arange(n))), shape=(n, n)).tocsr()
        rho = tp.rho_copper(t_trace)
        q_lin = current * current * rho / area          # W/m
        q_cell = np.zeros(n)
        cell_area = (dx[:, None] * dy[None, :]).ravel()
        q_cell[trace.ravel()] = q_lin * cell_area[trace.ravel()] / area
        rhs = q_cell + diag * ta
        T_new = spsolve(A.tocsc(), rhs)
        t_new = float(np.sum(T_new[trace.ravel()] * cell_area[trace.ravel()]) / area)
        if t_new > 1000.0:
            runaway = True
            T, t_trace = T_new, t_new
            break
        done = abs(t_new - t_trace) < 1e-4 * max(1.0, t_new - ta) and it > 2
        T, t_trace = T_new, 0.5 * (t_trace + t_new) if it > 3 else t_new
        if done:
            t_trace = t_new
            break
    rho = tp.rho_copper(t_trace)
    return TraceResult(t_trace=t_trace, rise=t_trace - ta,
                       power_per_m=current * current * rho / area,
                       r_per_m=rho / area, iterations=it, runaway=runaway)


def current_for_rise(rise: float, w: float, t: float, depth: float, board: Board) -> float:
    """Current that heats the trace by ``rise`` kelvin (bisection on log I)."""
    lo, hi = 1e-3, 1.0
    while solve_trace(hi, w, t, depth, board).rise < rise:
        lo, hi = hi, hi * 4.0
        if hi > 1e4:
            raise ValueError("no current below 10 kA reaches that rise")
    for _ in range(40):
        mid = math.sqrt(lo * hi)
        r = solve_trace(mid, w, t, depth, board)
        if r.runaway or r.rise > rise:
            hi = mid
        else:
            lo = mid
        if hi / lo < 1.0005:
            break
    return math.sqrt(lo * hi)


def width_for_current(current: float, rise: float, t: float, depth: float,
                      board: Board) -> float:
    """Narrowest width that keeps the rise at or below ``rise``."""
    lo, hi = 1e-6, 1e-3
    while True:
        r = solve_trace(current, hi, t, depth, board)
        if not r.runaway and r.rise <= rise:
            break
        lo, hi = hi, hi * 3.0
        if hi > board.width / 2:
            raise ValueError("no trace narrower than half the board meets that rise")
    for _ in range(40):
        mid = math.sqrt(lo * hi)
        r = solve_trace(current, mid, t, depth, board)
        if r.runaway or r.rise > rise:
            lo = mid
        else:
            hi = mid
        if hi / lo < 1.0005:
            break
    return hi
