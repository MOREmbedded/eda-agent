# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A menu level that reads empty or wrong is read once more, and only once.

MEASURED on AD 26.10.1.6: the first read of Tools came back "a menu opened
but exposed no items" one time in six. Reported on AD 25: asked for
"Tools|Convert|...", the reply said Convert was not in Tools and offered
the Design menu's entries, about one time in four, and the same call
worked straight after. The menu here is simulated; nothing touches Altium.
"""

from __future__ import annotations

from types import SimpleNamespace

from eda_agent.ui import menu


def _entry(name, y):
    return SimpleNamespace(name=name, rect=(0, y, 200, 20))


TOOLS = [_entry("Convert", 20), _entry("Update From PCB Libraries...", 40)]
DESIGN = [_entry("Update Schematic...", 20), _entry("Rules...", 40)]


class _Menu:
    """Tools, whose popup reads as each item of ``reads`` in turn."""

    def __init__(self, reads):
        self.reads = list(reads)
        self.current = []
        self.open = []
        self.pressed = []

    def install(self, monkeypatch):
        top = SimpleNamespace(name="Tools", rect=(0, 0, 40, 20))
        monkeypatch.setattr(menu, "bring_to_front", lambda pid: True)
        monkeypatch.setattr(menu, "bar_items", lambda pid: {"Tools": top})
        monkeypatch.setattr(menu, "close_open_menu", lambda pid: self.open.clear())
        monkeypatch.setattr(menu, "_submenus", lambda pid: list(self.open))
        monkeypatch.setattr(menu, "_newest_popup",
                            lambda pid, before, timeout=6.0: 1 if self.open else None)
        monkeypatch.setattr(menu, "_items_of", lambda hwnd: list(self.current))
        monkeypatch.setattr(menu, "_name", lambda node: node.name)
        monkeypatch.setattr(menu, "_visible_rect", lambda node: node.rect)
        monkeypatch.setattr(menu.win, "wait_until", lambda check, timeout, poll=0.02: check())
        monkeypatch.setattr(menu.win, "dialogs", lambda pid: [])

        def click(pid, x, y, after=0.3, expect="", until=None, wait=1.0):
            self.pressed.append(expect)
            if expect == "Tools":
                self.current = self.reads.pop(0) if self.reads else TOOLS
                self.open[:] = [SimpleNamespace(hwnd=1)]
            else:
                self.open.clear()
            return ""

        monkeypatch.setattr(menu, "_click", click)


def test_an_empty_first_read_is_read_again(monkeypatch):
    m = _Menu([[], TOOLS])
    m.install(monkeypatch)
    out = menu.click_path(1, "Tools|Update From PCB Libraries", settle=0.1)
    assert out["ok"] is True and out["retried"] is True
    assert m.pressed == ["Tools", "Tools", "Update From PCB Libraries"]


def test_another_menus_items_are_read_again(monkeypatch):
    m = _Menu([DESIGN, TOOLS])
    m.install(monkeypatch)
    out = menu.click_path(1, "Tools|Update From PCB Libraries", settle=0.1)
    assert out["ok"] is True
    assert m.pressed.count("Update From PCB Libraries") == 1


def test_two_failed_reads_report_both_and_stop(monkeypatch):
    m = _Menu([DESIGN, []])
    m.install(monkeypatch)
    out = menu.click_path(1, "Tools|Update From PCB Libraries", settle=0.1)
    assert out["ok"] is False and out["retried"] is True
    assert m.pressed == ["Tools", "Tools"], "one retry only"
    assert "exposed no items" in out["reason"]
    assert "Read twice" in out["reason"] and "not in 'Tools'" in out["reason"]
    assert "not in 'Tools'" in out["first_read"]["reason"]
    assert "Rules..." in out["first_read"]["offered"]
    assert "_read_failed" not in out


def test_a_first_read_that_works_is_not_repeated(monkeypatch):
    m = _Menu([TOOLS])
    m.install(monkeypatch)
    out = menu.click_path(1, "Tools|Update From PCB Libraries", settle=0.1)
    assert out["ok"] is True and "retried" not in out
    assert m.pressed == ["Tools", "Update From PCB Libraries"]


def test_other_failures_are_not_retried(monkeypatch):
    """A path that ends at a submenu ran nothing, and walking it again
    would not change that."""
    m = _Menu([TOOLS])
    m.install(monkeypatch)
    out = menu.click_path(1, "Tools", settle=0.1)
    assert out["ok"] is False and "retried" not in out
    assert m.pressed == ["Tools"]
