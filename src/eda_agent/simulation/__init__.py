# SPDX-License-Identifier: Apache-2.0
"""Simulation engines and jobs.

Every simulator, this project's own or another project's, is an engine:
it takes a job folder and writes normalised results into its own run
folder. The engines are gated by the features registry (installed, and
switched on), so what the dashboard's Features page shows is what the
Simulation page offers.

Other packages add engines through the ``eda_agent.engines`` entry-point
group: a callable that returns an ``Engine`` or a list of them. Its
``info.feature`` must name a feature, which the same package can add
through ``eda_agent.features``.
"""
from __future__ import annotations

import importlib.metadata as md
import os
import threading
import traceback
from typing import Any, Optional

from . import jobs as _jobs
from .engine import EXTERNAL, LIBRARY, Engine, EngineError, EngineInfo, JobKind, metric
from .kinds import KINDS, check_spec

__all__ = ["Engine", "EngineError", "EngineInfo", "JobKind", "metric", "KINDS",
           "engines", "get_engine", "engine_status", "submit", "refresh", "compare"]

GROUP = "eda_agent.engines"

_lock = threading.Lock()
_loaded: Optional[dict] = None
_threads: dict[str, threading.Thread] = {}


def _load(refresh: bool = False) -> dict:
    global _loaded
    if _loaded is not None and not refresh:
        return _loaded
    from .builtin import BUILTIN
    found: dict[str, Engine] = {e.info.id: e for e in BUILTIN}
    errors = []
    try:
        eps = md.entry_points(group=GROUP)
    except Exception:  # noqa: BLE001
        eps = ()
    from ..features.registry import load as load_features
    feature_ids = {f.id for f in load_features()["features"]}
    for ep in eps:
        origin = f"{ep.name} ({ep.value})"
        try:
            made = ep.load()()
            items = made if isinstance(made, list) else [made]
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            errors.append({"plugin": origin, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for e in items:
            info = getattr(e, "info", None)
            if not isinstance(e, Engine) or not isinstance(info, EngineInfo):
                errors.append({"plugin": origin, "error": "not an Engine with an EngineInfo"})
            elif info.id in found:
                errors.append({"plugin": origin, "error": f"engine id {info.id!r} is taken"})
            elif info.feature not in feature_ids:
                errors.append({"plugin": origin,
                               "error": f"feature {info.feature!r} is not registered"})
            elif info.process not in (LIBRARY, EXTERNAL):
                errors.append({"plugin": origin, "error": f"process {info.process!r}"})
            elif [k for k in info.kinds if k not in KINDS]:
                errors.append({"plugin": origin, "error": "unknown job kinds "
                               + ", ".join(k for k in info.kinds if k not in KINDS)})
            else:
                found[info.id] = e
    _loaded = {"engines": found, "errors": errors}
    return _loaded


def engines() -> list[Engine]:
    return list(_load()["engines"].values())


def get_engine(engine_id: str) -> Engine:
    try:
        return _load()["engines"][engine_id]
    except KeyError:
        raise KeyError(f"no engine {engine_id!r}; known: "
                       + ", ".join(sorted(_load()["engines"]))) from None


def engine_status(engine: Engine, deep: bool = False) -> dict[str, Any]:
    """Whether an engine can run here: its feature ready and switched on."""
    from ..features import detect, registry, state
    info = engine.info
    f = registry.get_feature(info.feature)
    deps = {d: detect.dependency_status(registry.get_dependency(d), deep, f)
            for d in f.all_deps()}
    fs = detect.feature_status(f, deps, f.switchable and not state.is_on(f.id))
    return {"id": info.id, "title": info.title, "feature": info.feature,
            "license": info.license, "process": info.process, "kinds": list(info.kinds),
            "summary": info.summary, "strengths": list(info.strengths),
            "homepage": info.homepage, "state": fs.state,
            "available": fs.state in (detect.READY, detect.PARTIAL),
            "missing": fs.missing,
            "versions": {d: s.version for d, s in deps.items() if s.version}}


def engines_for(kind_id: str) -> list[Engine]:
    return [e for e in engines() if kind_id in e.info.kinds]


def _progress(folder):
    def say(text: str, fraction: Optional[float] = None) -> None:
        extra: dict[str, Any] = {"message": text}
        if fraction is not None:
            extra["progress"] = max(0.0, min(1.0, float(fraction)))
        _jobs.set_status(folder, _jobs.RUNNING, **extra)
    return say


def _finish(engine: Engine, job: _jobs.Job, folder, out: dict[str, Any]) -> dict[str, Any]:
    out = dict(out)
    state = out.pop("state", _jobs.DONE)
    if state == _jobs.RUNNING:
        _jobs.set_status(folder, _jobs.RUNNING, message="running", **{
            k: v for k, v in out.items() if k in ("pid", "cells_estimate")})
        return {"state": _jobs.RUNNING}
    if state == _jobs.FAILED:
        _jobs.set_status(folder, _jobs.FAILED, error=out.get("error", "failed"),
                         log_tail=out.get("log_tail", ""))
        return {"state": _jobs.FAILED, "error": out.get("error", "failed")}
    res = {"ok": True, "engine": engine.info.id, "kind": job.kind, **out}
    _jobs.write_json(folder / "results.json", res)
    _jobs.set_status(folder, _jobs.DONE, message="done", progress=1.0)
    return {"state": _jobs.DONE, "results": res}


def _execute(engine: Engine, job: _jobs.Job, folder) -> dict[str, Any]:
    work = folder / "work"
    work.mkdir(parents=True, exist_ok=True)
    try:
        out = engine.run(job, work, _progress(folder))
    except EngineError as exc:
        _jobs.set_status(folder, _jobs.FAILED, error=str(exc))
        return {"state": _jobs.FAILED, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the user sees why, the log the trace
        (folder / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        _jobs.set_status(folder, _jobs.FAILED, error=f"{type(exc).__name__}: {exc}")
        return {"state": _jobs.FAILED, "error": f"{type(exc).__name__}: {exc}"}
    return _finish(engine, job, folder, out or {})


def submit(kind: str, engine_id: str, spec: Optional[dict] = None,
           inputs: Optional[dict[str, str]] = None, title: str = "",
           wait: bool = True, force: bool = False) -> dict[str, Any]:
    """Create (or find) the job and run it on one engine.

    ``wait`` runs a library engine here and returns its results; without
    it the run goes to a background thread and this returns at once. An
    external engine always returns once its program has started; poll
    with ``refresh``. A finished run is not repeated unless ``force``.
    """
    spec, inputs = dict(spec or {}), dict(inputs or {})
    engine = get_engine(engine_id)
    if kind not in engine.info.kinds:
        return {"ok": False, "error": f"{engine_id} does not run {kind} jobs; it runs "
                                      + ", ".join(engine.info.kinds)}
    problems = check_spec(kind, spec, inputs) + engine.check(kind, spec, inputs)
    if problems:
        return {"ok": False, "error": "; ".join(problems)}
    st = engine_status(engine)
    if not st["available"]:
        why = ("switched off on the Features page" if st["state"] == "off"
               else "missing: " + ", ".join(st["missing"]) if st["missing"] else st["state"])
        return {"ok": False, "error": f"engine {engine_id} is not available ({why})",
                "feature": engine.info.feature}
    try:
        job = _jobs.create(kind, spec, inputs, title)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    folder = job.run_folder(engine_id)
    with _lock:
        current = _jobs.read_json(folder / "status.json") or {}
        if current.get("state") == _jobs.RUNNING:
            return {"ok": True, "job": job.id, "engine": engine_id, "state": _jobs.RUNNING,
                    "note": "already running"}
        if current.get("state") == _jobs.DONE and not force:
            return {"ok": True, "job": job.id, "engine": engine_id, "state": _jobs.DONE,
                    "results": _jobs.read_json(folder / "results.json"), "cached": True}
        folder.mkdir(parents=True, exist_ok=True)
        for stale in ("results.json", "error.log"):
            (folder / stale).unlink(missing_ok=True)
        _jobs.write_json(folder / "status.json", {"state": _jobs.QUEUED})
        _jobs.set_status(folder, _jobs.RUNNING, message="starting", progress=0.0,
                         owner_pid=os.getpid())
    if wait or engine.info.process == EXTERNAL:
        out = _execute(engine, job, folder)
    else:
        key = f"{job.id}/{engine_id}"
        t = threading.Thread(target=_execute, args=(engine, job, folder),
                             name=f"sim-{key}", daemon=True)
        _threads[key] = t
        t.start()
        out = {"state": _jobs.RUNNING}
    return {"ok": out.get("state") != _jobs.FAILED, "job": job.id, "engine": engine_id,
            "folder": str(folder), **out}


def refresh(job_id: str, engine_id: str = "") -> dict[str, Any]:
    """A job with every engine's state; external runs are polled first."""
    job = _jobs.get(job_id)
    for name, st in _jobs.runs(job).items():
        if engine_id and name != engine_id:
            continue
        if st.get("state") != _jobs.RUNNING:
            continue
        try:
            engine = get_engine(name)
        except KeyError:
            continue
        if engine.info.process == EXTERNAL:
            try:
                out = engine.poll(job, job.work_folder(name))
            except Exception as exc:  # noqa: BLE001
                out = {"state": _jobs.FAILED, "error": f"{type(exc).__name__}: {exc}"}
            if out.get("state") != _jobs.RUNNING:
                _finish(engine, job, job.run_folder(name), out)
            else:
                _jobs.set_status(job.run_folder(name), _jobs.RUNNING,
                                 message=out.get("message", "running"),
                                 **({"progress": out["progress"]} if "progress" in out else {}))
        else:
            _fail_if_orphaned(job, name, st)
    return {"ok": True, **job.to_dict(), "folder": str(job.folder), "runs": _jobs.runs(job)}


def _fail_if_orphaned(job: _jobs.Job, name: str, st: dict) -> None:
    """Mark a library run failed when the process that ran it is gone.

    The owner check comes first and the status is read again after it:
    a run that finishes between the caller's read and this check has
    already written its result, and must not be overwritten."""
    with _lock:
        if _owner_alive(job, name, st):
            return
        folder = job.run_folder(name)
        now = _jobs.read_json(folder / "status.json") or {}
        if now.get("state") != _jobs.RUNNING:
            return
        # A library run whose process is gone (eda-agent stopped).
        _jobs.set_status(folder, _jobs.FAILED,
                         error="the run was interrupted (eda-agent stopped)")


def _owner_alive(job: _jobs.Job, name: str, st: dict) -> bool:
    pid = int(st.get("owner_pid") or 0)
    if pid == os.getpid():
        t = _threads.get(f"{job.id}/{name}")
        # Run in this process: in a thread, or by a caller still inside
        # submit(wait=True), which has no thread entry.
        return t is None or t.is_alive()
    if pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001 - cannot tell: assume it runs
        return True


def cancel(job_id: str, engine_id: str) -> dict[str, Any]:
    job = _jobs.get(job_id)
    engine = get_engine(engine_id)
    folder = job.run_folder(engine_id)
    st = _jobs.read_json(folder / "status.json") or {}
    if st.get("state") != _jobs.RUNNING:
        return {"ok": False, "error": f"{engine_id} is not running on {job_id}"}
    stopped = engine.cancel(job, job.work_folder(engine_id))
    if not stopped:
        return {"ok": False, "error": f"{engine_id} cannot be stopped from here"}
    _jobs.set_status(folder, _jobs.CANCELLED, message="cancelled")
    return {"ok": True}


def compare(job_id: str, rel_tol: float = 0.02) -> dict[str, Any]:
    """Every metric two or more engines reported on this job, side by side,
    with the largest relative spread and whether it is within ``rel_tol``;
    and every verdict the engines disagree on."""
    job = _jobs.get(job_id)
    done = {name: st["results"] for name, st in _jobs.runs(job).items()
            if st.get("state") == _jobs.DONE and isinstance(st.get("results"), dict)}
    names = sorted(done)
    rows = []
    keys = sorted({k for r in done.values() for k in (r.get("metrics") or {})})
    for key in keys:
        vals = {}
        for n in names:
            m = (done[n].get("metrics") or {}).get(key)
            if isinstance(m, dict) and isinstance(m.get("value"), (int, float)) \
                    and not isinstance(m.get("value"), bool):
                vals[n] = float(m["value"])
        if len(vals) < 2:
            continue
        lo, hi = min(vals.values()), max(vals.values())
        scale = max(abs(lo), abs(hi))
        spread = (hi - lo) / scale if scale > 0 else 0.0
        unit = next((done[n]["metrics"][key].get("unit", "") for n in vals), "")
        label = next((done[n]["metrics"][key].get("label", "") for n in vals), key)
        rows.append({"metric": key, "label": label, "unit": unit, "values": vals,
                     "spread": spread, "agree": spread <= rel_tol or (hi - lo) <= 1e-12})
    disagreements = []
    by_check: dict[str, dict[str, bool]] = {}
    for n in names:
        for v in done[n].get("verdicts") or []:
            by_check.setdefault(v["check"], {})[n] = bool(v.get("pass"))
    for check, votes in by_check.items():
        if len(votes) >= 2 and len(set(votes.values())) > 1:
            disagreements.append({"check": check, "votes": votes})
    return {"ok": True, "job": job.id, "kind": job.kind, "engines": names,
            "rel_tol": rel_tol, "metrics": rows,
            "agree": all(r["agree"] for r in rows) and not disagreements,
            "verdict_disagreements": disagreements}
