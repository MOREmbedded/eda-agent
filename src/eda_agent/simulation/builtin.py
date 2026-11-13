# SPDX-License-Identifier: Apache-2.0
"""The engines that come with eda-agent.

- ``eda-agent``: this project's own solvers, always present (numpy and
  scipy only). The reference the others are compared with.
- ``scikit-rf``: the scikit-rf project (BSD-3), imported in this process
  when installed. Its own Touchstone reader and checks give an
  independent second opinion on S-parameter data.
- ``openems``: openEMS (GPL-3), 3D FDTD, run as its own process exactly
  as ``sim_em_run`` runs it.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from .engine import EXTERNAL, LIBRARY, Engine, EngineError, EngineInfo, metric
from .jobs import Job

DEFAULT_TOL = 1e-3

#: Calculator output kinds as SI units (the toolkit gives values in SI).
_UNITS = {"resistance": "ohm", "time": "s", "capacitance": "F", "inductance": "H",
          "length": "m", "frequency": "Hz", "ratio": "", "conductance": "S",
          "current": "A", "voltage": "V", "power": "W", "temperature": "degC",
          "attenuation": "dB/m", "loss": "dB"}


def _tol(job: Job) -> float:
    t = float(job.spec.get("tolerance", DEFAULT_TOL))
    if not (t >= 0 and math.isfinite(t)):
        raise EngineError("tolerance must be a finite number of zero or more")
    return t


def s_quality(freq: np.ndarray, s: np.ndarray, tol: float) -> dict[str, Any]:
    """Normalised touchstone-check results from an (F, N, N) array."""
    n = s.shape[1]
    sv = np.linalg.svd(s, compute_uv=False)[:, 0] if len(freq) else np.zeros(0)
    k = int(np.argmax(sv)) if len(sv) else 0
    metrics = {
        "ports": metric(n, label="Ports"),
        "points": metric(int(len(freq)), label="Frequency points"),
        "f_min_hz": metric(float(freq[0]) if len(freq) else 0.0, "Hz", "Lowest frequency"),
        "f_max_hz": metric(float(freq[-1]) if len(freq) else 0.0, "Hz", "Highest frequency"),
        "passivity_max_sv": metric(float(sv[k]) if len(sv) else 0.0, "",
                                   "Largest singular value of S (1 = lossless)"),
        "passivity_worst_hz": metric(float(freq[k]) if len(freq) else 0.0, "Hz",
                                     "Where it is largest"),
    }
    passive = bool(len(sv) == 0 or sv[k] <= 1.0 + tol)
    verdicts = [{"check": "passivity", "pass": passive,
                 "detail": (f"largest singular value {sv[k]:.6f}" if len(sv) else "no data")
                           + ("" if passive else f" at {freq[k] / 1e6:.4g} MHz: the data "
                              "creates energy, which a simulation will amplify")}]
    diag = np.abs(np.diagonal(s, axis1=1, axis2=2))
    worst_rl = float(np.max(diag)) if diag.size else 0.0
    metrics["worst_return_loss_db"] = metric(
        round(20 * math.log10(max(worst_rl, 1e-15)), 4), "dB", "Worst |Sii|")
    if n >= 2:
        recip = float(np.max(np.abs(s - np.transpose(s, (0, 2, 1))))) if len(freq) else 0.0
        metrics["reciprocity_max_err"] = metric(recip, "", "Largest |Sij - Sji|")
        verdicts.append({"check": "reciprocity", "pass": recip <= tol,
                         "detail": f"largest |Sij - Sji| {recip:.3g}"})
        s21 = np.abs(s[:, 1, 0])
        metrics["s21_at_f_max_db"] = metric(
            round(20 * math.log10(max(float(s21[-1]), 1e-15)), 4), "dB", "S21 at the top frequency")
        metrics["s21_worst_db"] = metric(
            round(20 * math.log10(max(float(np.min(s21)), 1e-15)), 4), "dB", "Worst S21")
    return {"metrics": metrics, "verdicts": verdicts}


class EdaAgentEngine(Engine):
    info = EngineInfo(
        "eda-agent", "eda-agent solvers", "core", "Apache-2.0", LIBRARY,
        ("touchstone-check", "xsection-2d"),
        summary="This project's own solvers: the 2D field solver and the S-parameter "
                "checks. Always installed; the reference the other engines are "
                "compared with.",
        strengths=("always available", "every model documented with its accuracy"))

    def check(self, kind, spec, inputs):
        if kind == "xsection-2d":
            from ..engineering import toolkit
            names = {c["name"]: g["title"] for g in toolkit.catalog()["groups"]
                     for c in g["calculators"]}
            name = spec.get("calculator", "")
            if name and name not in names:
                return [f"no calculator {name!r}"]
        return []

    def run(self, job, folder, progress):
        if job.kind == "touchstone-check":
            from ..emc.touchstone import read_touchstone
            progress("reading the Touchstone file", 0.1)
            try:
                data = read_touchstone(job.input_path("touchstone"))
            except (OSError, ValueError) as exc:
                raise EngineError(str(exc)) from exc
            freq = np.asarray(data["freq_hz"], dtype=float)
            s = np.asarray(data["s"], dtype=complex).reshape(len(freq), data["ports"], data["ports"])
            out = s_quality(freq, s, _tol(job))
            out["metrics"]["z0_ohms"] = metric(float(data["z0"]), "ohm", "Reference impedance")
            out["notes"] = ["Touchstone 1.x reader of this project"]
            return out
        if job.kind == "xsection-2d":
            from ..engineering import toolkit
            progress("solving the cross-section", 0.2)
            res = toolkit.run(str(job.spec["calculator"]), dict(job.spec.get("inputs") or {}))
            if not res.get("ok"):
                raise EngineError(res.get("error") or "the calculator failed")
            metrics = {o["key"]: metric(o["value"], _UNITS.get(o.get("kind", ""), o.get("kind", "")),
                                        o.get("label", ""),
                                        display=o.get("display", ""))
                       for o in res.get("outputs", []) if isinstance(o.get("value"), (int, float))}
            (Path(folder) / "calculator.json").write_text(json.dumps(res, indent=1, default=str),
                                                          encoding="utf-8")
            return {"metrics": metrics, "notes": [res.get("model", "")] + list(res.get("warnings", [])),
                    "accuracy": res.get("accuracy", ""), "references": res.get("references", []),
                    "files": {"calculator": "calculator.json"}}
        raise EngineError(f"eda-agent does not run {job.kind} jobs")


class ScikitRfEngine(Engine):
    info = EngineInfo(
        "scikit-rf", "scikit-rf", "scikit-rf", "BSD-3-Clause", LIBRARY,
        ("touchstone-check",),
        summary="The scikit-rf project's Touchstone reader (1.x and 2.0) and network "
                "checks: an independent second opinion.",
        strengths=("Touchstone 2.0", "widely used and tested"),
        homepage="https://scikit-rf.readthedocs.io")

    def run(self, job, folder, progress):
        import skrf
        progress("reading with scikit-rf", 0.1)
        try:
            ntwk = skrf.Network(str(job.input_path("touchstone")))
        except Exception as exc:  # noqa: BLE001 - skrf raises many kinds
            raise EngineError(f"scikit-rf could not read the file: {exc}") from exc
        tol = _tol(job)
        out = s_quality(np.asarray(ntwk.f, dtype=float), np.asarray(ntwk.s, dtype=complex), tol)
        # scikit-rf's own verdicts, in its own terms.
        try:
            out["verdicts"].append({"check": "passivity (scikit-rf is_passive)",
                                    "pass": bool(ntwk.is_passive(tol=2 * tol)),
                                    "detail": "eigenvalues of I - S^H S"})
            if ntwk.nports >= 2:
                out["verdicts"].append({"check": "reciprocity (scikit-rf is_reciprocal)",
                                        "pass": bool(ntwk.is_reciprocal(tol=tol)),
                                        "detail": "S - S^T"})
        except Exception as exc:  # noqa: BLE001 - a check that fails is a note
            out.setdefault("notes", []).append(f"scikit-rf check failed: {exc}")
        z0 = np.asarray(ntwk.z0)
        out["metrics"]["z0_ohms"] = metric(float(np.real(z0.flat[0])) if z0.size else 50.0,
                                           "ohm", "Reference impedance")
        out.setdefault("notes", []).append(f"scikit-rf {skrf.__version__}")
        return out


class OpenEmsEngine(Engine):
    info = EngineInfo(
        "openems", "openEMS", "openems", "GPL-3.0 (own process)", EXTERNAL, ("em-3d",),
        summary="3D FDTD full-wave solver. Runs as its own process; minutes to hours.",
        strengths=("3D: vias, breakouts, connectors, antennas",),
        homepage="https://openems.de")

    _MODEL_KEYS = ("f_max_mhz", "f_min_mhz", "margin_mils", "port_ohms", "max_cells_m")

    def run(self, job, folder, progress):
        from ..emc.openems import ModelError, RUNNER, build_model, start_run
        from ..emc.stack import assumed_stack, stack_from_stackup
        from ..layout.model import LayoutBoard
        import shutil
        progress("building the openEMS model", 0.05)
        board = LayoutBoard.load(job.input_path("board"))
        if "stackup" in job.inputs:
            stackup = json.loads(job.input_path("stackup").read_text(encoding="utf-8"))
            stack = stack_from_stackup(stackup, board.layers)
        else:
            stack = assumed_stack(board.layers)
        sp = job.spec
        try:
            model = build_model(
                board, list(sp["nets"]), list(sp.get("ports") or []), stack,
                f_max_hz=float(sp.get("f_max_mhz", 6000.0)) * 1e6,
                f_min_hz=float(sp.get("f_min_mhz", 0.0)) * 1e6,
                margin_mils=float(sp.get("margin_mils", 200.0)),
                port_ohms=float(sp.get("port_ohms", 50.0)),
                max_cells=int(float(sp.get("max_cells_m", 20.0)) * 1e6))
        except ModelError as exc:
            raise EngineError(str(exc)) from exc
        folder = Path(folder)
        (folder / "model.json").write_text(json.dumps(model, indent=1), encoding="utf-8")
        shutil.copyfile(RUNNER, folder / "openems_runner.py")
        started = start_run(folder, "", int(sp.get("threads", 0) or 0))
        if not started.get("ok"):
            raise EngineError(started.get("error", "openEMS did not start")
                              + (f": {started['how']}" if started.get("how") else ""))
        return {"state": "running", "pid": started.get("pid"),
                "cells_estimate": model["estimate"]["cells"]}

    def poll(self, job, folder):
        from ..emc.openems import collect_results, run_status
        st = run_status(folder)
        if st["state"] == "running":
            return st
        if st["state"] != "done":
            return {"state": "failed", "error": st.get("error", "the run failed"),
                    "log_tail": st.get("log_tail", "")}
        res = collect_results(folder, plot=True)
        if not res.get("ok"):
            return {"state": "failed", "error": res.get("error", "no results")}
        from ..emc.touchstone import read_touchstone
        data = read_touchstone(res["touchstone"])
        freq = np.asarray(data["freq_hz"], dtype=float)
        s = np.asarray(data["s"], dtype=complex).reshape(len(freq), data["ports"], data["ports"])
        out = s_quality(freq, s, DEFAULT_TOL)
        files = {"touchstone": Path(res["touchstone"]).name}
        for key in ("plot", "sparams_png", "pattern_png"):
            if res.get(key):
                files[key] = Path(res[key]).name
        out.update({"state": "done", "files": files, "summary": res.get("summary"),
                    "notes": [res["warning"]] if res.get("warning") else []})
        return out

    def cancel(self, job, folder):
        from ..emc.openems import _read
        run = _read(Path(folder) / "run.json") or {}
        pid = int(run.get("pid", 0))
        if pid <= 0:
            return False
        import psutil
        try:
            psutil.Process(pid).terminate()
            return True
        except psutil.Error:
            return False


BUILTIN: tuple[Engine, ...] = (EdaAgentEngine(), ScikitRfEngine(), OpenEmsEngine())
