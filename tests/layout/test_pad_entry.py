# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Pad-entry audit: each finding fires on its defect and not on a clean entry."""

from __future__ import annotations

from eda_agent.layout.audit import CHECKS
from eda_agent.layout.model import (Component, Layer, LayoutBoard, Pad,
                                    PadCopper, Region, Track)
from eda_agent.layout.pad_entry import pad_entry_audit


def _pad(comp, name, x, y, net, w=24, h=30, rot=0.0):
    return Pad(comp, name, x, y, rotation=rot, net=net,
               copper=[PadCopper("Top", "rect", w, h)])


def _board(pads, tracks=(), regions=()):
    comps = sorted({p.comp for p in pads})
    return LayoutBoard(name="t", layers=[Layer("Top", order=0)],
                       components=[Component(c) for c in comps],
                       pads=list(pads), tracks=list(tracks),
                       regions=list(regions))


def _kinds(board):
    return {f["kind"] for f in pad_entry_audit(board)["findings"]}


def test_a_straight_centred_entry_is_clean():
    b = _board([_pad("R1", "1", 0, 0, "A"), _pad("R1", "2", 60, 0, "B")],
               [Track("Top", 0, 0, -100, 0, 10, net="A"),
                Track("Top", 60, 0, 160, 0, 10, net="B")])
    assert _kinds(b) == set()


def test_off_axis_entry_is_flagged_with_severity():
    b = _board([_pad("R1", "1", 0, 0, "A")],
               [Track("Top", 0, 0, -100, -100, 10, net="A")])
    f = [x for x in pad_entry_audit(b)["findings"] if x["kind"] == "off_axis"]
    assert f and f[0]["severity"] == "low"       # a 45 degree corner entry
    b = _board([_pad("R1", "1", 0, 0, "A")],
               [Track("Top", 0, 0, -100, -30, 10, net="A")])
    f = [x for x in pad_entry_audit(b)["findings"] if x["kind"] == "off_axis"]
    assert f and f[0]["severity"] == "high"


def test_off_center_landing_on_a_small_pad():
    b = _board([_pad("R1", "1", 0, 0, "A")],
               [Track("Top", 0, 12, -100, 12, 6, net="A")])
    assert "off_center" in _kinds(b)


def test_wider_than_pad_ignores_rounding():
    tight = _board([_pad("R1", "1", 0, 0, "A", w=9.84, h=9.84)],
                   [Track("Top", 0, 0, -100, 0, 10, net="A")])
    assert "wider_than_pad" not in _kinds(tight)
    wide = _board([_pad("R1", "1", 0, 0, "A", w=10, h=10)],
                  [Track("Top", 0, 0, -100, 0, 20, net="A")])
    assert "wider_than_pad" in _kinds(wide)


def test_crowded_small_pad():
    tracks = [Track("Top", 0, 0, x2, y2, 6, net="A")
              for x2, y2 in ((-100, 0), (100, 0), (0, 100))]
    assert "crowded" in _kinds(_board([_pad("U1", "1", 0, 0, "A")], tracks))


def test_trace_crossing_a_pad():
    b = _board([_pad("U1", "1", 0, 0, "A")],
               [Track("Top", -100, 0, 100, 0, 6, net="A")])
    assert "crosses_pad" in _kinds(b)


def test_unbalanced_two_pin_part_fed_by_a_fill_on_one_side():
    fill = Region("Top", [(-200, -200), (20, -200), (20, 200), (-200, 200)],
                  net="A", kind="pour")
    b = _board([_pad("C1", "1", 0, 0, "A"), _pad("C1", "2", 60, 0, "B")],
               [Track("Top", 60, 0, 160, 0, 6, net="B")], [fill])
    f = [x for x in pad_entry_audit(b)["findings"] if x["kind"] == "unbalanced"]
    assert f and f[0]["pad"] == "C1"


def test_an_unrouted_pad_is_not_called_unbalanced():
    b = _board([_pad("C1", "1", 0, 0, "A"), _pad("C1", "2", 60, 0, "B")],
               [Track("Top", 60, 0, 160, 0, 20, net="B")])
    assert "unbalanced" not in _kinds(b)


def test_registered_as_a_layout_check():
    assert "pad_entry_audit" in CHECKS and "loop_audit" in CHECKS


def test_only_high_severity_findings_fail_the_gate():
    diag = _board([_pad("R1", "1", 0, 0, "A")],
                  [Track("Top", 0, 0, -100, -100, 10, net="A")])
    assert pad_entry_audit(diag)["pass"] is True
    cross = _board([_pad("U1", "1", 0, 0, "A")],
                   [Track("Top", -100, 0, 100, 0, 6, net="A")])
    assert pad_entry_audit(cross)["pass"] is False
