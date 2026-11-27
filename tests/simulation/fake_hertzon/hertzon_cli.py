# SPDX-License-Identifier: Apache-2.0
"""A stand-in for ``hertzon-cli`` that follows its documented contract
(``cli_schema`` 1: ``version``, ``describe``, ``solve``, JSON progress on
stderr, the JSON summary on stdout, the exit codes). It contains no
Hertzon code; it lets the tests drive the engine without Hertzon.

The project is any JSON file with ``{"designs": [{"name", "variables":
{name: value}, "setups": [...]}]}``. ``FAKE_HERTZON_MODE`` picks how a
solve ends: ``ok`` (default), ``noconverge``, ``solverfail``, ``slow``.
Every solve writes its arguments to ``fake_argv.json`` in its folder.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

VERSION = "0.4.8"


def emit(ev):
    sys.stderr.write(json.dumps(ev) + "\n")
    sys.stderr.flush()


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def describe(argv):
    proj = load(argv[0]) if argv else None
    if proj is None:
        print("error: project not found or not readable", file=sys.stderr)
        return 3
    designs = []
    for d in proj["designs"]:
        designs.append({
            "name": d["name"], "kind": "3d", "units": "mm",
            "ports": [{"number": i + 1, "name": str(i + 1), "kind": "Lumped Port",
                       "modes": 1, "z0_ohm": 50.0} for i in range(2)],
            "variables": [{"name": k, "expression": str(v), "scope": "design",
                           "value": v, "unit": "m"} for k, v in d.get("variables", {}).items()],
            "setups": [{"name": s, "enabled": True, "sweeps": [{"name": "Sweep"}]}
                       for s in d.get("setups", ["Setup1"])]})
    print(json.dumps({"cli_schema": 1, "hertzon": VERSION, "project": os.path.abspath(argv[0]),
                      "designs": designs}))
    return 0


def opt(argv, name, many=False):
    vals = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == name]
    return vals if many else (vals[-1] if vals else "")


def solve(argv):
    with open("fake_argv.json", "w", encoding="utf-8") as fh:
        json.dump(argv, fh)
    mode = os.environ.get("FAKE_HERTZON_MODE", "ok")
    proj = load(argv[0]) if argv else None
    if proj is None:
        emit({"event": "error", "code": 3, "message": "project not found or not readable"})
        return 3
    dname = opt(argv, "--design") or proj["designs"][0]["name"]
    design = next((d for d in proj["designs"] if d["name"] == dname), None)
    if design is None:
        emit({"event": "error", "code": 3, "message": f"No design '{dname}'"})
        return 3
    sets = {}
    for item in opt(argv, "--set", many=True):
        k, _, v = item.partition("=")
        if k not in design.get("variables", {}):
            emit({"event": "error", "code": 3,
                  "message": f"No variable '{k}' in design '{dname}'; variables: "
                             f"{sorted(design.get('variables', {}))}"})
            return 3
        sets[k] = v
    f0, f1, n = 1e9, 6e9, 11
    if opt(argv, "--freq"):
        a, b, c = opt(argv, "--freq").split(":")
        f0, f1, n = float(a), float(b), int(c)
    freqs = [f0 + (f1 - f0) * k / (n - 1) for k in range(n)]
    if opt(argv, "--points"):
        freqs = [float(x) for x in opt(argv, "--points").split(",")]
    passes = 3
    for p in range(1, passes + 1):
        emit({"event": "progress", "percent": 25.0 * p, "message": f"Adaptive pass {p}"})
        emit({"event": "pass", "pass": p, "tets": 1000 * p,
              "delta_s": 0.1 / p if mode != "noconverge" else 0.5})
        if mode == "slow":
            time.sleep(1.0)
    emit({"event": "log", "level": "warning", "message": "port 2 is close to the boundary"})
    if mode == "solverfail":
        emit({"event": "error", "code": 1, "message": "meshing failed on face 12"})
        return 1
    out = opt(argv, "-o")
    path = os.path.join(out, f"{dname}_Setup1_Sweep.s2p") if os.path.isdir(out) else out
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("! fake hertzon\n# HZ S RI R 50\n")
        for f in freqs:
            ph = -2 * math.pi * f / 3e9
            s21 = (0.9 * math.cos(ph), 0.9 * math.sin(ph))
            fh.write(f"{f:.6g} 0.1 0 {s21[0]:.9f} {s21[1]:.9f} "
                     f"{s21[0]:.9f} {s21[1]:.9f} 0.1 0\n")
    converged = mode != "noconverge"
    emit({"event": "done", "touchstone": os.path.abspath(path), "seconds": 1.5, "cached": False})
    if "--json" in argv:
        print(json.dumps({"cli_schema": 1, "hertzon": VERSION, "project": argv[0],
                          "design": dname, "setup": "Setup1", "solution": "Sweep",
                          "variables": sets, "cached": False, "converged": converged,
                          "passes": passes, "tetrahedra": 3000,
                          "ports": [{"row": 1, "number": 1}, {"row": 2, "number": 2}],
                          "reference_impedance_ohm": 50.0,
                          "touchstone": os.path.abspath(path), "seconds": 1.5}))
    if not converged and "--require-convergence" in argv:
        return 5
    return 0


def main(argv):
    if not argv:
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "version":
        print(json.dumps({"cli_schema": 1, "hertzon": VERSION}) if "--json" in rest
              else f"hertzon {VERSION}")
        return 0
    if cmd == "describe":
        return describe(rest)
    if cmd == "solve":
        return solve(rest)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
