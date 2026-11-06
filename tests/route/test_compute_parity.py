# SPDX-License-Identifier: Apache-2.0
"""The Rust A* and the Python A* give the same answer.

``EDA_AGENT_COMPUTE=rust`` moves the grid search of
:mod:`eda_agent.route.router` into the optional eda-agent-compute wheel
(rust/eda_agent_compute). That is only an option worth having if it
changes nothing but the time, so these tests hold it to the Python
reference: the same path, the same budget verdict, the same expansion
count, call by call, and the same whole routing result, byte for byte.

They skip when the wheel is not installed, except where
EDA_AGENT_REQUIRE_RUST=1 (the CI job that builds it), where a missing
wheel fails: a skip there would be a green run that compared nothing.
"""

from __future__ import annotations

import json
import os
import random

import pytest

from eda_agent import compute
from eda_agent.route import RouterOptions, RoutingProblem
from eda_agent.route.model import NOBODY, DenseGrid
from eda_agent.route import router
from eda_agent.route.router import route_geometry, route_problem

from . import test_scale as scale

_REQUIRED = os.environ.get("EDA_AGENT_REQUIRE_RUST") == "1"


def _wheel():
    compute._probe = None
    mod, why = compute._load()
    if mod is None:
        if _REQUIRED:
            pytest.fail(f"EDA_AGENT_REQUIRE_RUST=1 but {why}")
        pytest.skip(why)
    return mod


@pytest.fixture
def mode(monkeypatch):
    def _set(m: str) -> None:
        monkeypatch.setenv(compute.ENV_VAR, m)
    return _set


def _dump(result: dict) -> str:
    result = json.loads(json.dumps(result, default=str))
    result["summary"].pop("compute", None)
    return json.dumps(result, sort_keys=True)


# ---------------------------------------------------------------------------
# Boards
# ---------------------------------------------------------------------------

RULES2 = {"clearance_mils": 6, "track_width_mils": 8, "via_size_mils": 24,
          "via_drill_mils": 12, "layers": ["TopLayer", "BottomLayer"]}


def _random_board(seed: int, n_nets: int = 14, size: int = 1200) -> dict:
    """Two- and three-pin nets scattered over a small board, some through
    hole, plus a few foreign tracks and unnetted keepout-like copper so
    every owner kind (net, None, two nets) shows up in the maps."""
    rnd = random.Random(seed)
    pads, tracks, vias = [], [], []
    for n in range(n_nets):
        for _ in range(rnd.choice((2, 2, 3))):
            pads.append({
                "x": rnd.randrange(40, size - 40),
                "y": rnd.randrange(40, size - 40),
                "x_size": rnd.choice((16, 20, 30, 60)),
                "y_size": rnd.choice((16, 20, 30)),
                "rotation": rnd.choice((0, 0, 90, 45, 30)),
                "layer": rnd.choice(("TopLayer", "BottomLayer", "MultiLayer")),
                "net": f"N{n}",
            })
    for k in range(6):
        x, y = rnd.randrange(100, size - 200), rnd.randrange(100, size - 100)
        tracks.append({"x1": x, "y1": y, "x2": x + rnd.randrange(50, 200),
                       "y2": y, "width": 8,
                       "layer": rnd.choice(("TopLayer", "BottomLayer")),
                       "net": rnd.choice((f"N{k}", ""))})
    for k in range(3):
        vias.append({"x": rnd.randrange(100, size - 100),
                     "y": rnd.randrange(100, size - 100), "size": 24,
                     "net": f"N{k + 6}"})
    return {"bbox": {"x1": 0, "y1": 0, "x2": size, "y2": size},
            "counts": {"pads": len(pads), "tracks": len(tracks),
                       "vias": len(vias)},
            "pads": pads, "tracks": tracks, "vias": vias}


OPTION_SETS = [
    RouterOptions(),
    RouterOptions(allow_diagonal=True),
    RouterOptions(allow_via_in_pad=True, via_cost=3.0),
    RouterOptions(bend_penalty=0.0, via_cost=1.0),
    # A budget small enough that some connections run out: the verdict
    # and the count at which they stop must match too.
    RouterOptions(max_expansions=300),
]


