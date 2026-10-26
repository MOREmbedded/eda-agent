# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""lib_get_component_details reports every part-count reading it has.

The library file reader said 2 parts where the open symbol said 1
(issue #88), and on another symbol a placed part said 4 against the
library's correct 5. Neither source is right every time, so the open
symbol's count and the highest part that has pins go beside the file's,
with a note when they disagree, rather than one being picked.
"""
from __future__ import annotations

from pathlib import Path

from tests.pascal_source import load

_LIB = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "Library.pas"


def test_all_three_readings_are_in_the_reply():
    body = load(_LIB)["Lib_GetComponentDetails"]
    assert ',"part_count":' in body
    assert ',"part_count_live":' in body
    assert ',"parts_with_pins":' in body
    assert '"part_count_note"' in body


def test_parts_with_pins_skips_pins_shared_by_every_part():
    """OwnerPartId 0 is a pin on every part; it says nothing about how
    many parts there are."""
    body = load(_LIB)["SymbolPartsWithPins"]
    assert "If Owner > Result Then Result := Owner;" in body


def test_the_counter_is_defined_before_its_caller():
    text = _LIB.read_text(encoding="utf-8")
    assert text.index("Function SymbolPartsWithPins(") < text.index(
        "Function Lib_GetComponentDetails(")
