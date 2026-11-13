# SPDX-License-Identifier: Apache-2.0
"""scripts/bench_router_engines.py, which makes the engine comparison
table in docs/ROUTER_ENGINES.md: the boards it builds, a real routing pass
on a small board with each engine, and the table it prints."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from eda_agent.layout.model import LayoutBoard

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bench_router_engines.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("bench_router_engines", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_boards_are_the_ones_the_docs_describe(bench):
    two, four, planes = (bench.KINDS[k][1](1) for k in ("mixed2", "mixed4", "mixed4p"))
    assert [len(b.pads) for b in (two, four, planes)] == [92, 92, 92]
    assert [len(b.layers) for b in (two, four, planes)] == [2, 4, 4]
    assert not two.regions and not four.regions
    assert sorted((r.layer, r.net) for r in planes.regions) == [("MidLayer1", "GND"),
                                                                ("MidLayer2", "+3V3")]
    nets = {p.net for p in two.pads if p.net}
    assert {"GND", "+3V3"} <= nets and sum(n.startswith("S") for n in nets) == 31

    grid = bench.KINDS["bga8"][1](1)
    balls = [p for p in grid.pads if p.comp == "U1"]
    assert len(balls) == 64 and len(grid.pads) == 64 + 2 * 40
    xs = sorted({round(p.x, 3) for p in balls})
    assert len(xs) == 8 and all(abs(b - a - 31.5) < 1e-9 for a, b in zip(xs, xs[1:]))
    assert [(r.layer, r.net) for r in grid.regions] == [("MidLayer1", "GND")]


def test_a_seed_gives_the_same_board_every_time(bench):
    def nets(board):
        return [(p.comp, p.name, p.net, p.x, p.y) for p in board.pads]

    for kind in bench.KINDS:
        build = bench.KINDS[kind][1]
        assert nets(build(2)) == nets(build(2))
    assert nets(bench.KINDS["mixed2"][1](1)) != nets(bench.KINDS["mixed2"][1](2))


@pytest.mark.parametrize("engine", ["default", "euan"])
def test_a_board_routed_by_each_engine_gives_a_full_record(bench, monkeypatch, engine):
    def small(seed):
        b = LayoutBoard(name=f"small-{seed}", outline=[(0, 0), (900, 0), (900, 500), (0, 500)],
                        layers=bench._layers(2), rules=list(bench.RULES))
        for ref, x in (("C1", 200), ("C2", 650)):
            comp, pads = bench.chip(ref, x, 250)
            b.components.append(comp)
            b.pads += pads
        b.pads[1].net = b.pads[2].net = "A"   # C1.2 to C2.1
        return b

    monkeypatch.setitem(bench.KINDS, "small", ("Small", small))
    rec = bench.route(("small", 1, engine))
    assert rec["engine"] == engine and rec["kind"] == "small" and rec["seed"] == 1
    assert rec["connections"] == 1 and rec["missing"] == 0
    assert rec["violations_before"] == 0 and rec["violations"] == 0
    assert rec["length"] > 0 and rec["seconds"] >= 0
    assert set(rec) == {"kind", "seed", "engine", "pads", "connections", "missing",
                        "violations_before", "violations", "vias", "length", "seconds"}


def test_the_table_totals_the_seeds_and_counts_only_violations_the_router_added(bench):
    def rec(kind, seed, engine, missing, before, after, vias, length, seconds):
        return {"kind": kind, "seed": seed, "engine": engine, "pads": 92, "connections": 50,
                "missing": missing, "violations_before": before, "violations": after,
                "vias": vias, "length": length, "seconds": seconds}

    records = [rec("mixed2", 1, "default", 0, 0, 0, 10, 1000.0, 1.2),
               rec("mixed2", 2, "default", 5, 2, 2, 20, 2500.0, 2.4),
               rec("mixed2", 1, "euan", 0, 0, 1, 11, 1100.0, 1.0),
               rec("mixed2", 2, "euan", 1, 2, 3, 21, 2600.0, 2.0)]
    lines = bench.table(records)
    assert lines[0].startswith("| Boards (2 of each) | Engine | Completion |")
    assert lines[2] == "| Mixed, 2 layers | default | 95.0% | 5 | 0 | 30 | 3.5 | 4 |"
    assert lines[3] == "|  | euan | 99.0% | 1 | 2 | 32 | 3.7 | 3 |"
    assert len(lines) == 4     # kinds that were not run get no row
