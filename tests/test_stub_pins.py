# SPDX-License-Identifier: Apache-2.0
"""sch_stub_pins on a real sheet.

Reported from an older build: the parts of a multi-part symbol were
confused, net labels came out drawn over each other, and connectors were
left without stubs with nothing said about them. The part confusion was
fixed before (Gen_StubPins takes a pin only from a part that shows it,
PinShownOn; tests/test_multipart_templates.py). Here: a pin is stubbed
once, labels read along the stub and keep apart, and each pin not stubbed
comes back with why.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from eda_agent.tools import generic
from tests.pascal_source import load

_ALTIUM = Path(__file__).resolve().parents[1] / "scripts" / "altium"


def _tool(monkeypatch, unconnected, reply=None):
    sent = []

    class _Bridge:
        async def send_command_async(self, command, params=None, timeout=None):
            sent.append((command, params))
            if command == "generic.get_unconnected_pins":
                return {"unconnected_pins": unconnected}
            return dict(reply or {"stubbed": 0, "failed": 0, "failed_pins": []})

    monkeypatch.setattr(generic, "get_bridge", lambda: _Bridge())
    captured = {}

    class _Mcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    generic.register_generic_tools(_Mcp())
    return captured["sch_stub_pins"], sent


def test_a_pin_listed_twice_is_stubbed_once(monkeypatch):
    pins = [{"designator": "U4", "pin_number": "7"}, {"designator": "U4", "pin_number": "7"},
            {"designator": "U4", "pin_number": "8"}, {"designator": "J1", "pin_number": "1"},
            {"designator": "J1", "pin_number": "1,2"}]
    tool, sent = _tool(monkeypatch, pins)
    out = asyncio.run(tool())
    stub = [p for c, p in sent if c == "generic.stub_pins"][0]
    assert stub["pins"] == "U4,7,U4_7|U4,8,U4_8|J1,1,J1_1"
    assert out["pins"] == ["U4,7,U4_7", "U4,8,U4_8", "J1,1,J1_1"]


def test_the_pins_not_stubbed_come_back_with_why(monkeypatch):
    reply = {"stubbed": 1, "failed": 1, "lengthened": 0,
             "failed_pins": [{"designator": "J2", "pin": "1",
                              "reason": "part_not_on_this_sheet"}]}
    tool, _ = _tool(monkeypatch, [{"designator": "J1", "pin_number": "1"},
                                  {"designator": "J2", "pin_number": "1"}], reply)
    out = asyncio.run(tool())
    assert out["failed_pins"] == reply["failed_pins"]


def test_no_designator_prefix_or_pin_type_is_dropped(monkeypatch):
    """Connectors are stubbed like any part: nothing filters J* or a
    passive or I/O pin out before the sheet is asked."""
    pins = [{"designator": d, "pin_number": "1", "pin_name": n}
            for d, n in (("J1", "1"), ("J2", "SHIELD"), ("P3", "A"), ("U1", "VCC"))]
    tool, sent = _tool(monkeypatch, pins)
    asyncio.run(tool())
    stub = [p for c, p in sent if c == "generic.stub_pins"][0]
    assert [r.split(",")[0] for r in stub["pins"].split("|")] == ["J1", "J2", "P3", "U1"]


# ---- the Pascal --------------------------------------------------------------

def _body() -> str:
    return load(_ALTIUM / "Generic.pas")["Gen_StubPins"]


def test_a_pin_is_taken_only_from_a_part_that_shows_it():
    assert "PinShownOn(Comp, Pin)" in _body()


def test_labels_read_along_the_stub_and_away_from_the_part():
    body = _body()
    assert "If (Orient = 1) Or (Orient = 3) Then NetLabel.Orientation := 1" in body
    assert "If (Orient = 2) Or (Orient = 3) Then" in body
    assert "NetLabel.Justification := 2;" in body


def test_a_label_that_would_overlap_gets_a_longer_stub():
    body = _body()
    loop = body[body.index("Repeat"):body.index("Until Clear Or (Tries > 4);")]
    assert "Len := StubLen * (Tries + 1);" in loop
    assert "StubLabelBox(Orient, EX, EY, Length(Lbl), BX1, BY1, BX2, BY2);" in loop
    assert "Clear := StubBoxClear(Boxes, BX1, BY1, BX2, BY2);" in loop
    # The wire runs to where the label went, and the box is kept for the
    # labels after it.
    assert body.index("Until Clear") < body.index("Boxes.Add(FloatToStr(BX1));") \
        < body.index("Wire.SetState_Vertex(2, Point(MilsToCoordF(EX), MilsToCoordF(EY)));")
    boxes = load(_ALTIUM / "Generic.pas")["StubBoxClear"]
    assert "If (X1 < BX2) And (BX1 < X2) And (Y1 < BY2) And (BY1 < Y2) Then" in boxes


def test_each_pin_not_stubbed_is_named_with_why():
    body = _body()
    assert "Why := 'pin_not_shown_on_this_sheet'" in body
    assert "Why := 'part_not_on_this_sheet'" in body
    assert "',\"failed_pins\":[' + FailedJson + ']}'" in body
