# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A rule set can be applied twice without doubling the board's rules.

``PCB_CreateDesignRule`` sets ``.Name`` and adds, with no check for a name
already present, so a second apply left two rules of one name and Altium's
priority order picked the winner. ``reconcile`` decides per rule whether
to create, update in place, or leave alone.

The descriptor fixtures in ``tests/data/altium_rule_descriptors.json`` are
the 159 rules of a real six-layer board, read through
``pcb_get_design_rules`` on AD 26.11 and kept with the owner's rule names
replaced. Parsing is tested against those rather than against invented
strings, because every awkward case here came from them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eda_agent.design.rule_manager import (
    SKIP_REASON_SCHEMATIC,
    board_rule_state,
    canonical_key,
    compare_rule,
    parse_descriptor,
    reconcile,
    to_mils,
    update_call,
)

_REAL = json.loads(
    (Path(__file__).parent / "data" / "altium_rule_descriptors.json")
    .read_text(encoding="utf-8"))


# --- units ------------------------------------------------------------

def test_a_bare_number_is_already_mils():
    assert to_mils("3") == 3.0


def test_mm_and_mil_both_parse():
    assert to_mils("0.254mm") == pytest.approx(10.0)
    assert to_mils("6mil") == 6.0


def test_a_negative_value_survives():
    """A clearance check is waived for one part by a negative gap."""
    assert to_mils("-20mm") == pytest.approx(-787.4015748)


def test_what_is_not_a_number_is_not_guessed_at():
    for text in ("No", "Shortest", "", "Allowed", "0.1mmm"):
        assert to_mils(text) is None, text


# --- Altium's spelling ------------------------------------------------

def test_altium_misspells_preferred_and_both_spellings_fold():
    assert canonical_key("Prefered") == "preferred"
    assert canonical_key("Preferred") == "preferred"
    assert canonical_key("PreferedWidth") == "preferredwidth"


def test_both_spellings_are_really_on_one_board():
    """Guards the reason the fold exists, against the real fixtures."""
    blob = " ".join(r["descriptor"] for r in _REAL)
    assert "Prefered=" in blob or "Prefered =" in blob
    assert "Preferred=" in blob or "Preferred =" in blob


def test_a_space_before_the_equals_is_tolerated():
    got = parse_descriptor("Solder Mask Expansion (Expansion =0mm) (All)")
    assert got["values"]["expansion"] == 0.0


def test_two_values_in_one_paren_both_parse():
    got = parse_descriptor(
        "Component Clearance Constraint ( Horizontal Gap = -20mm, "
        "Vertical Gap = -20mm ) (InComponent('J12')),(All)")
    assert got["values"]["horizontal gap"] == pytest.approx(-787.4015748)
    assert got["values"]["vertical gap"] == pytest.approx(-787.4015748)


def test_a_non_numeric_constraint_is_kept_as_written():
    got = parse_descriptor("Short-Circuit Constraint (Allowed=No) (All),(All)")
    assert got["text"]["allowed"] == "No"
    assert "allowed" not in got["values"]


# --- the DRC flag -----------------------------------------------------

def test_disabled_in_the_descriptor_is_the_drc_flag():
    on = parse_descriptor("Net Antennae (Tolerance=0mil) (All)")
    off = parse_descriptor("Net Antennae (Tolerance=0mil) (Disabled)(All)")
    assert on["drc_enabled"] is True and off["drc_enabled"] is False


def test_the_reported_field_wins_over_the_descriptor():
    """Newer scripts report drc_enabled; the word is the fallback."""
    rule = {"name": "R", "descriptor": "Net Antennae (Tolerance=0mil) (All)",
            "enabled": True, "drc_enabled": False}
    assert board_rule_state(rule)["drc_enabled"] is False


def test_an_older_script_without_the_field_still_answers():
    rule = {"name": "R", "enabled": True,
            "descriptor": "Net Antennae (Tolerance=0mil) (Disabled)(All)"}
    assert board_rule_state(rule)["drc_enabled"] is False


def test_enabled_true_does_not_mean_drc_checks_it():
    """The two flags differ on 37 of the 159 real rules."""
    off = [r for r in _REAL if "(Disabled)" in r["descriptor"]]
    assert off, "the fixtures should carry DRC-disabled rules"
    assert all(r["enabled"] is True for r in off)
    assert all(board_rule_state(r)["drc_enabled"] is False for r in off)


# --- the real board ---------------------------------------------------

def test_every_real_descriptor_parses_without_raising():
    for r in _REAL:
        state = board_rule_state(r)
        assert state["name"] == r["name"]
        assert isinstance(state["values"], dict)


def test_the_clearance_field_wins_over_the_descriptor():
    """Board Outline Clearance's descriptor always says Gap=0mil."""
    rule = {"name": "Outline", "enabled": True, "gap_mm": 0.254,
            "descriptor": "Board Outline Clearance (Gap=0mil) (All)"}
    assert board_rule_state(rule)["values"]["gap"] == pytest.approx(10.0)


# --- reconciling ------------------------------------------------------

def _board(*rules):
    return [dict({"enabled": True, "from_schematic": False}, **r) for r in rules]


def test_a_rule_the_board_lacks_is_created():
    got = reconcile([{"name": "Clearance_Fab_Min", "rule_type": "clearance",
                      "value": 6}], [])
    assert [r["name"] for r in got["create"]] == ["Clearance_Fab_Min"]
    assert got["in_step"] is False


