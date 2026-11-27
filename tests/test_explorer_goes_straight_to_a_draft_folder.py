# SPDX-License-Identifier: Apache-2.0
"""Explorer reaches a folder by its path, and items are made only in __Draft.

Reaching __Draft by reading the whole folder tree took minutes on a real
workspace (2026-11-27). goto_folder expands only the folders on the path.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

from eda_agent.ui import explorer


class FakeExplorer:
    """A folder tree: (name, indent) rows; a row's children show only once
    it is expanded."""

    def __init__(self, tree):
        self.tree, self.open, self.expanded, self.selected = tree, set(), [], None

    def folder_grid(self):
        return object()

    def scroll_to(self, grid, percent):
        return 100.0

    def visible_folders(self, grid):
        rows, hidden_below = [], None
        for i, (name, indent) in enumerate(self.tree):
            if hidden_below is not None and indent > hidden_below:
                continue
            hidden_below = None if i in self.open else indent
            rect = NS(left=0, right=100, top=i * 20, bottom=i * 20 + 18)
            rows.append({"name": name, "indent": indent, "top": i * 20, "key": i,
                         "element": NS(index=i, CurrentBoundingRectangle=rect)})
        return rows

    def expand(self, element):
        self.open.add(element.index)
        self.expanded.append(self.tree[element.index][0])
        return True

    def select(self, element):
        self.selected = self.tree[element.index][0]
        return True


TREE = [("Components", 0), ("Capacitors", 16), ("__Draft", 16), ("Old", 32),
        ("Models", 0), ("3D Models", 16), ("__Draft", 16)]


def test_only_the_folders_on_the_path_are_expanded():
    fake = FakeExplorer(TREE)
    got = explorer.goto_folder(fake, "Components/__Draft")
    assert got["ok"] and got["path"] == "Components/__Draft"
    assert fake.expanded == ["Components"] and fake.selected == "__Draft"
    assert got["center"] == [50, 49]


def test_the_same_name_is_found_under_the_right_parent():
    fake = FakeExplorer(TREE)
    got = explorer.goto_folder(fake, "models" + chr(92) + "__draft")
    assert got["ok"] and fake.expanded == ["Models"]
    assert got["center"][1] == 6 * 20 + 9


def test_a_missing_folder_says_where_it_looked():
    got = explorer.goto_folder(FakeExplorer(TREE), "Components/Nope")
    assert not got["ok"] and "'Nope'" in got["reason"]


def test_only_draft_paths_are_accepted():
    assert explorer.is_draft_path("Components/__Draft")
    assert explorer.is_draft_path("Components/__Draft/QPA9903")
    assert not explorer.is_draft_path("Components/Released")
    assert not explorer.is_draft_path("")


def _tools(monkeypatch):
    from eda_agent.tools import altium365
    tools = {}

    class Mcp:
        def tool(self, *a, **k):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

        def __getattr__(self, name):
            return lambda *a, **k: (lambda fn: fn)

    altium365.register_altium365_tools(Mcp())
    return tools


def test_the_tool_refuses_a_folder_outside_draft_and_a_saving_entry(monkeypatch):
    create = _tools(monkeypatch)["altium365_create_draft_item"]
    out = asyncio.run(create(folder="Components/Released"))
    assert not out["ok"] and "__Draft" in out["reason"]
    out = asyncio.run(create(folder="Components/__Draft", menu_entry="Save to Server"))
    assert not out["ok"] and "local edit" in out["reason"]
