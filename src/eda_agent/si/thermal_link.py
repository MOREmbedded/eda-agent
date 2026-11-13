# SPDX-License-Identifier: Apache-2.0
"""Copper temperature into the line models.

Copper's resistivity rises by 0.393 % per kelvin (IEC 60028, annealed
copper, referred to 20 degC, the temperature of the 5.8e7 S/m the line
solvers use). A hot trace loses more:

* at DC the resistance scales with the resistivity, (1 + alpha dT);
* in the skin-effect regime the current crowds into a skin depth that
  grows with sqrt(rho), so R scales with sqrt(1 + alpha dT).

Between the two, R(f) is scaled by (1 + alpha dT) ** (1/2 + R_dc / (2 R(f))):
the whole factor where R(f) is R_dc, the square root where R(f) is many
times it. The internal inductance and the roughness factor are left as
they are; both change by much less.

The temperature comes from a uniform value or from the thermal
analysis' ``result.npz`` (one temperature map per copper layer), sampled
along each trace section.
"""
from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from .rlgc import RLGC

ALPHA_CU = 0.00393          # 1/K at 20 degC
T_REF_C = 20.0


def scale_r(r: RLGC, temp_c: float) -> RLGC:
    """The line at ``temp_c``: R(f) scaled as the module says."""
    dt = float(temp_c) - T_REF_C
    if abs(dt) < 1e-9:
        return r
    s_dc = 1 + ALPHA_CU * dt
    if s_dc <= 0:
        raise ValueError(f"{temp_c} degC is below what copper's linear law covers")
    R = np.array(r.R, dtype=float, copy=True)
    n = R.shape[1]
    for i in range(n):
        rdc = max(float(R[0, i, i]), 1e-12)
        x = np.maximum(R[:, i, i] / rdc, 1.0)
        R[:, i, i] = R[:, i, i] * s_dc ** (0.5 + 0.5 / x)
    # mutual resistance (shared return) follows the same law
    for i in range(n):
        for j in range(n):
            if i != j:
                R[:, i, j] = R[:, i, j] * math.sqrt(
                    (R[:, i, i] / np.maximum(r.R[:, i, i], 1e-30)).mean()
                    * (R[:, j, j] / np.maximum(r.R[:, j, j], 1e-30)).mean())
    out = replace(r, R=R, info={**r.info, "temperature_c": float(temp_c)})
    return out


class Temperatures:
    """Copper temperature by layer and place."""

    def __init__(self, uniform_c: Optional[float] = None, npz: Optional[str] = None):
        self.uniform = uniform_c
        self.maps: dict[str, np.ndarray] = {}
        self.x0 = self.y0 = 0.0
        self.cell = 1.0
        self.source = ""
        if npz:
            with np.load(Path(npz), allow_pickle=False) as d:
                self.x0, self.y0 = float(d["x0"]), float(d["y0"])
                self.cell = float(d["cell_mils"])
                for name in d["layers"]:
                    self.maps[str(name)] = np.asarray(d[f"T_{name}"], dtype=float)
            self.source = Path(npz).name
        elif uniform_c is not None:
            self.source = f"{uniform_c:g} degC"

    @property
    def active(self) -> bool:
        return bool(self.maps) or self.uniform is not None

    def along(self, layer: str, pts: Sequence[tuple[float, float]]) -> Optional[float]:
        """Mean copper temperature along a polyline on ``layer``; the
        uniform value where the map has no such layer or no copper."""
        t = self.maps.get(layer)
        if t is None or len(pts) < 1:
            return self.uniform
        vals = []
        for (ax, ay), (bx, by) in zip(pts, list(pts[1:]) or list(pts)):
            n = max(int(math.dist((ax, ay), (bx, by)) / self.cell) + 1, 1)
            for k in range(n + 1):
                x = ax + (bx - ax) * k / n
                y = ay + (by - ay) * k / n
                ix = int((x - self.x0) / self.cell)
                iy = int((y - self.y0) / self.cell)
                if 0 <= iy < t.shape[0] and 0 <= ix < t.shape[1] and np.isfinite(t[iy, ix]):
                    vals.append(float(t[iy, ix]))
        if not vals:
            return self.uniform
        return float(np.mean(vals))

    def of_layer(self, layer: str) -> Optional[float]:
        t = self.maps.get(layer)
        if t is None or not np.isfinite(t).any():
            return self.uniform
        return float(np.nanmean(t))


__all__ = ["ALPHA_CU", "T_REF_C", "scale_r", "Temperatures"]
