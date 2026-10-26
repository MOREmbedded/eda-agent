# SPDX-License-Identifier: Apache-2.0
"""Offline tests for Win32 Altium window/dialog MCP tools."""

from __future__ import annotations

import pytest

from eda_agent.bridge.windows_ui import WindowsUiInspector


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


class _Inspector:
    def __init__(self):
        self.calls = []

    def list_windows(self, include_hidden=False):
        self.calls.append(("windows", include_hidden))
        return [{"handle": 10, "title": "Altium", "is_dialog": False}]

    def list_dialogs(self):
        self.calls.append(("dialogs",))
        return [{"handle": 20, "title": "Compile Error", "controls": []}]

    def capture_window(self, handle, path):
        self.calls.append(("capture", handle, path))
        return {"success": True, "path": path, "width": 800, "height": 600}

    def main_window_handle(self):
        return 10

    def click_button(self, dialog, button, caption, **kwargs):
        self.calls.append(("click", dialog, button, caption, kwargs))
        return {"success": kwargs["confirm"], "caption": caption}

    def interact_control(self, dialog, control, **kwargs):
        self.calls.append(("interact", dialog, control, kwargs))
        return {"success": kwargs["confirm"], "action": kwargs["action"]}


def _tools(monkeypatch):
    from eda_agent.tools import application
    inspector = _Inspector()
    monkeypatch.setattr(application, "_get_ui_inspector", lambda: inspector)
    mcp = _Mcp()
    application.register_application_tools(mcp)
    return mcp.tools, inspector


@pytest.mark.asyncio
async def test_window_and_dialog_inventory_bypass_bridge(monkeypatch):
    tools, inspector = _tools(monkeypatch)
    windows = await tools["app_list_windows"](include_hidden=True)
    dialogs = await tools["app_list_dialogs"]()
    assert windows["count"] == 1
    assert dialogs["dialogs"][0]["title"] == "Compile Error"
    assert inspector.calls == [("windows", True), ("dialogs",)]


@pytest.mark.asyncio
async def test_capture_delegates_without_focus_change(monkeypatch):
    tools, inspector = _tools(monkeypatch)
    result = await tools["app_capture_window"](10, "D:/tmp/altium.bmp")
    assert result["width"] == 800
    assert inspector.calls[-1] == ("capture", 10, "D:/tmp/altium.bmp")


@pytest.mark.asyncio
async def test_capture_all_dialogs_as_png(monkeypatch, tmp_path):
    tools, inspector = _tools(monkeypatch)
    result = await tools["app_capture_dialogs"](str(tmp_path))
    assert result["count"] == 1
    call = inspector.calls[-1]
    assert call[0:2] == ("capture", 20)
    assert str(call[2]).endswith("001_dialog_20.png")


@pytest.mark.asyncio
async def test_click_requires_explicit_flags(monkeypatch):
    tools, inspector = _tools(monkeypatch)
    result = await tools["app_click_dialog_button"](
        20, 21, "OK", confirm=True, allow_destructive=False,
    )
    assert result["success"] is True
    assert inspector.calls[-1][-1] == {
        "confirm": True, "allow_destructive": False,
    }


@pytest.mark.asyncio
async def test_script_error_parser_and_visual_context(monkeypatch, tmp_path):
    from eda_agent.tools import application

    tools, inspector = _tools(monkeypatch)

    class _Bridge:
        async def send_command_async(self, command, params=None, timeout=None):
            return {"file_path": "D:/p/Main.SchDoc", "kind": "SCH"}

    monkeypatch.setattr(application, "get_bridge", lambda: _Bridge())
    errors = await tools["app_get_script_errors"]()
    context = await tools["app_visual_context"](str(tmp_path / "context.bmp"))
    assert errors["diagnostics"][0]["kind"] == "compile_error"
    assert context["bridge_responsive"] is True
    assert context["capture"]["success"] is True


@pytest.mark.asyncio
async def test_control_interaction_passes_stale_handle_guards(monkeypatch):
    tools, inspector = _tools(monkeypatch)
    result = await tools["app_interact_dialog_control"](
        20, 23, "Edit", "old", "set_text", "new", confirm=True,
    )
    assert result["success"] is True
    assert inspector.calls[-1] == (
        "interact", 20, 23,
        {
            "expected_class": "Edit", "expected_text": "old",
            "action": "set_text", "value": "new", "confirm": True,
            "allow_destructive": False,
        },
    )


