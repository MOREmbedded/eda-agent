# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Loop detection finds the loops whose physical size matters.

One plan holds one of each: a synchronous buck (hot loop, output loop,
bootstrap, FB divider), an MCU with a decoupling cap and a crystal, an
inverting op-amp, a current-sense shunt and a gate-driven MOSFET.
"""

from __future__ import annotations

import pytest

from eda_agent.design.loops import find_loops, graph_from_plan
from eda_agent.design.plan import DesignPlan


def _p(refdes, lib_ref, value=None, role=None):
    d = {"refdes": refdes, "lib_ref": lib_ref}
    if value:
        d["value"] = value
    if role:
        d["role"] = role
    return d


def _n(name, *pins, **kw):
    return {"name": name, "pins": [{"refdes": r, "pin": p} for r, p in pins],
            **kw}


PLAN = DesignPlan.model_validate({
    "spec": "test", "summary": "test", "sheets": [{"name": "main"}],
    "parts": [
        _p("U1", "BUCK"), _p("L1", "L", "4.7uH"), _p("C1", "C", "10uF"),
        _p("C2", "C", "22uF"), _p("C3", "C", "100nF"), _p("R1", "R", "100k"),
        _p("R2", "R", "22k"),
        _p("U2", "MCU"), _p("C4", "C", "100nF"), _p("Y1", "XTAL", "8MHz"),
        _p("C5", "C", "18pF"), _p("C6", "C", "18pF"),
        _p("U3", "OPAMP"), _p("R3", "R", "10k"), _p("R4", "R", "1k"),
        _p("U4", "CSA"), _p("R5", "R", "0.01"),
        _p("U5", "DRV"), _p("Q1", "NMOS"), _p("R6", "R", "10"),
    ],
    "nets": [
        _n("VIN", ("U1", "VIN"), ("C1", "1"), ("R5", "1"), ("U4", "IN+"),
           is_power=True),
        _n("GND", ("U1", "GND"), ("C1", "2"), ("C2", "2"), ("R2", "2"),
           ("U2", "VSS"), ("C4", "2"), ("C5", "2"), ("C6", "2"),
           ("U3", "V-"), ("Q1", "S"), ("U5", "GND"), is_ground=True),
        _n("SW", ("U1", "SW"), ("L1", "1"), ("C3", "2")),
        _n("BST", ("U1", "BOOT"), ("C3", "1")),
        _n("VOUT", ("L1", "2"), ("C2", "1"), ("R1", "1"), is_power=True),
        _n("FBN", ("R1", "2"), ("R2", "1"), ("U1", "FB")),
        _n("P3V3", ("U2", "VDD"), ("C4", "1"), ("U3", "V+"), is_power=True),
        _n("XIN", ("U2", "XIN"), ("Y1", "1"), ("C5", "1")),
        _n("XOUT", ("U2", "XOUT"), ("Y1", "2"), ("C6", "1")),
        _n("AOUT", ("U3", "OUT"), ("R3", "1")),
        _n("INN", ("U3", "IN-"), ("R3", "2"), ("R4", "1")),
        _n("AIN", ("R4", "2"), ("U3", "IN+")),
        _n("VLOAD", ("R5", "2"), ("U4", "IN-")),
        _n("GDRV", ("U5", "GATE"), ("R6", "1")),
        _n("G1", ("R6", "2"), ("Q1", "G")),
        _n("D1N", ("Q1", "D"), ("U4", "OUT")),
    ],
})


@pytest.fixture(scope="module")
def loops():
    return find_loops(graph_from_plan(PLAN))


def _kinds(loops, kind):
    return [l for l in loops if l.kind == kind]


def test_buck_hot_loop_runs_through_the_input_cap_and_switch(loops):
    (hot,) = _kinds(loops, "hot_loop")
    assert {"C1", "U1"} <= set(hot.parts)
    assert ("U1", "VIN") in hot.members and ("U1", "SW") in hot.members
    assert hot.source == "pin_name"
    assert loops[0].kind == "hot_loop"          # most important first


def test_buck_output_and_bootstrap_loops(loops):
    (out,) = _kinds(loops, "output_loop")
    assert set(out.parts) == {"L1", "C2"}
    (boot,) = _kinds(loops, "bootstrap")
    assert "C3" in boot.parts


def test_regulator_feedback_divider(loops):
    fb = [l for l in _kinds(loops, "feedback") if "U1" in l.parts]
    assert fb and {"R1", "R2"} <= set(fb[0].parts)


def test_opamp_feedback_network(loops):
    fb = [l for l in _kinds(loops, "feedback") if "U3" in l.parts]
    assert fb and "R3" in fb[0].parts and "R4" not in fb[0].parts


def test_decoupling_and_crystal(loops):
    dec = [l for l in _kinds(loops, "decoupling") if "C4" in l.parts or
           ("C4", "1") in l.members]
    assert dec
    (xtal,) = _kinds(loops, "crystal")
    assert {"Y1", "U2", "C5", "C6"} <= set(xtal.parts)


def test_kelvin_shunt(loops):
    (k,) = _kinds(loops, "kelvin")
    assert set(k.parts) == {"R5", "U4"}


def test_gate_drive_through_resistor(loops):
    (g,) = _kinds(loops, "gate_drive")
    assert {"Q1", "U5", "R6"} == set(g.parts)


def test_structure_alone_finds_a_buck_without_pin_names():
    """Pins given by NUMBER only: the inductor node shared with an IC and a
    diode still identifies the switching converter."""
    plan = DesignPlan.model_validate({
        "spec": "t", "summary": "t", "sheets": [{"name": "main"}],
        "parts": [_p("U1", "BUCK"), _p("L1", "L"), _p("D1", "D"),
                  _p("C1", "C"), _p("C2", "C")],
        "nets": [
            _n("VIN", ("U1", "1"), ("C1", "1"), is_power=True),
            _n("GND", ("U1", "2"), ("D1", "2"), ("C1", "2"), ("C2", "2"),
               is_ground=True),
            _n("SWN", ("U1", "3"), ("L1", "1"), ("D1", "1")),
            _n("VOUT", ("L1", "2"), ("C2", "1"), ("U1", "4"),
               is_power=True),
        ],
    })
    hot = [l for l in find_loops(graph_from_plan(plan)) if l.kind == "hot_loop"]
    assert hot and hot[0].source == "structure" and "D1" in hot[0].parts


def test_every_loop_says_what_found_it(loops):
    assert all(l.source in ("role", "pin_name", "structure") for l in loops)
    assert all(l.as_dict()["members"] for l in loops)


def test_members_follow_the_path_so_geometry_does_not_fold(loops):
    """The analyzer draws the loop through its members in order; a part
    listed backwards folds the polygon over itself."""
    (hot,) = _kinds(loops, "hot_loop")
    assert [f"{r}.{p}" for r, p in hot.members] == [
        "C1.1", "U1.VIN", "U1.SW", "U1.GND", "C1.2"]
    (boot,) = _kinds(loops, "bootstrap")
    assert [f"{r}.{p}" for r, p in boot.members] == [
        "U1.BOOT", "C3.1", "C3.2", "U1.SW"]
    assert "L1" not in hot.parts            # the inductor is outside the hot loop


def test_no_switching_loop_without_a_switching_converter():
    plan = DesignPlan.model_validate({
        "spec": "t", "summary": "t", "sheets": [{"name": "main"}],
        "parts": [_p("U1", "LDO"), _p("C1", "C", "1uF"), _p("C2", "C", "1uF"),
                  _p("R1", "R", "10k"), _p("C3", "C", "10nF")],
        "nets": [
            _n("VIN", ("U1", "IN"), ("C1", "1"), is_power=True),
            _n("GND", ("U1", "GND"), ("C1", "2"), ("C2", "2"), ("C3", "2"),
               is_ground=True),
            _n("VOUT", ("U1", "OUT"), ("C2", "1"), ("R1", "1"),
               is_power=True),
            _n("FILT", ("R1", "2"), ("C3", "1")),
        ],
    })
    kinds = {l.kind for l in find_loops(graph_from_plan(plan))}
    assert not kinds & {"hot_loop", "output_loop", "bootstrap", "feedback",
                        "gate_drive", "kelvin"}


def test_find_loops_tool_on_a_plan():
    import asyncio

    from eda_agent.tools import design as design_tools

    tools = {}

    class _Mcp:
        def tool(self, *a, **k):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

        def __getattr__(self, name):
            return lambda *a, **k: (lambda fn: fn)

    design_tools.register_design_tools(_Mcp())
    out = asyncio.run(tools["design_find_loops"](
        plan_json=PLAN.model_dump(mode="json")))
    assert out["ok"] and out["source"] == "plan"
    assert out["by_kind"]["hot_loop"] == 1
    bad = asyncio.run(tools["design_find_loops"]())
    assert bad["ok"] is False
