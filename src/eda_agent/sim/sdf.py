# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Read the waveforms out of an Altium simulation results file (.sdf).

The layout, MEASURED on files Altium 26.10.1.6 wrote for known circuits:

    SDF: 1.0
    TotalPoints: <n>
    #ChartSetupsBegin ... #ChartSetupsEnd     display settings only
    Chart: <analysis name>                    one per analysis run
    ChartId: ...
    Wave: v(out)                              one block per wave
    WaveYUnitStr: V
    WavePoints: <n>
    WaveIsXComplex: TRUE|FALSE
    WaveIsYComplex: TRUE|FALSE
    WaveVariableType: NodeVoltage|General|...
    WaveIndependent: TRUE                     only on the sweep variable
    WaveExtendedDataStart\\n <n points as float64> \\n
    WaveDataStart\\n <the same points as float32>

Each point is x then y, each a float64, or a real and imaginary pair of
float64 when that axis is complex. The float32 block is a copy for
drawing and is ignored here. Checked against hand calculation: an RC
divider's AC magnitude and phase, its DC sweep, and a transient settling
to the expected level all decode exactly.

A results document that has been run more than once holds one chart per
run, under the same name, in run order.
"""

from __future__ import annotations

import cmath
import math
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

_EXT = b"WaveExtendedDataStart"
_DATA = b"WaveDataStart"
_HEADER = re.compile(rb"^([A-Za-z_]+):\s?(.*)$")


class SdfError(ValueError):
    """The file is not a results file this reader understands."""


@dataclass
class Wave:
    name: str
    unit: str
    variable_type: str
    independent: bool
    x_complex: bool
    y_complex: bool
    x: list = field(default_factory=list)
    y: list = field(default_factory=list)

    @property
    def points(self) -> int:
        return len(self.y)


@dataclass
class Chart:
    name: str
    waves: list = field(default_factory=list)

    def wave(self, name: str):
        """The wave whose name matches, ignoring case, or None."""
        want = name.strip().lower()
        for wave in self.waves:
            if wave.name.lower() == want:
                return wave
        return None


def read_sdf(path) -> list:
    """Every chart in the file, in the order they were written."""
    return parse_sdf(Path(path).read_bytes())


def parse_sdf(data: bytes) -> list:
    if not data.startswith(b"SDF:"):
        raise SdfError("not a simulation results file: no 'SDF:' header")
    end = data.find(b"#ChartSetupsEnd")
    pos = end + len(b"#ChartSetupsEnd") if end >= 0 else 0

    charts: list = []
    current = None
    header: dict = {}
    while pos < len(data):
        if data.startswith(_EXT, pos):
            wave, pos = _read_wave(data, pos, header)
            if current is None:
                raise SdfError("a wave appears before any chart")
            current.waves.append(wave)
            header = {}
            continue
        eol = data.find(b"\n", pos)
        if eol < 0:
            eol = len(data)
        line = data[pos:eol].rstrip(b"\r")
        pos = eol + 1
        m = _HEADER.match(line)
        if not m:
            continue
        key = m.group(1).decode("ascii")
        value = m.group(2).decode("latin-1").strip()
        if key == "Chart":
            current = Chart(name=value)
            charts.append(current)
            header = {}
        else:
            header[key] = value
    return charts


def _flag(header: dict, key: str) -> bool:
    return header.get(key, "").strip().upper() == "TRUE"


def _read_wave(data: bytes, pos: int, header: dict):
    if "Wave" not in header or "WavePoints" not in header:
        raise SdfError("wave data without a Wave/WavePoints header")
    count = int(header["WavePoints"])
    x_complex = _flag(header, "WaveIsXComplex")
    y_complex = _flag(header, "WaveIsYComplex")
    per_x = 2 if x_complex else 1
    per_y = 2 if y_complex else 1
    width = 8 * (per_x + per_y)

    start = pos + len(_EXT)
    if data[start:start + 1] == b"\n":
        start += 1
    stop = start + count * width
    if stop > len(data):
        raise SdfError(f"wave {header['Wave']!r} is cut short: "
                       f"{count} points need {count * width} bytes")
    values = struct.unpack_from(f"<{count * (per_x + per_y)}d", data, start)
    xs, ys = [], []
    step = per_x + per_y
    for i in range(count):
        chunk = values[i * step:(i + 1) * step]
        x = complex(chunk[0], chunk[1]) if x_complex else chunk[0]
        y = complex(chunk[per_x], chunk[per_x + 1]) if y_complex else chunk[per_x]
        xs.append(x)
        ys.append(y)

    # Skip the float32 copy: it follows WaveDataStart and is count points
    # at half the width.
    nxt = data.find(_DATA, stop)
    if nxt < 0:
        raise SdfError(f"wave {header['Wave']!r} has no WaveDataStart block")
    after = nxt + len(_DATA)
    if data[after:after + 1] == b"\n":
        after += 1
    after += count * width // 2

    wave = Wave(
        name=header["Wave"],
        unit=header.get("WaveYUnitStr", ""),
        variable_type=header.get("WaveVariableType", ""),
        independent=_flag(header, "WaveIndependent"),
        x_complex=x_complex, y_complex=y_complex, x=xs, y=ys)
    return wave, after


def latest_charts(charts: list) -> list:
    """The last run of each analysis, in the order the analyses first ran."""
    order, last = [], {}
    for chart in charts:
        if chart.name not in last:
            order.append(chart.name)
        last[chart.name] = chart
    return [last[name] for name in order]


def summarise(wave: Wave, max_points: int = 50) -> dict:
    """A wave as numbers a caller can use without the whole trace.

    Statistics are over every point. ``points`` keeps at most max_points
    samples, evenly spaced, always including the first and the last. A
    complex wave reports magnitude and phase in degrees, and the
    frequency (or other sweep) as a real number.
    """
    xs = [_real(x) for x in wave.x]
    if wave.y_complex:
        mags = [abs(y) for y in wave.y]
        phases = [math.degrees(cmath.phase(y)) for y in wave.y]
        out = {"name": wave.name, "unit": wave.unit, "points": wave.points,
               "complex": True, "magnitude": _stats(mags),
               "samples": _sample(list(zip(xs, mags, phases)), max_points),
               "sample_columns": ["x", "magnitude", "phase_deg"]}
    else:
        ys = [float(y) for y in wave.y]
        out = {"name": wave.name, "unit": wave.unit, "points": wave.points,
               "complex": False, **_stats(ys),
               "samples": _sample(list(zip(xs, ys)), max_points),
               "sample_columns": ["x", "y"]}
    if wave.independent:
        out["independent"] = True
    out["variable_type"] = wave.variable_type
    return out


def _real(value) -> float:
    return value.real if isinstance(value, complex) else float(value)


def _stats(values: list) -> dict:
    if not values:
        return {"min": None, "max": None, "first": None, "last": None}
    return {"min": min(values), "max": max(values),
            "first": values[0], "last": values[-1]}


def _sample(rows: list, limit: int) -> list:
    if limit <= 0 or not rows:
        return []
    if len(rows) <= limit:
        return [list(r) for r in rows]
    if limit == 1:
        return [list(rows[-1])]
    step = (len(rows) - 1) / (limit - 1)
    picked = [rows[round(i * step)] for i in range(limit)]
    return [list(r) for r in picked]
