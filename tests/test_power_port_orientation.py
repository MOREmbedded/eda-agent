# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""sch_place_power_port must be able to aim the glyph, and must refuse a
style it cannot draw.

The bridge accepts an ``orientation`` param (Generic.pas reads it and
falls back to a style-based default when it is negative). That default
used to group ``bar`` and ``wave`` with the ground styles and send them
DOWN, so a VCC rail drawn as a bar came out looking like a ground; now
only the three ground styles point down.

An unknown style (``"power_ground"``) used to fall through to a circle
and report success, so a ground came out as a supply symbol.

The template drawer sends an explicit orientation for every port, so
this is about the raw tool being usable on its own.
"""

from __future__ import annotations

import asyncio

import pytest


class _RecordingBridge:
    """Captures commands instead of talking to Altium."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def send_command_async(self, command, params=None, **kwargs):
        self.calls.append((command, dict(params or {})))
        return {"success": True}


@pytest.fixture
def tools(monkeypatch):
    from eda_agent.tools import generic as generic_mod

    bridge = _RecordingBridge()
    monkeypatch.setattr(generic_mod, "get_bridge", lambda: bridge)

    captured: dict = {}

    class _Capture:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    generic_mod.register_generic_tools(_Capture())
    return captured, bridge


def _params(bridge, command="generic.place_power_port"):
    return next(p for c, p in bridge.calls if c == command)


def test_orientation_defaults_to_auto(tools):
    """Omitting it must keep the previous behaviour exactly.

    -1 tells the bridge to derive from style, which is what callers got
    before this parameter existed.
    """
    fns, bridge = tools
    asyncio.run(fns["sch_place_power_port"](text="GND", x=0, y=0,
                                            style="gnd_power"))
    assert _params(bridge)["orientation"] == "-1"


def test_explicit_orientation_reaches_the_bridge(tools):
    """A VCC bar must be aimable UP, which is the case the default gets
    wrong."""
    fns, bridge = tools
    asyncio.run(fns["sch_place_power_port"](text="VCC", x=100, y=200,
                                            style="bar", orientation=1))
    sent = _params(bridge)
    assert sent["orientation"] == "1"
    assert sent["style"] == "bar"
    assert sent["text"] == "VCC"


@pytest.mark.parametrize("orientation", [0, 1, 2, 3])
def test_all_four_orientations_pass_through(tools, orientation):
    fns, bridge = tools
    asyncio.run(fns["sch_place_power_port"](text="V", x=0, y=0,
                                            orientation=orientation))
    assert _params(bridge)["orientation"] == str(orientation)


@pytest.mark.parametrize("style,expected", [
    ("bar", 1), ("circle", 1), ("arrow", 1), ("wave", 1),
    ("gnd_power", 3), ("gnd_signal", 3), ("gnd_earth", 3),
])
def test_the_template_aims_rails_up_and_grounds_down(style, expected):
    """The template drawer sends an explicit orientation for every port,
    so a change to the bridge's style-based fallback cannot move its
    glyphs. Built by the REAL builder; restating its rule here would
    assert nothing about the code."""
    from eda_agent.design.sch_template import build, place_symbol_pins

    pins = {"R1": place_symbol_pins("R1", [
        {"designator": "1", "name": "1", "x": -100, "y": 0,
         "orientation": 2, "length": 100}], 0, 0)}
    drawing = build({"parts": [{"designator": "R1"}],
                     "ports": [{"net": "NET", "at": "R1.1", "style": style}]},
                    pins)
    (port,) = drawing.ports
    assert port["style"] == style
    assert port["orientation"] == expected


@pytest.mark.parametrize("style", ["power_ground", "gnd", "vcc", "bars"])
def test_unknown_style_is_refused_before_the_bridge(tools, style):
    """A misspelt ground used to be drawn as a circle and reported placed."""
    fns, bridge = tools
    result = asyncio.run(fns["sch_place_power_port"](text="GND", x=0, y=0,
                                                     style=style))
    assert result["success"] is False
    assert "gnd_power" in result["error"]
    assert bridge.calls == []


def test_style_is_case_normalised(tools):
    fns, bridge = tools
    asyncio.run(fns["sch_place_power_port"](text="GND", x=0, y=0,
                                            style=" GND_Power "))
    assert _params(bridge)["style"] == "gnd_power"


def _pascal(name):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "scripts" / "altium"
    return (root / name).read_text(encoding="utf-8")


def _pascal_function(src, name):
    start = src.index(f"Function {name}(")
    return src[start:src.index("End;", start)]


def test_bridge_accepts_exactly_the_python_styles():
    """The Python list and the bridge's must agree, or one side refuses
    what the other accepts."""
    import re

    from eda_agent.tools.generic import POWER_PORT_STYLES

    utils = _pascal("Utils.pas")
    accepted = set(re.findall(r"S = '(\w+)'",
                              _pascal_function(utils, "IsPowerPortStyle")
                              + _pascal_function(utils, "IsGroundPortStyle")))
    assert accepted == set(POWER_PORT_STYLES)
    grounds = set(re.findall(r"S = '(\w+)'",
                             _pascal_function(utils, "IsGroundPortStyle")))
    assert grounds == {"gnd_power", "gnd_signal", "gnd_earth"}


def test_both_bridge_handlers_check_style_and_aim_by_ground_only():
    """The single and the batch handler are twins; each must refuse an
    unknown style and derive the default orientation from the ground test
    alone, so bar and wave rails point up."""
    gen = _pascal("Generic.pas")
    for name in ("Gen_PlacePowerPort", "Gen_PlacePowerPorts"):
        start = gen.index(f"Function {name}(")
        body = gen[start:gen.index("\nFunction ", start + 1)]
        assert "IsPowerPortStyle(StyleStr)" in body, name
        assert "If IsGroundPortStyle(StyleStr) Then" in body, name
        assert "(StyleStr = 'bar')" not in body, name
