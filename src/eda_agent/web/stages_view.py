# SPDX-License-Identifier: Apache-2.0
"""The dashboard's stage pipeline: each tracked design project on its way
from Feasibility to Production, and the switch that turns the gates on.

Off by default. The switch writes ``stages`` in the profile's
phase_gates.json, the same write ``eda-agent phases-enable`` makes, so the
dashboard and the three design_phase_* tools always agree on whether the
gates are on. The projects and their answers live in the server's
workspace folder (eda_agent.design.project_stages), never in a repository.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from .. import profile as profile_mod
from ..design import phase_gates as pg
from ..design import project_stages as ps


def _workspace(app: Flask) -> Path:
    from ..config import get_config
    return Path(app.config.get("WORKSPACE_DIR") or get_config().workspace_dir)


def state(workspace: Path) -> dict[str, Any]:
    """Whether the gates are on, what decided it, and every tracked project."""
    found = profile_mod.describe_phase_stages()
    running = tuple(found["stages"])
    directory = profile_mod.resolve_profile_dir()
    return {"ok": True, "enabled": bool(running), "stages_run": list(running),
            "source": found["source"], "problem": found["problem"],
            "env": profile_mod.PHASE_STAGES_ENV,
            "can_switch": directory is not None and found["source"] != "environment",
            "all": [{"stage": s, "title": pg.TITLES[s], "short": ps.short_title(s)}
                    for s in pg.STAGES],
            "folder": str(ps.folder(workspace)),
            "projects": ps.overview(workspace, running) if running else []}


def switch(on: bool) -> dict[str, Any]:
    """Turn every stage on, or the gates off. Recorded answers are untouched."""
    found = profile_mod.describe_phase_stages()
    if found["source"] == "environment":
        return {"ok": False, "reason": f"{profile_mod.PHASE_STAGES_ENV} is set, so it decides "
                                       f"which stages run; unset it to switch them here"}
    directory = profile_mod.resolve_profile_dir()
    if directory is None:
        return {"ok": False, "reason": "no profile is in effect, so there is nowhere to keep "
                                       "the setting; choose or make one first"}
    out = profile_mod.write_phase_stages(directory, pg.STAGES if on else ())
    if out["ok"]:
        out["note"] = ("The AI's design_phase_* tools appear or go when the MCP server "
                       "restarts; the pipeline here follows at once.")
    return out


def open_projects() -> dict[str, Any]:
    """The .PrjPcb projects open in Altium, to offer for tracking."""
    from .dashboard import _bridge_call

    reply = _bridge_call("project.get_open_projects", {}, timeout=8.0)
    if reply is None:
        return {"ok": False, "reason": "Altium did not answer, so the open projects are "
                                       "unknown. Add a project by its path instead."}
    paths = [str(p.get("project_path") or "") for p in (reply.get("projects") or [])
             if isinstance(p, dict)]
    return {"ok": True, "projects": [p for p in paths
                                     if Path(p).suffix.lower() in ps.PROJECT_SUFFIXES]}


def register(app: Flask) -> None:
    @app.route("/api/wb/stages")
    def wb_stages() -> Response:
        return jsonify(state(_workspace(app)))

    @app.route("/api/wb/stages/switch", methods=["POST"])
    def wb_stages_switch() -> Response:
        body = request.get_json(silent=True) or {}
        return jsonify(switch(bool(body.get("on"))))

    @app.route("/api/wb/stages/open-projects")
    def wb_stages_open() -> Response:
        return jsonify(open_projects())

    @app.route("/api/wb/stages/project", methods=["POST"])
    def wb_stages_project() -> Response:
        body = request.get_json(silent=True) or {}
        what, ws = str(body.get("action") or ""), _workspace(app)
        if what == "add":
            return jsonify(ps.add(ws, str(body.get("path") or "")))
        if what == "remove":
            return jsonify(ps.remove(ws, str(body.get("product") or "")))
        if what == "revision":
            return jsonify(ps.set_revision(ws, str(body.get("product") or ""),
                                           str(body.get("revision") or "")))
        return jsonify({"ok": False, "reason": "action is add, remove or revision"})

    @app.route("/api/wb/stages/checks")
    def wb_stages_checks() -> Response:
        return jsonify(ps.stage_checks(_workspace(app), request.args.get("product", ""),
                                       request.args.get("stage", "")))

    @app.route("/api/wb/stages/answer", methods=["POST"])
    def wb_stages_answer() -> Response:
        body = request.get_json(silent=True) or {}
        running = profile_mod.get_phase_stages()
        if not running:
            return jsonify({"ok": False, "reason": "the stage gates are off"})
        return jsonify(ps.answer(_workspace(app), str(body.get("product") or ""),
                                 str(body.get("key") or ""), str(body.get("outcome") or ""),
                                 str(body.get("detail") or ""), running))
