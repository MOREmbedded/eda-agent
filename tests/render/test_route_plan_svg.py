# SPDX-License-Identifier: Apache-2.0
from pathlib import Path
from xml.etree import ElementTree as ET

from eda_agent.render.route_plan_svg import render_route_plan_svg, write_route_plan_svg


def test_route_plan_svg_contains_structured_geometry_and_metrics(tmp_path: Path):
    plan = {
        "tracks": [{
            "x1": 0, "y1": 0, "x2": 30, "y2": 40,
            "width": 6, "layer": "TopLayer", "net_name": "USB_D+",
        }],
        "vias": [{"x": 30, "y": 40, "size": 20, "net": "USB_D+"}],
        "obstacles": [{"x1": 10, "y1": 10, "x2": 20, "y2": 20}],
    }
    output = tmp_path / "route.svg"
    result = write_route_plan_svg(plan, output)
    ET.parse(output)
    text = output.read_text(encoding="utf-8")
    assert 'data-net="USB_D+"' in text
    assert 'data-layer="TopLayer"' in text
    assert result["metrics"]["total_track_length_mils"] == 50
    assert result["metrics"]["via_count"] == 1


def test_empty_route_plan_is_rejected():
    try:
        render_route_plan_svg([], [])
    except ValueError as exc:
        assert "no geometry" in str(exc)
    else:
        raise AssertionError("empty plan should fail")
