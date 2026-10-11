# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""app_save_document writes one document and measures that it did.

app_save_all writes every open document, so one that cannot be written
stopped a save of another. The single-document save must not walk the
workspace or move focus, and must judge success by the file's age, since
DoFileSave does not raise when the editor declines.
"""

from __future__ import annotations

from tests.test_handlers_verify_their_outcome import _body, _decommented, _src


def test_saved_comes_from_the_file_age_around_the_save():
    body = _decommented(_body(_src("Application.pas"), "App_SaveDocument"))
    before = body.index("AgeBefore := FileAge(FilePath)")
    save = body.index("DoFileSave(")
    after = body.index("AgeAfter := FileAge(FilePath)")
    assert before < save < after
    assert "Saved := (AgeAfter >= 0) And (AgeAfter > AgeBefore)" in body


def test_it_touches_nothing_but_the_one_document():
    body = _decommented(_body(_src("Application.pas"), "App_SaveDocument"))
    for walk in ("SaveAllDirty", "WorkspaceDocPaths", "SaveProjectMembers",
                 "ShowDocument", "OpenObject", "FocusDocument"):
        assert walk not in body, walk


def test_the_command_is_dispatched():
    text = _src("Application.pas")
    assert "'save_document':       Result := App_SaveDocument(Params, RequestId);" in text
