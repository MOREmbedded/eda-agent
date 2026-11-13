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

from ...units import MM_PER_INCH, MM_PER_MIL, OZ_TO_MILS

# ---------------------------------------------------------------- units

#: Metres per mil and per inch, from the one definition in eda_agent.units.
MIL = MM_PER_MIL / 1000.0
INCH = MM_PER_INCH / 1000.0

#: Copper weight: 1 oz/ft^2 is the conventional 35 um figure that
#: eda_agent.units.OZ_TO_MILS states, in metres.
OZ_M = OZ_TO_MILS * MIL

UNITS: dict[str, dict[str, float]] = {
    "length": {"mil": MIL, "mm": 1e-3, "um": 1e-6, "in": INCH, "cm": 1e-2,
               "m": 1.0, "oz": OZ_M, "µm": 1e-6, "nm": 1e-9, "mils": MIL,
               "thou": MIL, "inch": INCH, "ft": 0.3048},
    "frequency": {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6, "GHz": 1e9, "THz": 1e12},
    "time": {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "ps": 1e-12,
             "fs": 1e-15, "µs": 1e-6},
    "current": {"A": 1.0, "mA": 1e-3, "uA": 1e-6, "kA": 1e3, "µA": 1e-6},
    "voltage": {"V": 1.0, "mV": 1e-3, "uV": 1e-6, "kV": 1e3, "µV": 1e-6},
    "resistance": {"ohm": 1.0, "mohm": 1e-3, "kohm": 1e3, "Mohm": 1e6,
                   "Ω": 1.0, "mΩ": 1e-3, "kΩ": 1e3, "MΩ": 1e6, "megohm": 1e6},
    "capacitance": {"F": 1.0, "mF": 1e-3, "uF": 1e-6, "nF": 1e-9, "pF": 1e-12,
                    "fF": 1e-15, "µF": 1e-6},
    "inductance": {"H": 1.0, "mH": 1e-3, "uH": 1e-6, "nH": 1e-9, "pH": 1e-12,
                   "µH": 1e-6},
    "power": {"W": 1.0, "mW": 1e-3, "uW": 1e-6, "kW": 1e3, "µW": 1e-6},
    "temperature": {"°C": 1.0, "C": 1.0, "degC": 1.0},
    "delta_t": {"°C": 1.0, "C": 1.0, "K": 1.0},
    "thermal_resistance": {"°C/W": 1.0, "C/W": 1.0, "K/W": 1.0},
    "thermal_conductivity": {"W/mK": 1.0, "W/m/K": 1.0},
    "h_coeff": {"W/m2K": 1.0},
    "velocity": {"m/s": 1.0, "LFM": 0.00508, "ft/min": 0.00508},
    "conductivity": {"S/m": 1.0},
    "resistivity": {"ohm.m": 1.0, "ohm.cm": 1e-2, "uohm.cm": 1e-8},
    "area": {"mm2": 1e-6, "mil2": MIL ** 2, "in2": INCH ** 2, "m2": 1.0},
    "ratio": {"": 1.0},
    "percent": {"%": 1.0},
    "ppm": {"ppm": 1.0},
    "db": {"dB": 1.0},
    "dbm": {"dBm": 1.0},
    "count": {"": 1.0},
    "angle": {"deg": 1.0},
    "choice": {},
    "choice_text": {},
    "bool": {},
}

#: The units a form offers for a kind, in order. The tables above also
#: accept the spellings left out here (µm, Ω, mils, ...).
UNIT_CHOICES: dict[str, list[str]] = {
    "length": ["mil", "mm", "um", "in", "cm", "m", "oz"],
    "frequency": ["Hz", "kHz", "MHz", "GHz"],
    "time": ["s", "ms", "us", "ns", "ps"],
    "current": ["A", "mA", "uA", "kA"],
    "voltage": ["V", "mV", "uV", "kV"],
    "resistance": ["ohm", "mohm", "kohm", "Mohm"],
    "capacitance": ["F", "uF", "nF", "pF", "fF"],
    "inductance": ["H", "mH", "uH", "nH", "pH"],
    "power": ["W", "mW", "uW", "kW"],
    "temperature": ["°C", "°F", "K"],
    "velocity": ["m/s", "LFM"],
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
    unit = unit.strip()
    if unit in table:
        return table[unit]
    # Case-insensitive only where it cannot change the meaning: "MW" must
    # never be read as "mW". A unit that differs from a known one only by
    # the case of an m/M prefix is refused.
    low = unit.lower()
    hits = [k for k in table if k.lower() == low]
    if len(hits) == 1 and not (unit[:1] in "mM" and len(unit) > 1 and hits[0][:1] in "mM"
                               and hits[0][:1] != unit[:1]):
        return table[hits[0]]
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
    if kind == "temperature" and unit.strip().lower().replace("°", "") in ("f", "degf"):
        return (value - 32.0) * 5.0 / 9.0
    if kind == "temperature" and unit.strip().lower() == "k":
        return value - 273.15
    if kind in ("choice", "bool"):
        return value
    return value * _unit_factor(kind, unit)


def from_si(value: float, kind: str, unit: str) -> float:
    if kind == "temperature" and unit.lower().replace("°", "") == "f":
        return value * 9.0 / 5.0 + 32.0
    return value / _unit_factor(kind, unit)


_PREFIX = [(1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""),
           (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"), (1e-15, "f"),
           (1e-18, "a")]
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
        mil = value / MIL
        if abs(mm) < 0.1:
            return f"{sig(value * 1e6, digits)} µm ({sig(mil, digits)} mil)"
        return f"{sig(mm, digits)} mm ({sig(mil, digits)} mil)"
    if kind == "area":
        return f"{sig(value * 1e6, digits)} mm² ({sig(value / MIL ** 2, digits)} mil²)"
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
        if self.kind in UNIT_CHOICES:
            d["units"] = list(UNIT_CHOICES[self.kind])
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

    def check(self, label: str, ours: float, other: float, model: str,
              kind: str = "resistance") -> None:
        """Record a cross-check of a primary output against another model."""
        if ours and other and math.isfinite(ours) and math.isfinite(other):
            self.checks.append({"label": label, "model": model,
                                "value": other, "display": fmt(other, kind),
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
        calc = Calculator(name, title, group, summary, inputs, fn, status)
        REGISTRY[name] = calc
        # fn(fn.__wrapped_parse__({"w": "5 mil"})) runs the bare function
        # on parsed inputs: what the tests and other calculators use.
        fn.__wrapped_parse__ = calc.parse
        return fn
    return deco


def need(cond: bool, message: str) -> None:
    if not cond:
        raise InputError(message)
