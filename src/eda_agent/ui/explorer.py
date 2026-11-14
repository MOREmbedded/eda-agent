# SPDX-License-Identifier: Apache-2.0
"""Altium's Explorer panel, read through UI Automation.

The Altium 365 GraphQL API lists components, symbols and footprints, but
has no list of 3D model items: a component only links to one, and a
footprint's revision only counts its children. Explorer shows them, so
this reads them there, the way a person would: select a folder in the
folder tree (``VaultFoldersGrid``) and read the item grid beside it.

Selecting a folder row is all it does. It types nothing, clicks nothing,
moves no pointer and opens no item: the rows are selected through UIA's
SelectionItem pattern and collapsed folders are expanded through
ExpandCollapse. Altium must be running and signed in, with Explorer open
(View > Panels > Explorer, docked or floating) and not minimised.

Everything here runs on whatever thread calls it, and that thread must
have COM initialised: call it through ``panels.on_com_thread``.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Iterable, Optional

TREE_ITEM, DATA_ITEM, TEXT, DATA_GRID = 50024, 50029, 50020, 50028
FOLDERS_GRID = "VaultFoldersGrid"
#: The item grids Explorer has been seen to use: one for 3D models and
#: other plain items, one for components.
ITEM_GRIDS = ("ItemsGrid", "FlexibleBrowseItemsGrid")
MODEL_COLUMNS = ("item_id", "revision", "name", "description", "state", "note")


class Explorer:
    """One Explorer panel, found by its folder grid."""

    def __init__(self, automation, module, root):
        self.uia, self.U, self.root = automation, module, root
        self.walker = automation.ControlViewWalker

    # --- finding things ------------------------------------------------------

    def find_all(self, under, control_type: Optional[int] = None,
                 automation_id: Optional[str] = None) -> list:
        U, uia = self.U, self.uia
        conditions = []
        if control_type:
            conditions.append(uia.CreatePropertyCondition(
                U.UIA_ControlTypePropertyId, control_type))
        if automation_id:
            conditions.append(uia.CreatePropertyCondition(
                U.UIA_AutomationIdPropertyId, automation_id))
        if not conditions:
            condition = uia.CreateTrueCondition()
        elif len(conditions) == 1:
            condition = conditions[0]
        else:
            condition = uia.CreateAndCondition(conditions[0], conditions[1])
        found = under.FindAll(U.TreeScope_Descendants, condition)
        return [found.GetElement(i) for i in range(found.Length)]

    def folder_grid(self):
        grids = self.find_all(self.root, DATA_GRID, FOLDERS_GRID)
        return grids[0] if grids else None

    def item_grid(self):
        for grid in self.find_all(self.root, DATA_GRID):
            try:
                if grid.CurrentAutomationId in ITEM_GRIDS:
                    return grid
            except Exception:                    # noqa: BLE001
                continue
        return None

    def _pattern(self, element, pattern_id, interface):
        try:
            found = element.GetCurrentPattern(pattern_id)
            return found.QueryInterface(interface) if found else None
        except Exception:                        # noqa: BLE001
            return None

    def scroll_to(self, grid, percent: float) -> float:
        """Scroll ``grid`` vertically; returns how much of it one view shows."""
        U = self.U
        scroll = self._pattern(grid, U.UIA_ScrollPatternId, U.IUIAutomationScrollPattern)
        if scroll is None or not scroll.CurrentVerticallyScrollable:
            return 100.0
        scroll.SetScrollPercent(-1, max(0.0, min(100.0, percent)))
        time.sleep(0.15)
        return float(scroll.CurrentVerticalViewSize or 100.0)

    def _label(self, row) -> tuple[str, int]:
        """A folder row's text and the x of its label, which is its indent."""
        for text in self.find_all(row, TEXT):
            try:
                name = (text.CurrentName or "").strip()
            except Exception:                    # noqa: BLE001
                continue
            if name:
                return name, int(text.CurrentBoundingRectangle.left)
        return "", int(row.CurrentBoundingRectangle.left)

    def visible_folders(self, grid) -> list[dict]:
        """Folder rows realised now, top to bottom: name, indent, element."""
        rows = []
        for row in self.find_all(grid, TREE_ITEM):
            try:
                if self.walker.GetParentElement(row).CurrentControlType == TREE_ITEM:
                    continue
                name, indent = self._label(row)
                rows.append({"name": name, "indent": indent,
                             "top": int(row.CurrentBoundingRectangle.top),
                             "key": tuple(row.GetRuntimeId()), "element": row})
            except Exception:                    # noqa: BLE001
                continue
        return sorted(rows, key=lambda r: r["top"])

    def expand(self, row) -> bool:
        """Expand a collapsed row; True when it was collapsed."""
        U = self.U
        pattern = self._pattern(row, U.UIA_ExpandCollapsePatternId,
                                U.IUIAutomationExpandCollapsePattern)
        if pattern is None:
            return False
        try:
            if pattern.CurrentExpandCollapseState == 0:     # collapsed
                pattern.Expand()
                time.sleep(0.4)
                return True
        except Exception:                        # noqa: BLE001
            pass
        return False

    def collapse(self, row) -> None:
        U = self.U
        pattern = self._pattern(row, U.UIA_ExpandCollapsePatternId,
                                U.IUIAutomationExpandCollapsePattern)
        try:
            if pattern is not None and pattern.CurrentExpandCollapseState == 1:
                pattern.Collapse()
                time.sleep(0.2)
        except Exception:                        # noqa: BLE001
            pass

    def select(self, row) -> bool:
        U = self.U
        pattern = self._pattern(row, U.UIA_SelectionItemPatternId,
                                U.IUIAutomationSelectionItemPattern)
        if pattern is None:
            return False
        pattern.Select()
        return True

    def item_rows(self, settle: float = 1.0) -> list[list[str]]:
        """Every row of the item grid, scrolled through, as its columns."""
        time.sleep(settle)
        grid = self.item_grid()
        if grid is None:
            return []
        seen: set[str] = set()
        rows: list[list[str]] = []
        percent, stale = 0.0, 0
        while True:
            view = self.scroll_to(grid, percent)
            new = 0
            for row in self.find_all(grid, TREE_ITEM) + self.find_all(grid, DATA_ITEM):
                try:
                    name = row.CurrentName or ""
                except Exception:                # noqa: BLE001
                    continue
                if ";" not in name or name in seen:
                    continue
                seen.add(name)
                new += 1
                rows.append(split_row(name))
            if percent >= 100 or view >= 100:
                return rows
            stale = stale + 1 if new == 0 else 0
            if stale > 5:
                return rows
            percent += max(view * 0.8, 1.0)


