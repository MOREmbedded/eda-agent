# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A by-name symbol lookup reads the open library and never writes it (#88).

The lookup used to fall back, on any miss, to saving the SchLib, closing it
and opening it again. A stale name did that on every call and wrote the
user's file. Measured on Altium 26.11: symbols created earlier in the session
are found in memory, so the reopen had stopped earning its cost.
"""

from __future__ import annotations

from tests.test_handlers_verify_their_outcome import _body, _decommented, _src


def _lib(func: str) -> str:
    return _decommented(_body(_src("Library.pas"), func))


def test_the_lookup_never_reopens_or_saves():
    body = _lib("LookupLibComponent")
    for call in ("RefreshSchLibFromDisk", "DoFileSave", "CloseObject", "OpenObject"):
        assert call not in body, call
    assert "FindLibComponentInMemory(SchLib, Name)" in body


def test_the_in_memory_lookup_walks_the_live_library():
    body = _lib("FindLibComponentInMemory")
    assert "ScanLibForComponent(SchLib, Name)" in body
    assert "SchLibIterator_Create" in _lib("ScanLibForComponent")


def test_an_explicit_refresh_is_still_available():
    text = _decommented(_src("Library.pas"))
    at = text.index("ExtractJsonValue(Params, 'refresh')")
    assert "RefreshSchLibFromDisk(LibPath)" in text[at:at + 300]


def test_get_components_names_the_symbols_the_file_does_not_have():
    body = _lib("Lib_GetComponents")
    assert '"unsaved_symbols":[' in body
    assert '"saved_names_not_in_open_library":[' in body
    assert "LiveLib.SchLibIterator_Create" in body


def test_an_unreachable_part_puts_the_editor_back_on_part_one():
    body = _lib("SelectLibComponentPart")
    failed = body.index("If Not StepLibComponentPartTo(SchLib, Component, Target) Then")
    assert "ReachLibPartOne(SchLib, Component, Name)" in body[failed:failed + 2500]
