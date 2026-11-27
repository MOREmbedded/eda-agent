# SPDX-License-Identifier: Apache-2.0
"""Library tools never send the agent to app_save_all.

A QPA9903 SchLib was saved by following the library tools' own advice,
"Run app_save_all", and Altium wrote all 29 open documents: other
projects' sheets and boards included. app_save_document writes only the
library. The advice lives in three places: the library tools'
descriptions, the library recipes in tool_guide, and the note the
Altium script adds to every deferred library edit.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_library_tool_descriptions_name_the_single_document_save():
    src = (ROOT / "src" / "eda_agent" / "tools" / "library.py").read_text(encoding="utf-8")
    assert "app_save_all" not in src


def test_library_recipes_use_the_single_document_save():
    from eda_agent.tools.guidance import _RECIPES as TASKS
    for task in TASKS:
        if task.get("document_kind") == "library":
            assert "app_save_all" not in task.get("use", []), task["task"]
            assert "app_save_all" not in str(task.get("note", "")), task["task"]


def test_the_scripts_library_notes_say_save_the_library_alone():
    pas = (ROOT / "scripts" / "altium" / "Library.pas").read_text(encoding="utf-8")
    # Only string literals: comments may describe the history.
    said = [m for m in re.findall(r"'([^']*)'", pas) if "app_save_all" in m]
    assert said and all("Do not use app_save_all" in s for s in said), said
    assert any("app_save_document" in m for m in re.findall(r"'([^']*)'", pas))
