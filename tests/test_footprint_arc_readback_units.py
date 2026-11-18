# SPDX-License-Identifier: Apache-2.0
"""A footprint arc read back in a mil session comes back in mils.

Live on AD 26.11 (2026-11-18) the pin-1 dot of a 1.27 mm SOIC read
xc -2.45, r 0.125 in a reply that said length_units "mil": the centre
and radius were left in the millimetres the bridge sends, while the
arc's width and every track beside it were converted.
"""
from eda_agent import lengths
from eda_agent.units import MILS_PER_MM


def test_arc_centre_and_radius_follow_the_session_unit():
    body = {"arcs": [{"xc": -2.45, "yc": 2.705, "r": 0.125, "width": 0.25,
                      "start": 0, "end": 360}],
            "tracks": [{"x1": -1.125, "y1": -2.5, "x2": 1.125, "y2": -2.5,
                        "width": 0.2}]}
    out = lengths.reply_in("lib_get_footprint_primitives", body, "mil", {})
    arc = out["arcs"][0]
    assert abs(arc["xc"] - -2.45 * MILS_PER_MM) < 1e-3
    assert abs(arc["yc"] - 2.705 * MILS_PER_MM) < 1e-3
    assert abs(arc["r"] - 0.125 * MILS_PER_MM) < 1e-3
    assert abs(arc["width"] - 0.25 * MILS_PER_MM) < 1e-3
    # Angles are not lengths.
    assert arc["start"] == 0 and arc["end"] == 360
