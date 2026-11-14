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
    gnd_clamp: list[tuple[float, float]] = field(default_factory=list)
    power_clamp: list[tuple[float, float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "model_type": self.model_type, "c_comp_f": self.c_comp,
                "ramp_r": self.ramp_r, "ramp_f": self.ramp_f, "r_load": self.r_load,
                "pulldown_points": len(self.pulldown), "pullup_points": len(self.pullup),
                "clamp_points": len(self.gnd_clamp) + len(self.power_clamp),
                "voltage": self.voltage}


#: The V-I tables kept, by keyword. [Pullup] and [POWER Clamp] voltages are
#: measured down from the supply: Vtable = Vcc - Vout.
_TABLES = {"pulldown": "pulldown", "pullup": "pullup", "gnd clamp": "gnd_clamp",
           "power clamp": "power_clamp"}

#: Model types that do not drive a line.
NOT_DRIVERS = ("input", "terminator", "series", "series_switch")


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
        elif section in _TABLES:
            if len(words) >= 2:
                v, i = number(words[0]), number(words[1])
                if v is not None and i is not None:
                    getattr(cur, _TABLES[section]).append((v, i))
        elif section == "voltage range" and words:
            v = number(words[0])
            if v is not None:
                cur.voltage = v
    return models


def read(path: str | Path) -> dict[str, BufferModel]:
    return parse(Path(path).read_text(encoding="utf-8", errors="replace"))


def _current(table: list[tuple[float, float]], v: float) -> Optional[float]:
    """I of a V-I table at v, between its points; None outside it."""
    pts = sorted(table)
    for (va, ia), (vb, ib) in zip(pts, pts[1:]):
        if va <= v <= vb and vb > va:
            return ia + (ib - ia) * (v - va) / (vb - va)
    return None


def _chord_r(table: list[tuple[float, float]], v0: float, v1: float) -> Optional[float]:
    """dV/dI of a V-I table between v0 and v1."""
    i0, i1 = _current(table, v0), _current(table, v1)
    if i0 is None or i1 is None or i1 == i0:
        return None
    return abs((v1 - v0) / (i1 - i0))


#: The part of the supply a driver's output sits from its rail while it
#: drives a line of about its own impedance: where its V-I table is read.
DRIVE_SPAN = 0.3


#: Pull-up and pull-down resistances further apart than this are not averaged.
UNEVEN = 10.0


def _span(model: BufferModel, table: list[tuple[float, float]]) -> float:
    """The table voltage the output resistance is read up to."""
    if model.voltage:
        return DRIVE_SPAN * model.voltage
    top = max((v for v, _ in table), default=0.0)
    return DRIVE_SPAN * top / 2 if top > 0 else 0.0


def driver(model: BufferModel) -> dict[str, Any]:
    """Output resistance, 10-90 % rise time and C_comp of a driver.

    The resistance is the chord of the pull-down and pull-up V-I tables
    from the rail (0 V in the table) to ``DRIVE_SPAN`` of the supply into
    the driving range. That is the side of the table the buffer works on:
    below 0 V the table is the clamp diode, not the transistor."""
    if model.model_type.lower() in NOT_DRIVERS:
        raise ValueError(f"model {model.name} is of type {model.model_type}: it does not "
                         "drive a line")
    out: dict[str, Any] = {"model": model.name}
    rs = []
    for key, table in (("r_pulldown_ohms", model.pulldown), ("r_pullup_ohms", model.pullup)):
        v1 = _span(model, table)
        r = _chord_r(table, 0.0, v1) if v1 > 0 else None
        if r:
            rs.append(r)
            out[key] = r
    if len(rs) == 2 and max(rs) > UNEVEN * min(rs):
        # one side barely conducts near its rail (an n-channel pull-up, an
        # open drain with a weak keeper): the mean would be that side alone
        out["r_out_ohms"] = min(rs)
        out["note"] = (f"pull-down {out['r_pulldown_ohms']:.3g} ohm and pull-up "
                       f"{out['r_pullup_ohms']:.3g} ohm differ widely; the stronger is used")
    elif rs:
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


#: An on-die termination weaker than this is taken as none.
TERMINATION_MAX_OHMS = 2000.0


def _termination(model: BufferModel) -> Optional[float]:
    """The resistance an input shows inside its rails: its clamp tables
    carry the on-die termination there (a clamp diode alone carries
    nothing between the rails). Both clamps in parallel, each read over
    the middle half of the supply."""
    if not model.voltage:
        return None
    g = 0.0
    for table in (model.gnd_clamp, model.power_clamp):
        r = _chord_r(table, 0.25 * model.voltage, 0.75 * model.voltage)
        if r:
            g += 1.0 / r
    if g <= 1.0 / TERMINATION_MAX_OHMS:
        return None
    return 1.0 / g


def receiver(model: BufferModel) -> dict[str, Any]:
    """C_comp of a receiver and, where its clamp tables carry one, its
    on-die termination."""
    out: dict[str, Any] = {"model": model.name}
    if model.c_comp[0] is not None:
        out["c_comp_f"] = model.c_comp[0]
    r = _termination(model)
    if r is not None:
        out["r_term_ohms"] = r
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
