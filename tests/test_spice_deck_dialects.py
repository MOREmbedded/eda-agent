# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A plan's circuit can be written for LTspice as well as ngspice.

The circuit lines are the deck_from_plan netlist the loop simulator runs;
only the file around them differs. A deck missing a part is not written
unless asked, and then says what it lacks. The divider below is written
out by hand as the known answer, so the writer is checked against the
format and not against itself.
"""
from __future__ import annotations

import asyncio

import pytest

from eda_agent.design.plan import DesignPlan
from eda_agent.sim.deck import deck_from_plan
from eda_agent.sim.dialects import analysis_lines, deck_gaps, write_deck
from eda_agent.tools import design as design_tools

DIVIDER = {
    "spec": "t", "summary": "divider", "sheets": [{"name": "main"}],
    "parts": [{"refdes": "R1", "lib_ref": "RES", "value": "10k"},
              {"refdes": "R2", "lib_ref": "RES", "value": "4.7k"},
              {"refdes": "R3", "lib_ref": "RES", "value": "100k"}],
    "nets": [{"name": "+12V", "pins": [{"refdes": "R1", "pin": "1"},
                                       {"refdes": "R3", "pin": "1"}]},
             {"name": "MID", "pins": [{"refdes": "R1", "pin": "2"},
                                      {"refdes": "R2", "pin": "1"}]},
             {"name": "GND", "pins": [{"refdes": "R2", "pin": "2"},
                                      {"refdes": "R3", "pin": "2"}]}]}

LTSPICE_DIVIDER = "\n".join([
    "* divider",
    "RR1 _12V MID 10000",
    "RR2 MID 0 4700",
    "RR3 _12V 0 100000",
    "Vsup__12V _12V 0 DC 12",
    ".op",
    ".backanno",
    ".end", ""])


def test_the_divider_matches_the_hand_written_ltspice_deck():
    deck = deck_from_plan(DesignPlan.model_validate(DIVIDER), title="divider")
    assert deck["ok"], deck
    assert write_deck(deck["netlist"], "ltspice") == LTSPICE_DIVIDER


def test_ngspice_gets_the_same_circuit_without_backanno():
    deck = deck_from_plan(DesignPlan.model_validate(DIVIDER), title="divider")
    text = write_deck(deck["netlist"], "ngspice",
                      {"tran": {"step": 1e-6, "stop": 1e-3}})
    assert ".backanno" not in text
    assert ".tran 1e-06 0.001" in text and text.count(".end") == 1


@pytest.mark.parametrize("analyses,why", [
    ({"tran": {"step": 1e-3, "stop": 1e-6}}, "smaller than stop"),
    ({"ac": {"points": 10, "start": 1e6, "stop": 1}}, "below stop"),
    ({"dc": {"source": "V1; rm", "start": 0, "stop": 1, "step": 0.1}},
     "source"),
    ({"noise": {}}, "unknown analysis"),
])
def test_a_bad_analysis_is_refused_before_any_simulator(analyses, why):
    with pytest.raises(ValueError, match=why):
        analysis_lines(analyses)


def test_every_gap_is_named():
    plan = dict(DIVIDER)
    plan["parts"] = DIVIDER["parts"] + [
        {"refdes": "U1", "lib_ref": "REG", "mpn": "LM1117"}]
    plan["nets"] = [dict(n) for n in DIVIDER["nets"]]
    plan["nets"][1] = {"name": "MID", "pins": [
        {"refdes": "R1", "pin": "2"}, {"refdes": "R2", "pin": "1"},
        {"refdes": "U1", "pin": "1"}]}
    deck = deck_from_plan(DesignPlan.model_validate(plan))
    assert deck_gaps(deck) == ["U1: no manufacturer model (LM1117)"]


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


def _tool():
    mcp = _Mcp()
    design_tools.register_design_tools(mcp)
    return mcp.tools["design_export_spice_deck"]


def test_an_incomplete_deck_is_not_written_unless_asked(tmp_path):
    plan = dict(DIVIDER)
    plan["parts"] = DIVIDER["parts"] + [
        {"refdes": "U1", "lib_ref": "REG", "mpn": "LM1117"}]
    plan["nets"] = [dict(n) for n in DIVIDER["nets"]]
    plan["nets"][1] = {"name": "MID", "pins": [
        {"refdes": "R1", "pin": "2"}, {"refdes": "R2", "pin": "1"},
        {"refdes": "U1", "pin": "1"}]}
    target = tmp_path / "d.net"
    out = asyncio.run(_tool()(plan_json=plan, dialect="ltspice",
                              path=str(target)))
    assert not out["ok"] and not target.exists()
    out = asyncio.run(_tool()(plan_json=plan, dialect="ltspice",
                              path=str(target), allow_partial=True))
    assert out["ok"] and not out["complete"]
    assert "* NOT IN THIS DECK: U1: no manufacturer model (LM1117)" in \
        target.read_text()


def test_the_tool_writes_the_ltspice_file(tmp_path):
    target = tmp_path / "divider.net"
    out = asyncio.run(_tool()(plan_json=DIVIDER, dialect="ltspice",
                              path=str(target)))
    assert out["ok"] and out["complete"]
    assert target.read_text() == LTSPICE_DIVIDER
