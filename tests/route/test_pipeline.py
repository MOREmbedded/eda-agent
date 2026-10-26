# SPDX-License-Identifier: Apache-2.0
from pathlib import Path
from xml.etree import ElementTree as ET

from eda_agent.route.pipeline import build_route_package, geometry_obstacle_rects


def _geometry():
    return {
        "bbox": {"x1": 0, "y1": 0, "x2": 500, "y2": 500},
        "pads": [
            {"x": 100, "y": 100, "x_size": 40, "y_size": 40,
             "layer": "TopLayer", "net": "N1"},
            {"x": 400, "y": 400, "x_size": 40, "y_size": 40,
             "layer": "TopLayer", "net": "N1"},
        ],
        "tracks": [], "vias": [],
    }


def test_45_degree_package_is_reviewable_and_persisted(tmp_path: Path):
    result = build_route_package(
        _geometry(), tmp_path, routing_style="45deg", grid_pitch_mils=25,
    )
    assert result["ok"] is True
    assert result["route"]["summary"]["routed"] == 1
    tracks = result["route"]["tracks"]
    assert any(
        abs(t["x2"] - t["x1"]) == abs(t["y2"] - t["y1"]) != 0
        for t in tracks
    )
    assert result["acceptance"]["safe_to_apply"] is True
    ET.parse(tmp_path / "routing-plan.svg")
    assert (tmp_path / "routing-plan.json").exists()


def test_package_reports_unknown_requested_net(tmp_path: Path):
    result = build_route_package(_geometry(), tmp_path, nets=["MISSING"])
    assert result["unknown_nets"] == ["MISSING"]
    assert result["acceptance"]["safe_to_apply"] is False


def test_geometry_obstacles_include_pad_extents():
    obstacles = geometry_obstacle_rects(_geometry())
    assert obstacles[0] == {"x1": 80, "y1": 80, "x2": 120, "y2": 120}
