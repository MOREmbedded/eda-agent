# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""EasyEDA draws a hand template in two passes: placement at the
template's positions, then the template's wiring checked against the
pins as placed."""

from __future__ import annotations

import asyncio
import json

import pytest

PLAN = {
    "spec": "divider", "summary": "two resistors",
    "sheets": [{"name": "main"}],
    "parts": [{"refdes": "R1", "lib_ref": "RES", "value": "10k"},
              {"refdes": "R2", "lib_ref": "RES", "value": "4k7"}],
    "nets": [{"name": "MID", "pins": [{"refdes": "R1", "pin": "1"},
                                      {"refdes": "R2", "pin": "2"}]}],
}
RESOLVED = {"R1": {"library_uuid": "lib", "uuid": "r"},
            "R2": {"library_uuid": "lib", "uuid": "r"}}
TEMPLATE = {
    "parts": [{"designator": "R1", "x": 1000, "y": 1000, "rotation": 90},
              {"designator": "R2", "x": 1000, "y": 600, "rotation": 90}],
    "wires": [{"net": "MID", "path": ["R1.1", "R2.2"]}],
    "ports": [{"net": "VCC", "at": "R1.2"}, {"net": "GND", "at": "R2.1"}],
}
# Read back after placing, in mils: R1 vertical at x=1000, R2 below it.
PINS = {"R1.1": [1000, 900], "R1.2": [1000, 1100],
        "R2.1": [1000, 500], "R2.2": [1000, 700]}


@pytest.fixture
def tools():
    from eda_agent.tools.easyeda import register_easyeda_tools
    captured = {}

    class _Capture:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    register_easyeda_tools(_Capture())
    return captured


def test_parts_are_placed_where_the_template_puts_them(tools):
    out = asyncio.run(tools["easyeda_emit_template"](
        TEMPLATE, json.dumps(PLAN), RESOLVED))
    assert out["ok"] and out["runnable"]
    places = [c["arguments"] for c in out["calls"]
              if c["tool"] == "easyeda_place_schematic_component"]
    assert [(a["x"], a["y"], a["rotation"]) for a in places] == [
        (1000.0, 1000.0, 90.0), (1000.0, 600.0, 90.0)]
    assert out["next_step"] == "easyeda_emit_template_connections"


def test_the_template_wiring_is_emitted_once_checked(tools):
    out = asyncio.run(tools["easyeda_emit_template_connections"](
        TEMPLATE, PINS, json.dumps(PLAN)))
    assert out["ok"], out
    tools_used = [c["tool"] for c in out["calls"]]
    assert tools_used.count("easyeda_add_wire") == 1
    flags = {c["arguments"]["name"]: c["arguments"]["kind"]
             for c in out["calls"] if c["tool"] == "easyeda_create_net_flag"}
    assert flags == {"VCC": "Power", "GND": "Ground"}


def test_a_plan_mismatch_emits_nothing(tools):
    wrong = dict(TEMPLATE, wires=[{"net": "MID", "path": ["R1.2", [900, 1100]]}])
    out = asyncio.run(tools["easyeda_emit_template_connections"](
        wrong, PINS, json.dumps(PLAN)))
    assert not out["ok"] and out["calls"] == [] and out["blockers"]
