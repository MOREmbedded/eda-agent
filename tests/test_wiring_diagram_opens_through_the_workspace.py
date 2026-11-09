# SPDX-License-Identifier: Apache-2.0
"""app_open_document and app_reload_document open a .WirDoc.

Measured on AD 26.11.1.5 (2026-11-08): the extension list had no .wirdoc,
so app_reload_document closed a wiring diagram and then could not reopen
it (KIND_REQUIRED). Passing a kind did not help: 'HarnessWiringDiagram',
the kind the document is created under, made Client.OpenDocument return
Nil, and 'HARNESS' opened the file as something sch_get_harness could not
see while reporting success.

What worked was WorkspaceManager:OpenObject, which sch_place_harness_wires
and app_set_active_document already use. These tests hold App_OpenDocument
to that route for a .WirDoc whatever kind is passed, and to reporting the
kind Altium actually gave the document rather than echoing the request.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAS = (ROOT / "scripts" / "altium" / "Application.pas").read_text(encoding="utf-8")


def _body(name: str) -> str:
    start = PAS.index(f"Function {name}(")
    end = PAS.index("\nEnd;", start)
    return PAS[start:end]


def test_a_wirdoc_is_a_known_extension():
    assert re.search(r"Ext = '\.wirdoc' Then DocKind :=", _body("App_OpenDocument"))


def test_a_wirdoc_opens_through_the_workspace_manager_whatever_kind_is_asked():
    body = _body("App_OpenDocument")
    assert "ViaWorkspace := LowerCase(ExtractFileExt(FilePath)) = '.wirdoc';" in body
    assert "ServerDoc := OpenThroughWorkspace(FilePath)" in body
    route = _body("OpenThroughWorkspace")
    assert "AddStringParameter('ObjectKind', 'Document');" in route
    assert "RunProcess('WorkspaceManager:OpenObject');" in route
    assert "Client.GetDocumentByPath(FilePath)" in route


def test_the_reply_names_the_kind_altium_gave_not_the_one_asked_for():
    body = _body("App_OpenDocument")
    assert "ActualKind := ServerDoc.Kind;" in body
    assert "EscapeJsonString(ActualKind)" in body
    assert "EscapeJsonString(DocKind) +\n" not in body.replace("\r\n", "\n")
    assert '"kind_ignored":' in body


def test_reload_reopens_through_the_same_open():
    """A reload is a close and then App_OpenDocument, so it inherits the fix."""
    assert "Result := App_OpenDocument(OpenParams, RequestId);" in _body("App_ReloadDocument")
