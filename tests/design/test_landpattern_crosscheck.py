# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Cross-check: the IPC pattern from a package against the vendor's own.

Both come from one datasheet, so a large disagreement means one of the two
was transcribed wrong. Every vendor pattern in ``_landpattern_cases``
agrees with its package within ``CROSS_CHECK_TOL``; one mistyped number on
either side must not.
"""

from __future__ import annotations

import dataclasses

import pytest

from eda_agent.design.landpattern import build_footprint, compare_to_vendor
from eda_agent.design.landpattern.compare import (
    CROSS_CHECK_TOL,
    RATIO_LIMIT,
    cross_check,
)
from tests.design import _landpattern_cases as cases

VENDOR_CASES = sorted(n for n, f in cases.ALL_CASES.items()
                      if f().vendor is not None)


def test_twenty_two_cases_carry_a_vendor_pattern():
    assert len(VENDOR_CASES) == 22


@pytest.mark.parametrize("name", VENDOR_CASES)
def test_every_vendor_pattern_agrees_with_its_package(name):
    spec = cases.ALL_CASES[name]()
    r = cross_check(spec)
    assert r["ran"] and r["ok"], [c["message"] for c in r["checks"] if not c["ok"]]
    assert r["checks"], name
    for c in r["checks"]:
        assert c["verdict"] == "agree"
        assert c["vendor_cite"] == spec.vendor.cite.short()
        if c["quantity"] not in ("pad_count", "position", "size"):
            assert c["ipc_from"], c


_KEYS = {"span": "span", "span_y": "span", "pad_length": "pad_length",
         "pad_width": "pad_width", "tab_length": "tab", "tab_width": "tab",
         "ep_x": "ep", "ep_y": "ep", "land_diameter": "land_diameter"}


def test_the_tolerances_are_the_measured_worst_cases():
    """Each tolerance sits at the largest real deviation in the case file,
    plus at most 0.1 mm: loosening one, which would let a transcription
    error through, fails here."""
    worst: dict[str, float] = {}
    for name in VENDOR_CASES:
        spec = cases.ALL_CASES[name]()
        for c in cross_check(spec)["checks"]:
            key = _KEYS.get(c["quantity"])
            if key:
                worst[key] = max(worst.get(key, 0.0), abs(c["diff"]))
    for key, tol in CROSS_CHECK_TOL.items():
        if key == "pitch":
            continue
        assert worst[key] <= tol + 1e-9, (key, worst[key], tol)
        assert tol - worst[key] <= 0.1 + 1e-9, (key, worst[key], tol)
    assert CROSS_CHECK_TOL["pitch"] <= 0.005


def _vendor(spec, **changes):
    spec.vendor = dataclasses.replace(spec.vendor, **changes)
    return spec


def _failed(report: dict) -> dict:
    bad = {c["quantity"]: c for c in report["checks"] if not c["ok"]}
    assert bad, report["checks"]
    return bad


def test_vendor_land_width_transcribed_wrong():
    """TI D0008A example layout prints 0.6; read as 1.6."""
    r = cross_check(_vendor(cases.soic8(), pad_width=1.6))
    c = _failed(r)["pad_width"]
    assert c["verdict"] == "likely transcription error"
    assert c["ipc"] == 0.6 and c["vendor"] == 1.6
    assert "D0008A package outline, p. 54" in c["message"]
    assert "D0008A example board layout, p. 55" in c["message"]
    assert "lead_width" in c["message"]


def test_a_factor_apart_fails_inside_the_absolute_tolerance():
    """0.6 read as 0.06: 0.54 mm apart, inside 0.6 mm, but ten times off."""
    r = cross_check(_vendor(cases.soic8(), pad_width=0.06))
    c = _failed(r)["pad_width"]
    assert c["ratio"] >= RATIO_LIMIT and "factor" in c["message"]


def test_vendor_span_taken_toe_to_toe():
    """D0008A: lands 1.55 on 5.4 centre to centre; the toe-to-toe 6.95 typed
    as the span."""
    r = cross_check(_vendor(cases.soic8(), span=6.95))
    assert "span" in _failed(r)


def test_a_package_misread_the_sanity_checks_cannot_see():
    """D0008A E 5.80-6.19 typed as 7.80-8.19: still a consistent package,
    but its IPC span no longer matches TI's own layout. (A 1 mm slip, to
    6.80-7.19, moves the IPC span from 0.5 under TI's to 0.5 over it and
    is not caught: TI's own heel already sits at the tolerance.)"""
    from eda_agent.design.landpattern.check import check_package

    d = cases.soic8().to_dict()
    d["lead_span_x"] = [7.80, 8.19]
    assert check_package(d)["ok"]
    from eda_agent.design.landpattern import PackageSpec
    r = cross_check(PackageSpec.from_dict(d))
    c = _failed(r)["span"]
    assert "lead_span_x" in c["message"]


def test_pitch_must_match_exactly():
    r = cross_check(_vendor(cases.tssop8(), pitch=0.5))
    assert "pitch" in _failed(r)


def test_corner_pad_crystal_lands_need_not_sit_on_the_terminal_pitch():
    """Abracon ABM8G: terminals 1.8 apart, recommended lands 2.2 apart."""
    r = cross_check(cases.abm8g_crystal())
    pitch = next(c for c in r["checks"] if c["quantity"] == "pitch")
    assert pitch["ok"] and pitch["tol"] == CROSS_CHECK_TOL["span"]


def test_exposed_pad_transcribed_wrong():
    """TI RGT0016C thermal land 1.68 read as 2.68."""
    r = cross_check(_vendor(cases.qfn16(), ep_x=2.68))
    assert "ep_x" in _failed(r)


def test_no_vendor_pattern_means_nothing_ran():
    r = cross_check(cases.msop8())
    assert r["ran"] is False and r["ok"] and "vendor" in r["reason"]


def test_explicit_vendor_lands_are_checked_by_count_and_position():
    """Nexperia SOT23 Fig. 2 lands; one dropped, and one moved 1 mm."""
    from eda_agent.design.landpattern import CustomPad

    lands = (CustomPad("1", -1.0, 0.95, 0.7, 0.6), CustomPad("2", -1.0, -0.95, 0.7, 0.6),
             CustomPad("3", 1.0, 0.0, 0.7, 0.6))
    spec = cases.sot23_3()
    spec.vendor = dataclasses.replace(spec.vendor, pads=lands, pad_length=None,
                                      pad_width=None, span=None)
    assert cross_check(spec)["ok"]
    spec.vendor = dataclasses.replace(spec.vendor, pads=lands[:2])
    assert "pad_count" in _failed(cross_check(spec))
    moved = (lands[0], lands[1], CustomPad("3", 2.0, 0.0, 0.7, 0.6))
    spec.vendor = dataclasses.replace(spec.vendor, pads=moved)
    assert "position" in _failed(cross_check(spec))


def test_compare_to_vendor_keeps_its_old_tolerances_by_default():
    """tab_tol and land_tol are new and optional: unset, the tab is held to
    length_tol and width_tol and the grid land to width_tol, as before."""
    spec = cases.dpak()
    m = build_footprint(spec)
    old = compare_to_vendor(m, spec.vendor, length_tol=0.5, width_tol=0.45)
    tabs = {c["check"]: c for c in old["checks"] if c["check"].startswith("tab")}
    assert tabs["tab_length"]["tol"] == 0.5 and tabs["tab_width"]["tol"] == 0.45
    assert not old["ok"]
    new = compare_to_vendor(m, spec.vendor, length_tol=0.5, width_tol=0.45,
                            tab_tol=0.7)
    assert new["ok"]
