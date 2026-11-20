# SPDX-License-Identifier: Apache-2.0
"""proj_close answers Altium's WPF "Unsaved Changes" prompt for its own project.

MEASURED 2026-11-20 on AD 26.11, on a throwaway project: sch_place_note,
then proj_close(save=True). The close stopped on "Unsaved Changes" ("Save
changes?"). Its buttons are WPF elements, not child windows, so the dialog
report offered none, proj_close re-raised the blocked-bridge error after
2.9 s and left the prompt up, blocking every later call.

The prompt is now answered through UI Automation: Save when saving, Don't
Save when discarding, and only when every document it names belongs to the
project. Anything else is cancelled, which abandons the close and changes
nothing, instead of being left open.
"""
from __future__ import annotations

import asyncio

import pytest

from eda_agent.bridge.exceptions import AltiumTimeoutError
from eda_agent.tools import project as project_tools
from eda_agent.ui import uia, windows

PROJECT = r"F:\scratch\CloseCheck.PrjPcb"
SHEET = r"F:\scratch\Sheet1.SchDoc"


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


class _Dialog:
    def __init__(self, hwnd, title):
        self.hwnd = hwnd
        self.title = title


class _Altium:
    def __init__(self, texts=("Save changes to Sheet1.SchDoc?",),
                 members=(SHEET,), invoke_ok=True):
        self.texts = list(texts)
        self.members = list(members)
        self.invoke_ok = invoke_ok
        self.dialogs = {9: "Unsaved Changes"}
        self.presses: list[str] = []
        self.project_open = True

    def get_altium_status(self):
        return {"running": True, "pid": 77}

    async def send_command_async(self, command, params=None, timeout=None):
        if command == "project.get_documents":
            return [{"file_name": p.rsplit("\\", 1)[-1], "file_path": p}
                    for p in self.members]
        if command == "project.close":
            raise AltiumTimeoutError(
                "Altium is showing a modal dialog 3s into this call",
                details={"dialogs": {"blocked": True, "dialogs": [
                    {"title": "Unsaved Changes", "hwnd": 9,
                     "buttons": []}]}})
        if command == "project.get_open_projects":
            return {"projects": [{"project_path": PROJECT}]
                    if self.project_open else []}
        raise AssertionError(f"unexpected command {command}")

    def describe_window(self, hwnd, depth=4, limit=400):
        return {"ok": True, "elements": [{"name": t} for t in self.texts]
                + [{"name": "Save"}, {"name": "Don\u2019t Save"},
                   {"name": "Cancel"}]}

    def invoke(self, hwnd, name):
        self.presses.append(name)
        if not self.invoke_ok:
            return {"ok": False, "reason": "no element"}
        self.dialogs.pop(hwnd, None)
        if name in ("Save", "Don't Save"):
            self.project_open = False
        return {"ok": True}

    def list_dialogs(self, pid):
        return [_Dialog(h, t) for h, t in self.dialogs.items()]


@pytest.fixture()
def altium(monkeypatch):
    def install(**kwargs):
        fake = _Altium(**kwargs)
        monkeypatch.setattr(project_tools, "get_bridge", lambda: fake)
        monkeypatch.setattr(windows, "available", lambda: True)
        monkeypatch.setattr(windows, "automation_enabled", lambda: True)
        monkeypatch.setattr(windows, "dialogs", fake.list_dialogs)
        monkeypatch.setattr(windows, "wait_for_close",
                            lambda hwnd, timeout=0: hwnd not in fake.dialogs)
        monkeypatch.setattr(windows, "wait_until",
                            lambda check, timeout, *a, **k: check())
        monkeypatch.setattr(uia, "available", lambda: True)
        monkeypatch.setattr(uia, "describe_window", fake.describe_window)
        monkeypatch.setattr(uia, "invoke", fake.invoke)
        import eda_agent.saving as saving
        monkeypatch.setattr(saving, "saving_off", lambda *a, **k: False)
        mcp = _Mcp()
        project_tools.register_project_tools(mcp)
        return fake, mcp.tools["proj_close"]
    return install


def _close(tool, **kwargs):
    return asyncio.run(tool(**kwargs))


def test_save_presses_save_and_the_project_closes(altium):
    fake, close = altium()
    out = _close(close, project_path=PROJECT, save=True)
    assert fake.presses == ["Save"]
    assert out["success"] is True and out["closed"] is True
    assert out["saved"] == ["Sheet1.SchDoc"]
    assert out["prompts_answered"] == 1


def test_discard_presses_dont_save(altium):
    fake, close = altium()
    out = _close(close, project_path=PROJECT, save=False)
    assert fake.presses == ["Don't Save"]
    assert out["closed"] is True
    assert out["discarded"] == ["Sheet1.SchDoc"]


def test_full_paths_in_the_prompt_are_matched_as_paths(altium):
    fake, close = altium(texts=(SHEET, PROJECT))
    out = _close(close, project_path=PROJECT, save=True)
    assert fake.presses == ["Save"]
    assert out["closed"] is True


@pytest.mark.parametrize("save", [True, False])
def test_a_document_of_another_project_cancels(altium, save):
    fake, close = altium(texts=("Save changes to Board.PcbDoc?",))
    out = _close(close, project_path=PROJECT, save=save)
    assert fake.presses == ["Cancel"]
    assert out["success"] is False and out["closed"] is False
    assert out["prompt_cancelled"] is True
    assert "Board.PcbDoc" in out["reason"]


def test_a_prompt_naming_nothing_readable_is_cancelled_not_left_open(altium):
    fake, close = altium(texts=("Save changes?",))
    out = _close(close, project_path=PROJECT, save=True)
    assert fake.presses == ["Cancel"]
    assert out["closed"] is False
    assert out["prompt_cancelled"] is True


def test_a_cancel_that_fails_says_the_prompt_is_open(altium):
    fake, close = altium(texts=("Save changes?",), invoke_ok=False)
    out = _close(close, project_path=PROJECT, save=True)
    assert out["prompt_cancelled"] is False
    assert "still open" in out["reason"]


def test_saving_switched_off_never_presses_save(altium, monkeypatch):
    fake, close = altium()
    import eda_agent.saving as saving
    monkeypatch.setattr(saving, "saving_off", lambda *a, **k: True)
    out = _close(close, project_path=PROJECT, save=True)
    assert fake.presses == ["Cancel"]
    assert out["closed"] is False