class _Con:
    GW_OWNER = 4
    BM_CLICK = 0x00F5
    WM_SETTEXT = 0x000C
    BM_SETCHECK = 0x00F1
    BST_CHECKED = 1
    BST_UNCHECKED = 0
    CB_SETCURSEL = 0x014E
    LB_SETCURSEL = 0x0186


class _Gui:
    def __init__(self):
        self.parents = {21: 20, 23: 20, 24: 20, 20: 0}
        self.posted = []
        self.sent = []

    def EnumWindows(self, callback, arg):
        callback(10, arg)
        callback(20, arg)

    def EnumChildWindows(self, hwnd, callback, arg):
        if hwnd == 20:
            callback(21, arg)
            callback(23, arg)
            callback(24, arg)

    def IsWindow(self, hwnd):
        return hwnd in {10, 20, 21, 23, 24}

    def IsWindowVisible(self, hwnd):
        return True

    def IsWindowEnabled(self, hwnd):
        return True

    def GetWindowText(self, hwnd):
        return {10: "Altium Designer", 20: "Confirm", 21: "Delete", 23: "old", 24: ""}[hwnd]

    def GetClassName(self, hwnd):
        return {10: "TApplication", 20: "#32770", 21: "Button", 23: "Edit", 24: "ComboBox"}[hwnd]

    def GetWindowRect(self, hwnd):
        return (0, 0, 800, 600)

    def GetWindow(self, hwnd, flag):
        return 10 if hwnd == 20 else 0

    def GetParent(self, hwnd):
        return self.parents.get(hwnd, 0)

    def GetDlgCtrlID(self, hwnd):
        return 1

    def PostMessage(self, hwnd, message, wparam, lparam):
        self.posted.append((hwnd, message, wparam, lparam))

    def SendMessage(self, hwnd, message, wparam, lparam):
        self.sent.append((hwnd, message, wparam, lparam))
        return 0


class _Process:
    def GetWindowThreadProcessId(self, hwnd):
        return 1, 99 if hwnd in {10, 20, 21, 23, 24} else 100


class _Backend:
    def __init__(self):
        self.con = _Con()
        self.gui = _Gui()
        self.process = _Process()


def test_inspector_enumerates_dialog_controls_without_live_windows():
    inspector = WindowsUiInspector(99, backend=_Backend())
    dialogs = inspector.list_dialogs()
    assert dialogs[0]["title"] == "Confirm"
    assert dialogs[0]["controls"][0]["text"] == "Delete"


def test_inspector_finds_altium_vcl_message_form(monkeypatch):
    backend = _Backend()
    original = backend.gui.GetClassName
    monkeypatch.setattr(backend.gui, "GetClassName",
                        lambda hwnd: "TMessageForm" if hwnd == 20 else original(hwnd))
    inspector = WindowsUiInspector(99, backend=backend)
    dialogs = inspector.list_dialogs()
    assert [d["handle"] for d in dialogs] == [20]
    assert dialogs[0]["controls"][0]["text"] == "Delete"
    assert backend.gui.posted == []


def test_inspector_blocks_unconfirmed_and_destructive_clicks():
    backend = _Backend()
    inspector = WindowsUiInspector(99, backend=backend)
    assert inspector.click_button(20, 21, "Delete")["success"] is False
    assert inspector.click_button(20, 21, "Delete", confirm=True)["success"] is False
    allowed = inspector.click_button(
        20, 21, "Delete", confirm=True, allow_destructive=True,
    )
    assert allowed["success"] is True
    assert backend.gui.posted == [(21, _Con.BM_CLICK, 0, 0)]


def test_inspector_edits_only_exact_standard_controls():
    backend = _Backend()
    inspector = WindowsUiInspector(99, backend=backend)
    result = inspector.interact_control(
        20, 23, expected_class="Edit", expected_text="old",
        action="set_text", value="new", confirm=True,
    )
    assert result["success"] is True
    assert backend.gui.sent[-1] == (23, _Con.WM_SETTEXT, 0, "new")
    with pytest.raises(ValueError, match="class changed"):
        inspector.interact_control(
            20, 23, expected_class="ComboBox", expected_text="old",
            action="select_index", value=0, confirm=True,
        )
