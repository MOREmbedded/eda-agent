# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Simulator discovery, the plan-to-deck builder, and loop gain in ngspice.

The circuits here are test harnesses built from ideal elements, with an
analytic answer to check against. They are never design models."""

from __future__ import annotations

import cmath
import math

import pytest

from eda_agent.design.plan import DesignPlan
from eda_agent.sim.deck import deck_from_plan, rail_voltage, subckt_nodes
from eda_agent.sim.ngspice import (find_ngspice, loop_gain,
                                   simulator_backends, stability_margins)

needs_ngspice = pytest.mark.skipif(find_ngspice() is None,
                                   reason="ngspice not installed")


def test_backends_offer_an_install_and_never_run_one(monkeypatch):
    import eda_agent.sim.ngspice as ng

    monkeypatch.setattr(ng, "find_ngspice", lambda: None)
    monkeypatch.setattr(ng.shutil, "which", lambda name: None)
    rep = simulator_backends()
    assert not rep["ngspice"]["available"]
    assert "ngspice" in rep["install_offers"]
    assert "scoop" not in rep["install_offers"]["ngspice"]   # no scoop here
    assert "approval" in rep["install_policy"]


def test_rail_voltage_reads_the_name():
    assert rail_voltage("+12V") == 12 and rail_voltage("3V3") == 3.3
    assert rail_voltage("VCC_1V8") == 1.8 and rail_voltage("V3P3") == 3.3
    assert rail_voltage("-15V") == -15
    assert rail_voltage("VCC") is None and rail_voltage("VBAT") is None


def test_margins_of_a_known_two_pole_loop():
    # T = A0 / ((1 + jf/p1)(1 + jf/p2)): unity crossover and phase by hand.
    a0, p1, p2 = 1e5, 10.0, 1e6
    freqs = [10 ** (k / 100) for k in range(0, 801)]
    gain = [a0 / ((1 + 1j * f / p1) * (1 + 1j * f / p2)) for f in freqs]
    m = stability_margins(freqs, gain)
    # |T| = 1: f^2 (1 + f^2/p2^2) = (a0 p1)^2 (p1 negligible)
    x = (-1 + math.sqrt(1 + 4 * (a0 * p1 / p2) ** 2)) / 2 * p2 ** 2
    fc = math.sqrt(x)
    pm = 180 + math.degrees(cmath.phase(
        a0 / ((1 + 1j * fc / p1) * (1 + 1j * fc / p2))))
    assert m["crossover_hz"] == pytest.approx(fc, rel=0.01)
    assert m["phase_margin_deg"] == pytest.approx(pm, abs=0.3)
    assert "gain_margin_db" not in m          # two poles never reach -180


def _plan(parts, nets):
    return DesignPlan.model_validate({
        "spec": "t", "summary": "t", "sheets": [{"name": "main"}],
        "parts": parts, "nets": nets})


def test_deck_reports_a_part_without_a_vendor_model():
    plan = _plan(
        [{"refdes": "U1", "lib_ref": "OPAMP", "mpn": "XYZ123"},
         {"refdes": "R1", "lib_ref": "RES", "value": "10k"}],
        [{"name": "OUT", "pins": [{"refdes": "U1", "pin": "1"},
                                  {"refdes": "R1", "pin": "1"}]},
         {"name": "GND", "pins": [{"refdes": "R1", "pin": "2"},
                                  {"refdes": "U1", "pin": "2"}]}])
    deck = deck_from_plan(plan)
    assert not deck["ok"]
    assert deck["missing_models"][0]["mpn"] == "XYZ123"
    assert "never" in deck["model_policy"]
    assert "R_R1" not in deck["netlist"] and "RR1 OUT 0 10000" in deck["netlist"]


def test_deck_maps_subckt_nodes_by_pin_name(tmp_path):
    lib = tmp_path / "amp.lib"
    lib.write_text(".SUBCKT AMP1 INP INN VCC VEE\n+ OUT\nE1 OUT 0 INP INN 1e5\n"
                   ".ENDS AMP1\n")
    assert subckt_nodes(str(lib), "amp1") == ["INP", "INN", "VCC", "VEE", "OUT"]
    plan = _plan(
        [{"refdes": "U1", "lib_ref": "AMP"},
         {"refdes": "R1", "lib_ref": "RES", "value": "1k"}],
        [{"name": "OUT", "pins": [{"refdes": "U1", "pin": "OUT"},
                                  {"refdes": "U1", "pin": "INN"},
                                  {"refdes": "R1", "pin": "1"}]},
         {"name": "+5V", "pins": [{"refdes": "U1", "pin": "VCC"},
                                  {"refdes": "U1", "pin": "INP"}]},
         {"name": "GND", "pins": [{"refdes": "U1", "pin": "VEE"},
                                  {"refdes": "R1", "pin": "2"}]}])
    deck = deck_from_plan(plan, models={"U1": {"file": str(lib),
                                               "subckt": "AMP1"}},
                          break_at=("U1", "OUT"))
    assert deck["ok"], deck
    assert "XU1 _5V OUT _5V 0 lg_drv AMP1" in deck["netlist"]
    assert "Vsup__5V _5V 0 DC 5" in deck["netlist"]
    assert (deck["driver_node"], deck["load_node"]) == ("lg_drv", "OUT")


def test_a_rail_with_no_stated_voltage_is_reported():
    plan = _plan([{"refdes": "R1", "lib_ref": "RES", "value": "1k"}],
                 [{"name": "VIN", "is_power": True,
                   "pins": [{"refdes": "R1", "pin": "1"},
                            {"refdes": "R1", "pin": "2"}]}])
    assert deck_from_plan(plan)["unknown_supplies"] == ["VIN"]
    assert deck_from_plan(plan, supplies={"VIN": 12})["ok"]


@needs_ngspice
def test_double_injection_counts_the_load_on_the_output():
    # Two-pole follower; the 10k load on the 1k output resistance cuts the
    # gain by 10/11 and moves the second pole to 1.1 MHz. By hand: crossover
    # 751 kHz, phase margin 55.7 degrees.
    net = """* two-pole follower