# ---------------------------------------------------------------------------
# The mode switch
# ---------------------------------------------------------------------------


def test_python_is_the_default(monkeypatch):
    monkeypatch.delenv(compute.ENV_VAR, raising=False)
    assert compute.requested_mode() == "python"
    assert compute.native() is None
    assert compute.active() == "python"


def test_an_unknown_mode_is_an_error_not_python(mode):
    mode("rsut")
    with pytest.raises(ValueError, match="rsut"):
        compute.native()


def test_rust_without_the_wheel_refuses_and_auto_falls_back(mode, monkeypatch):
    monkeypatch.setattr(compute, "_probe", (None, "not installed (test)"))
    mode("rust")
    with pytest.raises(compute.ComputeUnavailable, match="not installed"):
        compute.native()
    st = compute.status()
    assert st["error"] and st["active"] == "none"
    mode("auto")
    assert compute.native() is None
    assert compute.active() == "python"


def test_a_wheel_of_another_api_version_counts_as_missing(monkeypatch):
    import sys
    import types
    fake = types.ModuleType("eda_agent_compute")
    fake.API_VERSION = compute.EXPECTED_API + 1
    fake.__version__ = "9.9.9"
    monkeypatch.setitem(sys.modules, "eda_agent_compute", fake)
    monkeypatch.setattr(compute, "_probe", None)
    mod, why = compute._load()
    assert mod is None and "API" in why
    monkeypatch.setattr(compute, "_probe", None)


def test_a_rust_run_fails_before_routing_anything(mode, monkeypatch):
    monkeypatch.setattr(compute, "_probe", (None, "not installed (test)"))
    mode("rust")
    problem = RoutingProblem.from_geometry(_random_board(1), RULES2,
                                           grid_pitch_mils=10)
    with pytest.raises(compute.ComputeUnavailable):
        route_problem(problem)
    # Nothing was routed: no net's copper was added to the maps.
    fresh = RoutingProblem.from_geometry(_random_board(1), RULES2,
                                         grid_pitch_mils=10)
    assert problem.blocked == fresh.blocked


def test_the_report_names_the_implementation(mode):
    mode("python")
    r = route_geometry(_random_board(2), RULES2, grid_pitch_mils=10)
    assert r["summary"]["compute"] == "python"


# ---------------------------------------------------------------------------
# The flat maps the kernel reads
# ---------------------------------------------------------------------------


def _passable_from_dense(d: DenseGrid, li, ix, iy, net) -> bool:
    if not (0 <= ix < d.nx and 0 <= iy < d.ny):
        return False
    v = d.blocked[li * d.nx * d.ny + ix * d.ny + iy]
    return v == -1 or v == d.net_ids.get(net, -3)


@pytest.mark.parametrize("seed", range(4))
def test_the_flat_map_agrees_with_the_dicts_cell_by_cell(seed):
    problem = RoutingProblem.from_geometry(_random_board(seed), RULES2,
                                           grid_pitch_mils=10)
    d = problem.dense_grid()
    nets = sorted(problem.terminals) + ["NOT_A_NET"]
    for li in range(len(problem.layers)):
        for ix in range(-1, problem.nx + 1):
            for iy in range(-1, problem.ny + 1, 3):
                for net in nets[::3]:
                    assert (_passable_from_dense(d, li, ix, iy, net)
                            == problem.passable(li, ix, iy, net)), \
                        (li, ix, iy, net)


def test_the_flat_map_kept_in_step_equals_one_built_afresh(mode):
    """Routing adds each net's copper to the maps while the flat view
    exists; every write must reach it, or the kernel routes through
    copper the Python search would avoid."""
    _wheel()
    mode("rust")
    problem = RoutingProblem.from_geometry(_random_board(5), RULES2,
                                           grid_pitch_mils=10)
    route_problem(problem)
    kept = problem._dense
    assert kept is not None
    fresh = DenseGrid.build(problem)
    # Net ids may be numbered differently; compare what they mean.
    names = {v: k for k, v in kept.net_ids.items()}
    names_f = {v: k for k, v in fresh.net_ids.items()}

    def meaning(arr, ids):
        return [ids.get(v, v) for v in arr]
    assert meaning(kept.blocked, names) == meaning(fresh.blocked, names_f)
    assert meaning(kept.via_blocked, names) == meaning(fresh.via_blocked,
                                                       names_f)
    assert kept.pad_any == fresh.pad_any
    assert NOBODY in kept.blocked


