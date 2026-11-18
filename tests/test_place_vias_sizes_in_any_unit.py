# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A via's size in a batch, in whatever unit the entry names.

MEASURED 2026-11-18 on AD 26.11.1: pcb_place_vias given ``size_mm`` and
``hole_size_mm`` placed the 50/28 mil default. The session boundary
(lengths.py) renames a ``size_mm`` below the top level of a call to
``size_mils``, in mils, and the tool read only ``size_mm`` and ``size``.
In a mm session a plain ``size`` was not converted at all, so a 0.6 mm
via went out as 0.6 mil.

The calls are converted the way the server converts them (args_in with
the tool's own parameter names), then run against a recording bridge.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from eda_agent import lengths
from eda_agent.tools import library, pcb


class _Bridge:
    def __init__(self):
        self.sent = []

    async def send_command_async(self, command, params, **kwargs):
        self.sent.append((command, params))
        return {"success": True}


def _tool(module, register, name, monkeypatch):
    bridge = _Bridge()
    monkeypatch.setattr(module, "get_bridge", lambda: bridge)
    captured = {}

    class _Mcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

        def __getattr__(self, attr):
            return lambda *a, **k: (lambda fn: fn)

    getattr(module, register)(_Mcp())
    return captured[name], bridge


def _through_boundary(fn, name, args, unit):
    params = set(inspect.signature(fn).parameters)
    assert not lengths.unknown_unit_names(args, params)
    return asyncio.run(fn(**lengths.args_in(name, args, unit, params)))


def _vias(bridge):
    (command, params), = bridge.sent
    assert command == "pcb.place_vias"
    return [entry.split(",") for entry in params["vias"].split("|")]


@pytest.mark.parametrize("unit", ["mil", "mm"])
def test_size_mm_on_an_entry_is_the_size_placed(monkeypatch, unit):
    fn, bridge = _tool(pcb, "register_pcb_tools", "pcb_place_vias", monkeypatch)
    x = 100 if unit == "mil" else 2.54
    _through_boundary(fn, "pcb_place_vias", {"vias": [
        {"x": x, "y": x, "size_mm": 0.6, "hole_size_mm": 0.3}]}, unit)
    (via,) = _vias(bridge)
    assert float(via[0]) == pytest.approx(100)
    assert float(via[2]) == pytest.approx(0.6 / 0.0254, abs=1e-4)
    assert float(via[3]) == pytest.approx(0.3 / 0.0254, abs=1e-4)


def test_a_plain_size_is_in_the_sessions_unit(monkeypatch):
    fn, bridge = _tool(pcb, "register_pcb_tools", "pcb_place_vias", monkeypatch)
    _through_boundary(fn, "pcb_place_vias", {"vias": [
        {"x": 2.54, "y": 0, "size": 0.6, "hole_size": 0.3}]}, "mm")
    (via,) = _vias(bridge)
    assert float(via[2]) == pytest.approx(0.6 / 0.0254, abs=1e-4)
    assert float(via[3]) == pytest.approx(0.3 / 0.0254, abs=1e-4)


def test_size_in_mils_and_the_defaults_are_unchanged(monkeypatch):
    fn, bridge = _tool(pcb, "register_pcb_tools", "pcb_place_vias", monkeypatch)
    _through_boundary(fn, "pcb_place_vias", {"vias": [
        {"x": 0, "y": 0, "size": 24, "hole_size": 12}, {"x": 0, "y": 0}]}, "mil")
    first, second = _vias(bridge)
    assert first[2:4] == ["24", "12"]
    assert second[2:4] == ["50", "28"]


def test_a_called_directly_size_mm_still_works(monkeypatch):
    fn, bridge = _tool(pcb, "register_pcb_tools", "pcb_place_vias", monkeypatch)
    asyncio.run(fn(vias=[{"x": 0, "y": 0, "size_mm": 0.508, "hole_size_mm": 0.254}]))
    (via,) = _vias(bridge)
    assert via[2:4] == ["20", "10"]


def test_a_footprint_via_takes_its_size_in_the_unit_it_names():
    payload, problems = library._vias_payload(
        [{"x": 0, "y": 0, "size_mm": 1.0, "hole_size_mm": 0.5},
         {"x": 0, "y": 0, "size_mils": 1000 / 25.4 * 0.8, "hole_size": 0.4}],
        size=40, hole_size=20, units="mm")
    assert problems == []
    assert payload.split("~~") == ["x=0;y=0;size=1;hole_size=0.5",
                                   "x=0;y=0;size=0.8;hole_size=0.4"]
