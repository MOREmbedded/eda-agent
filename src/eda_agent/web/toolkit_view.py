# SPDX-License-Identifier: Apache-2.0
"""The dashboard's Calculators view: the PCB toolkit, run by hand.

Serves the calculator catalogue (the forms are built from it), runs a
calculator, and reads the shown board's layer stack as ready-made
inputs. The calculators themselves are eda_agent.engineering.toolkit;
the MCP tools (tools/toolkit.py) run the same registry.
"""
from __future__ import annotations

from typing import Any

from flask import Flask, Response, jsonify, request


def _stackup(board: str) -> dict[str, Any]:  # pragma: no cover - needs Altium
    """BOARD's layer stack, read in one turn that gives the front back."""
    from ..bridge.altium_bridge import following_front, get_bridge, tool_call
    from .workbench import _same_file
    with following_front(background=False), tool_call():
        bridge = get_bridge()
        front = bridge.send_command("application.get_active_document", {"front": "true"},
                                    timeout=15.0)
        prev = front.get("file_path", "") if isinstance(front, dict) else ""
        bridge.send_command("pcb.focus_board", {"board_path": board}, timeout=30.0)
        try:
            return bridge.send_command("pcb.get_layer_stackup", {}, timeout=30.0)
        finally:
            if prev and not _same_file(prev, board):
                try:
                    bridge.send_command("application.set_active_document",
                                        {"file_path": prev}, timeout=30.0)
                except Exception:                         # noqa: BLE001
                    pass


def board_values(board: str, read=None) -> dict[str, Any]:
    """The shown board's stack as calculator inputs, or why not."""
    from ..engineering.toolkit.prefill import from_stackup
    if not board:
        return {"ok": False, "reason": "no board is shown"}
    try:
        live = (read or _stackup)(board)
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "reason": f"could not read the board's stackup: {exc}"}
    out = from_stackup(live if isinstance(live, dict) else {})
    out["board"] = board
    return out


def register(app: Flask) -> None:
    @app.route("/api/wb/toolkit/catalog")
    def wb_toolkit_catalog() -> Response:
        from ..engineering import toolkit
        return jsonify(toolkit.catalog())

    @app.route("/api/wb/toolkit/run", methods=["POST"])
    def wb_toolkit_run() -> Response:
        from ..engineering import toolkit
        body = request.get_json(silent=True) or {}
        inputs = body.get("inputs")
        return jsonify(toolkit.run(str(body.get("name") or ""),
                                   inputs if isinstance(inputs, dict) else {}))

    @app.route("/api/wb/toolkit/board")
    def wb_toolkit_board() -> Response:
        from .cockpit import get_cockpit
        c = get_cockpit()
        shown = c.memo.file if c.memo else (c.pinned or "")
        return jsonify(board_values(shown or ""))
