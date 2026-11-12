# SPDX-License-Identifier: Apache-2.0
"""Run one eda-agent openEMS model. Copied beside ``model.json`` and run by
openEMS's own Python as a separate process:

    python openems_runner.py model.json [--threads N]

Nothing in eda-agent imports this file; it imports openEMS (GPL-3) only
inside ``main`` and needs nothing from eda-agent, only numpy and openEMS.

It excites each port in turn, writes ``status.json`` as it goes, and ends
with ``results.json``: the frequency list, the full S matrix as
[re, im] pairs, and for an antenna the far field at the deepest S11. On
any error it writes ``error.json`` with the traceback instead.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback

EPS0 = 8.8541878128e-12


#: Waits before each retry of the final rename. Same window as
#: eda_agent.atomicfile, which this program cannot import: a Windows
#: scanner may hold the freshly written target for a few milliseconds.
_BACKOFF = (0.05, 0.1, 0.2, 0.4, 0.8)


def _write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    for wait in _BACKOFF:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(wait)
    os.replace(tmp, path)


def merge_lines(values, min_gap, keep=()):
    """Sorted mesh lines with near-duplicates merged. Two lines a hair
    apart make one tiny cell, and the FDTD time step follows the smallest
    cell. Lines in ``keep`` (a port's line) always stay; the others near
    them go."""
    keep = sorted(float(k) for k in keep)
    out = list(keep)
    for v in sorted(float(x) for x in values):
        if all(abs(v - o) >= min_gap for o in out):
            out.append(v)
    return sorted(out)


def port_decay(sim_path, n_ports):
    """How far the port voltages have died down by the end of the run, in
    dB below their peak (worst port), or None if the probes cannot be read.
    Below -40 dB the S-parameters are settled whatever the energy says."""
    worst = None
    for k in range(1, n_ports + 1):
        path = os.path.join(sim_path, f"port_ut{k}")
        try:
            rows = []
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("%") or not line.strip():
                        continue
                    rows.append(abs(float(line.split()[1])))
        except (OSError, ValueError, IndexError):
            return None
        if not rows:
            return None
        peak = max(rows) or 1e-30
        tail = max(rows[-max(1, len(rows) // 10):])
        db = 20 * math.log10(max(tail / peak, 1e-15))
        worst = db if worst is None else max(worst, db)
    return worst


def build(model, excite, np, ContinuousStructure, openEMS):
    """The CSX structure and FDTD set-up for one excitation."""
    fdtd = openEMS(NrTS=int(model.get("max_steps", 2_000_000)),
                   EndCriteria=float(model["end_criteria"]))
    exc = model["excitation"]
    fdtd.SetGaussExcite(float(exc["f0_hz"]), float(exc["fc_hz"]))
    fdtd.SetBoundaryCond(list(model["boundary"]))
    csx = ContinuousStructure()
    fdtd.SetCSX(csx)
    mesh = csx.GetGrid()
    mesh.SetDeltaUnit(1e-3)

    f_c = float(exc["f0_hz"])
    for k, d in enumerate(model["dielectrics"]):
        kappa = 2 * math.pi * f_c * EPS0 * d["er"] * d["tan_d"]
        mat = csx.AddMaterial(f"diel{k}", epsilon=d["er"], kappa=kappa)
        mat.AddBox([d["xy1"][0], d["xy1"][1], d["z1"]], [d["xy2"][0], d["xy2"][1], d["z2"]],
                   priority=1)

    metal = csx.AddMetal("copper")
    for s in model["sheets"]:
        xs = [p[0] for p in s["pts"]]
        ys = [p[1] for p in s["pts"]]
        metal.AddPolygon(np.array([xs, ys]), "z", s["z"], priority=10)
    for v in model["vias"]:
        metal.AddCylinder([v["xy"][0], v["xy"][1], v["z1"]], [v["xy"][0], v["xy"][1], v["z2"]],
                          v["r"], priority=10)

    ports = []
    for p in model["ports"]:
        ports.append(fdtd.AddLumpedPort(p["number"], p["ohms"], p["start"], p["stop"], "z",
                                        excite=1.0 if p["number"] == excite else 0.0,
                                        priority=5))

    m = model["mesh"]
    gap = float(m.get("min_gap_mm", 0.01))
    via_xy = [v["xy"] for v in model["vias"] if v.get("role") == "signal"]
    xs = list(m["x"]) + [v[0] for v in via_xy]
    ys = list(m["y"]) + [v[1] for v in via_xy]
    mesh.AddLine("x", merge_lines(xs, gap, m.get("keep_x", ())))
    mesh.AddLine("y", merge_lines(ys, gap, m.get("keep_y", ())))
    zs = list(m["z"])
    for d in model["dielectrics"]:
        zs += [float(z) for z in np.linspace(d["z1"], d["z2"], 4)]
    mesh.AddLine("z", merge_lines(zs, gap))
    mesh.SmoothMeshLines("x", m["res_mm"], m["ratio"])
    mesh.SmoothMeshLines("y", m["res_mm"], m["ratio"])
    mesh.SmoothMeshLines("z", m["res_mm"], m["ratio"])

    nf2ff = fdtd.CreateNF2FFBox() if model.get("nf2ff") else None
    return csx, fdtd, ports, nf2ff


def main(argv):
    model_path = os.path.abspath(argv[1])
    threads = 0
    if "--threads" in argv:
        threads = int(argv[argv.index("--threads") + 1])
    folder = os.path.dirname(model_path)
    status_path = os.path.join(folder, "status.json")
    t0 = time.time()
    try:
        home = os.environ.get("EDA_AGENT_OPENEMS_DIR", "")
        if home and hasattr(os, "add_dll_directory") and os.path.isdir(home):
            os.add_dll_directory(home)
        import numpy as np
        from CSXCAD import ContinuousStructure
        from openEMS import openEMS

        with open(model_path, encoding="utf-8") as fh:
            model = json.load(fh)
        freq = np.linspace(max(float(model["f_min_hz"]), 1e6), float(model["f_max_hz"]),
                           int(model["points"]))
        n = len(model["ports"])
        s = [[None] * n for _ in range(n)]
        antenna = None
        decay = []
        for exc in range(1, n + 1):
            _write(status_path, {"excitation": exc, "of": n,
                                 "elapsed_s": round(time.time() - t0)})
            _csx, fdtd, ports, nf2ff = build(model, exc, np, ContinuousStructure, openEMS)
            sim_path = os.path.join(folder, f"exc{exc}")
            fdtd.Run(sim_path, cleanup=True, verbose=0, numThreads=threads)
            for p, row in zip(ports, model["ports"]):
                p.CalcPort(sim_path, freq, ref_impedance=row["ohms"])
            decay.append(port_decay(sim_path, n))
            inc = ports[exc - 1].uf_inc
            for i, p in enumerate(ports):
                val = p.uf_ref / inc
                s[i][exc - 1] = [[float(c.real), float(c.imag)] for c in val]
            if nf2ff is not None and exc == 1:
                antenna = _far_field(np, ports[0], nf2ff, sim_path, freq)
        result = {"freq_hz": [float(f) for f in freq],
                  "ports": [{"number": p["number"], "name": p["name"], "ohms": p["ohms"]}
                            for p in model["ports"]],
                  "s": s,  # s[i][j][k] = [re, im] of S(i+1)(j+1) at freq[k]
                  "elapsed_s": round(time.time() - t0, 1),
                  "port_decay_db": decay,
                  "settled": (None if any(d is None for d in decay)
                              else all(d <= -40 for d in decay))}
        if antenna:
            result["antenna"] = antenna
        _write(os.path.join(folder, "results.json"), result)
        return 0
    except Exception as exc:  # noqa: BLE001 - reported in error.json
        _write(os.path.join(folder, "error.json"),
               {"error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-4000:],
                "elapsed_s": round(time.time() - t0, 1)})
        return 1


def _far_field(np, port, nf2ff, sim_path, freq):
    """Directivity, efficiency and two pattern cuts at the deepest S11."""
    s11 = np.abs(port.uf_ref / port.uf_inc)
    k = int(np.argmin(s11))
    f_res = float(freq[k])
    theta = np.arange(-180.0, 181.0, 2.0)
    phi = np.array([0.0, 90.0])
    res = nf2ff.CalcNF2FF(sim_path, f_res, theta, phi, center=[0, 0, 0])
    d_max = float(np.ravel(res.Dmax)[0])
    p_rad = float(np.ravel(res.Prad)[0])
    p_acc = float(np.real(port.P_acc[k]))
    e = np.asarray(res.E_norm[0])
    e_max = float(np.max(e)) or 1.0
    gain = 20 * np.log10(np.maximum(e / e_max, 1e-6)) + 10 * math.log10(max(d_max, 1e-12))
    return {"f_res_hz": f_res, "s11_db": float(20 * math.log10(max(s11[k], 1e-15))),
            "directivity_dbi": 10 * math.log10(max(d_max, 1e-12)),
            "radiated_w": p_rad, "accepted_w": p_acc,
            "efficiency": (p_rad / p_acc) if p_acc > 0 else None,
            "pattern": {"theta_deg": [float(t) for t in theta],
                        "phi0": [float(v) for v in gain[:, 0]],
                        "phi90": [float(v) for v in gain[:, 1]]}}


if __name__ == "__main__":
    sys.exit(main(sys.argv))
