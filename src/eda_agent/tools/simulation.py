# SPDX-License-Identifier: Apache-2.0
"""Features, dependencies and simulation engines.

``app_features`` says what this installation can do and what is missing;
``app_install_features`` installs it when the user agrees. The ``sim_job_*``
tools run a problem on one or more simulation engines (this project's own
solvers, scikit-rf, openEMS, plugins) and compare their answers.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

__all__ = ["register_simulation_tools"]


def _failed(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def register_simulation_tools(mcp) -> None:

    @mcp.tool()
    async def app_features(deep: bool = False, feature: str = "") -> dict[str, Any]:
        """What this eda-agent installation can do, and what is missing.

        Every feature, built in (signal and power integrity, thermal,
        EMI, placer and router, calculators) or optional (3D models,
        SPICE, openEMS, the Rust kernels, the dashboard, the simulation
        engines...), with its state (ready, partial, missing, off,
        unavailable), what it offers, its tools, and each dependency with
        its version, license, how it installs and where eda-agent looks
        for it. Use it when a tool reports a missing package or program.

        Args:
            deep: also run the slow checks (openEMS import, program versions).
            feature: one feature id to report; empty for all.
        """
        from ..features import report
        rep = await asyncio.to_thread(report, bool(deep))
        if feature:
            feats = [f for f in rep["features"] if f["id"] == feature]
            if not feats:
                return {"ok": False, "error": f"no feature {feature!r}; known: "
                        + ", ".join(f["id"] for f in rep["features"])}
            used = set(feats[0]["needs"]) | {d for g in feats[0]["any_of"] for d in g} \
                | set(feats[0]["adds"])
            rep = {**rep, "features": feats,
                   "dependencies": [d for d in rep["dependencies"] if d["id"] in used]}
        return rep

    @mcp.tool()
    async def app_install_features(features: list[str], confirm: bool = False,
                                   include_dev: bool = False) -> dict[str, Any]:
        """Install what features need. Ask the user first: it changes their
        Python environment.

        Without ``confirm`` it returns the plan and changes nothing: the pip
        packages, the Rust wheels it would build, the separate environments
        for GPL engines, and the programs the user must install by hand
        (openEMS, LTspice, KiCad...). With ``confirm=True`` it runs the
        automatic steps (pip into this Python, minutes for large packages)
        and returns each step's result and the log file.

        Args:
            features: feature ids from app_features, or ["all"].
            confirm: True to install; the user must have agreed.
            include_dev: with "all", also the test suite and website tools.
        """
        from ..features import install
        names = [str(f) for f in features or []]
        try:
            chosen = install.select([n for n in names if n != "all"],
                                    everything="all" in names, include_dev=bool(include_dev))
        except KeyError as exc:
            return {"ok": False, "error": str(exc.args[0])}
        if not chosen:
            return {"ok": False, "error": "name at least one feature, or 'all'"}
        plan = await asyncio.to_thread(install.plan, chosen)
        if not confirm or not plan.automatic:
            out = {"ok": True, "plan": plan.to_dict(), "installed": False}
            if plan.automatic:
                out["next_step"] = ("show the user this plan; with their OK call again "
                                    "with confirm=True")
            return out
        try:
            result = await asyncio.to_thread(install.execute, plan)
        except Exception as exc:  # noqa: BLE001
            return _failed(exc)
        result["note"] = ("packages installed into a running server are importable at "
                          "once; a reinstalled package that was already loaded needs a "
                          "restart of eda-agent")
        return result

    @mcp.tool()
    async def sim_engines(kind: str = "", deep: bool = False) -> dict[str, Any]:
        """The simulation engines, the job kinds, and which engines can run here.

        Each engine says its license, whether it runs in this process or as
        its own program, the job kinds it accepts, and whether its feature
        is installed and switched on.

        Args:
            kind: only the engines for this job kind.
            deep: run the slow availability checks.
        """
        from .. import simulation as sim
        from ..simulation.kinds import KINDS

        def collect():
            engines = [sim.engine_status(e, bool(deep)) for e in sim.engines()
                       if not kind or kind in e.info.kinds]
            kinds = [{"id": k.id, "title": k.title, "summary": k.summary,
                      "inputs": k.inputs,
                      "spec": {key: {"type": t, "required": r, "help": h}
                               for key, (t, r, h) in k.spec.items()},
                      "engines": [e.info.id for e in sim.engines_for(k.id)]}
                     for k in KINDS.values() if not kind or k.id == kind]
            return {"ok": True, "engines": engines, "kinds": kinds,
                    "plugin_errors": sim._load()["errors"]}
        return await asyncio.to_thread(collect)

    @mcp.tool()
    async def sim_job_run(kind: str, engines: list[str], spec: Optional[dict] = None,
                          inputs: Optional[dict] = None, title: str = "",
                          force: bool = False) -> dict[str, Any]:
        """Run one problem on one or more simulation engines.

        The job is a folder named by a hash of the problem, so the same
        problem is the same job: a finished engine's results are returned
        again without solving (unless ``force``), and a second engine lands
        beside the first for sim_job_compare. Engines that run in this
        process finish before this returns; an external one (openEMS) is
        started and keeps running: poll sim_job_status.

        Args:
            kind: the job kind (sim_engines lists them and their spec keys).
            engines: engine ids, e.g. ["eda-agent", "scikit-rf"].
            spec: the kind's settings, e.g. {"calculator": "microstrip",
                "inputs": {...}} for xsection-2d.
            inputs: input files by role, e.g. {"touchstone": "C:/x/line.s4p"}.
            title: a name for the job list.
            force: solve again even when results exist.
        """
        from .. import simulation as sim
        if not engines:
            return {"ok": False, "error": "name at least one engine (sim_engines lists them)"}
        out = []
        for e in engines:
            try:
                r = await asyncio.to_thread(sim.submit, kind, str(e), dict(spec or {}),
                                            {str(k): str(v) for k, v in (inputs or {}).items()},
                                            title, True, bool(force))
            except KeyError as exc:
                r = {"ok": False, "engine": e, "error": str(exc.args[0])}
            except Exception as exc:  # noqa: BLE001
                r = {"engine": e, **_failed(exc)}
            out.append(r)
        job = next((r.get("job") for r in out if r.get("job")), None)
        result: dict[str, Any] = {"ok": all(r.get("ok") for r in out), "job": job, "runs": out}
        done = [r for r in out if r.get("state") == "done"]
        if job and len(done) >= 2:
            result["compare"] = await asyncio.to_thread(sim.compare, job)
        return result

    @mcp.tool()
    async def sim_job_status(job: str, engine: str = "") -> dict[str, Any]:
        """A job's spec and every engine's state, results when done. Polls an
        external engine (openEMS) first.

        Args:
            job: the job id from sim_job_run or sim_jobs.
            engine: only this engine.
        """
        from .. import simulation as sim
        try:
            return await asyncio.to_thread(sim.refresh, job, engine)
        except KeyError as exc:
            return {"ok": False, "error": str(exc.args[0])}

    @mcp.tool()
    async def sim_job_compare(job: str, rel_tol: float = 0.02) -> dict[str, Any]:
        """Set the engines' answers on one job side by side: every metric two
        or more engines gave, its spread, and any check they disagree on.

        Args:
            job: the job id.
            rel_tol: relative spread still counted as agreement (0.02 = 2 %).
        """
        from .. import simulation as sim
        try:
            return await asyncio.to_thread(sim.compare, job, float(rel_tol))
        except KeyError as exc:
            return {"ok": False, "error": str(exc.args[0])}

    @mcp.tool()
    async def sim_jobs(limit: int = 30) -> dict[str, Any]:
        """Recent simulation jobs, newest first, with each engine's state."""
        from ..simulation import jobs
        rows = await asyncio.to_thread(jobs.list_jobs, None, max(1, int(limit)))
        return {"ok": True, "folder": str(jobs.jobs_dir()), "jobs": rows}
