# SPDX-License-Identifier: Apache-2.0
"""The dashboard's Usage view: the tokens the AI spent on the EDA tools.

Reads the records eda_agent.diag.usage keeps per workspace, so a
dashboard started on its own (``eda-agent dashboard``) shows them as
well as the one an MCP server runs. Every workspace with records is
offered; the newest is shown first. Read only: the switch is the
``usage_stats`` setting (the Config tab in Altium, or the profile in
the Workspace view).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from ..diag import usage


def state(workspace: str = "", days: int = 0) -> dict[str, Any]:
    known = usage.known_workspaces()
    paths = [str(r["path"]) for r in known]
    live = usage.get_recorder()
    # This process's own folder only when it has records: a dashboard
    # started on its own records nothing, and its start folder offered
    # first hid the workspaces that have them.
    if (live.workspace is not None and str(live.workspace) not in paths
            and usage.usage_dir(live.workspace).is_dir()):
        paths.append(str(live.workspace))
    chosen = workspace if workspace in paths else (paths[0] if paths else "")
    report = usage.workspace_report(Path(chosen), days) if chosen else None
    out: dict[str, Any] = {
        "ok": True, "on": usage.setting_on(), "workspaces": paths,
        "workspace": chosen, "days": days, "report": report,
        "chars_per_token": usage.CHARS_PER_TOKEN,
    }
    if live.session and str(live.workspace or "") == chosen:
        snap = live.snapshot()
        if snap["this_session"]["total"]["calls"]:
            out["session"] = {"tag": snap["session"], **snap["this_session"]}
    return out


def register(app: Flask) -> None:
    @app.route("/api/wb/usage")
    def wb_usage() -> Response:
        try:
            days = max(0, int(request.args.get("days") or 0))
        except ValueError:
            days = 0
        return jsonify(state(str(request.args.get("workspace") or ""), days))
