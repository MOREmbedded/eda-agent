# SPDX-License-Identifier: Apache-2.0
from eda_agent.route.audit import audit_route_plan


def test_route_audit_accepts_return_via_and_matched_pair():
    plan = {
        "tracks": [
            {"x1": 0, "y1": 0, "x2": 100, "y2": 0, "net_name": "D+"},
            {"x1": 0, "y1": 10, "x2": 100, "y2": 10, "net_name": "D-"},
        ],
        "vias": [{"x": 100, "y": 0, "net": "D+"}],
    }
    result = audit_route_plan(
        plan, reference_vias=[{"x": 110, "y": 0}], high_speed_nets=["D+"],
        diff_pairs=[{"positive": "D+", "negative": "D-", "max_skew_mils": 5}],
    )
    assert result["ok"] is True
    assert result["diff_pairs"][0]["skew_mils"] == 0


def test_route_audit_flags_return_path_skew_and_via_count():
    plan = {
        "tracks": [
            {"x1": 0, "y1": 0, "x2": 200, "y2": 0, "net_name": "P"},
            {"x1": 0, "y1": 10, "x2": 100, "y2": 10, "net_name": "N"},
        ],
        "vias": [
            {"x": 200 + i, "y": 0, "net": "P"} for i in range(3)
        ],
    }
    result = audit_route_plan(
        plan, high_speed_nets=["P"], max_vias_per_net=2,
        diff_pairs=[{"positive": "P", "negative": "N", "max_skew_mils": 10}],
    )
    codes = {f["code"] for f in result["findings"]}
    assert {"missing_nearby_return_via", "excessive_vias", "diff_pair_skew"} <= codes
    assert result["ok"] is False
