# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Clearance-family rules report the gap they hold, not the descriptor's.

Measured on AD 26.10 on a scratch board: a Board Outline Clearance rule
(kind 63) read Gap 0.254 mm when created and 3 mm after a write, and its
descriptor said Gap=0mil both times. Reading only the descriptor, a caller
concluded the rule did nothing (#39.7). The typed Gap is read through a
local assigned straight from the iterator, where DelphiScript narrows.
"""

from __future__ import annotations

import re
from pathlib import Path

PCB = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "PCB.pas"


def _body(name: str) -> str:
    text = PCB.read_text(encoding="utf-8-sig")
    start = re.search(rf"^Function {name}\(", text, re.M).start()
    return text[start:text.index("\nEnd;\n", start)]


def _kinds_ok(body: str) -> None:
    for kind in ("eRule_Clearance", "24", "52", "63"):
        assert f"(Kind = {kind})" in body, kind


def test_the_gap_is_read_through_a_typed_iterator_local():
    body = _body("ClearanceGapMM")
    assert "ClearRule : IPCB_ClearanceConstraint;" in body
    assert "ClearRule := Iter.FirstPCBObject;" in body
    assert "ClearRule.Gap" in body
    _kinds_ok(body)


def test_both_rule_readers_report_gap_mm():
    listing = _body("PCB_GetDesignRules")
    assert "ClearRule := GapIter.FirstPCBObject;" in listing
    assert ',"gap_mm":' in listing
    _kinds_ok(listing)
    single = _body("PCB_GetRuleProperties")
    assert "ClearanceGapMM(Board, RuleName)" in single
    assert ',"gap_mm":' in single
