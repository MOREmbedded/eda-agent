# SPDX-License-Identifier: Apache-2.0
"""The dashboard's Features and Simulation views.

Features: every feature with its state, its dependencies with versions
and licenses, an install button that runs the same installer as
``eda-agent install-deps`` (one install at a time, its output streamed
to the page), and the on/off switch of the simulation engines.

Simulation: the engines, the jobs, a job's results from each engine side
by side, and runs started from the page (the same ``simulation.submit``
the ``sim_job_run`` tool calls).

Both are served by the local dashboard, behind its loopback guard.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from flask import Flask, Response, jsonify, request


class _Installer:
    """The one install the dashboard runs at a time."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.lines: list[str] = []
        self.result: dict | None = None
        self.features: list[str] = []
        self.started = 0.0

    def start(self, names: list[str], everything: bool) -> dict[str, Any]:
        from ..features import install
        try:
            chosen = install.select(names, everything=everything)
        except KeyError as exc:
            return {"ok": False, "error": str(exc.args[0])}
        if not chosen:
            return {"ok": False, "error": "no feature chosen"}
        plan = install.plan(chosen)
        if not plan.automatic:
            return {"ok": True, "started": False, "plan": plan.to_dict()}
        with self.lock:
            if self.running:
                return {"ok": False, "error": "an install is already running"}
            self.running, self.lines, self.result = True, [], None
            self.features, self.started = plan.features, time.time()

        def work() -> None:
            try:
                res = install.execute(plan, on_line=self._line)
            except Exception as exc:  # noqa: BLE001 - shown on the page
                res = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "steps": []}
            with self.lock:
                self.result, self.running = res, False

        threading.Thread(target=work, name="features-install", daemon=True).start()
        return {"ok": True, "started": True, "plan": plan.to_dict()}

    def _line(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)
            del self.lines[:-400]

    def state(self) -> dict[str, Any]:
        with self.lock:
            return {"running": self.running, "features": self.features,
                    "started": self.started, "lines": list(self.lines[-200:]),
                    "result": self.result}


INSTALLER = _Installer()


def _report(deep: bool) -> dict[str, Any]:
    from .. import simulation as sim
    from ..features import report
    rep = report(deep=deep)
    rep["install"] = INSTALLER.state()
    rep["engines"] = [{"id": e.info.id, "feature": e.info.feature, "title": e.info.title}
                      for e in sim.engines()]
    return rep


def register(app: Flask) -> None:
    @app.route("/api/wb/features")
    def wb_features() -> Response:
        return jsonify(_report(request.args.get("deep") == "1"))

    @app.route("/api/wb/features/switch", methods=["POST"])
    def wb_features_switch() -> Response:
        from ..features import state
        body = request.get_json(silent=True) or {}
        try:
            off = state.set_switch(str(body.get("id") or ""), bool(body.get("on")))
        except (KeyError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc.args[0] if exc.args else exc)})
        return jsonify({"ok": True, "off": sorted(off)})

    @app.route("/api/wb/features/plan", methods=["POST"])
    def wb_features_plan() -> Response:
        from ..features import install
        body = request.get_json(silent=True) or {}
        names = [str(n) for n in body.get("features") or []]
        try:
            chosen = install.select(names, everything=bool(body.get("all")))
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc.args[0])})
        return jsonify({"ok": True, "plan": install.plan(chosen).to_dict()})

    @app.route("/api/wb/features/install", methods=["GET", "POST"])
    def wb_features_install() -> Response:
        if request.method == "GET":
            return jsonify({"ok": True, **INSTALLER.state()})
        body = request.get_json(silent=True) or {}
        names = [str(n) for n in body.get("features") or []]
        return jsonify(INSTALLER.start(names, bool(body.get("all"))))

    @app.route("/api/wb/sim/engines")
    def wb_sim_engines() -> Response:
        from .. import simulation as sim
        from ..simulation.kinds import KINDS
        return jsonify({
            "ok": True,
            "engines": [sim.engine_status(e) for e in sim.engines()],
            "kinds": [{"id": k.id, "title": k.title, "summary": k.summary,
                       "inputs": k.inputs,
                       "engines": [e.info.id for e in sim.engines_for(k.id)]}
                      for k in KINDS.values()],
            "errors": sim._load()["errors"]})

    @app.route("/api/wb/sim/jobs")
    def wb_sim_jobs() -> Response:
        from ..simulation import jobs
        return jsonify({"ok": True, "folder": str(jobs.jobs_dir()),
                        "jobs": jobs.list_jobs(limit=200)})

    @app.route("/api/wb/sim/job/<job_id>")
    def wb_sim_job(job_id: str) -> Response:
        from .. import simulation as sim
        try:
            out = sim.refresh(job_id)
            out["compare"] = sim.compare(job_id)
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc.args[0])})
        # JSON objects lose their order on the way; the engine's own order
        # (headline figures first) goes as a list.
        for run in out.get("runs", {}).values():
            res = run.get("results")
            if isinstance(res, dict) and isinstance(res.get("metrics"), dict):
                res["metric_order"] = list(res["metrics"])
        # Engines that could also take this job, by their own check.
        runnable = []
        for e in sim.engines_for(out.get("kind", "")):
            try:
                if not e.check(out["kind"], out.get("spec") or {}, out.get("inputs") or {}):
                    runnable.append(e.info.id)
            except Exception:  # noqa: BLE001 - a check that fails is a no
                pass
        out["runnable"] = runnable
        return jsonify(out)

    @app.route("/api/wb/sim/file/<job_id>/<engine>/<name>")
    def wb_sim_file(job_id: str, engine: str, name: str) -> Response:
        """A file an engine wrote (plot, Touchstone), from its work folder."""
        from flask import abort, send_file
        from ..simulation import jobs
        ok = lambda s: bool(s) and all(c.isalnum() or c in "-_." for c in s) and not s.startswith(".")  # noqa: E731
        if not ok(engine) or not ok(name):
            abort(404)
        try:
            job = jobs.get(job_id)
        except KeyError:
            abort(404)
        path = job.work_folder(engine) / name
        if not path.is_file():
            abort(404)
        return send_file(path, as_attachment=not name.lower().endswith(".png"), download_name=name)

    @app.route("/api/wb/sim/run", methods=["POST"])
    def wb_sim_run() -> Response:
        from .. import simulation as sim
        body = request.get_json(silent=True) or {}
        spec = body.get("spec") if isinstance(body.get("spec"), dict) else {}
        inputs = body.get("inputs") if isinstance(body.get("inputs"), dict) else {}
        try:
            out = sim.submit(str(body.get("kind") or ""), str(body.get("engine") or ""),
                             spec, {str(k): str(v) for k, v in inputs.items() if v},
                             str(body.get("title") or ""), wait=False,
                             force=bool(body.get("force")))
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc.args[0])})
        return jsonify(out)
