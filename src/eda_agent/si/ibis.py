# SPDX-License-Identifier: Apache-2.0
"""IBIS buffer models: read them, and reduce a driver to its edge and
impedance for the channel eye.

An IBIS file (ANSI/EIA-656) describes an I/O buffer by tables: the
output's current against voltage when it pulls up and when it pulls
down, the edge rate, and the die capacitance C_comp. This reader takes
what the eye needs from a ``[Model]``:

* C_comp (typ, min, max);
* the ramp: dV/dt of the rising and falling edge between 20 % and 80 %
  of the swing into the fixture load R_load;
* the [Pulldown] and [Pullup] V-I tables, whose slope at the output's
  resting point is the driver's output resistance.

The reduction is linear: a driver of that resistance and that edge,
with C_comp at the pad; a receiver as C_comp beside its termination.
That is right for a reflection and loss budget at the typical corner;
the nonlinear buffer (its V-I curve through the whole swing) is for a
full IBIS simulation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_SUFFIX = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6,
           "G": 1e9, "T": 1e12}
_NUM = re.compile(r"^([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)([a-zA-Z]*)")


def number(text: str) -> Optional[float]:
    """An IBIS number: 1.2e-3, 3.5pF, 120m, NA."""
    m = _NUM.match(text.strip())
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    if unit:
        scale = _SUFFIX.get(unit[0])
        if scale is not None and not unit.lower().startswith("meg"):
            val *= scale
        elif unit.lower().startswith("meg"):
            val *= 1e6
    return val


def _triple(words: list[str]) -> list[Optional[float]]:
    vals = [number(w) for w in words[:3]]
    while len(vals) < 3:
        vals.append(None)
    return vals


def _ratio(word: str) -> Optional[tuple[float, float]]:
    if "/" not in word:
        return None
    a, b = word.split("/", 1)
    dv, dt = number(a), number(b)
    if dv is None or dt is None or dt <= 0:
        return None
    return dv, dt


@dataclass
class BufferModel:
    name: str
    model_type: str = ""
    c_comp: list[Optional[float]] = field(default_factory=lambda: [None, None, None])
    ramp_r: Optional[tuple[float, float]] = None      # typ (dV, dt), 20-80 %
    ramp_f: Optional[tuple[float, float]] = None
    r_load: float = 50.0
    pulldown: list[tuple[float, float]] = field(default_factory=list)   # (V, I typ)
    pullup: list[tuple[float, float]] = field(default_factory=list)
    voltage: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "model_type": self.model_type, "c_comp_f": self.c_comp,
                "ramp_r": self.ramp_r, "ramp_f": self.ramp_f, "r_load": self.r_load,
                "pulldown_points": len(self.pulldown), "pullup_points": len(self.pullup),
                "voltage": self.voltage}


def parse(text: str) -> dict[str, BufferModel]:
    """Every [Model] of an IBIS file, typical columns."""
    models: dict[str, BufferModel] = {}
    cur: Optional[BufferModel] = None
    section = ""
    voltage: Optional[float] = None
    for raw in text.splitlines():
        line = raw.split("|", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("["):
            m = re.match(r"\s*\[([^\]]+)\]\s*(.*)", line)
            if not m:
                continue
            key = m.group(1).strip().lower()
            rest = m.group(2).strip()
            section = key
            if key == "model":
                cur = BufferModel(rest.split()[0] if rest else f"model{len(models) + 1}",
                                  voltage=voltage)
                models[cur.name] = cur
            elif key == "voltage range" and rest:
                v = number(rest.split()[0])
                if cur is not None:
                    cur.voltage = v
                voltage = v
            continue
        if cur is None:
            continue
        words = line.split()
        low = words[0].lower()
        if section == "model":
            if low == "model_type" and len(words) > 1:
                cur.model_type = words[1]
            elif low == "c_comp" and len(words) > 1:
                cur.c_comp = _triple(words[1:])
        elif section == "ramp":
            if low == "dv/dt_r" and len(words) > 1:
                cur.ramp_r = _ratio(words[1])
            elif low == "dv/dt_f" and len(words) > 1:
                cur.ramp_f = _ratio(words[1])
            elif low == "r_load" and len(words) > 1:
                cur.r_load = number(words[-1]) or 50.0
        elif section in ("pulldown", "pullup"):
            if len(words) >= 2:
                v, i = number(words[0]), number(words[1])
                if v is not None and i is not None:
                    (cur.pulldown if section == "pulldown" else cur.pullup).append((v, i))
        elif section == "voltage range" and words:
            v = number(words[0])
            if v is not None:
                cur.voltage = v
    return models


def read(path: str | Path) -> dict[str, BufferModel]:
    return parse(Path(path).read_text(encoding="utf-8", errors="replace"))


def _slope_r(table: list[tuple[float, float]], v0: float) -> Optional[float]:
    """dV/dI of a V-I table at v0, from the two points that straddle it."""
    pts = sorted(table)
    for (va, ia), (vb, ib) in zip(pts, pts[1:]):
        if va <= v0 <= vb and ib != ia:
            r = (vb - va) / (ib - ia)
            return abs(r) if r != 0 else None
    return None


def driver(model: BufferModel) -> dict[str, Any]:
    """Output resistance, 10-90 % rise time and C_comp of a driver."""
    out: dict[str, Any] = {"model": model.name}
    rs = [r for r in (_slope_r(model.pulldown, 0.0),
                      _slope_r(model.pullup, 0.0)) if r]
    if rs:
        out["r_out_ohms"] = sum(rs) / len(rs)
    ramp = model.ramp_r or model.ramp_f
    if ramp:
        dv, dt = ramp
        # dt is the 20-80 % time; a linear edge's 10-90 % time is 8/6 of it
        out["rise_s"] = dt * 0.8 / 0.6
        out["swing_v"] = dv / 0.6
    if model.c_comp[0] is not None:
        out["c_comp_f"] = model.c_comp[0]
    return out


def receiver(model: BufferModel) -> dict[str, Any]:
    out: dict[str, Any] = {"model": model.name}
    if model.c_comp[0] is not None:
        out["c_comp_f"] = model.c_comp[0]
    return out


def pick(models: dict[str, BufferModel], name: str = "") -> BufferModel:
    if not models:
        raise ValueError("no [Model] in the IBIS file")
    if name:
        if name not in models:
            raise ValueError(f"no model {name!r}; models: {', '.join(models)}")
        return models[name]
    if len(models) == 1:
        return next(iter(models.values()))
    raise ValueError(f"the file has {len(models)} models; name one: {', '.join(models)}")
