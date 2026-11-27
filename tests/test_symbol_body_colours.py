# SPDX-License-Identifier: Apache-2.0
"""A new symbol body takes the library's own colours, or the ones given.

Bodies were always block yellow with a black outline. SkyComm's library
draws its symbols #FFFFB0 with a dark-red outline, so every part built
here matched nothing else in it.
"""
from __future__ import annotations

import asyncio

import pytest

from eda_agent.design.symbol_colours import (
    DEFAULT_FILL, DEFAULT_OUTLINE, body_colours_in, choose, to_bgr, to_hex)

SKY_FILL = to_bgr("#FFFFB0")      # 11599871
DARK_RED = to_bgr("#800000")      # 128


def test_hex_is_read_as_altiums_bgr():
    assert SKY_FILL == 11599871 and DARK_RED == 128
    assert to_hex(8454143) == "#FFFF80"
    assert to_bgr("") is None and to_bgr(128) == 128
    with pytest.raises(ValueError):
        to_bgr("yellow")


def _geo(fill, outline):
    return {"primitives": [{"kind": "rect", "is_solid": True,
                            "area_color": fill, "color": outline},
                           {"kind": "line", "color": 0}]}


def test_the_librarys_most_used_body_colours_are_learned():
    geos = [_geo(SKY_FILL, DARK_RED), _geo(SKY_FILL, DARK_RED), _geo(DEFAULT_FILL, 0)]
    assert body_colours_in(geos) == (SKY_FILL, DARK_RED)
    assert body_colours_in([{"primitives": []}]) is None


def test_given_beats_learned_beats_default():
    learned = (SKY_FILL, DARK_RED)
    assert choose(learned=learned) == {"fill": SKY_FILL, "outline": DARK_RED,
                                       "source": "library"}
    got = choose(fill="#FFFFFF", learned=learned)
    assert got["fill"] == 0xFFFFFF and got["outline"] == DARK_RED
    assert choose() == {"fill": DEFAULT_FILL, "outline": DEFAULT_OUTLINE,
                        "source": "default"}


def _tool(monkeypatch, existing):
    from eda_agent.tools import library

    sent = []

    class Bridge:
        async def send_command_async(self, command, params=None, timeout=None):
            sent.append((command, dict(params or {})))
            if command == "application.get_active_document":
                return {"file_path": "C:/lib/Parts.SchLib"}
            if command == "library.get_components":
                return {"components": [{"name": n} for n in existing]}
            if command == "library.get_symbol_geometry":
                return _geo(SKY_FILL, DARK_RED)
            return {"success": True}

    monkeypatch.setattr(library, "get_bridge", lambda: Bridge())
    from eda_agent import preferences
    monkeypatch.setattr(preferences, "_read",
                        lambda workspace=None: {"checks_off": "symbol_pins"})

    async def no_schlib(bridge):
        return ""
    monkeypatch.setattr(library, "_unfocused_schlib", no_schlib)
    tools = {}

    class Mcp:
        def tool(self, *a, **k):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn
            return deco

        def __getattr__(self, name):
            return lambda *a, **k: (lambda fn: fn)

    library.register_library_tools(Mcp())
    return tools, sent


def _bodies(sent):
    return [p for c, p in sent if c == "library.add_symbol_rectangle"]


def _pins(*pairs):
    return [{"designator": d, "name": n} for d, n in pairs]


def test_parts_tool_draws_bodies_in_the_librarys_colours(monkeypatch):
    tools, sent = _tool(monkeypatch, ["QPA9903", "OTHER"])
    asyncio.run(tools["lib_create_symbol_in_parts"](
        name="NEW", parts=[{"left_pins": _pins(("1", "A"))},
                           {"right_pins": _pins(("2", "B"))}]))
    bodies = _bodies(sent)
    assert len(bodies) == 2
    assert all(b["fill_color"] == SKY_FILL and b["border_color"] == DARK_RED
               for b in bodies)


def test_colours_given_to_the_ic_tool_win(monkeypatch):
    tools, sent = _tool(monkeypatch, [])
    asyncio.run(tools["lib_create_ic_symbol"](
        name="NEW", left_pins=_pins(("1", "A")), right_pins=_pins(("2", "B")),
        body="rectangle", body_fill="#FFFFB0", body_outline="#800000"))
    (body,) = _bodies(sent)
    assert body["fill_color"] == SKY_FILL and body["border_color"] == DARK_RED


def test_an_empty_library_keeps_block_yellow(monkeypatch):
    tools, sent = _tool(monkeypatch, [])
    asyncio.run(tools["lib_create_ic_symbol"](
        name="NEW", left_pins=_pins(("1", "A")), right_pins=_pins(("2", "B")),
        body="rectangle"))
    (body,) = _bodies(sent)
    assert body["fill_color"] == DEFAULT_FILL and body["border_color"] == DEFAULT_OUTLINE


def test_a_bad_colour_is_refused_before_anything_is_drawn(monkeypatch):
    tools, sent = _tool(monkeypatch, [])
    out = asyncio.run(tools["lib_create_ic_symbol"](
        name="NEW", left_pins=_pins(("1", "A")), right_pins=_pins(("2", "B")),
        body="rectangle", body_fill="yellow"))
    assert out["ok"] is False and not _bodies(sent)
