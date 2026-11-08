# SPDX-License-Identifier: Apache-2.0
"""obj_modify says modified:false when every write it asked for was refused.

Live on 2026-11-08: setting a note's ShowBorder (a note refuses it) and a
text frame's FontId=0 were both refused, properties.unknown / failed said
so, and the reply still read modified:true. ``modified`` was "a mutating
call matched something", not "something was written".

Writes are now counted where SetSchProperty ends, and only when nothing in
that write was refused. A modify reports modified:false when no write was
counted AND every property it asked for is recorded as refused. Both
conditions, so a property that slipped past the counting keeps the old
answer rather than claiming nothing changed.

The Pascal cannot run here, so the structure is held by source and the
decision itself by a line-for-line Python mirror.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "scripts" / "altium"
UTILS = (ROOT / "Utils.pas").read_text(encoding="utf-8")
GENERIC = (ROOT / "Generic.pas").read_text(encoding="utf-8")


def _body(text: str, header: str) -> str:
    start = text.index(header)
    return text[start:text.index("\nEnd;", start)]


def test_a_refusal_is_noted_before_the_dedup_exit():
    """The same refusal on a second object is recorded once but is still a
    refusal of that write."""
    body = _body(UTILS, "Procedure NotePropertyDiag(")
    assert body.index("_PropCallRefused := True") < body.index("Then Exit;")


def test_the_counters_reset_with_the_diagnostics():
    body = _body(UTILS, "Procedure ResetPropertyDiag(")
    assert "_PropertyWrites := 0;" in body
    assert "_PropCallRefused := False;" in body


def test_set_sch_property_brackets_each_write():
    body = _body(GENERIC, "Function SetSchProperty(")
    after_begin = body[body.index("\nBegin"):]
    assert after_begin.lstrip("\r\nBegin").lstrip().startswith("BeginPropertyWrite(0);")
    assert body.rstrip().endswith("EndPropertyWrite(Result);")


def test_both_schematic_replies_use_it():
    new = "And Not ((Mode = 'modify') And NothingWasWritten(SetStr)))"
    assert GENERIC.count(new) == 2
    old = ("BoolToJsonStr(IsMutating And ((TotalMatched > 0) Or "
           "(Mode = 'create')))\n")
    assert old not in GENERIC.replace("\r\n", "\n")


def nothing_was_written(set_str: str, writes: int, diag: str) -> bool:
    """NothingWasWritten, line for line."""
    if writes > 0 or not set_str.strip():
        return False
    bracketed = "|" + diag + "|"
    for pair in set_str.split("|"):
        if "=" not in pair:
            continue
        name = pair.split("=", 1)[0].strip()
        if not name:
            continue
        if (f"|unknown:{name}|" not in bracketed
                and f"|failed:{name}|" not in bracketed):
            return False
    return True


@pytest.mark.parametrize("set_str, writes, diag, expected", [
    # The live cases: one refused property, nothing written.
    ("ShowBorder=False", 0, "unknown:ShowBorder", True),
    ("FontId=0", 0, "failed:FontId", True),
    # Something landed: modified.
    ("TextMargin=20", 1, "", False),
    ("ShowBorder=False|TextMargin=20", 1, "unknown:ShowBorder", False),
    # One refused, one neither counted nor refused: keep modified:true.
    ("ShowBorder=False|TextMargin=20", 0, "unknown:ShowBorder", False),
    # A name inside another is not a match.
    ("Font=1", 0, "unknown:FontId", False),
    ("", 0, "", False),
])
def test_the_decision(set_str, writes, diag, expected):
    assert nothing_was_written(set_str, writes, diag) is expected
