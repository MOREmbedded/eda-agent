# SPDX-License-Identifier: Apache-2.0
"""Schematic data to an LTspice deck.

The emitter is pure, so the cases that matter are testable with no
Altium and no LTspice. Two of them matter more than the rest: a deck
whose node order is wrong still simulates and lies, and a deck with no
node 0 is not a deck at all.
"""
from __future__ import annotations

import pytest

from eda_agent.engineering.spice_netlist import build_deck, validate_analysis


def _comp(designator, prefix, value):
    return {"designator": designator, "suggested_prefix": prefix,
            "suggested_value": value}


def _pins(*nets):
    return [{"pin_number": str(i), "net": n} for i, n in enumerate(nets, 1)]


DIVIDER = (
    [_comp("R1", "R", "10k"), _comp("R2", "R", "10k"), _comp("V1", "V", "5")],
    {"R1": _pins("VIN", "VOUT"), "R2": _pins("VOUT", "GND"),
     "V1": _pins("VIN", "GND")},
)


# ---------------------------------------------------------------- known answer

def test_a_divider_emits_the_deck_a_human_would_write():
    result = build_deck(*DIVIDER, analysis="op", title="divider")
    assert result.ok
    assert result.deck == (
        "* divider\n"
        "R1 VIN VOUT 10k\n"
        "R2 VOUT 0 10k\n"
        "V1 VIN 0 5\n"
        ".op\n"
        ".end\n"
    )


def test_the_analysis_directive_is_written_with_one_leading_dot():
    for given in ("tran 1m", ".tran 1m"):
        deck = build_deck(*DIVIDER, analysis=given).deck
        assert "\n.tran 1m\n" in deck
        assert ".." not in deck


def test_extra_directives_land_before_the_analysis():
    result = build_deck(*DIVIDER, analysis="op",
                        extra_directives=(".include mymodels.lib",))
    body = result.deck.splitlines()
    assert body.index(".include mymodels.lib") < body.index(".op")


# ---------------------------------------------------------------- ground

def test_the_ground_net_becomes_node_zero():
    deck = build_deck(*DIVIDER, analysis="op").deck
    assert "R2 VOUT 0 10k" in deck
    assert "GND" not in deck


def test_a_custom_ground_net_name_is_honoured():
    comps = [_comp("R1", "R", "1k"), _comp("V1", "V", "5")]
    conn = {"R1": _pins("VIN", "AGND"), "V1": _pins("VIN", "AGND")}
    assert build_deck(comps, conn, analysis="op", ground_net="AGND").ok


def test_ground_matching_is_case_insensitive():
    comps = [_comp("R1", "R", "1k"), _comp("V1", "V", "5")]
    conn = {"R1": _pins("VIN", "gnd"), "V1": _pins("VIN", "GND")}
    deck = build_deck(comps, conn, analysis="op").deck
    assert deck.count(" 0 ") + deck.count(" 0\n") >= 2


def test_a_deck_with_no_ground_is_refused_rather_than_emitted():
    """No node 0 means SPICE either fails to converge or solves a
    different circuit, and the second is worse than an error."""
    comps = [_comp("R1", "R", "1k"), _comp("V1", "V", "5")]
    conn = {"R1": _pins("A", "B"), "V1": _pins("A", "B")}
    result = build_deck(comps, conn, analysis="op")
    assert result.ok is False
    assert "node 0" in result.reason and "ground_net" in result.reason


# ---------------------------------------------------------------- node order

def test_passives_are_not_flagged_because_their_order_is_irrelevant():
    assert build_deck(*DIVIDER, analysis="op").pin_order_assumptions == []


def test_a_diode_is_emitted_but_its_order_is_reported_as_an_assumption():
    comps = [_comp("D1", "D", "1N4148"), _comp("V1", "V", "5")]
    conn = {"D1": _pins("VIN", "GND"), "V1": _pins("VIN", "GND")}
    result = build_deck(comps, conn, analysis="op")
    assert result.ok and "D1 VIN 0 1N4148" in result.deck
    assumed = result.pin_order_assumptions
    assert [a["designator"] for a in assumed] == ["D1"]
    assert assumed[0]["nodes"] == ["VIN", "0"]


def test_a_transistor_is_flagged_too():
    comps = [_comp("Q1", "Q", "2N3904"), _comp("V1", "V", "5")]
    conn = {"Q1": _pins("C", "B", "GND"), "V1": _pins("C", "GND")}
    result = build_deck(comps, conn, analysis="op")
    assert result.ok
    assert [a["designator"] for a in result.pin_order_assumptions] == ["Q1"]


def test_pins_are_ordered_numerically_not_as_strings():
    """A 10-pin part numbered 1, 2, 10 must not order 1, 10, 2."""
    comps = [_comp("M1", "M", "IRF540"), _comp("V1", "V", "5")]
    conn = {"M1": [{"pin_number": "10", "net": "D"},
                   {"pin_number": "2", "net": "G"},
                   {"pin_number": "1", "net": "S"},
                   {"pin_number": "3", "net": "GND"}],
            "V1": _pins("D", "GND")}
    result = build_deck(comps, conn, analysis="op")
    assert result.ok
    assert result.pin_order_assumptions[0]["pin_numbers"] == ["1", "2", "3", "10"]
    assert "M1 S G 0 D IRF540" in result.deck


