# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Live layout view tools: watch a board change, and say why first.

The in-house placer and router publish a snapshot of the board when a job
starts and ends, and progress while routing (``design/live.py``). These
tools open the dashboard's Layout tab on that view, log a decision before
acting on it, and publish a board by hand. None of them touches the
bridge: the view is drawn from files in the workspace.

Intended use while laying out a board:

1. ``design_live_view`` once, and give the user the URL.
2. Before each step (a block's placement, a routing order, a rule
   change), ``design_live_note`` with what is about to happen and why.
3. Run the step. ``pcb_autoplace`` and ``pcb_autoroute`` jobs publish on
   their own; ``design_live_snapshot`` publishes any other board state.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8766

# The dashboard this module started, when the MCP server had not.
_server: dict[str, Any] = {}
_server_lock = threading.Lock()


def _probe(host: str, port: int, timeout: float = 1.0) -> str:
    """What answers on ``host:port``: ``live`` (a dashboard with the Layout
    tab), ``other`` (something without it) or ``none``.

    Asked of ``/api/live`` itself, not just the port: an older eda-agent
    process left holding the port answers there too, and a URL into it
    shows no Layout tab. Plain ``http.client``, which never goes through
    a proxy: a system proxy must not see, or answer for, a loopback call.
    """
    import http.client

    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/api/live")
        resp = conn.getresponse()
        status, body = resp.status, resp.read()
    except http.client.HTTPException:
        return "other"
    except OSError:
        return "none"
    finally:
        conn.close()
    try:
        data = json.loads(body.decode("utf-8", "replace") or "{}")
    except ValueError:
        return "other"
    return "live" if status == 200 and isinstance(data, dict) and data.get("ok") else "other"


def _disabled_by_env() -> bool:
    """The MCP server's own dashboard opt-out, when the server is loaded.

    Read from the running server module rather than a second copy of its
    list of variable names, which would drift.
    """
    server = sys.modules.get("eda_agent.server")
    check = getattr(server, "_dashboard_disabled_via_env", None)
    return bool(check()) if callable(check) else False


def _ensure_dashboard(host: str = DASHBOARD_HOST,
                      port: int = DASHBOARD_PORT) -> tuple[str, str]:
    """Make sure the dashboard answers on ``host:port``.

    Returns ``(state, detail)``: ``running`` (already up, in this process
    or a standalone ``eda-agent dashboard``), ``started`` (started here),
    ``disabled`` (the user turned the dashboard off) or ``failed``.
    """
    seen = _probe(host, port)
    if seen == "live":
        return "running", ""
    if seen == "other":
        return "failed", (f"something on {host}:{port} answers but has no Layout tab, "
                          "most likely an older eda-agent process still holding the "
                          "port. Stop it (`eda-agent stop-dashboard`, or end the "
                          "process) and call this again.")
    if _disabled_by_env():
        return "disabled", ("the dashboard is turned off for this server (headless "
                            "mode). Run `eda-agent dashboard` in a terminal to see it.")
    with _server_lock:
        if _server.get("srv") is not None and _probe(host, port) == "live":
            return "running", ""
        try:
            from werkzeug.serving import make_server

            from ..web.dashboard import create_app
        except ImportError as exc:
            return "failed", (f"the dashboard needs Flask ({exc}); install the web "
                              "extra: pip install eda-agent[web]")
        try:
            # make_server binds here, so a port already taken fails now
            # rather than on a background thread nobody reads.
            srv = make_server(host, port, create_app(), threaded=True)
        except OSError as exc:
            return "failed", f"could not bind {host}:{port}: {exc}"
        t = threading.Thread(target=srv.serve_forever, name="dashboard-server",
                             daemon=True)
        t.start()
        _server.update(srv=srv, thread=t)
    return "started", ""


