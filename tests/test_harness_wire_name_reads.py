# SPDX-License-Identifier: Apache-2.0
"""A harness wire answers Name, so obj_query and obj_delete can find it.

A harness wire, splice or cable has no Name property; its name lives only
in its description ("Harness Wire W2 ..."). GetSchProperty answered Name
for those objects with an empty string marked unreadable, so on a live
wiring diagram (2026-11-08) obj_query listed two wires with Name "" and
`obj_delete filter="Name=W2"` matched nothing; only UniqueId worked.

The parser sch_get_harness already used now lives above GetSchProperty
(DelphiScript resolves names top-down) and the Name branch falls back to
it. The parse itself is mirrored here in Python and held to the same
cases the Pascal handles.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAS = (ROOT / "scripts" / "altium" / "Generic.pas").read_text(encoding="utf-8")


def _body(name: str) -> str:
    start = PAS.index(f"Function {name}(")
    return PAS[start:PAS.index("\nEnd;", start)]


def test_the_parser_is_defined_before_the_reader_that_calls_it():
    assert PAS.index("Function SchHarnessName(") < PAS.index("Function GetSchProperty(")


def test_name_falls_back_to_the_harness_description():
    reader = _body("GetSchProperty")
    branch = reader[reader.index("Else If PropName = 'Name'"):]
    branch = branch[:branch.index("Else If PropName = 'LibReference'")]
    assert "SchHarnessName(Obj.GetState_DescriptionString)" in branch
    # Unreadable only when the fallback found nothing either.
    assert "If Result = '' Then NotePropertyDiag('unreadable', PropName);" in branch


def test_sch_get_harness_and_the_reader_share_one_parser():
    assert "Result := SchHarnessName(Desc);" in _body("HarnessNameFromDescription")


def _mirror(desc: str) -> str:
    """SchHarnessName, line for line."""
    if desc[:8] != "Harness ":
        return ""
    desc = desc[8:]
    p = desc.find(" ")
    if p < 0:
        return ""
    desc = desc[p + 1:].strip()
    if not desc or desc[0] == "(":
        return ""
    p = desc.find(" ")
    return desc[:p] if p >= 0 else desc


@pytest.mark.parametrize("desc, name", [
    ("Harness Wire W2 (Points: 6)", "W2"),
    ("Harness Wire W1", "W1"),
    ("Harness Splice SPL1 at (2000,1500)", "SPL1"),
    ("Harness Pin J1-2", "J1-2"),
    ("Harness Twist (2100,1200)", ""),
    ("Wire (100,200)", ""),
    ("Harness Wire", ""),
])
def test_the_parse(desc, name):
    assert _mirror(desc) == name
