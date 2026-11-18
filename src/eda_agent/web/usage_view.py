# SPDX-License-Identifier: Apache-2.0
"""The dashboard's Usage view: the tokens the AI spent on the EDA tools.

Reads the records eda_agent.diag.usage keeps per workspace, so a
dashboard started on its own (``eda-agent dashboard``) shows them as
well as the one an MCP server runs. Read only: the switch is the
``usage_stats`` setting (the Config tab in Altium, or the profile in
the Workspace view).

A workspace is usually one client's design folder, so by default the
view offers only the dashboard's own one: the design workspace (the
folder ``eda-agent start`` keeps .eda-agent/workspace.json in) at or
above the folder the dashboard runs in, else that folder itself, and
the folders inside it. The other workspaces are counted, never named,
until the viewer asks for all of them (``?all=1``, the "All workspaces"
box, off by default). Without it a ``workspace`` outside that scope is
ignored, so a link cannot open another client's records either.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from flask import Flask, Response, jsonify, request

from ..diag import usage


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _inside(path: str, root: str) -> bool:
    """``path`` is ``root`` or a folder under it."""
    p, r = _norm(path), _norm(root)
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # on another drive
        return False


def current_scope() -> Optional[str]:
    """The dashboard's own workspace, or None when it has none."""
    live = usage.get_recorder().workspace
    if live is None:
        return None
    from ..workspace_start import find_workspace
    try:
        root = find_workspace(Path(live))
    except OSError:
        root = None
    return str(root if root is not None else live)


def state(workspace: str = "", days: int = 0, show_all: bool = False) -> dict[str, Any]:
    known = usage.known_workspaces()
    paths = [str(r["path"]) for r in known]
    live = usage.get_recorder()
    # This process's own folder only when it has records: a dashboard
    # started on its own records nothing, and its start folder offered
    # first hid the workspaces that have them.
    if (live.workspace is not None and str(live.workspace) not in paths
            and usage.usage_dir(live.workspace).is_dir()):
        paths.append(str(live.workspace))
    scope = current_scope()
    hidden = 0
    if not show_all:
        mine = [p for p in paths if scope is not None and _inside(p, scope)]
        hidden = len(paths) - len(mine)
        paths = mine
    chosen = workspace if workspace in paths else (paths[0] if paths else "")
    report = usage.workspace_report(Path(chosen), days) if chosen else None
    out: dict[str, Any] = {
        "ok": True, "on": usage.setting_on(), "workspaces": paths,
        "workspace": chosen, "days": days, "report": report,
        "chars_per_token": usage.CHARS_PER_TOKEN,
        "all": show_all, "scope": scope or "", "hidden": hidden,
    }
    if live.session and str(live.workspace or "") == chosen:
        snap = live.snapshot()
        if snap["this_session"]["total"]["calls"]:
            out["session"] = {"tag": snap["session"], **snap["this_session"]}
    return out


def _flag(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def register(app: Flask) -> None:
    @app.route("/api/wb/usage")
    def wb_usage() -> Response:
        try:
            days = max(0, int(request.args.get("days") or 0))
        except ValueError:
            days = 0
        return jsonify(state(str(request.args.get("workspace") or ""), days,
                             show_all=_flag(request.args.get("all"))))
