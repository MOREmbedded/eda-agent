# SPDX-License-Identifier: Apache-2.0
"""Touchstone 1.x files (.s1p, .s2p, ... .sNp): read, write, summarise.

Written from the Touchstone File Format Specification 1.1 (IBIS Open
Forum). The option line ``# <unit> <parameter> <format> R <ohms>`` sets
the frequency unit (Hz kHz MHz GHz), the parameter (S Y Z; only S is
read here), the number format (RI real/imag, MA magnitude/angle in
degrees, DB dB/angle) and the reference resistance. Data rows hold the
frequency and N*N pairs; for N >= 3 a row wraps after four pairs and
the matrix is written row by row, but a 2-port file is the one exception
and is written in the order S11 S21 S12 S22.

scikit-rf (BSD) reads these as well and does much more; this reader keeps
the core install free of it.
"""

from __future__ import annotations

import cmath
import math
import re
from pathlib import Path
from typing import Optional

_UNITS = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9}


def _ports_from_name(path: Path) -> Optional[int]:
    m = re.search(r"\.s(\d+)p$", path.name, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _to_complex(a: float, b: float, fmt: str) -> complex:
    if fmt == "ri":
        return complex(a, b)
    if fmt == "ma":
        return cmath.rect(a, math.radians(b))
    return cmath.rect(10 ** (a / 20), math.radians(b))  # db


def read_touchstone(path: str | Path, ports: Optional[int] = None) -> dict:
    """Read a Touchstone 1.x S-parameter file.

    Returns ``{"ports", "z0", "freq_hz": [...], "s": [[[complex]]]}`` with
    ``s[k][i][j]`` = S(i+1)(j+1) at ``freq_hz[k]``.
    """
    path = Path(path)
    n = ports or _ports_from_name(path)
    if not n:
        raise ValueError(f"cannot tell the port count of {path.name}: name it .sNp")
    unit, param, fmt, z0 = 1e9, "s", "ma", 50.0
    numbers: list[float] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("!", 1)[0].strip()
        if not line:
            continue
        if line.startswith("#"):
            toks = line[1:].lower().split()
            k = 0
            while k < len(toks):
                t = toks[k]
                if t in _UNITS:
                    unit = _UNITS[t]
                elif t in ("s", "y", "z", "h", "g"):
                    param = t
                elif t in ("ri", "ma", "db"):
                    fmt = t
                elif t == "r" and k + 1 < len(toks):
                    z0 = float(toks[k + 1])
                    k += 1
                k += 1
            continue
        if line.startswith("["):
            raise ValueError("Touchstone 2.0 keyword files are not read here")
        numbers.extend(float(x) for x in line.split())
    if param != "s":
        raise ValueError(f"{path.name} holds {param.upper()}-parameters; only S is read")
    per_row = 1 + 2 * n * n
    if len(numbers) % per_row:
        raise ValueError(f"{path.name}: {len(numbers)} numbers is not a whole "
                         f"number of {n}-port rows")
    freqs, s = [], []
    for r in range(len(numbers) // per_row):
        row = numbers[r * per_row:(r + 1) * per_row]
        freqs.append(row[0] * unit)
        vals = [_to_complex(row[1 + 2 * k], row[2 + 2 * k], fmt) for k in range(n * n)]
        if n == 2:
            m = [[vals[0], vals[2]], [vals[1], vals[3]]]
        else:
            m = [vals[i * n:(i + 1) * n] for i in range(n)]
        s.append(m)
    return {"ports": n, "z0": z0, "freq_hz": freqs, "s": s}


def write_touchstone(path: str | Path, freq_hz: list[float], s: list,
                     z0: float = 50.0, comment: str = "") -> Path:
    """Write S-parameters as Touchstone 1.1, RI format, frequency in Hz.

    ``s[k][i][j]`` as ``read_touchstone`` returns it. The file extension
    is set from the port count.
    """
    n = len(s[0]) if s else 1
    path = Path(path).with_suffix(f".s{n}p")
    lines = [f"! {c}" for c in comment.splitlines() if c] if comment else []
    lines.append(f"# Hz S RI R {z0:g}")
    for f, m in zip(freq_hz, s):
        if n == 2:
            order = [m[0][0], m[1][0], m[0][1], m[1][1]]
        else:
            order = [m[i][j] for i in range(n) for j in range(n)]
        pairs = [f"{v.real:.9g} {v.imag:.9g}" for v in order]
        chunk = 4 if n >= 3 else len(pairs)
        first = True
        for k in range(0, len(pairs), chunk):
            head = f"{f:.9g} " if first else "  "
            lines.append(head + " ".join(pairs[k:k + chunk]))
            first = False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _db(v: complex) -> float:
    return 20 * math.log10(max(abs(v), 1e-15))


def summarize(data: dict, at_hz: Optional[list[float]] = None) -> dict:
    """The numbers a reviewer wants from S-parameters.

    For every port: worst return loss (largest |Sii|) and, when Sii dips,
    its deepest point (an antenna's resonance). For a 2-port or more:
    S21 insertion loss, worst and at the band edges. ``at_hz`` adds the
    values at chosen frequencies (nearest sample).
    """
    f, s, n = data["freq_hz"], data["s"], data["ports"]
    if not f:
        return {"ports": n, "points": 0}
    out: dict = {"ports": n, "points": len(f), "z0": data.get("z0", 50.0),
                 "f_min_mhz": round(f[0] / 1e6, 4), "f_max_mhz": round(f[-1] / 1e6, 4),
                 "ports_detail": []}
    z0 = data.get("z0", 50.0)
    for p in range(n):
        sii = [s[k][p][p] for k in range(len(f))]
        db = [_db(v) for v in sii]
        k_worst = max(range(len(f)), key=lambda k: db[k])
        k_best = min(range(len(f)), key=lambda k: db[k])
        zin = [z0 * (1 + v) / (1 - v) if abs(1 - v) > 1e-12 else complex(math.inf)
               for v in sii]
        out["ports_detail"].append({
            "port": p + 1,
            "worst_s_db": round(db[k_worst], 2), "worst_at_mhz": round(f[k_worst] / 1e6, 3),
            "best_s_db": round(db[k_best], 2), "best_at_mhz": round(f[k_best] / 1e6, 3),
            "zin_at_best_ohms": [round(zin[k_best].real, 2), round(zin[k_best].imag, 2)],
        })
    if n >= 2:
        s21 = [_db(s[k][1][0]) for k in range(len(f))]
        k_w = min(range(len(f)), key=lambda k: s21[k])
        out["s21"] = {"worst_db": round(s21[k_w], 2), "worst_at_mhz": round(f[k_w] / 1e6, 3),
                      "at_f_min_db": round(s21[0], 2), "at_f_max_db": round(s21[-1], 2)}
    if at_hz:
        rows = []
        for target in at_hz:
            k = min(range(len(f)), key=lambda k: abs(f[k] - target))
            rows.append({"f_mhz": round(f[k] / 1e6, 4),
                         "s_db": [[round(_db(s[k][i][j]), 2) for j in range(n)]
                                  for i in range(n)]})
        out["at"] = rows
    return out
