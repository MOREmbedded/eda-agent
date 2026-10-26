# SPDX-License-Identifier: Apache-2.0
from eda_agent.design.placement_audit import audit_placement


BOARD = {"x1": 0, "y1": 0, "x2": 1000, "y2": 800}


def test_good_role_aware_placement_passes():
    result = audit_placement([
        {"ref": "U1", "x": 500, "y": 400, "width": 100, "height": 100},
        {"ref": "C1", "x": 620, "y": 400, "width": 40, "height": 40,
         "role": "decoupling", "serves": "U1"},
        {"ref": "J1", "x": 50, "y": 650, "width": 50, "height": 80,
         "role": "connector"},
    ], BOARD)
    assert result["ok"] is True
    assert result["score"] == 100


def test_overlap_outside_and_proximity_are_reported():
    result = audit_placement([
        {"ref": "U1", "x": 500, "y": 400, "width": 100, "height": 100},
        {"ref": "C1", "x": 500, "y": 400, "width": 40, "height": 40,
         "role": "decoupling", "serves": "MISSING"},
        {"ref": "J1", "x": 500, "y": 600, "width": 50, "height": 80,
         "role": "connector"},
        {"ref": "R1", "x": 990, "y": 790, "width": 100, "height": 100},
    ], BOARD)
    codes = {f["code"] for f in result["findings"]}
    assert {"courtyard_overlap", "outside_board", "missing_anchor",
            "connector_far_from_edge"} <= codes
    assert result["ok"] is False


def test_far_termination_reports_distance():
    result = audit_placement([
        {"ref": "U1", "x": 100, "y": 100, "width": 40, "height": 40},
        {"ref": "R1", "x": 800, "y": 100, "width": 40, "height": 40,
         "role": "termination", "serves": "U1"},
    ], BOARD)
    finding = next(f for f in result["findings"] if f["code"] == "termination_too_far")
    assert finding["distance_mils"] == 700
