# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Schematic pin nets against board pad nets after an ECO."""

from __future__ import annotations

from eda_agent.core.eco_verify import verify_eco


def _part(designator, *pins):
    return {"designator": designator,
            "pins": [{"pin_number": p, "net": n} for p, n in pins]}


def _pads(designator, *pads):
    return [{"component": designator, "name": p, "net": n} for p, n in pads]


def test_a_finished_eco_is_complete():
    out = verify_eco([_part("U1", ("1", "VCC"), ("2", "GND"))],
                     _pads("U1", ("1", "VCC"), ("2", "GND")))
    assert out["complete"] is True
    assert out["nothing_compared"] is False
    assert (out["pins_checked"], out["pads_checked"]) == (2, 2)


def test_pads_left_without_a_net_are_unbound_and_counted_per_part():
    """One part left mostly unbound while its twin finished."""
    nets = [(str(n), f"N{n}") for n in range(1, 14)]
    left = [(p, n if int(p) <= 3 else "") for p, n in nets]
    out = verify_eco([_part("U14", *nets), _part("U15", *nets)],
                     _pads("U14", *nets) + _pads("U15", *left))
    assert out["complete"] is False
    assert out["unbound_count"] == 10
    assert out["unbound_by_component"] == {"U15": 10}
    assert {r["designator"] for r in out["unbound"]} == {"U15"}


def test_a_pad_on_another_net_is_mismatched():
    out = verify_eco([_part("R1", ("1", "SDA"))], _pads("R1", ("1", "SCL")))
    assert out["mismatched"] == [{"designator": "R1", "pin": "1",
                                  "schematic_net": "SDA", "pad_net": "SCL"}]
    assert out["complete"] is False


def test_each_copy_of_a_repeated_pad_name_is_checked():
    out = verify_eco([_part("U1", ("EP", "GND"))],
                     _pads("U1", ("EP", "GND"), ("EP", ""), ("EP", "GND")))
    assert out["pads_checked"] == 3
    assert out["unbound_count"] == 1
    assert out["complete"] is False


def test_pins_and_pads_with_no_partner_are_listed_both_ways():
    out = verify_eco([_part("U1", ("1", "A"), ("2", "B"))],
                     _pads("U1", ("1", "A"), ("3", "C")))
    assert out["pins_without_pad"] == [
        {"designator": "U1", "pin": "2", "net": "B"}]
    assert out["pads_without_pin"] == [
        {"designator": "U1", "pin": "3", "net": "C"}]
    assert out["complete"] is False


def test_an_unconnected_pin_is_counted_not_reported():
    out = verify_eco([_part("U1", ("1", "A"), ("2", ""))],
                     _pads("U1", ("1", "A"), ("2", "")))
    assert out["complete"] is True
    assert out["pins_unconnected"] == 1
    assert out["unbound_count"] == 0


def test_designators_and_pad_names_match_ignoring_case_and_spaces():
    out = verify_eco([_part(" u1 ", ("a1", "X"))], _pads("U1", ("A1 ", "X")))
    assert out["complete"] is True


def test_a_pad_with_no_component_is_left_out():
    out = verify_eco([_part("U1", ("1", "A"))],
                     _pads("U1", ("1", "A")) + _pads("", ("1", "GND")))
    assert out["pads_checked"] == 1
    assert out["pads_without_pin_count"] == 0


def test_an_empty_side_is_never_complete():
    for components, pads in (([], _pads("U1", ("1", "A"))),
                             ([_part("U1", ("1", "A"))], []),
                             ([], [])):
        out = verify_eco(components, pads)
        assert out["nothing_compared"] is True
        assert out["complete"] is False


def test_long_lists_are_cut_and_say_so():
    nets = [(str(n), "N") for n in range(1, 11)]
    out = verify_eco([_part("U1", *nets)],
                     _pads("U1", *[(p, "") for p, _ in nets]), limit=4)
    assert out["unbound_count"] == 10
    assert len(out["unbound"]) == 4
    assert out["unbound_truncated"] is True
    assert out["mismatched_truncated"] is False


def test_malformed_rows_are_skipped():
    out = verify_eco([None, "x", {"designator": "U1", "pins": [None, {}]},
                      _part("U2", ("1", "A"))],
                     [None, {"name": "1"}] + _pads("U2", ("1", "A")))
    assert out["pins_checked"] == 1
    assert out["complete"] is True
