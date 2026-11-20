# SPDX-License-Identifier: Apache-2.0
"""An edit a tool made reads as modified until it is written or discarded.

MEASURED 2026-11-20 on AD 26.11: sch_place_note called MarkDocDirtyByPath,
which calls IServerDocument.SetModified(True), and the document listing
still said modified:false. proj_close's save pass then left the sheet
unchanged on disk and the close stopped on "Unsaved Changes".

So the bridge's own record of the edit, kept by full path, is part of
"modified"; a save that the server document did not write goes through
the editor's File > Save; and a discard or a close forgets the record.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.pascal_source import load

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "altium"
MAIN = load(SCRIPTS / "Main.pas")
PROJECT = load(SCRIPTS / "Project.pas")


def _calls(body: str, name: str) -> bool:
    return re.search(r"\b" + re.escape(name) + r"\s*\(", body) is not None


def test_the_edit_record_keeps_full_paths() -> None:
    assert "EditedPaths" in MAIN["NoteDocEdited"]
    assert "EditedPaths" in MAIN["DocEditedExactly"]


def test_modified_includes_the_edit_record() -> None:
    assert _calls(MAIN["ServerDocIsModified"], "DocEditedExactly"), (
        "ServerDocIsModified reads only IServerDocument.Modified, which "
        "stays False after API edits")


def test_an_unwritten_edit_is_saved_through_the_editor() -> None:
    body = MAIN["SaveOneDocByDocRef"]
    first = body.find("DoFileSave(")
    fallback = body.find("WorkspaceManager:SaveObject")
    assert first >= 0 and fallback > first, (
        "the File > Save fallback must follow the server-document save")
    assert _calls(body[first:fallback], "DocEditedExactly"), (
        "the fallback must run only for a document a tool edited")


def test_a_discard_forgets_the_edit_record() -> None:
    assert _calls(PROJECT["ClearDocModifiedByPath"], "ForgetDocEditedExactly"), (
        "a discarded document would still read as modified and the "
        "save=False close would be refused as still_modified")


def test_a_close_forgets_its_members_edits() -> None:
    body = PROJECT["Proj_Close"]
    assert _calls(body, "ForgetPathsEditedExactly")
    assert _calls(body, "UnwrittenProjectMembers")
    assert "not_shown_written" in body


def test_forget_by_name_clears_the_path_record_too() -> None:
    assert "EditedPaths" in MAIN["ForgetDocEdited"]