def split_row(name: str) -> list[str]:
    """Columns of an item grid row: UIA names one as ``;col1;col2;...``."""
    columns = [c.strip() for c in name.split(";")]
    return columns[1:] if columns and columns[0] == "" else columns


def subtree(rows: list[dict], roots: Iterable[str]) -> list[dict]:
    """The rows under each named root, the root included, by indent: a row
    belongs to a root until a row at the root's indent or less comes."""
    wanted = {r.strip().lower() for r in roots}
    picked: list[dict] = []
    current: Optional[int] = None
    for row in rows:
        if current is not None and row["indent"] <= current:
            current = None
        if current is None and row["name"].strip().lower() in wanted:
            current = row["indent"]
            picked.append(row)
            continue
        if current is not None:
            picked.append(row)
    return picked


def folder_paths(rows: list[dict]) -> list[str]:
    """The path of each row, from the indents of the rows above it."""
    if not rows:
        return []
    indents = sorted({r["indent"] for r in rows})
    step = min((b - a for a, b in zip(indents, indents[1:])), default=16) or 16
    stack: list[str] = []
    paths = []
    for row in rows:
        depth = max(0, round((row["indent"] - indents[0]) / step))
        stack = stack[:depth] + [row["name"]]
        paths.append("/".join(stack))
    return paths


def _every_folder(explorer: Explorer, grid) -> list[dict]:
    """Every folder row now in the tree, top to bottom, scrolling it."""
    ordered: list[dict] = []
    seen: set = set()
    percent = 0.0
    while True:
        view = explorer.scroll_to(grid, percent)
        for row in explorer.visible_folders(grid):
            if row["key"] not in seen:
                seen.add(row["key"])
                ordered.append(row)
        if percent >= 100 or view >= 100:
            return ordered
        percent += max(view * 0.8, 5.0)


def _expand_keys(explorer: Explorer, grid, keys: set, opened: list) -> bool:
    """Expand the rows with these keys wherever they are in the tree."""
    grew = False
    percent = 0.0
    keys = set(keys)
    while keys:
        view = explorer.scroll_to(grid, percent)
        for row in explorer.visible_folders(grid):
            if row["key"] in keys:
                keys.discard(row["key"])
                if explorer.expand(row["element"]):
                    opened.append(row["key"])
                    grew = True
        if percent >= 100 or view >= 100:
            break
        percent += max(view * 0.8, 5.0)
    return grew


def _leaf_names(roots: Iterable[str]) -> list[str]:
    return [r.strip().strip("/").split("/")[-1] for r in roots]


