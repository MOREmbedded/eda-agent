# SPDX-License-Identifier: Apache-2.0
"""No script reads a rectangle's fields.

On AD 26.12.2 BoundingRectangle hands the script an interface whose fields
it cannot name: .Left, .X1 and .GetLeft each halt it with "Undeclared
identifier", past any Try (2026-11-27). Boxes come from each object's own
coordinates through the helpers in Utils.pas.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ALTIUM = Path(__file__).resolve().parents[1] / "scripts" / "altium"


@pytest.fixture(scope="module")
def lint():
    spec = importlib.util.spec_from_file_location("_altium_lint_rect", ALTIUM / "lint.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_altium_lint_rect"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("line", [
    "    R := Obj.BoundingRectangle;",
    "    B := Comp.BoundingRectangleNoNameComment;",
    "    X := Pad.BoundingRectangleOnLayer(L).Left;",
    "    BR := Room.BoundingRect;",
    "    R := Board.GraphicalView_ViewportRect;",
    "    R : TCoordRect;",
])
def test_the_rule_flags_every_form(lint, line):
    assert lint.RULE_RECTANGLE_FIELDS.pattern.search(line)


def test_no_source_reads_a_rectangle(lint):
    hits = []
    for pas in sorted(ALTIUM.glob("*.pas")):
        if pas.name == "Altium_MCP.pas":
            continue
        for i, raw in enumerate(pas.read_text(encoding="utf-8").splitlines(), 1):
            if lint.RULE_RECTANGLE_FIELDS.pattern.search(lint.strip_comments_and_strings(raw)):
                hits.append(f"{pas.name}:{i}")
    assert hits == []


def test_every_box_helper_reads_through_a_typed_local():
    utils = (ALTIUM / "Utils.pas").read_text(encoding="utf-8")
    body = utils[utils.index("Function PcbLeafBox("):utils.index("Function PcbGroupBox(")]
    for kind, local in (("ePadObject", "Pad := Obj;"), ("eTrackObject", "Track := Obj;"),
                        ("eArcObject", "Arc := Obj;"), ("eFillObject", "Fill := Obj;"),
                        ("eTextObject", "Txt := Obj;")):
        assert kind in body and local in body
