# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""obj_delete in a SchLib counts only objects that actually went.

REPORTED from outside (AD 26.8): obj_delete of library pins answered
"matched 100000" and deleted nothing. The SchLib branch of
ProcessSchDocObjects removed each match through the component and counted
it at once; a pin of the symbol on screen is held by the library, as a
visible parameter is (test_schlib_visible_parameter_delete.py), so the
remove did nothing, the next pass found the same pin, and the loop counted
it until its 100000 guard ran out.
"""
from __future__ import annotations

from pathlib import Path

from tests.pascal_source import load

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "altium"


def _lib_delete() -> str:
    body = load(SCRIPTS / "Generic.pas")["ProcessSchDocObjects"]
    start = body.index("If Mode = 'delete' Then")
    return body[start:body.index("SchEndEdit(SchLib", start)]


def test_a_remove_is_not_counted_straight_after_it_is_made():
    body = _lib_delete()
    remove = body.index("LibComp.RemoveSchObject(FoundObj)")
    after = body[remove:body.index("End;", body.index("LibPending := True", remove))]
    assert "Inc(TotalMatched)" not in after
    assert "LibPending := True" in after


def test_counted_only_when_the_object_is_not_found_again():
    body = _lib_delete()
    gate = body.index("If LibPending And ((FoundObj = Nil) Or (ThisAddr <> LastAddr)) Then")
    assert body.index("Inc(TotalMatched)") > gate


def test_a_pin_found_again_is_removed_through_the_library():
    body = _lib_delete()
    stuck = body.index("If LibPending And (ThisAddr = LastAddr) Then")
    assert body.index("SchLib.RemoveSchObject(FoundObj)", stuck) > stuck


def test_a_pin_that_will_not_go_is_a_fault_not_a_loop():
    body = _lib_delete()
    fault = body.index("NoteDeleteFault('an object stayed on the symbol")
    assert body.index("Break", fault) > fault
    assert "SameTries > 1" in body