def test_a_part_with_the_wrong_node_count_is_skipped_not_guessed():
    comps = [_comp("Q1", "Q", "2N3904"), _comp("R1", "R", "1k"),
             _comp("V1", "V", "5")]
    conn = {"Q1": _pins("A", "B"),  # a BJT needs three
            "R1": _pins("A", "GND"), "V1": _pins("A", "GND")}
    result = build_deck(comps, conn, analysis="op")
    assert result.ok
    assert "Q1" not in result.deck
    assert any(s["designator"] == "Q1" and "needs 3" in s["why"]
               for s in result.skipped)


# ---------------------------------------------------------------- net names

def test_net_names_illegal_in_spice_are_renamed_and_the_rename_reported():
    comps = [_comp("R1", "R", "1k"), _comp("V1", "V", "5")]
    conn = {"R1": _pins("VBUS/5V", "GND"), "V1": _pins("VBUS/5V", "GND")}
    result = build_deck(comps, conn, analysis="op")
    assert result.ok
    assert "VBUS_5V" in result.deck
    assert result.net_renames == {"VBUS/5V": "VBUS_5V"}


def test_a_clean_net_name_is_not_reported_as_renamed():
    assert build_deck(*DIVIDER, analysis="op").net_renames == {}


# ---------------------------------------------------------------- skipping

@pytest.mark.parametrize("comp,conn,needle", [
    ({"designator": "U1"}, _pins("A", "GND"), "no SPICE prefix"),
    (_comp("R9", "R", "1k"), [], "no connectivity"),
    (_comp("R9", "R", "1k"), _pins("A", ""), "on no net"),
    ({"designator": "R9", "suggested_prefix": "R"}, _pins("A", "GND"), "no value"),
])
def test_a_component_that_cannot_be_emitted_says_why(comp, conn, needle):
    comps = [comp, _comp("R1", "R", "1k"), _comp("V1", "V", "5")]
    connectivity = {"R1": _pins("A", "GND"), "V1": _pins("A", "GND"),
                    str(comp["designator"]): conn}
    result = build_deck(comps, connectivity, analysis="op")
    assert result.ok, "the rest of the circuit still emits"
    assert any(needle in s["why"] for s in result.skipped), result.skipped


def test_emitting_nothing_at_all_is_a_refusal_with_the_reasons():
    result = build_deck([{"designator": "U1"}], {"U1": _pins("A", "GND")},
                        analysis="op")
    assert result.ok is False
    assert "nothing could be emitted" in result.reason
    assert result.skipped


# ---------------------------------------------------------------- missing models

def test_missing_models_refuse_by_default_and_name_the_parts():
    result = build_deck(*DIVIDER, analysis="op", missing_models=("U1", "U2"))
    assert result.ok is False
    assert "U1, U2" in result.reason
    assert "sim_attach_model" in result.reason
    assert result.deck == "", "nothing is emitted on a refusal"
    assert result.missing_models == ["U1", "U2"]


def test_placeholder_mode_emits_the_rest_and_comments_the_gap():
    comps = [*DIVIDER[0], _comp("U1", "X", "LT1763")]
    conn = {**DIVIDER[1], "U1": _pins("VIN", "VOUT", "GND")}
    result = build_deck(comps, conn, analysis="op", missing_models=("U1",),
                        on_missing_model="placeholder")
    assert result.ok
    assert "* U1 needs a manufacturer SPICE model" in result.deck
    assert "\nU1 " not in result.deck, "it must not be emitted as a real element"
    assert any(s["designator"] == "U1" for s in result.skipped)


def test_an_unknown_missing_model_mode_is_refused():
    result = build_deck(*DIVIDER, analysis="op", on_missing_model="improvise")
    assert result.ok is False and "on_missing_model" in result.reason


# ---------------------------------------------------------------- analysis

@pytest.mark.parametrize("analysis", ["tran 1m", "ac dec 100 1 1meg", "op",
                                      ".tran 0 1m 0 1u", "noise V(out) V1 dec 10 1 1g"])
def test_known_analyses_are_accepted(analysis):
    assert validate_analysis(analysis) is None


@pytest.mark.parametrize("analysis", ["", "   ", "transient 1m", "sweep"])
def test_an_unknown_or_empty_analysis_is_refused(analysis):
    assert validate_analysis(analysis) is not None
    assert build_deck(*DIVIDER, analysis=analysis).ok is False


# ---------------------------------------------------------------- naming

def test_a_designator_already_carrying_its_prefix_is_not_doubled():
    assert "RR1" not in build_deck(*DIVIDER, analysis="op").deck


def test_a_designator_whose_letter_disagrees_gets_the_spice_prefix():
    """A part annotated U3 but classified as a diode must still be a D
    element, or SPICE reads it as a subcircuit call."""
    comps = [_comp("U3", "D", "1N4148"), _comp("V1", "V", "5")]
    conn = {"U3": _pins("A", "GND"), "V1": _pins("A", "GND")}
    deck = build_deck(comps, conn, analysis="op").deck
    assert "DU3 A 0 1N4148" in deck
