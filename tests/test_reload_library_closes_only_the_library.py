# SPDX-License-Identifier: Apache-2.0
"""lib_reload_library closes the library it names and nothing else.

Live on Altium 26.11 it raised "Unsaved Changes" for the board in front:
WorkspaceManager:CloseObject acts on the focused document, not the one
named. The library was never reloaded and the ECO still missed its new
footprint. A source guard, as the Pascal cannot run here.
"""
from __future__ import annotations

import re

from tests.test_handlers_verify_their_outcome import _body, _decommented, _src


def _reload() -> str:
    return _decommented(_body(_src("Library.pas"), "Lib_ReloadLibrary"))


def test_the_focused_document_is_never_closed():
    body = _reload()
    assert "CloseObject" not in body
    assert re.search(r"ServerDoc\s*:=\s*Client\.GetDocumentByPath\(LibPath\)", body)
    assert "Client.CloseDocument(ServerDoc)" in body
    assert "PathKey(ServerDoc.FileName) <> PathKey(LibPath)" in body


def test_only_a_library_is_reloaded():
    body = _reload()
    assert "'.PCBLIB'" in body and "'.SCHLIB'" in body
    assert "NOT_A_LIBRARY" in body


def test_a_dirty_library_is_refused_before_any_close():
    body = _reload()
    refuse = body.index("'MODIFIED'")
    assert "ServerDoc.Modified" in body[:refuse]
    assert "DocWasEdited(LibPath)" in body[:refuse]
    assert refuse < body.index("Client.CloseDocument(ServerDoc)")
    # A save from here would raise nothing, but a close of a dirty
    # document would: nothing in the handler saves or clears the flag.
    assert "DoFileSave" not in body and "SetModified" not in body


def test_the_projects_holding_it_resolve_their_models_again():
    body = _reload()
    assert "DM_Compile" in body
    assert '"projects_recompiled"' in body
