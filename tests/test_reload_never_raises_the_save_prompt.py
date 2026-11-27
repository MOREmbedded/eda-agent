"""A reload or close never raises Altium's save prompt for other documents.

app_reload_document(discard_changes=True) closed the QPA9903 SchLib and
reopened it. The close raised "Confirm Save Locally for (1), Save to
Server for (5) Modified Documents" on the owner's screen (2026-11-27):
one answer there writes, or pushes to Altium 365, work this server never
touched. Discarding now reloads in place, and a close is refused while
any other open document is modified.

The same day obj_modify wrote Orientation=270 to a pin raw; Altium then
raised "Unhandled value for pin orientation" on every redraw. Degrees map
to quarter turns, and anything else is refused.
"""
from __future__ import annotations

import re
from pathlib import Path

PAS = Path(__file__).resolve().parents[1] / "scripts" / "altium"


def _routine(file: str, name: str) -> str:
    text = (PAS / file).read_text(encoding="utf-8")
    start = re.search(rf"^Function {name}\b", text, re.M).start()
    end = re.search(r"^(Function|Procedure) ", text[start + 10:], re.M)
    return text[start:start + 10 + (end.start() if end else len(text))]


def _code(src: str) -> str:
    """Without { } comments and string literals."""
    return re.sub(r"\{[^}]*\}|'[^']*'", "", src)


def test_a_discarding_reload_loads_in_place_and_closes_nothing():
    body = _code(_routine("Application.pas", "App_ReloadDocument"))
    discard = body[body.index("If DiscardChanges Then"):body.index("CloseParams :=")]
    assert "DoFileLoad" in discard and "Exit" in discard
    assert "CloseDocument" not in discard and "App_CloseDocument" not in discard


def test_a_close_is_refused_while_other_documents_are_modified():
    body = _code(_routine("Application.pas", "App_CloseDocument"))
    assert body.index("OtherModifiedDocuments(") < body.index("Client.CloseDocument(")
    assert "OTHERS_MODIFIED" in _routine("Application.pas", "App_CloseDocument")


def test_schematic_orientation_is_written_as_quarter_turns():
    body = _code(_routine("Generic.pas", "SetSchProperty"))
    branch = body[body.index("SchObjectHasOrientation(Obj) Then") - 200:]
    assert "SchOrientationOf(Value)" in branch[:600]
    assert "StrToIntDef(Value, 0)" not in branch[:600]


def _orientation(value: str) -> int:
    """SchOrientationOf, as Generic.pas writes it."""
    if not re.fullmatch(r"-?\d+", value):
        return -1
    n = int(value)
    if 0 <= n <= 3:
        return n
    if n % 90 == 0:
        return (int(n / 90) % 4 + 4) % 4
    return -1


def test_degrees_map_to_quarter_turns_and_others_are_refused():
    src = _routine("Generic.pas", "SchOrientationOf")
    assert "Mod 90" in src and "Div 90" in src
    assert [_orientation(v) for v in ("0", "90", "180", "270", "360", "-90", "2")] == \
        [0, 1, 2, 3, 0, 3, 2]
    assert _orientation("266") == -1 and _orientation("45") == -1


def test_pad_geometry_reads_no_bounding_rectangle_fields():
    """AD 26.12.2 halted Lib_GetPadGeometry on PadBR.Right, so every footprint
    build's readback and every lib_link_footprint failed (2026-11-27)."""
    body = _code(_routine("Library.pas", "Lib_GetPadGeometry"))
    assert "BoundingRectangle" not in body


def test_pads_are_collected_before_they_are_changed():
    body = _code(_routine("Library.pas", "Lib_SetPadProperties"))
    walk = body.index("GrpIter.FirstPCBObject")
    edit = body.index("ApplyPadEdit(")
    assert body.index("Target.GroupIterator_Destroy", walk) < edit
    assert "Matched.Add(Pad)" in body and "PadNth" in body


def test_footprint_walks_key_on_more_than_the_address():
    """On AD 26.12.2 every footprint read saw only its first object."""
    lib = (PAS / "Library.pas").read_text(encoding="utf-8")
    assert "PrimKeyTail(" in _routine("Utils.pas", "PrimKeyTail")
    for name in ("Lib_GetPadGeometry", "Lib_GetFootprintPrimitives", "Lib_SetPadProperties"):
        assert "PrimKeyTail(" in _routine("Library.pas", name), name
    assert "Ids.IndexOf(PadAddr + " not in lib    # ids stay plain addresses


def test_footprint_primitives_read_no_bounding_rectangle():
    body = _code(_routine("Library.pas", "Lib_GetFootprintPrimitives"))
    assert "BoundingRectangle" not in body and "TCoordRect" not in body
