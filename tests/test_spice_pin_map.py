# SPDX-License-Identifier: Apache-2.0
"""Matching symbol pins to SPICE model nodes by name, never by guesswork."""
from __future__ import annotations

from eda_agent.sim import pin_map


def _pins(*pairs):
    return [{"pin_number": n, "pin_name": name} for n, name in pairs]


LDO = _pins(("1", "GND"), ("2", "VOUT"), ("3", "VIN"), ("4", "SET"), ("5", "GND"))


def test_nodes_are_matched_by_name_in_model_order():
    m = pin_map.propose(["IN", "OUT", "SET", "GND"],
                        _pins(("1", "IN"), ("2", "OUT"), ("3", "SET"), ("4", "GND")))
    assert m["ok"] and m["method"] == "names" and not m["needs_review"]
    assert [n["pins"] for n in m["nodes"]] == [["1"], ["2"], ["3"], ["4"]]


def test_aliases_match_but_ask_for_review_and_repeated_pins_join_one_node():
    m = pin_map.propose(["IN", "OUT", "SET", "GND"], LDO)
    assert m["ok"] and m["needs_review"]
    assert [n["pins"] for n in m["nodes"]] == [["3"], ["2"], ["4"], ["1", "5"]]


def test_an_unmatched_node_is_refused_not_guessed():
    m = pin_map.propose(["IN", "OUT", "ADJ2", "GND"], LDO)
    assert not m["ok"]
    assert m["unmatched_nodes"] == ["ADJ2"]
    assert "override" in m["reason"]


def test_overline_and_sign_spelling():
    assert pin_map.canonical("S\\H\\D\\N\\") == pin_map.canonical("~SHDN") == "SHDN"
    assert pin_map.canonical("IN+") == pin_map.canonical("+IN")
    assert pin_map.canonical("IN+") != pin_map.canonical("IN-")


def test_opamp_inputs_are_not_swapped():
    m = pin_map.propose(["INP", "INN", "OUT", "V+", "V-"],
                        _pins(("2", "-IN"), ("3", "+IN"), ("6", "OUT"),
                              ("7", "VCC"), ("4", "VEE")))
    assert m["ok"]
    assert [n["pins"] for n in m["nodes"]] == [["3"], ["2"], ["6"], ["7"], ["4"]]


def test_two_nodes_claiming_one_pin_is_refused():
    m = pin_map.propose(["VIN", "IN"], _pins(("1", "IN"), ("2", "X")))
    assert not m["ok"] and m["clashes"] == {"1": ["VIN", "IN"]}


def test_pin_number_order_only_when_no_name_matches_and_counts_agree():
    m = pin_map.propose(["1", "2", "3"], _pins(("3", "A"), ("1", "B"), ("2", "C")))
    assert m["ok"] and m["method"] == "pin_number_order" and m["needs_review"]
    assert [n["pins"] for n in m["nodes"]] == [["1"], ["2"], ["3"]]
    m2 = pin_map.propose(["1", "2", "3"], _pins(("1", "A"), ("2", "B")))
    assert not m2["ok"]


def test_override_wins_and_is_checked():
    m = pin_map.propose(["a", "b", "c"], LDO, override=["3", "2", "1+5"])
    assert m["ok"] and m["method"] == "override"
    assert m["nodes"][2]["pins"] == ["1", "5"]
    assert m["unused_pins"] == ["4"]
    bad = pin_map.propose(["a", "b", "c"], LDO, override=["3", "2", "9"])
    assert not bad["ok"] and "9" in bad["reason"]
    short = pin_map.propose(["a", "b", "c"], LDO, override=["3", "2"])
    assert not short["ok"]


def test_override_lookup_prefers_the_symbol_specific_entry():
    store = {pin_map.override_key("LT3080"): {"pins": ["1"]},
             pin_map.override_key("LT3080", "LDO_DFN"): {"pins": ["2"]}}
    assert pin_map.lookup_override(store, "lt3080", "LDO_DFN") == ["2"]
    assert pin_map.lookup_override(store, "lt3080", "OTHER") == ["1"]
    assert pin_map.lookup_override(store, "LT3081") is None


def test_standard_orders_for_discretes():
    d = pin_map.standard_order("D", _pins(("1", "K"), ("2", "A")))
    assert [n["pins"] for n in d["nodes"]] == [["2"], ["1"]]
    q = pin_map.standard_order("Q", _pins(("1", "B"), ("2", "E"), ("3", "C")))
    assert [n["pins"] for n in q["nodes"]] == [["3"], ["1"], ["2"]]
    fet = pin_map.standard_order("M", _pins(("1", "S"), ("2", "S"), ("3", "S"),
                                            ("4", "G"), ("5", "D"), ("6", "D")))
    assert [n["pins"] for n in fet["nodes"]] == [["5", "6"], ["4"], ["1", "2", "3"]]
    assert pin_map.standard_order("Q", _pins(("1", "1"), ("2", "2"), ("3", "3"))) is None


def test_unused_model_ports_need_no_pin():
    from eda_agent.sim.model_library import unused_node
    nodes = [unused_node(1), "IN", "OUT", "SET"]
    m = pin_map.propose(nodes, _pins(("1", "IN"), ("2", "OUT"), ("3", "SET")))
    assert m["ok"] and m["method"] == "names"
    assert m["nodes"][0] == {"index": 1, "node": unused_node(1), "pins": [], "how": "unused"}
    # pin-number fallback skips the unused port too
    n = pin_map.propose([unused_node(1), "a", "b"], _pins(("1", "X"), ("2", "Y")))
    assert n["method"] == "pin_number_order"
    assert [x["pins"] for x in n["nodes"]] == [[], ["1"], ["2"]]


def test_override_can_leave_a_port_unconnected():
    m = pin_map.propose(["a", "b", "c"], LDO, override=["-", "3", "2"])
    assert m["ok"] and m["nodes"][0]["how"] == "unused" and m["nodes"][0]["pins"] == []
