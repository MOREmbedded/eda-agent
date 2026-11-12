# SPDX-License-Identifier: Apache-2.0
"""Units, inputs, results and the calculator registry for the PCB toolkit.

Every calculator is declared once, with its inputs, their physical kind
and default unit, its model and its stated accuracy. Three surfaces read
that declaration: the dashboard builds its forms from it, the MCP tool
validates against it, and the tests walk it to prove every calculator
runs. So a calculator cannot exist in one place and not the others.

Values travel in SI internally. An input may be a bare number (taken in
the input's default unit), a string such as ``"5 mil"`` or ``"2.4GHz"``,
or ``{"value": 5, "unit": "mil"}``. Outputs come back in SI with their
kind, plus a ready-formatted display string.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ---------------------------------------------------------------- units

#: Copper weight: 1 oz/ft^2 is taken as 1.378 mil (35.0 um), the same
#: convention as eda_agent.units.OZ_TO_MILS.
OZ_M = 1.378 * 25.4e-6

UNITS: dict[str, dict[str, float]] = {
    "length": {"m": 1.0, "cm": 1e-2, "mm": 1e-3, "um": 1e-6, "µm": 1e-6,
               "nm": 1e-9, "mil": 25.4e-6, "mils": 25.4e-6, "thou": 25.4e-6,
               "in": 0.0254, "inch": 0.0254, "ft": 0.3048, "oz": OZ_M},
    "frequency": {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9, "thz": 1e12},
    "time": {"s": 1.0, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9,
             "ps": 1e-12, "fs": 1e-15},
    "current": {"a": 1.0, "ma": 1e-3, "ua": 1e-6, "ka": 1e3},
    "voltage": {"v": 1.0, "mv": 1e-3, "uv": 1e-6, "kv": 1e3},
    "resistance": {"ohm": 1.0, "Ω": 1.0, "mohm": 1e-3, "mΩ": 1e-3,
                   "kohm": 1e3, "kΩ": 1e3, "megohm": 1e6, "MΩ": 1e6},
    "capacitance": {"f": 1.0, "mf": 1e-3, "uf": 1e-6, "µf": 1e-6, "nf": 1e-9,
                    "pf": 1e-12, "ff": 1e-15},
    "inductance": {"h": 1.0, "mh": 1e-3, "uh": 1e-6, "µh": 1e-6, "nh": 1e-9,
                   "ph": 1e-12},
    "power": {"w": 1.0, "mw": 1e-3, "uw": 1e-6, "kw": 1e3},
    "temperature": {"c": 1.0, "°c": 1.0, "degc": 1.0},
    "delta_t": {"c": 1.0, "°c": 1.0, "k": 1.0},
    "thermal_resistance": {"c/w": 1.0, "°c/w": 1.0, "k/w": 1.0},
    "thermal_conductivity": {"w/mk": 1.0, "w/m/k": 1.0},
    "h_coeff": {"w/m2k": 1.0},
    "velocity": {"m/s": 1.0, "lfm": 0.00508, "ft/min": 0.00508},
    "conductivity": {"s/m": 1.0, "ms/m": 1e6},
    "resistivity": {"ohm.m": 1.0, "ohm.cm": 1e-2, "uohm.cm": 1e-8},
    "area": {"m2": 1.0, "mm2": 1e-6, "mil2": (25.4e-6) ** 2, "in2": 0.0254 ** 2},
    "ratio": {"": 1.0, "x": 1.0},
    "percent": {"%": 1.0},
    "ppm": {"ppm": 1.0},
    "db": {"db": 1.0},
    "dbm": {"dbm": 1.0},
    "count": {"": 1.0},
    "angle": {"deg": 1.0},
    "choice": {},
    "choice_text": {},
    "bool": {},
}

#: The unit a kind is shown in when the caller has no preference.
DISPLAY = {
    "length": "mm", "frequency": "MHz", "time": "ns", "current": "A",
    "voltage": "V", "resistance": "ohm", "capacitance": "pF",
    "inductance": "nH", "power": "W", "temperature": "°C", "delta_t": "°C",
    "thermal_resistance": "°C/W", "thermal_conductivity": "W/mK",
    "h_coeff": "W/m²K", "velocity": "m/s", "conductivity": "S/m",
    "resistivity": "Ω·m", "area": "mm²", "ratio": "", "percent": "%",
    "ppm": "ppm", "db": "dB", "dbm": "dBm", "count": "", "angle": "°",
}


class InputError(ValueError):
    """An input the calculator cannot use, with a message for a person."""


_NUM = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(.*?)\s*$")


def _unit_factor(kind: str, unit: str) -> float:
    table = UNITS.get(kind, {})
    if unit in table:
        return table[unit]
    low = unit.lower().replace(" ", "")
    for k, v in table.items():
        if k.lower() == low:
            return v
    if kind == "temperature" and low in ("f", "°f", "degf"):
        return float("nan")  # handled by the caller
    raise InputError(f"unit '{unit}' is not a {kind} unit; use one of "
                     + ", ".join(k for k in table if k))


def to_si(value: Any, kind: str, unit: str) -> float:
    """``value`` in ``unit`` (or its own unit) as an SI float."""
    if isinstance(value, dict):
        unit = str(value.get("unit") or unit)
        value = value.get("value")
    if isinstance(value, bool):
        raise InputError("a number is needed, not true/false")
    if isinstance(value, str):
        m = _NUM.match(value)
        if not m:
            raise InputError(f"'{value}' is not a number")
        value = float(m.group(1))
        if m.group(2):
            unit = m.group(2)
    if not isinstance(value, (int, float)):
        raise InputError(f"'{value}' is not a number")
    value = float(value)
    if not math.isfinite(value):
        raise InputError("the number must be finite")
    if kind == "temperature" and unit.lower().replace("°", "") in ("f", "degf"):
        return (value - 32.0) * 5.0 / 9.0
    if kind == "temperature" and unit.lower() == "k":
        return value - 273.15
    if kind in ("choice", "bool"):
        return value
    return value * _unit_factor(kind, unit)


def from_si(value: float, kind: str, unit: str) -> float:
    if kind == "temperature" and unit.lower().replace("°", "") == "f":
        return value * 9.0 / 5.0 + 32.0
    return value / _unit_factor(kind, unit)


_PREFIX = [(1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""),
           (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"), (1e-15, "f")]
_SI_SYMBOL = {"frequency": "Hz", "time": "s", "current": "A", "voltage": "V",
              "resistance": "Ω", "capacitance": "F", "inductance": "H",
              "power": "W"}


def sig(x: float, digits: int = 4) -> str:
    if x == 0 or not math.isfinite(x):
        return str(x)
    mag = int(math.floor(math.log10(abs(x))))
    dec = max(0, digits - 1 - mag)
    return f"{x:.{dec}f}"


def fmt(value: float, kind: str, digits: int = 4) -> str:
    """A human string: engineering prefix for electrical kinds, both mm
    and mil for lengths, plain otherwise."""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    if kind in _SI_SYMBOL:
        if value == 0:
            return f"0 {_SI_SYMBOL[kind]}"
        a = abs(value)
        for f, p in _PREFIX:
            if a >= f * 0.9995:
                return f"{sig(value / f, digits)} {p}{_SI_SYMBOL[kind]}"
        return f"{value:.3e} {_SI_SYMBOL[kind]}"
    if kind == "length":
        mm = value * 1e3
        mil = value / 25.4e-6
        if abs(mm) < 0.1:
            return f"{sig(value * 1e6, digits)} µm ({sig(mil, digits)} mil)"
        return f"{sig(mm, digits)} mm ({sig(mil, digits)} mil)"
    if kind == "area":
        return f"{sig(value * 1e6, digits)} mm² ({sig(value / (25.4e-6) ** 2, digits)} mil²)"
    unit = DISPLAY.get(kind, "")
    return f"{sig(value, digits)} {unit}".strip()


# -------------------------------------------------------------- declarations

@dataclass
class Input:
    key: str
    label: str
    kind: str
    default: Any
    unit: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    choices: Optional[list[tuple[str, str]]] = None
    help: str = ""
    optional: bool = False
    #: Where the dashboard can prefill this from the open board, e.g.
    #: "stackup.dielectric_height" (see web/toolkit_view.py).
    prefill: str = ""
    show_if: Optional[dict[str, list[str]]] = None

    def spec(self) -> dict[str, Any]:
        d = {"key": self.key, "label": self.label, "kind": self.kind,
             "default": self.default, "unit": self.unit, "help": self.help,
             "optional": self.optional}
        if self.kind in UNITS and UNITS[self.kind]:
            d["units"] = [u for u in UNITS[self.kind] if u not in ("µm", "µs", "µf", "µh", "Ω", "mΩ", "kΩ", "MΩ", "°c", "degc", "mils", "thou", "inch")]
        for k in ("min", "max", "prefill", "show_if"):
            v = getattr(self, k)
            if v not in (None, ""):
                d[k] = v
        if self.choices:
            d["choices"] = [{"value": a, "label": b} for a, b in self.choices]
        return d


@dataclass
class Result:
    outputs: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    status: str = "published"       # exact | solver | published | estimate
    accuracy: str = ""
    references: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def add(self, key: str, label: str, value: float, kind: str,
            primary: bool = False, digits: int = 4, note: str = "") -> "Result":
        v = None if value is None else float(value)
        self.outputs.append({
            "key": key, "label": label, "value": v, "kind": kind,
            "display": fmt(v, kind, digits) if v is not None else "n/a",
            "primary": primary, **({"note": note} if note else {})})
        return self

    def value(self, key: str) -> float:
        for o in self.outputs:
            if o["key"] == key:
                return o["value"]
        raise KeyError(key)

    def check(self, label: str, ours: float, other: float, model: str) -> None:
        """Record a cross-check of a primary output against another model."""
        if ours and other and math.isfinite(ours) and math.isfinite(other):
            self.checks.append({"label": label, "model": model,
                                "value": other,
                                "difference_percent": round((other / ours - 1) * 100, 3)})

    def to_dict(self) -> dict[str, Any]:
        return {"ok": True, "model": self.model, "status": self.status,
                "accuracy": self.accuracy, "references": self.references,
                "warnings": self.warnings, "outputs": self.outputs,
                "cross_checks": self.checks, **self.extra}


@dataclass
class Calculator:
    name: str
    title: str
    group: str
    summary: str
    inputs: list[Input]
    fn: Callable[[dict[str, Any]], Result]
    status: str = "published"

    def spec(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "group": self.group,
                "summary": self.summary, "status": self.status,
                "inputs": [i.spec() for i in self.inputs]}

    def parse(self, raw: dict[str, Any]) -> dict[str, Any]:
        raw = dict(raw or {})
        out: dict[str, Any] = {}
        known = {i.key for i in self.inputs}
        unknown = [k for k in raw if k not in known]
        if unknown:
            raise InputError("unknown input(s): " + ", ".join(sorted(unknown))
                             + ". Known: " + ", ".join(sorted(known)))
        for i in self.inputs:
            v = raw.get(i.key, i.default)
            if v is None or v == "":
                if i.optional:
                    out[i.key] = None
                    continue
                v = i.default
            if i.kind == "choice":
                allowed = [a for a, _ in (i.choices or [])]
                v = str(v)
                if v not in allowed:
                    raise InputError(f"{i.key} must be one of {', '.join(allowed)}")
                out[i.key] = v
                continue
            if i.kind == "choice_text":
                out[i.key] = str(v)
                continue
            if i.kind == "bool":
                out[i.key] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
                continue
            try:
                x = to_si(v, i.kind, i.unit)
            except InputError as e:
                raise InputError(f"{i.key}: {e}") from None
            lo = None if i.min is None else to_si(i.min, i.kind, i.unit)
            hi = None if i.max is None else to_si(i.max, i.kind, i.unit)
            if lo is not None and x < lo:
                raise InputError(f"{i.label} must be at least {i.min} {i.unit}".strip())
            if hi is not None and x > hi:
                raise InputError(f"{i.label} must be at most {i.max} {i.unit}".strip())
            out[i.key] = x
        return out

    def run(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            args = self.parse(raw)
            res = self.fn(args)
        except InputError as e:
            return {"ok": False, "calculator": self.name, "reason": str(e)}
        except (ValueError, ZeroDivisionError, OverflowError, ArithmeticError) as e:
            return {"ok": False, "calculator": self.name,
                    "reason": f"cannot compute with these inputs: {e}"}
        d = res.to_dict()
        d["calculator"] = self.name
        d["title"] = self.title
        return d


REGISTRY: dict[str, Calculator] = {}

GROUPS = [
    ("conductor", "Conductor"),
    ("impedance", "Impedance"),
    ("pairs", "Differential pairs"),
    ("via", "Via"),
    ("plane", "Planes and PDN"),
    ("crosstalk", "Crosstalk"),
    ("signal", "Signal and timing"),
    ("thermal", "Thermal"),
    ("passives", "Ohm's law and passives"),
    ("layout", "Padstack and layout"),
    ("reference", "Conversions and reference"),
]


def calculator(name: str, title: str, group: str, summary: str,
               inputs: list[Input], status: str = "published"):
    def deco(fn: Callable[[dict[str, Any]], Result]):
        if name in REGISTRY:
            raise RuntimeError(f"calculator {name} declared twice")
        REGISTRY[name] = Calculator(name, title, group, summary, inputs, fn, status)
        return fn
    return deco


def need(cond: bool, message: str) -> None:
    if not cond:
        raise InputError(message)
