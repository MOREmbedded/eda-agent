# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A dialog press presses the button named, or nothing.

MEASURED on AD 26.10.1.6: asked to press "Don't Save" on the WPF
"Unsaved Changes" prompt, app_press_dialog_button pressed Save. The
button's name carries a curly apostrophe (U+2019), the press by name
missed, and the fallback sent Enter, which a dialog maps to its default
button. The reply said "Don't Save". A save prompt for an existing file
would have saved without a word. Nothing here touches Altium.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from eda_agent.ui import uia, windows
from eda_agent.ui.captions import plain

CURLY = "Don’t Save"
TYPED = "Don't Save"


class _FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def press(monkeypatch):
    from eda_agent.tools import uiauto
    monkeypatch.setattr(uiauto, "_altium_pid", lambda: (4242, None))
    mcp = _FakeMcp()
    uiauto.register_uiauto_tools(mcp)
    return mcp.tools["app_press_dialog_button"], uiauto


def _handleless(uiauto, monkeypatch, invoke):
    """A WPF dialog: no button windows, a UIA invoke that answers as given."""
    dialog = SimpleNamespace(hwnd=31, title="Unsaved Changes",
                             find_button=lambda caption: None,
                             buttons=lambda: [])
    monkeypatch.setattr(uiauto.windows, "dialogs", lambda pid: [dialog])
    monkeypatch.setattr(uiauto.windows, "wait_for_close",
                        lambda hwnd, timeout=5.0: True)
    keys = []
    monkeypatch.setattr(uiauto.windows, "press_key",
                        lambda hwnd, key: keys.append(key))
    monkeypatch.setattr(uia, "available", lambda: True)
    monkeypatch.setattr(uia, "invoke", invoke)
    return keys


def test_typographic_punctuation_folds_to_what_a_caller_types():
    assert plain(CURLY) == TYPED
    assert plain("“OK”") == '"OK"'
    assert plain("Browse…") == "Browse..."


def test_the_win32_matcher_finds_a_curly_caption_by_its_typed_name():
    assert windows._normalise(CURLY) == windows._normalise(TYPED)


def test_the_uia_matcher_finds_a_curly_caption_by_its_typed_name(monkeypatch):
    save = SimpleNamespace(kind="save")
    dont = SimpleNamespace(kind="dont")
    root = SimpleNamespace(kind="root")
    info = {
        id(save): {"name": "Save", "automation_id": "", "type": "Button"},
        id(dont): {"name": CURLY, "automation_id": "", "type": "Button"},
    }
    monkeypatch.setattr(uia, "_element", lambda hwnd: root)
    monkeypatch.setattr(uia, "_children",
                        lambda node: [save, dont] if node is root else [])
    monkeypatch.setattr(uia, "_describe", lambda node: info[id(node)])
    monkeypatch.setattr(uia, "_pattern", lambda element, pid, iface: None)
    hit = uia._find_pressable(1, TYPED)
    assert hit is not None, "the typed name must reach the curly button"
    assert hit[0] is dont, "and it must be that button, not Save"


def test_every_caption_matcher_in_the_ui_layer_folds_first():
    """The fix has twins: each module grew its own caption matcher.

    A matcher written as str(text or "").strip().lower() compares the raw
    caption, so a curly apostrophe misses again in whichever module it
    lives in.
    """
    import re
    from pathlib import Path
    ui_dir = Path(__file__).resolve().parents[1] / "src" / "eda_agent" / "ui"
    raw = re.compile(r'str\(text or ""\)\s*(\.replace\("&", ""\))?\s*\.strip\(\)')
    offenders = [f"{p.name}:{n}" for p in sorted(ui_dir.glob("*.py"))
                 for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                 if raw.search(line)]
    assert not offenders, f"caption matchers that skip captions.plain: {offenders}"


def test_a_missed_button_is_never_answered_with_enter(press, monkeypatch):
    tool, uiauto = press
    keys = _handleless(uiauto, monkeypatch, lambda hwnd, name: {
        "ok": False, "reason": f"no element named {name!r}",
        "offered": ["Save", CURLY, "Cancel"]})
    out = asyncio.run(tool(button_caption=TYPED,
                           dialog_title="Unsaved Changes"))
    assert keys == [], f"a key was sent for a button nobody found: {keys}"
    assert out["ok"] is False and out["pressed"] is None
    assert CURLY in out["offered"]


def test_a_missed_dismiss_still_escapes(press, monkeypatch):
    tool, uiauto = press
    keys = _handleless(uiauto, monkeypatch, lambda hwnd, name: {
        "ok": False, "reason": "no element"})
    out = asyncio.run(tool(button_caption="Cancel",
                           dialog_title="Unsaved Changes"))
    assert keys == ["escape"]
    assert out["ok"] and out["outcome_verified"] is True


def test_a_found_button_is_invoked_and_no_key_follows(press, monkeypatch):
    tool, uiauto = press
    asked = []

    def invoke(hwnd, name):
        asked.append(name)
        return {"ok": True, "element": CURLY, "how": "invoke"}

    keys = _handleless(uiauto, monkeypatch, invoke)
    out = asyncio.run(tool(button_caption=TYPED,
                           dialog_title="Unsaved Changes"))
    assert asked == [TYPED] and keys == []
    assert out["ok"] and out["method"] == "uia"
