# SPDX-License-Identifier: Apache-2.0
"""pcb_place_image: the tool layer over design/image_regions.py.

The geometry is tested in test_image_regions.py; what matters here is
the safety behaviour -- planning by default, the primitive cap, unit
conversion, and stopping at the first refusal instead of leaving a
thousand failures to undo.
"""
from __future__ import annotations

import asyncio

import pytest

from eda_agent.tools import pcb as pcb_module


class _Bridge:
    def __init__(self, fail_at: int | None = None):
        self.calls = []
        self.fail_at = fail_at

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            return {"success": False, "reason": "unknown layer"}
        return {"success": True}


def _tools(monkeypatch, bridge):
    monkeypatch.setattr(pcb_module, "get_bridge", lambda: bridge)
    captured = {}

    class DummyMcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    pcb_module.register_pcb_tools(DummyMcp())
    return captured["pcb_place_image"]


def _png(tmp_path, rows, name="logo.png"):
    from PIL import Image
    image = Image.new("RGB", (len(rows[0]), len(rows)), (255, 255, 255))
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch == "#":
                image.putpixel((x, y), (0, 0, 0))
    path = tmp_path / name
    image.save(path)
    return path


def test_dry_run_is_the_default_and_places_nothing(tmp_path, monkeypatch):
    bridge = _Bridge()
    tool = _tools(monkeypatch, bridge)
    path = _png(tmp_path, ["##", "##"])

    result = asyncio.run(tool(str(path), x=0, y=0, width=100))
    assert result["dry_run"] is True
    assert result["region_count"] == 1
    assert bridge.calls == []
    assert "nothing placed" in result["note"]


def test_committing_places_one_region_per_rectangle(tmp_path, monkeypatch):
    bridge = _Bridge()
    tool = _tools(monkeypatch, bridge)
    # Two separate blocks -> two rectangles.
    path = _png(tmp_path, ["##..##"])

    result = asyncio.run(tool(str(path), x=0, y=0, width=60, dry_run=False))
    assert result["success"] is True and result["placed"] == 2
    assert [c for c, _ in bridge.calls] == ["pcb.place_region_poly"] * 2


def test_the_layer_reaches_the_bridge(tmp_path, monkeypatch):
    bridge = _Bridge()
    tool = _tools(monkeypatch, bridge)
    path = _png(tmp_path, ["#"])

    asyncio.run(tool(str(path), x=0, y=0, width=10,
                     layer="BottomOverlay", dry_run=False))
    assert bridge.calls[0][1]["layer"] == "BottomOverlay"


def test_millimetres_are_converted_to_mils(tmp_path, monkeypatch):
    bridge = _Bridge()
    tool = _tools(monkeypatch, bridge)
    path = _png(tmp_path, ["#"])

    result = asyncio.run(tool(str(path), x=0, y=0, width=25.4, units="mm"))
    # 25.4 mm is 1000 mil across.
    assert result["scale"]["width_mils"] == pytest.approx(1000)


def test_a_bad_unit_is_refused(tmp_path, monkeypatch):
    tool = _tools(monkeypatch, _Bridge())
    path = _png(tmp_path, ["#"])
    assert "error" in asyncio.run(tool(str(path), x=0, y=0, width=10, units="furlong"))


def test_too_many_regions_is_refused_before_anything_is_placed(tmp_path, monkeypatch):
    bridge = _Bridge()
    tool = _tools(monkeypatch, bridge)
    rows = ["".join("#" if (x + y) % 2 == 0 else "." for x in range(10))
            for y in range(10)]
    path = _png(tmp_path, rows)

    result = asyncio.run(tool(str(path), x=0, y=0, width=100,
                              dry_run=False, max_regions=10))
    assert result["success"] is False
    assert "exceeds max_regions" in result["reason"]
    assert bridge.calls == [], "the cap is checked before the first placement"


def test_the_cap_does_not_block_a_dry_run(tmp_path, monkeypatch):
    """Seeing how bad it is must not require raising the limit first."""
    bridge = _Bridge()
    tool = _tools(monkeypatch, bridge)
    rows = ["".join("#" if (x + y) % 2 == 0 else "." for x in range(10))
            for y in range(10)]
    path = _png(tmp_path, rows)

    result = asyncio.run(tool(str(path), x=0, y=0, width=100, max_regions=10))
    assert result["region_count"] == 50 and bridge.calls == []


def test_placement_stops_at_the_first_refusal(tmp_path, monkeypatch):
    """A wrong layer fails identically every time; finishing the run
    would leave every one of them to undo."""
    bridge = _Bridge(fail_at=2)
    tool = _tools(monkeypatch, bridge)
    path = _png(tmp_path, ["#.#.#.#"])

    result = asyncio.run(tool(str(path), x=0, y=0, width=70, dry_run=False))
    assert result["success"] is False
    assert result["placed"] == 1
    assert "unknown layer" in result["reason"]
    assert len(bridge.calls) == 2, "it stopped rather than placing the rest"


def test_art_with_no_ink_is_refused_with_the_threshold_named(tmp_path, monkeypatch):
    tool = _tools(monkeypatch, _Bridge())
    path = _png(tmp_path, ["..", ".."])
    result = asyncio.run(tool(str(path), x=0, y=0, width=10))
    assert result["success"] is False and "threshold" in result["reason"]


def test_a_missing_image_is_refused(tmp_path, monkeypatch):
    tool = _tools(monkeypatch, _Bridge())
    result = asyncio.run(tool(str(tmp_path / "nope.png"), x=0, y=0, width=10))
    assert result["success"] is False and "not found" in result["reason"]
