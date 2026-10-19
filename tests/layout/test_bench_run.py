# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The benchmark runner: what it scores per board, how it sums them up,
and how it treats boards that fail, run long or are not finished."""

from __future__ import annotations

import json

import pytest

from eda_agent.layout import bench_run
from eda_agent.layout.bench_run import (BenchOptions, fixture_paths, options_from,
                                        parse_args, read_board_list, run_board,
                                        run_many, summarize)
from eda_agent.layout.model import Layer, LayoutBoard, Pad, PadCopper, Rule, Track
from tests.layout.test_placer import _chain_board


def _routed_board() -> LayoutBoard:
    """Two pads joined by the person's track, and an outline drawn on a
    mechanical layer as finished boards have."""
    def pad(ref, x):
        return Pad(ref, "1", x, 100, net="A", copper=[PadCopper("TopLayer", "rect", 20, 20)])

    return LayoutBoard(
        name="two pads", outline=[(0, 0), (400, 0), (400, 200), (0, 200)],
        layers=[Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)],
        pads=[pad("R1", 50), pad("R2", 350)],
        tracks=[Track("TopLayer", 50, 100, 350, 100, 8, "A"),
                Track("Mechanical1", 0, 0, 400, 0, 5), Track("Mechanical1", 400, 0, 400, 200, 5),
                Track("Mechanical1", 400, 200, 0, 200, 5), Track("Mechanical1", 0, 200, 0, 0, 5)],
        rules=[Rule("Clearance", "0", "All", "All", 1, True, {"gap": 6.0},
                    "Clearance Constraint (Gap=6mil) (All),(All)")])


def test_route_mode_scores_the_person_and_the_engine_on_copper():
    rec = run_board(_routed_board(), BenchOptions(mode="route"))
    assert rec["ok"] and "place" not in rec
    assert rec["human"]["fully_routed"] and rec["human"]["length"] == 300.0
    assert rec["engine"]["fully_routed"] and rec["engine"]["violations"] == 0
    # The outline is not the person's routing: the engine's straight track
    # is about as long as theirs, not a quarter of it.
    assert 0.95 <= rec["length_vs_human"] <= 1.2
    assert rec["engine"]["sharp_bends"] == 0


def test_place_route_mode_places_the_scrambled_parts_then_routes():
    rec = run_board(_chain_board(), BenchOptions(mode="place_route", strategy="analytic"))
    assert rec["ok"]
    p = rec["place"]
    assert p["movable"] == 12 and p["strategy"] == "analytic" and not p["fallback"]
    assert p["overlaps"] == 0 and p["failed"] == 0
    assert rec["hpwl_vs_human"] == round(p["hpwl"] / rec["human"]["hpwl"], 3)
    assert rec["engine"]["completion"] > 0


def test_place_mode_does_not_route():
    rec = run_board(_chain_board(), BenchOptions(mode="place", strategy="analytic"))
    assert "place" in rec and "engine" not in rec


def test_only_finished_boards_are_scored_when_asked():
    unrouted = _chain_board()
    rec = run_board(unrouted, BenchOptions(mode="route", finished_only=True))
    assert not rec["ok"] and rec["skipped"]
    assert run_board(_routed_board(), BenchOptions(mode="route", finished_only=True))["ok"]


def _rec(completion, violations=0, vias=(10, 10), length=(100.0, 100.0), place=None):
    rec = {"ok": True, "wall": 2.0,
           "human": {"fully_routed": True, "vias": vias[0], "length": length[0],
                     "segments": 5, "overlaps": 0},
           "engine": {"completion": completion, "fully_routed": completion == 1.0,
                      "violations": violations, "vias": vias[1], "length": length[1],
                      "segments": 7, "sharp_bends": 1, "seconds": 1.5},
           "router": {"stage_fallback": False},
           "vias_vs_human": round(vias[1] / vias[0], 3) if vias[0] else None,
           "length_vs_human": round(length[1] / length[0], 3)}
    if place is not None:
        rec["place"] = place
        rec["hpwl_vs_human"] = 1.2
    return rec


def test_summary_counts_and_means_over_the_boards_that_ran():
    s = summarize({
        "a": _rec(1.0, vias=(10, 12)),
        "b": _rec(0.9, violations=2, vias=(0, 3)),
        "c": {"ok": False, "timeout": True, "error": "over the cap", "wall": 9.0},
        "d": {"ok": False, "error": "ValueError: bad", "wall": 1.0},
        "e": {"ok": False, "skipped": "not finished", "wall": 1.0},
    })
    assert (s["boards"], s["scored"], s["timeouts"], s["errors"], s["skipped"]) == (5, 2, 1, 1, 1)
    r = s["routing"]
    assert r["completion"]["mean"] == 0.95 and r["completion"]["n"] == 2
    assert r["fully_routed"] == 1 and r["drc_clean"] == 1 and r["violations"] == 2
    # A person's board with no vias gives no ratio; it is left out, not 0.
    assert r["vias_vs_human"]["n"] == 1 and r["vias_vs_human"]["median"] == 1.2
    assert r["vias"] == 15 and r["human_vias"] == 10
    assert s["board_seconds"] == 15.0
    assert "placement" not in s
    assert "fully routed 1/2" in bench_run.format_summary(s)


def test_route_mode_measures_detours_and_via_clusters_for_both():
    rec = run_board(_routed_board(), BenchOptions(mode="route"))
    for who in ("human", "engine"):
        d = rec[who]["detour"]
        assert d["connections"] == 1 and abs(d["median"] - 1.0) < 0.05 and d["over_1_5"] == 0
        assert rec[who]["via_clusters"]["clusters"] == 0


def test_summary_of_detours_and_via_clusters():
    def det(n, median, p95, over15, over2, pieces):
        return {"connections": n, "median": median, "p95": p95, "over_1_5": over15,
                "over_2": over2, "pieces_median": pieces}

    a, b, old = _rec(1.0), _rec(1.0), _rec(1.0)
    a["engine"].update(detour=det(10, 1.1, 1.8, 2, 1, 3),
                       via_clusters={"clusters": 1, "vias": 4, "largest": 4})
    a["human"].update(detour=det(8, 1.2, 2.5, 3, 2, 4),
                      via_clusters={"clusters": 0, "vias": 0, "largest": 0})
    b["engine"].update(detour=det(20, 1.3, 2.2, 5, 3, 5),
                       via_clusters={"clusters": 2, "vias": 7, "largest": 4})
    b["human"].update(detour={"connections": 0},
                      via_clusters={"clusters": 1, "vias": 3, "largest": 3})
    s = summarize({"a": a, "b": b, "old": old})
    r = s["routing"]
    assert r["detour"] == {"boards": 2, "connections": 30, "over_1_5": 7, "over_2": 4,
                           "median": 1.2, "p95": 2.0, "pieces_median": 4.0}
    # A board whose person joins nothing by tracks alone gives no figure.
    assert r["human_detour"]["boards"] == 1 and r["human_detour"]["connections"] == 8
    assert r["via_clusters"] == {"clusters": 3, "vias": 11, "largest": 4}
    assert r["human_via_clusters"] == {"clusters": 1, "vias": 3, "largest": 3}
    text = bench_run.format_summary(s)
    assert "over 1.5x 7/30" in text and "via clusters 3 (11 vias), person 1 (3 vias)" in text
    assert "track pieces 21 (person 15, 1.40x)" in text
    # Results written before these were measured still sum up.
    assert "detour" not in summarize({"old": _rec(1.0)})["routing"]


def test_summary_of_placements():
    def place(overlaps, moved, fallback=False, strategy="blocks"):
        return {"overlaps": overlaps, "overlaps_moved": moved, "violations": 0, "failed": 0,
                "body_overlaps": 0, "fallback": fallback, "strategy": strategy, "seconds": 0.5}

    s = summarize({"a": _rec(1.0, place=place(0, 0)),
                   "b": _rec(1.0, place=place(2, 0, True, "analytic")),
                   "c": _rec(1.0, place=place(1, 1))})
    p = s["placement"]
    assert p["overlap_free"] == 1 and p["no_overlap_with_a_moved_part"] == 2
    assert p["fallbacks"] == 1 and p["strategies"] == {"analytic": 1, "blocks": 2}
    assert p["hpwl_vs_human"]["median"] == 1.2
    assert "overlap-free 1" in bench_run.format_summary(s)


def test_board_lists_from_lines_json_and_earlier_results(tmp_path):
    lines = tmp_path / "list.txt"
    lines.write_text("a.json.gz  # first\n\n# a comment\nb.json\n", encoding="utf-8")
    assert read_board_list(lines) == ["a.json.gz", "b.json"]
    js = tmp_path / "list.json"
    js.write_text(json.dumps(["a.json"]), encoding="utf-8")
    assert read_board_list(js) == ["a.json"]
    js.write_text(json.dumps({"a.json": {}, "b.json": {}}), encoding="utf-8")
    assert read_board_list(js) == ["a.json", "b.json"]
    js.write_text(json.dumps({"mode": "route", "boards": {"c.json": {}}}), encoding="utf-8")
    assert read_board_list(js) == ["c.json"]


def test_a_listed_board_not_in_the_directory_is_an_error(tmp_path):
    for name in ("b.json", "a.json.gz", "notes.txt"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    assert [p.name for p in fixture_paths(tmp_path)] == ["a.json.gz", "b.json"]
    assert [p.name for p in fixture_paths(tmp_path, limit=1)] == ["a.json.gz"]
    with pytest.raises(SystemExit):
        fixture_paths(tmp_path, ["a.json.gz", "missing.json"])


def test_engine_options_from_the_command_line(tmp_path):
    base = ["--fixtures", str(tmp_path)]
    assert options_from(parse_args(base)).stages is None
    assert options_from(parse_args(base + ["--legacy-router"])).stages is False
    opts = options_from(parse_args(base + ["--stage", "pad_vias=bga", "--stage",
                                           "plane_fanout=true", "--stage", "lane_cost=inf",
                                           "--stage", "bus_min=4", "--mode", "place_route",
                                           "--strategy", "analytic", "--no-planes"]))
    assert opts.stages == {"pad_vias": "bga", "plane_fanout": True,
                           "lane_cost": float("inf"), "bus_min": 4}
    assert (opts.mode, opts.strategy, opts.planes) == ("place_route", "analytic", False)
    resolved = bench_run.resolved_options(opts)
    assert resolved["stages_resolved"]["pad_vias"] == "bga"
    assert resolved["strategy_resolved"] == "analytic"
    with pytest.raises(SystemExit):
        parse_args(base + ["--legacy-router", "--stage", "bends=false"])


def test_a_broken_fixture_is_recorded_against_its_board(tmp_path):
    _routed_board().save(tmp_path / "good.json")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    got = run_many(fixture_paths(tmp_path), BenchOptions(mode="route"), jobs=0)
    assert got["good.json"]["ok"]
    assert not got["broken.json"]["ok"] and "JSONDecodeError" in got["broken.json"]["error"]


def test_a_fixture_is_scored_with_the_reads_corrections(tmp_path):
    # A fixture saved by an earlier read keeps that read's faults: the
    # benchmark scores the person's board as the reader reads it now.
    from tests.layout.test_read_altium import _nested_pours

    _nested_pours("+3V3").save(tmp_path / "nested.json")
    board, repaired = bench_run.load_fixture(tmp_path / "nested.json")
    assert repaired == 1 and board.regions[3].net == "CORE"
    rec = bench_run.run_path(tmp_path / "nested.json", BenchOptions(mode="route"))
    assert rec["read_repairs"] == 1 and rec["human"]["violations"] == 0


def test_a_board_past_the_time_cap_is_stopped_and_recorded(tmp_path):
    _routed_board().save(tmp_path / "slow.json")
    got = run_many(fixture_paths(tmp_path), BenchOptions(mode="route"), jobs=1,
                   timeout=0.05)
    rec = got["slow.json"]
    assert not rec["ok"] and rec["timeout"] and "cap" in rec["error"]


def test_a_locked_output_file_does_not_stop_the_run(tmp_path, monkeypatch, capsys):
    # A sync client holding the results file made the rename fail and took
    # the whole run down with it.
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _routed_board().save(fixtures / "one.json")
    out = tmp_path / "out.json"
    real = bench_run.replace_with_retry

    def locked(tmp, target):
        if str(target) == str(out):
            raise PermissionError(5, "Access is denied")
        real(tmp, target)

    monkeypatch.setattr(bench_run, "replace_with_retry", locked)
    monkeypatch.setattr(bench_run, "FINAL_WAIT", 0.0)
    assert bench_run.main(["--fixtures", str(fixtures), "--jobs", "0", "--out", str(out),
                           "--quiet"]) == 0
    others = [p for p in tmp_path.glob("out.*.json")]
    assert not out.exists() and len(others) == 1
    assert json.loads(others[0].read_text(encoding="utf-8"))["summary"]["scored"] == 1
    assert "instead" in capsys.readouterr().out


def test_the_output_records_the_code_the_options_and_the_summary(tmp_path, capsys):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _routed_board().save(fixtures / "one.json.gz")
    out = tmp_path / "out.json"
    assert bench_run.main(["--fixtures", str(fixtures), "--jobs", "0", "--out", str(out),
                           "--label", "unit", "--quiet"]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["label"] == "unit" and data["mode"] == "route"
    assert data["code"]["source_hash"] == data["code"]["source_hash_end"]
    assert data["code"]["changed_during_run"] is False
    assert data["options"]["stages_resolved"]["bends"] in (True, False)
    assert list(data["boards"]) == ["one.json.gz"]
    assert data["summary"]["routing"]["fully_routed"] == 1
    assert "fully routed 1/1" in capsys.readouterr().out
