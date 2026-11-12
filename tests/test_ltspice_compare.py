# SPDX-License-Identifier: Apache-2.0
"""Comparing an LTspice circuit with the Altium schematic."""
from __future__ import annotations

import pytest

from eda_agent.sim.ltspice_compare import compare, parse_netlist, spice_value

# What LTspice writes for a schematic: auto-named nets, X-prefixed ICs,
# 'QQ1' for a transistor named Q1.
LT_NET = """\
* C:\\designs\\ldo.asc
V1 N001 0 5
XU1 N001 OUT N002 0 LT3080
R1 N002 0 100k
C1 OUT 0 4.7u Rser=5m
QQ1 OUT N003 0 0 BC847B
R2 N003 N001 10K
.model NPN NPN
.lib LT3080.sub
.tran 1m
.backanno
.end
"""


def _p(num, name, net):
    return {"pin_number": num, "pin_name": name, "net": net}


ALTIUM = [
    {"designator": "V1", "value": "5", "pins": [_p("1", "+", "VIN"), _p("2", "-", "GND")]},
    {"designator": "U1", "value": "LT3080",
     "pins": [_p("1", "GND", "GND"), _p("2", "OUT", "VOUT"), _p("3", "IN", "VIN"),
              _p("4", "SET", "SET")]},
    # R1 drawn the other way round: orientation must not matter
    {"designator": "R1", "value": "100k", "pins": [_p("1", "1", "GND"), _p("2", "2", "SET")]},
    {"designator": "C1", "value": "4u7 X7R", "pins": [_p("1", "1", "VOUT"), _p("2", "2", "GND")]},
    {"designator": "Q1", "value": "BC847B",
     "pins": [_p("1", "B", "BASE"), _p("2", "E", "GND"), _p("3", "C", "VOUT")]},
    {"designator": "R2", "value": "10k", "pins": [_p("1", "1", "BASE"), _p("2", "2", "VIN")]},
]
U1_MAP = {"U1": ["3", "2", "4", "1"]}


@pytest.mark.parametrize("text,value", [
    ("10k", 1e4), ("10K", 1e4), ("4.7u", 4.7e-6), ("4u7", 4.7e-6), ("4K7", 4.7e3),
    ("1meg", 1e6), ("2R2", 2.2), ("100nF X7R", 1e-7), ("10k 1%", 1e4), ("1e3", 1e3),
    ("abc", None), ("", None),
])
def test_values_are_read_as_numbers(text, value):
    got = spice_value(text)
    assert (got is None and value is None) or got == pytest.approx(value)


def test_parse_strips_ltspice_prefixes_and_keeps_node_counts():
    parts = {p["designator"]: p for p in parse_netlist(LT_NET)["parts"]}
    assert set(parts) == {"V1", "U1", "R1", "C1", "Q1", "R2"}
    assert parts["U1"]["nodes"] == ["N001", "OUT", "N002", "0"]
    assert parts["U1"]["value"] == "LT3080"
    assert parts["Q1"]["nodes"] == ["OUT", "N003", "0", "0"]
    assert parts["C1"]["value"] == "4.7u Rser=5m"


def test_the_same_circuit_matches_despite_different_net_names():
    r = compare(parse_netlist(LT_NET), ALTIUM, pin_maps=U1_MAP)
    assert r["match"], r
    assert r["net_map"]["N001"] == "VIN" and r["net_map"]["N002"] == "SET"
    assert r["parts_compared"] == 6


def test_value_difference_is_reported():
    altium = [dict(c) for c in ALTIUM]
    altium[2] = {**altium[2], "value": "120k"}
    r = compare(parse_netlist(LT_NET), altium, pin_maps=U1_MAP)
    assert r["value_differences"] == [{"designator": "R1", "ltspice": "100k",
                                       "altium": "120k"}]
    assert not r["match"]


def test_a_miswired_pin_is_reported():
    altium = [dict(c) for c in ALTIUM]
    # Q1 emitter to VOUT instead of GND
    altium[4] = {**altium[4], "pins": [_p("1", "B", "BASE"), _p("2", "E", "VOUT"),
                                       _p("3", "C", "VOUT")]}
    r = compare(parse_netlist(LT_NET), altium, pin_maps=U1_MAP)
    wrong = [w for w in r["wiring_differences"] if w["designator"] == "Q1"]
    assert wrong and wrong[0]["terminal"] == 3 and wrong[0]["altium_net"] == "VOUT"
    assert not r["match"]


def test_parts_on_one_side_only_and_designator_map():
    altium = [c for c in ALTIUM if c["designator"] != "R2"]
    altium.append({**ALTIUM[5], "designator": "R20"})
    r = compare(parse_netlist(LT_NET), altium, pin_maps=U1_MAP)
    assert r["only_in_ltspice"] == ["R2"] and r["only_in_altium"] == ["R20"]
    r2 = compare(parse_netlist(LT_NET), altium, pin_maps=U1_MAP,
                 designator_map={"R2": "R20"})
    assert r2["match"], r2


def test_a_subckt_without_a_pin_map_is_unchecked_not_guessed():
    r = compare(parse_netlist(LT_NET), ALTIUM)
    assert [u["designator"] for u in r["unchecked"]] == ["U1"]
    assert not r["match"]


def test_two_ltspice_nets_shorted_in_altium_are_reported():
    altium = [dict(c) for c in ALTIUM]
    altium[5] = {**altium[5], "pins": [_p("1", "1", "VIN"), _p("2", "2", "VIN")]}
    altium[4] = {**altium[4], "pins": [_p("1", "B", "VIN"), _p("2", "E", "GND"),
                                       _p("3", "C", "VOUT")]}
    r = compare(parse_netlist(LT_NET), altium, pin_maps=U1_MAP)
    assert r["nets_merged_in_altium"] == {"VIN": ["N001", "N003"]}


@pytest.mark.parametrize("line,nodes,model", [
    ("Q1 c b e 2N3904", ["c", "b", "e"], "2N3904"),
    ("Q1 c b e 0 2N3904", ["c", "b", "e", "0"], "2N3904"),
    ("Q1 c b e BC847 2", ["c", "b", "e"], "BC847 2"),
    ("M1 d g s IRF540", ["d", "g", "s"], "IRF540"),
    ("M1 d g s s NMOS1 L=1u W=10u", ["d", "g", "s", "s"], "NMOS1 L=1u W=10u"),
])
def test_three_or_four_terminal_devices(line, nodes, model):
    p = parse_netlist(line)["parts"][0]
    assert p["nodes"] == nodes and p["value"] == model