Vp inp 0 DC 0
E1 n1 0 inp out_fb 1e5
R1 n1 n2 1k
C1 n2 0 15.915u
E2 n3 0 n2 0 1
R2 n3 out 1k
C2 out 0 159.15p
Rl out_load 0 10k
Rfb out_load out_fb 1
.end"""
    r = loop_gain(net, "out", "out_load")
    assert r["ok"], r
    assert r["crossover_hz"] == pytest.approx(751e3, rel=0.01)
    assert r["phase_margin_deg"] == pytest.approx(55.7, abs=0.5)


@needs_ngspice
def test_simulate_loops_tool_runs_an_opamp_feedback_loop(tmp_path):
    """Plan -> loops -> deck -> ngspice -> margins, through the tool."""
    import asyncio

    import eda_agent.tools.design as d

    lib = tmp_path / "amp.lib"
    # Harness stand-in for a vendor file: 1e5 gain, poles at 10 Hz and 1 MHz.
    lib.write_text(""".SUBCKT AMP2 INP INN VCC VEE OUT
E1 n1 0 INP INN 1e5
R1 n1 n2 1k
C1 n2 0 15.915u
E2 n3 0 n2 0 1
R2 n3 OUT 1k
C2 OUT 0 159.15p
.ENDS AMP2
""")
    plan = {"spec": "t", "summary": "non-inverting x2", "sheets": [{"name": "main"}],
            "parts": [{"refdes": "U1", "lib_ref": "AMP"},
                      {"refdes": "R1", "lib_ref": "RES", "value": "10k"},
                      {"refdes": "R2", "lib_ref": "RES", "value": "10k"},
                      {"refdes": "C1", "lib_ref": "CAP", "value": "100n"}],
            "nets": [
                {"name": "OUT", "pins": [{"refdes": "U1", "pin": "OUT"},
                                         {"refdes": "R1", "pin": "1"}]},
                {"name": "FBN", "pins": [{"refdes": "U1", "pin": "IN-"},
                                         {"refdes": "R1", "pin": "2"},
                                         {"refdes": "R2", "pin": "1"}]},
                {"name": "GND", "pins": [{"refdes": "R2", "pin": "2"},
                                         {"refdes": "U1", "pin": "V-"},
                                         {"refdes": "U1", "pin": "IN+"},
                                         {"refdes": "C1", "pin": "2"}]},
                {"name": "+5V", "pins": [{"refdes": "U1", "pin": "V+"},
                                         {"refdes": "C1", "pin": "1"}]}]}
    tools = {}

    class _M:
        def tool(self, *a, **k):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

        def __getattr__(self, name):
            return lambda *a, **k: (lambda fn: fn)

    d.register_design_tools(_M())
    out = asyncio.run(tools["design_simulate_loops"](
        plan, models={"U1": {"file": str(lib), "subckt": "AMP2",
                             "pins": ["IN+", "IN-", "V+", "V-", "OUT"]}}))
    assert out.get("ran"), out
    (loop,) = out["worst_first"]
    # Gain 2: half the loop gain of a follower, so a lower crossover and
    # more margin than the 55.7 degrees of the follower test.
    assert 60 < loop["phase_margin_deg"] < 80 and not loop["low"]