def test_a_rule_that_already_matches_is_left_alone():
    board = _board({"name": "Clearance_Fab_Min",
                    "descriptor": "Clearance Constraint (Gap=6mil) (All),(All)"})
    got = reconcile([{"name": "Clearance_Fab_Min", "rule_type": "clearance",
                      "value": 6}], board)
    assert got["same"] == ["Clearance_Fab_Min"]
    assert got["create"] == [] and got["update"] == []
    assert got["in_step"] is True


def test_the_millimetre_round_trip_is_not_a_difference():
    """6 mil comes back as 0.152mm, which is 5.984 mil."""
    board = _board({"name": "W", "descriptor":
                    "Width Constraint (Min=0.152mm) (Max=2mm) (All)"})
    got = reconcile([{"name": "W", "rule_type": "width", "value": 6}], board)
    assert got["same"] == ["W"], got


def test_a_rule_whose_value_differs_is_updated_not_recreated():
    board = _board({"name": "W", "descriptor":
                    "Width Constraint (Min=0.15mm) (Max=2mm) (Preferred=0.2mm) (All)"})
    got = reconcile([{"name": "W", "rule_type": "width", "value": 10}], board)
    assert got["create"] == []
    assert len(got["update"]) == 1
    diff = got["update"][0]["differences"][0]
    assert diff["field"] == "value" and diff["want"] == 10


def test_matching_is_case_insensitive_the_way_altium_names_are():
    board = _board({"name": "clearance_fab_min",
                    "descriptor": "Clearance Constraint (Gap=6mil) (All),(All)"})
    got = reconcile([{"name": "Clearance_Fab_Min", "rule_type": "clearance",
                      "value": 6}], board)
    assert got["same"] and not got["create"]


def test_a_schematic_owned_rule_is_never_written():
    board = _board({"name": "Supply", "from_schematic": True,
                    "descriptor": "Width Constraint (Min=0.15mm) (All)"})
    got = reconcile([{"name": "Supply", "rule_type": "width", "value": 99}],
                    board)
    assert got["update"] == [] and got["create"] == []
    assert got["skipped"] == [{"name": "Supply",
                               "reason": SKIP_REASON_SCHEMATIC}]


def test_a_rule_the_profile_does_not_define_is_reported_never_deleted():
    board = _board({"name": "SomeoneElses",
                    "descriptor": "Clearance Constraint (Gap=8mil) (All),(All)"})
    got = reconcile([], board)
    assert [e["name"] for e in got["extra"]] == ["SomeoneElses"]
    assert "delete" not in got


def test_a_doubled_name_is_reported_rather_than_compared_twice():
    """What an apply without this module produced."""
    board = _board(
        {"name": "W", "descriptor": "Width Constraint (Min=6mil) (All)"},
        {"name": "W", "descriptor": "Width Constraint (Min=9mil) (All)"})
    got = reconcile([{"name": "W", "rule_type": "width", "value": 6}], board)
    assert got["duplicates"] == ["W"]
    assert got["same"] == ["W"]


def test_the_drc_flag_is_a_difference_when_the_profile_asks_for_one():
    board = _board({"name": "N", "descriptor":
                    "Net Antennae (Tolerance=0mil) (Disabled)(All)"})
    got = reconcile([{"name": "N", "rule_type": "clearance",
                      "drc_enabled": True}], board)
    assert got["update"][0]["differences"] == [
        {"field": "drc_enabled", "want": True, "got": False}]


def test_a_kind_whose_value_the_descriptor_does_not_carry_says_so():
    board = _board({"name": "X", "descriptor": "Silk primitive without silk layer"})
    got = reconcile([{"name": "X", "rule_type": "clearance", "value": 6}], board)
    assert got["update"][0]["differences"][0]["got"] is None


# --- turning an update into a call ------------------------------------

def test_an_update_becomes_set_rule_properties_arguments():
    entry = {"name": "W", "rule_type": "width",
             "differences": [{"field": "value", "want": 10, "got": 6}],
             "planned": {"value": 10}}
    assert update_call(entry) == {"name": "W", "min_width_mils": 10}


def test_a_kind_with_no_in_place_writer_returns_nothing():
    """Better reported than deleted and recreated, which loses priority."""
    entry = {"name": "V", "rule_type": "via_size",
             "differences": [{"field": "value", "want": 10, "got": 8}],
             "planned": {"value": 10}}
    assert update_call(entry) is None


def test_the_drc_flag_is_handed_to_its_own_tool():
    entry = {"name": "N", "rule_type": "clearance",
             "differences": [{"field": "drc_enabled", "want": True,
                              "got": False}],
             "planned": {"drc_enabled": True}}
    assert update_call(entry) == {"name": "N", "_drc_enabled": True}


def test_reconciling_the_real_board_against_itself_is_in_step():
    """Every rule read back and compared to what it already holds."""
    planned = []
    for r in _REAL:
        state = board_rule_state(r)
        if state["from_schematic"] or "gap" not in state["values"]:
            continue
        planned.append({"name": r["name"], "rule_type": "clearance",
                        "value": state["values"]["gap"]})
    got = reconcile(planned, _REAL)
    assert got["update"] == [], got["update"][:3]
    assert got["create"] == []
    assert len(got["same"]) == len(planned) and planned
