# SPDX-License-Identifier: Apache-2.0
"""Offline restart-supervisor and dialog diagnostic tests."""

from __future__ import annotations

from eda_agent.bridge.altium_supervisor import (
    AltiumBridgeSupervisor,
    read_restart_status,
)
from eda_agent.bridge.dialog_diagnostics import classify_dialog


def _dialog(title, text, button="OK"):
    return {
        "handle": 20,
        "title": title,
        "controls": [
            {"handle": 21, "class_name": "Static", "text": text},
            {"handle": 22, "class_name": "Button", "text": button},
        ],
    }


def test_compile_error_extracts_file_line_symbol():
    result = classify_dialog(_dialog(
        "Compile Error",
        "Generic.pas(742): Undeclared identifier: FooBar",
    ))
    assert result["kind"] == "compile_error"
    assert result["file"] == "Generic.pas"
    assert result["line"] == 742
    assert result["symbol"] == "FooBar"
    assert result["automation_safe"] is True


def test_save_and_license_dialogs_are_protected():
    assert classify_dialog(_dialog("Altium", "Save changes?"))["automation_safe"] is False
    assert classify_dialog(_dialog("License", "Sign in required"))["automation_safe"] is False


class _Inspector:
    def __init__(self, dialogs=None, windows=None):
        self.dialogs = dialogs or []
        self.windows = windows or []
        self.hotkeys = []
        self.closed = []

    def list_dialogs(self):
        return self.dialogs

    def list_windows(self):
        return self.windows

    def main_window_handle(self):
        return 10

    def send_hotkey(self, hwnd, hotkey):
        self.hotkeys.append((hwnd, hotkey))
        return {"success": True}

    def close_window(self, hwnd, title):
        self.closed.append((hwnd, title))
        return {"success": True, "method": "wm_close"}


def test_supervisor_stops_launches_and_verifies_version(tmp_path):
    inspector = _Inspector()
    supervisor = AltiumBridgeSupervisor(
        inspector,
        tmp_path,
        lambda: {"pong": True, "script_version": "2026.08.02.1"},
        sleep=lambda _: None,
    )
    result = supervisor.restart(
        launch_hotkey="ctrl+alt+f9",
        expected_script_version="2026.08.02.1",
    )
    assert result["success"] is True
    assert inspector.hotkeys == [(10, "ctrl+f3"), (10, "ctrl+alt+f9")]
    assert read_restart_status(tmp_path)["state"] == "running"


def test_supervisor_refuses_protected_dialog(tmp_path):
    inspector = _Inspector([_dialog("Altium", "Save changes?")])
    supervisor = AltiumBridgeSupervisor(
        inspector, tmp_path, lambda: {}, sleep=lambda _: None,
    )
    result = supervisor.restart(launch_hotkey="ctrl+alt+f9")
    assert result["state"] == "blocked_by_dialog"
    assert inspector.hotkeys == []


def test_supervisor_closes_status_window_before_relaunch(tmp_path):
    inspector = _Inspector(windows=[{"handle": 44, "title": "EDA Agent MCP"}])
    supervisor = AltiumBridgeSupervisor(
        inspector, tmp_path,
        lambda: {"pong": True, "script_version": "v2"}, sleep=lambda _: None,
    )
    result = supervisor.restart(launch_hotkey="f9", expected_script_version="v2")
    assert result["success"] is True
    assert inspector.closed == [(44, "EDA Agent MCP")]
    assert inspector.hotkeys == [(10, "f9")]


def test_compile_error_uses_direct_stop(tmp_path):
    inspector = _Inspector([_dialog(
        "Compile Error", "Generic.pas(10): Undeclared identifier: Nope"
    )], windows=[{"handle": 44, "title": "EDA Agent MCP"}])
    supervisor = AltiumBridgeSupervisor(
        inspector, tmp_path,
        lambda: {"pong": True}, sleep=lambda _: None,
    )
    supervisor.restart(launch_hotkey="f9")
    assert inspector.closed == []
    assert inspector.hotkeys[0] == (10, "ctrl+f3")


def test_supervisor_requires_configured_launch_hotkey(tmp_path):
    supervisor = AltiumBridgeSupervisor(
        _Inspector(), tmp_path, lambda: {}, sleep=lambda _: None,
    )
    result = supervisor.restart(launch_hotkey="")
    assert result["state"] == "setup_required"
