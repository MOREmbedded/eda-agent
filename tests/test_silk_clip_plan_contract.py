# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""What a silk clip plan line must carry, and what refuses it.

``pcb_apply_silk_clip_plan`` and ``pcb_clone_silk_track_fragment`` had
never been run against a board, and the reason turned out not to be
neglect: both identify a track by its object address and nothing emitted
one, so they were unreachable. ``pcb_get_overlay_tracks`` fixes that, and
running them for the first time turned up two requirements the docstring
did not state.

A plan line with no ``width`` is refused as STALE, because the missing
field reads as a width that changed. A track with no component owner is
refused as BAD_OWNER however complete the line is. Both were found by
getting them wrong against a real board; these keep the handler and the
docstring saying the same thing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.pascal_source import load

_PCB_PAS = (Path(__file__).resolve().parents[1] / "scripts" / "altium"
            / "PCB.pas")


def _code(text: str) -> str:
    return re.sub(r"\{[^}]*\}", "", text)


@pytest.fixture(scope="module")
def apply_body() -> str:
    return _code(load(_PCB_PAS)["PCB_ApplySilkClipPlan"])


@pytest.fixture(scope="module")
def reader_body() -> str:
    # Not _code(): a Pascal brace comment opens with the same character a
    # JSON object does, so stripping comments eats the body's literals.
    return load(_PCB_PAS)["PCB_GetOverlayTracks"]


# --- the reader is what makes the writers reachable ----------------------

def test_the_reader_gives_every_field_a_plan_line_needs():
    """Verified live: a line built from exactly these fields validated,
    applied, and reported already_applied on a second pass."""
    body = load(_PCB_PAS)["PCB_GetOverlayTracks"]
    for field in ("address", "layer", "component", "x1", "y1", "x2", "y2",
                  "width"):
        assert f'"{field}"' in body, field


def test_the_reader_gives_raw_coordinates_not_mils(reader_body):
    """The plan compares coordinates as TEXT against IntToStr(Track.X1),
    so a value that went through millimetres never matches."""
    for axis in ("X1", "Y1", "X2", "Y2", "Width"):
        assert f"IntToStr(Track.{axis})" in reader_body, axis
    # The mils are there too, under names that cannot be mistaken for the
    # raw ones.
    for axis in ("x1_mils", "y1_mils", "x2_mils", "y2_mils", "width_mils"):
        assert f'"{axis}"' in reader_body, axis


def test_the_reader_only_walks_the_two_overlay_layers(reader_body):
    assert "MkSet(eTopOverlay, eBottomOverlay)" in reader_body


# --- what the apply requires ---------------------------------------------

def test_a_line_without_width_is_refused_as_stale(apply_body):
    """MEASURED on a board: the missing field reads as a width that
    changed, and the refusal names the address."""
    assert ("If (IntToStr(Track.Width) <> ExtractJsonValue(Line, 'width'))"
            in apply_body)
    assert "'STALE_PLAN', 'Geometry changed for ' + AddressText" in apply_body


def test_a_track_with_no_component_owner_is_refused(apply_body):
    """A free overlay track cannot be clipped however complete the line
    is, which is why a board of free silk could not exercise this."""
    assert "OwnerName := ExtractJsonValue(Line, 'component');" in apply_body
    assert "Parent := Track.Component;" in apply_body
    assert "'BAD_OWNER', 'A component owner is required'" in apply_body


def test_the_assembly_layer_has_to_match_the_side(apply_body):
    """Mechanical11 pairs with the top and Mechanical12 with the bottom;
    the other pairing is refused rather than quietly moving the track to
    the wrong side's assembly layer."""
    assert ("((OrigLayer = 'TopOverlay') And (TargetLayer <> 'Mechanical11'))"
            in apply_body)
    assert ("((OrigLayer = 'BottomOverlay') And (TargetLayer <> 'Mechanical12'))"
            in apply_body)


def test_only_an_overlay_layer_is_accepted(apply_body):
    assert ("((OrigLayer <> 'TopOverlay') And (OrigLayer <> 'BottomOverlay'))"
            in apply_body)


def test_applying_twice_reports_the_second_as_already_applied(apply_body):
    """Verified live: validate 1/0/0, apply 1/0/1, apply again 1/1/0."""
    assert "Already" in apply_body
    assert "already_applied" in load(_PCB_PAS)["PCB_ApplySilkClipPlan"]


# --- the clone's own guard -----------------------------------------------

def test_a_fragment_off_the_original_segment_is_refused():
    """Verified live: moving one end off the line gave NOT_SUBSEGMENT."""
    body = _code(load(_PCB_PAS)["PCB_CloneSilkTrackFragment"])
    assert "SilkPointOnTrack(FoundTrack, X1, Y1)" in body
    assert "SilkPointOnTrack(FoundTrack, X2, Y2)" in body
    assert "'NOT_SUBSEGMENT'" in body


def test_the_clone_keeps_the_component_owner():
    """The whole point of the tool: a replicated track that lost its owner
    would be free silk the next clip plan then refuses."""
    body = _code(load(_PCB_PAS)["PCB_CloneSilkTrackFragment"])
    replicate = body.index("FoundTrack.Replicate")
    assert body.index("Parent.AddPCBObject(", replicate) > replicate


def test_both_writers_demand_the_named_board_is_focused():
    """They act on the focused board, so a path that names another one is
    refused rather than silently editing the wrong document."""
    for name in ("PCB_ApplySilkClipPlan", "PCB_CloneSilkTrackFragment"):
        body = _code(load(_PCB_PAS)[name])
        assert "'WRONG_BOARD'" in body, name
        assert "LowerCase(Board.FileName) <> LowerCase(BoardPath)" in body, name
