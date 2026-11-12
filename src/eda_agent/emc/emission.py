# SPDX-License-Identifier: Apache-2.0
"""Radiated emission estimates and the limit lines they are judged against.

Differential-mode radiation of a small current loop over a reflecting
ground (an open-area test site), after Ott, *Electromagnetic Compatibility
Engineering* (2009), eq. 12-3:

    E = 263e-16 * f^2 * A * I / r            [V/m; Hz, m^2, A, m]

A trace over its reference plane is such a loop: its area is the trace
length times the height to the plane. The current is a trapezoid wave;
its n-th harmonic has the one-sided amplitude (Paul, *Introduction to
Electromagnetic Compatibility*, 2006, ch. 3)

    I_n = 2 I d |sinc(n pi d)| |sinc(n pi t_r / T)|,   sinc(x) = sin(x)/x

with d the duty cycle, t_r the rise time and T the period.

This is an estimate for ranking nets and catching an obvious problem, not
a compliance prediction: common-mode current on cables, which no board
geometry alone can give, usually dominates a real scan.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

#: Ott eq. 12-3 constant, ground reflection included.
OTT_K = 263e-16

#: Limit lines, quasi-peak below 1 GHz, average above. Each entry:
#: (distance in m, [(f_lo Hz, f_hi Hz, dBuV/m), ...]).
LIMITS: dict[str, tuple[float, list[tuple[float, float, float]]]] = {
    "cispr32_b_3m": (3.0, [(30e6, 230e6, 40.0), (230e6, 1e9, 47.0),
                           (1e9, 3e9, 50.0), (3e9, 6e9, 54.0)]),
    "cispr32_b_10m": (10.0, [(30e6, 230e6, 30.0), (230e6, 1e9, 37.0)]),
    "cispr32_a_3m": (3.0, [(30e6, 230e6, 50.0), (230e6, 1e9, 57.0),
                           (1e9, 3e9, 56.0), (3e9, 6e9, 60.0)]),
    "cispr32_a_10m": (10.0, [(30e6, 230e6, 40.0), (230e6, 1e9, 47.0)]),
    "fcc15_b_3m": (3.0, [(30e6, 88e6, 40.0), (88e6, 216e6, 43.5),
                         (216e6, 960e6, 46.0), (960e6, 40e9, 54.0)]),
    "fcc15_a_10m": (10.0, [(30e6, 88e6, 39.0), (88e6, 216e6, 43.5),
                           (216e6, 960e6, 46.4), (960e6, 40e9, 49.5)]),
}
DEFAULT_LIMIT = "cispr32_b_3m"


def limit_at(name: str, f_hz: float) -> Optional[float]:
    """The limit in dBuV/m at ``f_hz``, or None outside its range."""
    _, bands = LIMITS[name]
    for lo, hi, value in bands:
        if lo <= f_hz <= hi:
            return value
    return None


def db_uv(v_per_m: float) -> float:
    return 20 * math.log10(max(v_per_m, 1e-30) / 1e-6)


def loop_field(f_hz: float, area_m2: float, current_a: float, r_m: float) -> float:
    """Ott eq. 12-3: field of a small loop at ``r_m``, in V/m."""
    return OTT_K * f_hz * f_hz * area_m2 * current_a / r_m


def _sinc(x: float) -> float:
    return 1.0 if x == 0 else math.sin(x) / x


def trapezoid_harmonic(n: int, amplitude: float, duty: float, rise_s: float,
                       period_s: float) -> float:
    """One-sided amplitude of the n-th harmonic of a trapezoid wave."""
    return (2 * amplitude * duty * abs(_sinc(n * math.pi * duty))
            * abs(_sinc(n * math.pi * rise_s / period_s)))


@dataclass
class Drive:
    """How a net is driven, for the emission estimate."""
    freq_hz: float
    rise_s: float = 0.0          # 0: 10 % of the period, capped at 1 ns
    volts: float = 3.3
    duty: float = 0.5
    z_ohms: float = 50.0         # the current is volts / z_ohms

    @classmethod
    def from_dict(cls, d: dict) -> "Drive":
        rise = d.get("rise_ns")
        return cls(freq_hz=float(d.get("freq_hz") or d.get("freq_mhz", 0) * 1e6),
                   rise_s=float(rise) * 1e-9 if rise not in (None, "") else 0.0,
                   volts=float(d.get("volts", 3.3)),
                   duty=float(d.get("duty", 0.5)),
                   z_ohms=float(d.get("z_ohms", 50.0)))

    def rise(self) -> float:
        if self.rise_s > 0:
            return self.rise_s
        return min(0.1 / self.freq_hz, 1e-9)


def spectrum(area_m2: float, drive: Drive, limit: str = DEFAULT_LIMIT,
             f_max_hz: float = 6e9, max_harmonics: int = 2000) -> dict:
    """Every harmonic inside the limit's range: field, limit and margin.

    Returns the worst (smallest margin) harmonic and the list, worst first.
    """
    r_m, bands = LIMITS[limit]
    lo = bands[0][0]
    hi = min(bands[-1][1], f_max_hz)
    period = 1.0 / drive.freq_hz
    current = drive.volts / drive.z_ohms
    rows = []
    n = max(1, math.ceil(lo / drive.freq_hz))
    while n <= max_harmonics and n * drive.freq_hz <= hi:
        f = n * drive.freq_hz
        i_n = trapezoid_harmonic(n, current, drive.duty, drive.rise(), period)
        if i_n > 0:
            e = db_uv(loop_field(f, area_m2, i_n, r_m))
            lim = limit_at(limit, f)
            if lim is not None:
                rows.append({"harmonic": n, "f_mhz": round(f / 1e6, 3),
                             "current_ma": round(i_n * 1e3, 4),
                             "e_dbuv_m": round(e, 1), "limit_dbuv_m": lim,
                             "margin_db": round(lim - e, 1)})
        n += 1
    rows.sort(key=lambda r: r["margin_db"])
    return {"limit": limit, "distance_m": r_m,
            "worst": rows[0] if rows else None, "harmonics": rows}