def register_live_tools(mcp):
    """Register the live layout view tools with the MCP server."""

    @mcp.tool()
    async def design_live_view(open_browser: bool = False) -> dict[str, Any]:
        """Open the live layout view and return its URL.

        Makes sure the local dashboard is running (it usually is, started
        with the MCP server) and points at its Layout tab, which draws the
        board as the in-house placer and router change it: parts, pads,
        tracks per layer, vias, pours, blocks and planned lanes, with new
        and moved items glowing for 30 s and removed copper fading out.
        Next to it is the decision log, newest first, and a status line
        (routed n / N nets, k partly). Give the URL to the user so they can
        watch while you work. Reads files only; no bridge call.

        Args:
            open_browser: also open the URL in the default browser.

        Returns:
            ``{"url", "dashboard": "running"|"started"|"disabled"|"failed",
            "version", "board", "status"}``, plus ``"reason"`` when the
            dashboard is not up. ``version`` is 0 until a board has been
            published.
        """
        import asyncio

        from ..design import live

        state, detail = await asyncio.to_thread(_ensure_dashboard)
        url = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/#layout"
        out: dict[str, Any] = {"url": url, "dashboard": state}
        if detail:
            out["reason"] = detail
        doc = await asyncio.to_thread(live.read_board)
        snap = (doc or {}).get("snapshot") or {}
        status = snap.get("status") or {}
        out["version"] = int((doc or {}).get("version") or 0)
        out["board"] = snap.get("board", "")
        out["status"] = {k: status[k] for k in ("nets", "routed", "partly", "not_started")
                         if k in status}
        if open_browser and state in ("running", "started"):
            try:
                import webbrowser
                webbrowser.open(url)
                out["opened"] = True
            except Exception as exc:  # noqa: BLE001 - reported, not fatal
                out["opened"] = False
                out["open_error"] = str(exc)
        return out

    @mcp.tool()
    async def design_live_note(section: str, text: str) -> dict[str, Any]:
        """Log a decision to the live view, before acting on it.

        The Layout tab lists decisions newest first next to the board, so
        the user sees what is about to happen and why, and can stop it.
        Call this BEFORE the step it describes, one decision per call, in
        a sentence or two: what you will do and the reason (the datasheet
        figure, the rule, the measurement).

        Args:
            section: what the decision is about, a short label such as
                ``placement``, ``routing``, ``rules`` or a block's name.
            text: the decision and its reason.

        Returns:
            The logged entry ``{"seq", "time", "ts", "section", "text"}``,
            or ``{"error": ...}`` when ``text`` is empty.
        """
        from ..design import live

        if not str(text).strip():
            return {"error": "text is empty: say what you are about to do and why"}
        return live.note(str(section).strip() or "note", str(text).strip())

    @mcp.tool()
    async def design_live_snapshot(note: str = "",
                                   board_json_path: str = "",
                                   expect_file: str = "") -> dict[str, Any]:
        """Publish a board to the live view as its next version.

        Placement and routing jobs publish on their own; use this for any
        other state: the live board (``expect_file``, a few seconds on a
        large board, and never done unasked because reading some boards
        has crashed Altium's scripting engine), a LayoutBoard JSON file,
        or the last board again with a note.

        Args:
            note: what this snapshot shows; also logged as a decision.
            board_json_path: a LayoutBoard JSON file.
            expect_file: full path of the focused board, to read it from
                Altium as it is now. The read refuses if another board is
                focused.
            (Neither: republish the board the last engine job, apply or
            snapshot in this server published.)

        Returns:
            ``{"version", "board", "status": {nets, routed, partly,
            not_started}, "changes": {added, moved, removed}, "seconds",
            "url"}``, or ``{"error": ...}``.
        """
        import asyncio

        from ..design import live
        from ..layout.model import LayoutBoard

        if board_json_path:
            path = Path(board_json_path).expanduser()
            if not path.is_file():
                return {"error": f"no such file: {path}"}
            try:
                board = await asyncio.to_thread(LayoutBoard.load, path)
            except Exception as exc:  # noqa: BLE001 - reported to the caller
                return {"error": f"could not read a LayoutBoard from {path}: {exc}"}
        elif expect_file:
            from ..layout.read_altium import WrongBoard, read_live_board
            try:
                board = await asyncio.to_thread(read_live_board, expect_file)
            except WrongBoard as exc:
                return {"error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - reported to the caller
                return {"error": f"board read failed: {exc}"}
        else:
            board = live.last_board()
            if board is None:
                return {"error": "no board has been published in this server yet; "
                                 "pass board_json_path"}
        try:
            out = await asyncio.to_thread(live.publish, board, note, "manual")
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            return {"error": f"publish failed: {exc}"}
        out["url"] = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/#layout"
        return out