def test_a_direct_write_into_the_dicts_rebuilds_the_flat_map():
    problem = RoutingProblem.from_geometry(_random_board(3), RULES2,
                                           grid_pitch_mils=10)
    d1 = problem.dense_grid()
    cell = next((ix, iy) for ix in range(problem.nx) for iy in range(problem.ny)
                if (ix, iy) not in problem.blocked[0])
    problem.blocked[0][cell] = {None}
    d2 = problem.dense_grid()
    assert d2 is not d1
    assert not problem.passable(0, *cell, "N0")
    assert not _passable_from_dense(d2, 0, *cell, "N0")


# ---------------------------------------------------------------------------
# Call by call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("opt", OPTION_SETS, ids=lambda o: repr(o)[13:60])
@pytest.mark.parametrize("seed", range(3))
def test_each_search_returns_the_same_path_verdict_and_count(seed, opt, mode):
    native = _wheel()
    problem = RoutingProblem.from_geometry(_random_board(seed), RULES2,
                                           grid_pitch_mils=10)
    rnd = random.Random(seed)
    nets = sorted(problem.terminals)
    calls = 0
    for net in nets:
        terms = problem.terminals[net]
        for a in terms:
            for b in terms:
                if a is b:
                    continue
                starts = [(li, *a.cell) for li in a.layers]
                # A target set with stray far cells too, so the heuristic
                # box is not just the goal.
                targets = {(li, *b.cell) for li in b.layers}
                if rnd.random() < 0.5:
                    targets.add((0, rnd.randrange(problem.nx),
                                 rnd.randrange(problem.ny)))
                mode("python")
                want = router._astar(problem, net, starts, targets, opt)
                mode("rust")
                got = router._astar(problem, net, starts, targets, opt)
                assert got == want, (net, starts, targets)
                calls += 1
    assert calls > 10
    del native


# ---------------------------------------------------------------------------
# Whole runs
# ---------------------------------------------------------------------------


def _both(geometry, rules, pitch, opt, mode) -> tuple[str, str]:
    out = []
    for m in ("python", "rust"):
        mode(m)
        problem = RoutingProblem.from_geometry(geometry, rules,
                                               grid_pitch_mils=pitch)
        r = route_problem(problem, opt)
        assert r["summary"]["compute"] == m
        out.append(_dump(r))
    return out[0], out[1]


@pytest.mark.parametrize("opt", OPTION_SETS, ids=lambda o: repr(o)[13:60])
@pytest.mark.parametrize("seed", range(4))
def test_a_whole_run_is_identical(seed, opt, mode):
    _wheel()
    want, got = _both(_random_board(seed), RULES2, 10, opt, mode)
    assert got == want


def test_the_dense_reported_board_is_identical(mode):
    """The 1090-pad board of test_scale at its default pitch: hundreds
    of nets, each routed one becoming an obstacle for the next."""
    _wheel()
    want, got = _both(scale._board(True), scale.RULES, 25,
                      RouterOptions(), mode)
    assert got == want
    assert json.loads(want)["summary"]["routed"] > 10


def test_the_comparison_can_tell_a_different_answer(mode):
    """A comparison that every answer passes proves nothing. Change one
    knob on the Rust side only and the whole-run comparison must see it."""
    _wheel()
    geometry = _random_board(0)
    mode("python")
    want = _dump(route_geometry(geometry, RULES2, grid_pitch_mils=10))
    mode("rust")
    got = _dump(route_problem(
        RoutingProblem.from_geometry(geometry, RULES2, grid_pitch_mils=10),
        RouterOptions(bend_penalty=0.0)))
    assert got != want
