# SPDX-License-Identifier: Apache-2.0
"""Read and capture Altium's native Windows UI independently of DelphiScript.

The polling bridge can be blocked by a modal dialog or stopped by a script
error. These helpers deliberately talk to Win32 using the Altium PID, so
diagnostics still work when file IPC does not. Importing this module is safe on
non-Windows hosts; constructing :class:`WindowsUiInspector` is not.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import ctypes
from pathlib import Path
from typing import Any, Optional


class WindowsUiUnavailable(RuntimeError):
    """Raised when pywin32 or a Windows desktop session is unavailable."""


def get_altium_ui_inspector(bridge: Any) -> "WindowsUiInspector":
    """Create an inspector bound to the Altium process owned by *bridge*.

    Kept here so application/project tools share the same PID validation and
    tests can replace this single boundary without importing pywin32.
    """
    pid = bridge.process_manager.get_altium_pid()
    if not pid:
        raise WindowsUiUnavailable("Altium Designer process not found")
    return WindowsUiInspector(pid)


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    class_name: str
    rect: tuple[int, int, int, int]
    visible: bool
    enabled: bool
    owner_handle: int
    is_dialog: bool


@dataclass(frozen=True)
class ControlInfo:
    handle: int
    parent_handle: int
    text: str
    class_name: str
    rect: tuple[int, int, int, int]
    visible: bool
    enabled: bool
    control_id: int


class _PyWin32Backend:
    def __init__(self) -> None:
        try:
            import win32api
            import win32con
            import win32gui
            import win32process
            import win32ui
        except ImportError as exc:  # pragma: no cover - platform dependent
            raise WindowsUiUnavailable(
                "Windows UI tools require pywin32 on Windows"
            ) from exc
        self.con = win32con
        self.api = win32api
        self.gui = win32gui
        self.process = win32process
        self.ui = win32ui


class WindowsUiInspector:
    """Enumerate, inspect and capture windows belonging to one Altium PID."""

    _DESTRUCTIVE_WORDS = (
        "delete", "discard", "overwrite", "remove", "erase", "yes to all",
        "удал", "сброс", "перезапис", "заменить все",
    )

    # Altium paints its dialog buttons with Delphi/VCL classes, not the Win32
    # "Button" class: OK on "Choose Documents To Compare" is TXPBitBtn, every
    # ECO button likewise, and even a plain message box answers to TButton.
    # A strict == "button" test therefore rejects every button in the product.
    # All of these accept BM_CLICK, which is what this method posts. The
    # destructive-caption guard below is unchanged.
    _BUTTON_CLASSES = frozenset((
        "button", "txpbitbtn", "txpbutton", "txpspeedbutton",
        "tbitbtn", "tbutton", "tspeedbutton",
    ))

    def __init__(self, pid: int, backend: Any = None) -> None:
        if int(pid) <= 0:
            raise ValueError("pid must be positive")
        self.pid = int(pid)
        self._w = backend or _PyWin32Backend()

    def list_windows(self, include_hidden: bool = False) -> list[dict[str, Any]]:
        windows: list[WindowInfo] = []

        def visit(hwnd: int, _: Any) -> bool:
            _, window_pid = self._w.process.GetWindowThreadProcessId(hwnd)
            if window_pid != self.pid:
                return True
            visible = bool(self._w.gui.IsWindowVisible(hwnd))
            if not include_hidden and not visible:
                return True
            cls = self._w.gui.GetClassName(hwnd)
            windows.append(WindowInfo(
                handle=int(hwnd),
                title=self._w.gui.GetWindowText(hwnd),
                class_name=cls,
                rect=tuple(self._w.gui.GetWindowRect(hwnd)),
                visible=visible,
                enabled=bool(self._w.gui.IsWindowEnabled(hwnd)),
                owner_handle=int(self._w.gui.GetWindow(hwnd, self._w.con.GW_OWNER) or 0),
                # Altium's SI warning uses the VCL TMessageForm class.
                # Treat it as a dialog so a blocked editor is not reported idle.
                is_dialog=(cls in {"#32770", "TMessageForm"}),
            ))
            return True

        self._w.gui.EnumWindows(visit, None)
        return [asdict(item) for item in windows]

    def list_controls(self, window_handle: int) -> list[dict[str, Any]]:
        if not self._belongs_to_pid(window_handle):
            raise ValueError("window_handle does not belong to the Altium process")
        controls: list[ControlInfo] = []

        def visit(hwnd: int, _: Any) -> bool:
            controls.append(ControlInfo(
                handle=int(hwnd),
                parent_handle=int(self._w.gui.GetParent(hwnd) or 0),
                text=self._w.gui.GetWindowText(hwnd),
                class_name=self._w.gui.GetClassName(hwnd),
                rect=tuple(self._w.gui.GetWindowRect(hwnd)),
                visible=bool(self._w.gui.IsWindowVisible(hwnd)),
                enabled=bool(self._w.gui.IsWindowEnabled(hwnd)),
                control_id=int(self._w.gui.GetDlgCtrlID(hwnd)),
            ))
            return True

        self._w.gui.EnumChildWindows(int(window_handle), visit, None)
        return [asdict(item) for item in controls]

    def list_dialogs(self) -> list[dict[str, Any]]:
        dialogs = []
        for window in self.list_windows(include_hidden=False):
            if not window["is_dialog"]:
                continue
            item = dict(window)
            item["controls"] = self.list_controls(window["handle"])
            dialogs.append(item)
        return dialogs

    def main_window_handle(self) -> int:
        """Return the largest visible non-dialog top-level Altium window."""
        candidates = [w for w in self.list_windows() if not w["is_dialog"]]
        if not candidates:
            raise WindowsUiUnavailable("No visible Altium top-level window found")
        def area(item: dict[str, Any]) -> int:
            left, top, right, bottom = item["rect"]
            return max(0, right - left) * max(0, bottom - top)
        return int(max(candidates, key=area)["handle"])

    def send_hotkey(self, window_handle: int, hotkey: str) -> dict[str, Any]:
        """Focus an Altium window and send a strict modifier+key chord."""
        if not self._belongs_to_pid(window_handle):
            raise ValueError("window_handle does not belong to the Altium process")
        tokens = [t.strip().lower() for t in hotkey.split("+") if t.strip()]
        if not tokens:
            raise ValueError("hotkey is empty")
        modifier_map = {
            "ctrl": self._w.con.VK_CONTROL,
            "control": self._w.con.VK_CONTROL,
            "alt": self._w.con.VK_MENU,
            "shift": self._w.con.VK_SHIFT,
        }
        modifiers = [modifier_map[t] for t in tokens[:-1] if t in modifier_map]
        if len(modifiers) != len(tokens) - 1:
            raise ValueError("only ctrl/alt/shift modifiers are supported")
        key_name = tokens[-1]
        if key_name.startswith("f") and key_name[1:].isdigit():
            number = int(key_name[1:])
            if number < 1 or number > 24:
                raise ValueError("function key must be F1..F24")
            key = self._w.con.VK_F1 + number - 1
        elif len(key_name) == 1 and key_name.isalnum():
            key = ord(key_name.upper())
        else:
            raise ValueError("final key must be A-Z, 0-9 or F1..F24")

        method = "foreground"
        try:
            self._w.gui.SetForegroundWindow(int(window_handle))
            for vk in modifiers:
                self._w.api.keybd_event(vk, 0, 0, 0)
            self._w.api.keybd_event(key, 0, 0, 0)
            self._w.api.keybd_event(key, 0, self._w.con.KEYEVENTF_KEYUP, 0)
            for vk in reversed(modifiers):
                self._w.api.keybd_event(vk, 0, self._w.con.KEYEVENTF_KEYUP, 0)
        except Exception:
            # Windows forbids SetForegroundWindow from many background
            # threads. Queue the same chord directly to the verified Altium
            # window so autonomous restart is not hostage to focus stealing.
            method = "post_message"
            for vk in modifiers:
                self._w.gui.PostMessage(
                    int(window_handle), self._w.con.WM_KEYDOWN, vk, 0
                )
            self._w.gui.PostMessage(
                int(window_handle), self._w.con.WM_KEYDOWN, key, 0
            )
            self._w.gui.PostMessage(
                int(window_handle), self._w.con.WM_KEYUP, key, 0
            )
            for vk in reversed(modifiers):
                self._w.gui.PostMessage(
                    int(window_handle), self._w.con.WM_KEYUP, vk, 0
                )
        return {"success": True, "window_handle": int(window_handle),
                "hotkey": hotkey, "method": method}

    def close_window(self, window_handle: int, expected_title: str) -> dict[str, Any]:
        """Post WM_CLOSE only to an exact freshly-validated Altium window."""
        if not self._belongs_to_pid(window_handle):
            raise ValueError("window_handle does not belong to the Altium process")
        actual = self._w.gui.GetWindowText(int(window_handle))
        if actual != expected_title:
            raise ValueError(
                f"window title changed: expected {expected_title!r}, got {actual!r}"
            )
        self._w.gui.PostMessage(
            int(window_handle), self._w.con.WM_CLOSE, 0, 0
        )
        return {"success": True, "window_handle": int(window_handle),
                "title": actual, "method": "wm_close"}

    def capture_window(self, window_handle: int, output_path: str | Path) -> dict[str, Any]:
        """Capture one Altium window to BMP or PNG without focus/zoom changes."""
        if not self._belongs_to_pid(window_handle):
            raise ValueError("window_handle does not belong to the Altium process")
        left, top, right, bottom = self._w.gui.GetWindowRect(int(window_handle))
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            raise ValueError("window has an empty capture rectangle")
        path = Path(output_path).expanduser().resolve()
        requested_format = path.suffix.lower().lstrip(".")
        if requested_format not in {"bmp", "png"}:
            requested_format = "png"
            path = path.with_suffix(".png")
        bitmap_path = path if requested_format == "bmp" else path.with_suffix(".capture.bmp")
        path.parent.mkdir(parents=True, exist_ok=True)

        hwnd_dc = self._w.gui.GetWindowDC(int(window_handle))
        source_dc = self._w.ui.CreateDCFromHandle(hwnd_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = self._w.ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(source_dc, width, height)
            memory_dc.SelectObject(bitmap)
            # PW_RENDERFULLCONTENT=2 captures DirectComposition-backed windows
            # on supported Windows versions. Fall back to BitBlt if rejected.
            print_window = getattr(self._w.gui, "PrintWindow", None)
            if print_window is not None:
                printed = bool(print_window(
                    int(window_handle), memory_dc.GetSafeHdc(), 2
                ))
            else:
                # pywin32 312 no longer exposes win32gui.PrintWindow in some
                # builds. The stable user32 ABI is available on every Windows
                # version supported by Altium.
                printed = bool(ctypes.windll.user32.PrintWindow(
                    int(window_handle), memory_dc.GetSafeHdc(), 2
                ))
            if not printed:
                memory_dc.BitBlt((0, 0), (width, height), source_dc, (0, 0), self._w.con.SRCCOPY)
            bitmap.SaveBitmapFile(memory_dc, str(bitmap_path))
        finally:
            try:
                self._w.gui.DeleteObject(bitmap.GetHandle())
            except Exception:  # a leaked GDI handle, not a failed capture
                pass
            memory_dc.DeleteDC()
            source_dc.DeleteDC()
            self._w.gui.ReleaseDC(int(window_handle), hwnd_dc)
        if requested_format == "png":
            try:
                from PIL import Image
                with Image.open(bitmap_path) as captured:
                    captured.save(path, format="PNG", optimize=True)
            finally:
                try:
                    bitmap_path.unlink()
                except OSError:
                    pass
        return {
            "success": True,
            "path": str(path),
            "format": requested_format,
            "width": width,
            "height": height,
            "window_handle": int(window_handle),
            "title": self._w.gui.GetWindowText(int(window_handle)),
        }

    def click_button(
        self,
        dialog_handle: int,
        button_handle: int,
        expected_caption: str,
        *,
        confirm: bool = False,
        allow_destructive: bool = False,
    ) -> dict[str, Any]:
        """Click an exact dialog button only after explicit confirmation."""
        if not confirm:
            return {"success": False, "reason": "confirm=true is required"}
        if not self._belongs_to_pid(dialog_handle) or not self._belongs_to_pid(button_handle):
            raise ValueError("dialog/button does not belong to the Altium process")
        if not self._is_descendant(dialog_handle, button_handle):
            raise ValueError("button_handle is not a child of dialog_handle")
        cls = self._w.gui.GetClassName(int(button_handle))
        caption = self._w.gui.GetWindowText(int(button_handle)).strip()
        if cls.lower() not in self._BUTTON_CLASSES:
            raise ValueError("target control is not a clickable button")
        if caption != expected_caption.strip():
            raise ValueError("button caption changed; refresh dialog inventory")
        destructive = any(word in caption.lower() for word in self._DESTRUCTIVE_WORDS)
        if destructive and not allow_destructive:
            return {"success": False, "reason": "destructive caption requires allow_destructive=true"}
        self._w.gui.PostMessage(int(button_handle), self._w.con.BM_CLICK, 0, 0)
        return {"success": True, "caption": caption, "button_handle": int(button_handle)}

    def interact_control(
        self,
        dialog_handle: int,
        control_handle: int,
        *,
        expected_class: str,
        expected_text: str,
        action: str,
        value: Any = None,
        confirm: bool = False,
        allow_destructive: bool = False,
    ) -> dict[str, Any]:
        """Perform a constrained standard-control action using Win32 messages."""
        if not confirm:
            return {"success": False, "reason": "confirm=true is required"}
        if not self._belongs_to_pid(dialog_handle) or not self._belongs_to_pid(control_handle):
            raise ValueError("dialog/control does not belong to the Altium process")
        if not self._is_descendant(dialog_handle, control_handle):
            raise ValueError("control_handle is not a child of dialog_handle")
        actual_class = self._w.gui.GetClassName(int(control_handle))
        actual_text = self._w.gui.GetWindowText(int(control_handle))
        if actual_class.lower() != expected_class.strip().lower():
            raise ValueError("control class changed; refresh dialog inventory")
        if actual_text != expected_text:
            raise ValueError("control text changed; refresh dialog inventory")

        action = action.strip().lower()
        if action == "click":
            return self.click_button(
                dialog_handle, control_handle, expected_text,
                confirm=True, allow_destructive=allow_destructive,
            )
        if action == "set_text":
            if actual_class.lower() != "edit":
                raise ValueError("set_text requires an Edit control")
            self._w.gui.SendMessage(int(control_handle), self._w.con.WM_SETTEXT, 0, str(value or ""))
        elif action == "set_checked":
            if actual_class.lower() != "button":
                raise ValueError("set_checked requires a Button/checkbox control")
            state = self._w.con.BST_CHECKED if bool(value) else self._w.con.BST_UNCHECKED
            self._w.gui.SendMessage(int(control_handle), self._w.con.BM_SETCHECK, state, 0)
        elif action == "select_index":
            index = int(value)
            cls = actual_class.lower()
            if cls == "combobox":
                message = self._w.con.CB_SETCURSEL
            elif cls == "listbox":
                message = self._w.con.LB_SETCURSEL
            else:
                raise ValueError("select_index requires ComboBox or ListBox")
            result = self._w.gui.SendMessage(int(control_handle), message, index, 0)
            if int(result) < 0:
                raise ValueError("selection index was rejected by the control")
        else:
            raise ValueError("action must be click, set_text, set_checked or select_index")
        return {
            "success": True,
            "action": action,
            "control_handle": int(control_handle),
            "class_name": actual_class,
            "previous_text": actual_text,
            "value": value,
        }

    def _belongs_to_pid(self, hwnd: int) -> bool:
        if not self._w.gui.IsWindow(int(hwnd)):
            return False
        _, pid = self._w.process.GetWindowThreadProcessId(int(hwnd))
        return int(pid) == self.pid

    def _is_descendant(self, parent: int, child: int) -> bool:
        current = int(child)
        while current:
            if current == int(parent):
                return True
            current = int(self._w.gui.GetParent(current) or 0)
        return False
