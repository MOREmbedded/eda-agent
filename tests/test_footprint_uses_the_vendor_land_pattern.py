# SPDX-License-Identifier: Apache-2.0
"""lib_create_footprint_from_package builds the vendor's land pattern.

Live on Altium 26.11 a package given with the datasheet's recommended
pads came out as the IPC-7351B nominal pattern, and nothing in the reply
said so. The lands now follow ``vendor`` when it is given, and
``land_pattern`` and the notes say which pattern was built.
"""
from __future__ import annotations

import dataclasses

from eda_agent.design.landpattern.builder import build_footprint
from eda_agent.design.landpattern.compare import pattern_metrics
from eda_agent.design.landpattern.spec import CustomPad, VendorLandPattern
from tests.design import _landpattern_cases as cases
from tests.test_footprint_from_package_tools import altium  # noqa: F401 - fixture


def _explicit_soic8():
    """SOIC-8 with the recommended lands listed pad by pad, 5.2 mm apart
    and 1.5 x 0.55 mm, a pattern IPC-7351B would not produce."""
    pads = []
    for i in range(4):
        y = 1.905 - 1.27 * i
        pads.append(CustomPad(str(i + 1), -2.6, y, 1.5, 0.55))
        pads.append(CustomPad(str(8 - i), 2.6, y, 1.5, 0.55))
    vendor = VendorLandPattern(pads=tuple(pads))
    return dataclasses.replace(cases.soic8(), vendor=vendor), pads


def test_explicit_vendor_pads_are_placed_as_given():
    spec, pads = _explicit_soic8()
    model = build_footprint(spec, vendor_lands=True)
    got = sorted((p.number, p.x, p.y, p.width, p.height) for p in model.pads)
    want = sorted((q.number, q.x, q.y, q.width, q.height) for q in pads)
    assert got == want
    assert model.ipc["lands_from"] == "vendor"
    assert any("placed as given" in n for n in model.ipc["notes"])
    assert "recommended land pattern" in model.description


def test_a_parametric_vendor_pattern_sets_size_and_span():
    model = build_footprint(cases.soic8(), vendor_lands=True)
    met = pattern_metrics(model)
    assert (met["pad_length"], met["pad_width"], met["span"]) == (1.55, 0.6, 5.4)


def test_the_quad_exposed_pad_follows_the_vendor():
    model = build_footprint(cases.qfn16(), vendor_lands=True)
    met = pattern_metrics(model)
    v = cases.qfn16().vendor
    assert (met["span"], met["span_y"], met["ep_x"], met["ep_y"]) == (
        v.span, v.span_y, v.ep_x, v.ep_y)


def test_the_ipc_pattern_is_still_the_default_of_the_engine():
    """The vendor cross-check measures the IPC pattern against the vendor's,
    so build_footprint keeps IPC unless asked."""
    model = build_footprint(cases.soic8())
    assert "lands_from" not in model.ipc
    assert pattern_metrics(model)["pad_length"] != 1.55


def test_the_tool_writes_the_vendor_lands_and_says_so(altium):  # noqa: F811
    run, box = altium
    spec, pads = _explicit_soic8()
    out = run(package=spec.to_dict())
    assert out["ok"], out.get("reason") or out["readback"]
    assert out["land_pattern"] == "vendor"
    assert out["notes"][0].startswith("lands: the manufacturer's recommended")
    written = sorted((p["designator"], float(p["x"]), float(p["x_size"]),
                      float(p["y_size"])) for p in box["bridge"].pads)
    assert written == sorted((q.number, q.x, q.width, q.height) for q in pads)
