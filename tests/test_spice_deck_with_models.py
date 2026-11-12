# SPDX-License-Identifier: Apache-2.0
"""build_deck with vendor models: the model's name, node order and file.

The bug this guards: an IC with a vendor model used to be emitted with
its Comment as the subcircuit name, no .include, and its pins in pin-
number order. That deck runs and answers wrongly.
"""
from __future__ import annotations

from eda_agent.engineering.spice_netlist import build_deck


def _comp(des, prefix, value=""):
    return {"designator": des, "spice_prefix": prefix, "value": value}


LDO_PINS = [
    {"pin_number": "1", "pin_name": "GND", "net": "GND"},
    {"pin_number": "2", "pin_name": "VOUT", "net": "VOUT"},
    {"pin_number": "3", "pin_name": "VIN", "net": "VIN"},
    {"pin_number": "4", "pin_name": "SET", "net": "NSET"},
    {"pin_number": "5", "pin_name": "GND", "net": "GND"},
]
BASE = [_comp("U1", "X", "LT3080EDD"), _comp("V1", "V", "5"),
        _comp("R1", "R", "100k")]
CONN = {"U1": LDO_PINS,
        "V1": [{"pin_number": "1", "net": "VIN"}, {"pin_number": "2", "net": "GND"}],
        "R1": [{"pin_number": "1", "net": "NSET"}, {"pin_number": "2", "net": "GND"}]}
MODEL = {"name": "LT3080", "file": r"C:\models\LT3080.sub",
         "nodes": ["IN", "OUT", "SET", "GND"]}


def test_subckt_is_emitted_in_model_node_order_with_its_name_and_include():
    r = build_deck(BASE, CONN, analysis="op", models={"U1": MODEL})
    assert r.ok, r.reason
    assert "XU1 VIN VOUT NSET 0 LT3080" in r.deck
    assert '.include "LT3080.sub"' in r.deck
    assert r.deck.index(".include") < r.deck.index(".op")
    assert r.model_files == [{"path": r"C:\models\LT3080.sub", "include_as": "LT3080.sub"}]
    assert r.pin_maps["U1"]["method"] == "names"


def test_alias_matches_are_reported_for_review():
    r = build_deck(BASE, CONN, analysis="op", models={"U1": MODEL})
    flagged = {a["designator"]: a for a in r.pin_order_assumptions}
    assert flagged["U1"]["method"] == "names"       # VIN=IN, VOUT=OUT via aliases


def test_exact_names_are_not_flagged():
    model = {**MODEL, "nodes": ["VIN", "VOUT", "SET", "GND"]}
    r = build_deck(BASE, CONN, analysis="op", models={"U1": model})
    assert r.ok and r.pin_order_assumptions == []


def test_unmatched_pins_refuse_the_deck_by_default():
    """Leaving the modelled part out simulates a different circuit: the
    live check read 0 V at the output of such a deck. So it is refused,
    like a missing model, unless a placeholder deck is asked for."""
    model = {**MODEL, "nodes": ["IN", "OUT", "ADJ2", "GND"]}
    r = build_deck(BASE, CONN, analysis="op", models={"U1": model})
    assert r.ok is False and "U1" in r.reason and r.deck == ""
    assert r.pin_map_problems and r.pin_map_problems[0]["designator"] == "U1"
    assert "ADJ2" in r.pin_map_problems[0]["reason"]
    p = build_deck(BASE, CONN, analysis="op", models={"U1": model},
                   on_missing_model="placeholder")
    assert p.ok and "XU1" not in p.deck and "* U1 (LT3080) left out" in p.deck


def test_adi_vcntrl_matches_an_altium_vcontrol_pin():
    pins = LDO_PINS[:4] + [{"pin_number": "5", "pin_name": "VCONTROL", "net": "VIN"}]
    model = {**MODEL, "nodes": ["SET", "OUT", "Vcntrl", "IN"]}
    r = build_deck(BASE, {**CONN, "U1": pins}, analysis="op", models={"U1": model})
    assert r.ok and "XU1 NSET VOUT VIN VIN LT3080" in r.deck


def test_saved_override_is_used():
    model = {**MODEL, "nodes": ["a", "b", "c", "d"], "override": ["3", "2", "4", "1+5"]}
    r = build_deck(BASE, CONN, analysis="op", models={"U1": model})
    assert "XU1 VIN VOUT NSET 0 LT3080" in r.deck
    assert r.pin_order_assumptions == []


def test_pins_of_one_node_on_different_nets_are_reported():
    pins = [dict(p) for p in LDO_PINS]
    pins[4]["net"] = "PGND"
    r = build_deck(BASE, {**CONN, "U1": pins}, analysis="op", models={"U1": MODEL})
    assert any("different nets" in p["reason"] for p in r.pin_map_problems)


def test_two_files_with_one_base_name_get_distinct_include_names():
    comps = BASE + [_comp("U2", "X", "LT3080")]
    conn = {**CONN, "U2": LDO_PINS}
    other = {**MODEL, "file": r"D:\other\LT3080.sub"}
    r = build_deck(comps, conn, analysis="op", models={"U1": MODEL, "U2": other})
    names = [m["include_as"] for m in r.model_files]
    assert names == ["LT3080.sub", "LT3080_2.sub"]


def test_discretes_take_terminal_order_from_pin_names():
    comps = [_comp("D1", "D", "1N4148"), _comp("V1", "V", "5")]
    conn = {"D1": [{"pin_number": "1", "pin_name": "K", "net": "GND"},
                   {"pin_number": "2", "pin_name": "A", "net": "VIN"}],
            "V1": [{"pin_number": "1", "net": "VIN"}, {"pin_number": "2", "net": "GND"}]}
    r = build_deck(comps, conn, analysis="op",
                   models={"D1": {"name": "1N4148", "file": r"C:\m\diodes.lib"}})
    assert "D1 VIN 0 1N4148" in r.deck
    assert r.pin_order_assumptions == []
    assert '.include "diodes.lib"' in r.deck


def test_power_fet_with_three_pins_runs_as_vdmos():
    comps = [_comp("Q1", "M", "BSC016"), _comp("V1", "V", "5")]
    conn = {"Q1": [{"pin_number": "1", "pin_name": "S", "net": "GND"},
                   {"pin_number": "4", "pin_name": "G", "net": "VIN"},
                   {"pin_number": "5", "pin_name": "D", "net": "VIN"}],
            "V1": [{"pin_number": "1", "net": "VIN"}, {"pin_number": "2", "net": "GND"}]}
    r = build_deck(comps, conn, analysis="op")
    assert "MQ1 VIN VIN 0 BSC016" in r.deck


def test_without_models_the_old_behaviour_is_kept():
    r = build_deck(BASE, CONN, analysis="op")
    assert "XU1 GND VOUT VIN NSET GND LT3080EDD".replace("GND", "0") in r.deck
    assert r.model_files == []
    assert [a["designator"] for a in r.pin_order_assumptions] == ["U1"]


def test_an_unused_model_port_gets_its_own_floating_node():
    from eda_agent.sim.model_library import unused_node
    model = {**MODEL, "nodes": [unused_node(1), "IN", "OUT", "SET", "GND"]}
    r = build_deck(BASE, CONN, analysis="op", models={"U1": model})
    assert "XU1 NC_U1_1 VIN VOUT NSET 0 LT3080" in r.deck