def read_all_folders(explorer: Explorer, roots: Iterable[str], opened: list,
                     progress: Callable[[str], None] = lambda _m: None) -> list[dict]:
    """Find the named folders, expand them and everything under them, and
    list those rows in order, scrolling the folder tree as needed.

    A root is a folder name ("3D Models") or a path ("Models/3D Models").
    Until every root is seen, the folders on a path, and the collapsed
    folders whose name mentions "model", are expanded a level at a time;
    nothing else is opened. ``opened`` collects what was expanded, so the
    caller can fold the tree back.
    """
    grid = explorer.folder_grid()
    if grid is None:
        raise LookupError("no Explorer folder tree on screen")
    roots = [r for r in roots if r.strip()]
    leaves = _leaf_names(roots)
    on_path = {part.strip().lower() for r in roots for part in r.strip("/").split("/")[:-1]}
    for _level in range(4):
        rows = _every_folder(explorer, grid)
        names = {r["name"].strip().lower() for r in rows}
        if all(leaf.lower() in names for leaf in leaves):
            break
        ahead = {r["key"] for r in rows
                 if r["name"].strip().lower() in on_path
                 or (not on_path and "model" in r["name"].lower())}
        if not _expand_keys(explorer, grid, ahead - set(opened), opened):
            break
    for _pass in range(30):
        targets = {r["key"] for r in subtree(_every_folder(explorer, grid), leaves)}
        if not _expand_keys(explorer, grid, targets - set(opened), opened):
            break
    picked = subtree(_every_folder(explorer, grid), leaves)
    progress(f"{len(picked)} folders under {', '.join(roots)}")
    return picked


def fold_back(explorer: Explorer, opened: list) -> None:
    """Collapse what read_all_folders expanded, deepest first."""
    grid = explorer.folder_grid()
    if grid is None:
        return
    for key in reversed(opened):
        percent = 0.0
        while True:
            view = explorer.scroll_to(grid, percent)
            row = next((r for r in explorer.visible_folders(grid) if r["key"] == key), None)
            if row is not None:
                explorer.collapse(row["element"])
                break
            if percent >= 100 or view >= 100:
                break
            percent += max(view * 0.8, 5.0)
    explorer.scroll_to(grid, 0.0)


def read_models(explorer: Explorer, roots: Iterable[str],
                progress: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    """Every item in the folders under ``roots``, by folder."""
    opened: list = []
    try:
        return _read_models(explorer, read_all_folders(explorer, roots, opened, progress),
                            progress)
    finally:
        fold_back(explorer, opened)


def _read_models(explorer: Explorer, folders: list[dict],
                 progress: Callable[[str], None]) -> dict[str, Any]:
    paths = folder_paths(folders)
    grid = explorer.folder_grid()
    by_folder: dict[str, int] = {}
    items: dict[str, dict] = {}
    for row, path in zip(folders, paths):
        element = row["element"]
        if not explorer.select(element):
            # The row was scrolled away and its element went stale; find it again.
            percent = 0.0
            element = None
            while element is None:
                view = explorer.scroll_to(grid, percent)
                element = next((r["element"] for r in explorer.visible_folders(grid)
                                if r["key"] == row["key"]), None)
                if element is not None or percent >= 100 or view >= 100:
                    break
                percent += max(view * 0.8, 5.0)
            if element is None or not explorer.select(element):
                by_folder[path] = -1
                continue
        rows = explorer.item_rows()
        by_folder[path] = len(rows)
        progress(f"{path}: {len(rows)}")
        for columns in rows:
            record = dict(zip(MODEL_COLUMNS, columns + [""] * len(MODEL_COLUMNS)))
            record["folder"] = path
            items.setdefault(record["item_id"] + "/" + record["revision"], record)
    return {"folders": by_folder, "items": list(items.values())}


def find_explorer(automation, module) -> Optional[Explorer]:
    """The Explorer panel among Altium's windows, or None."""
    import win32gui

    found: list[int] = []

    def visit(hwnd, _extra):
        try:
            if win32gui.IsWindowVisible(hwnd):
                found.append(hwnd)
        except Exception:                        # noqa: BLE001
            pass
    win32gui.EnumWindows(visit, None)
    for hwnd in found:
        try:
            if win32gui.GetClassName(hwnd) not in ("TClientBarControl", "TDocumentForm",
                                                   "TCustomForm", "TApplicationForm",
                                                   "TDXPMainForm"):
                continue
            root = automation.ElementFromHandle(hwnd)
            explorer = Explorer(automation, module, root)
            if explorer.folder_grid() is not None:
                return explorer
        except Exception:                        # noqa: BLE001
            continue
    return None


def scan_models(roots: Iterable[str],
                progress: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    """Read the 3D models (or any items) in the Explorer folders ``roots``.
    Needs COM on the calling thread."""
    import comtypes.client
    from .comgen import get_module
    get_module("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as U

    automation = comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)
    explorer = find_explorer(automation, U)
    if explorer is None:
        return {"ok": False, "reason": (
            "Altium's Explorer panel is not on screen: open it (View > Panels > "
            "Explorer), sign in to the workspace, and leave Altium not minimised")}
    got = read_models(explorer, roots, progress)
    if not got["folders"]:
        return {"ok": False, "reason": (
            f"no folder named {', '.join(roots)} in Explorer's folder tree; pass the "
            "folder names your library keeps 3D models in")}
    return {"ok": True, **got}
