# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Fab stats classify vias by their layer span, not by a layer object.

Via.StartLayer returns a layer OBJECT. Compared with eTopLayer it never
matches, so every via fell through to "buried": a board of ordinary
through vias reported all of them buried. Via.LowLayer / HighLayer are
the TLayer values, the ones the via writers set.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.pascal_source import load

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "altium"


def test_fab_stats_reads_the_span():
    body = load(_SCRIPTS / "PCB.pas")["PCB_GetFabStats"]
    assert "Via.LowLayer = eTopLayer" in body
    assert "Via.HighLayer = eBottomLayer" in body


def test_no_script_compares_a_layer_object_with_a_constant():
    pattern = re.compile(r"\.(?:StartLayer|StopLayer)\s*(?:=|<>)\s*e[A-Z]")
    for pas in _SCRIPTS.glob("*.pas"):
        if pas.name == "Altium_MCP.pas":
            continue
        hits = [n for n, line in enumerate(pas.read_text(encoding="utf-8")
                                           .splitlines(), 1)
                if pattern.search(line) and not line.lstrip().startswith("{")]
        assert not hits, (f"{pas.name} lines {hits} compare StartLayer or "
                          f"StopLayer with a layer constant; read .LayerId "
                          f"or use LowLayer / HighLayer")
