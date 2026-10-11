# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The library pad read says where the copper is and whether a slot lines up.

A custom pad's copper sat 0.775 mm from the origin that x_mm reports, a
misaligned slot read the same as a good one because hole_rotation was
missing, and the mask source folded three cache states into two.
"""

from __future__ import annotations

import re
from pathlib import Path

from eda_agent.design.footprint_audit import pad_position_mm

LIBRARY = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "Library.pas"


def _body(name: str) -> str:
    text = LIBRARY.read_text(encoding="utf-8")
    start = text.index(f"Function {name}(")
    end = text.find("\nFunction ", start + 1)
    return text[start:end]


def test_the_audit_uses_the_copper_centre_when_the_read_has_one():
    custom = {"x_mm": -1.6, "y_mm": 0.0, "copper_x_mm": -0.825, "copper_y_mm": 0.0}
    assert pad_position_mm(custom) == (-0.825, 0.0)
    assert pad_position_mm({"x_mm": 1.0, "y_mm": 2.0}) == (1.0, 2.0)


def test_the_read_reports_copper_centre_hole_rotation_and_slot_alignment():
    body = _body("Lib_GetPadGeometry")
    for key in ("copper_x_mm", "copper_y_mm", "hole_rotation", "slot_aligned",
                "mask_expansion_state", "paste_expansion_state"):
        assert f'"{key}"' in body, key
    assert '"x_mm":' in body and "Pad.X - XOrg" in body, (
        "x_mm stays the origin; existing readers depend on it")


def test_slot_alignment_compares_long_axes_not_angles():
    body = _body("Lib_GetPadGeometry")
    lines = re.findall(r"HoleAligned := (.+);", body)
    assert any("TopXSize >= Pad.TopYSize" in ln and "HoleTurn" in ln
               for ln in lines), lines
    assert "Rotation = Pad.HoleRotation" not in body


def test_all_three_cache_states_are_named():
    body = _body("CacheStateName")
    for state, name in (("eCacheManual", "manual"), ("eCacheValid", "valid"),
                        ("eCacheInvalid", "invalid")):
        assert re.search(rf"State = {state} Then Result := '{name}'", body)


def test_both_mask_sides_and_the_separate_flag_are_reported():
    """Not in the API reference, so held back until measured: on AD 26.10
    both fields exist on TPadCache, 0.2 top and 0.05 bottom read back
    apart, and a bottom value written while linked came back on
    unlinking. Read through a declared cache local."""
    body = re.sub(r"\{[^}]*\}", "", _body("Lib_GetPadGeometry"))
    assert "PadCache := Pad.GetState_Cache;" in body
    assert "PadCache.UseSeparateExpansions" in body
    assert "PadCache.SolderMaskBottomExpansion" in body
    assert '"mask_bottom_expansion_mm"' in body and '"mask_separate"' in body
