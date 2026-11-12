# SPDX-License-Identifier: Apache-2.0
"""Incremental SA objective: bit-identical to the full-board ``score``.

The polish scores each move with ``_IncrementalObjective``, which re-scores
only the nets / parts a move touches. These tests pin down that it is an
optimisation only: every evaluation equals ``score(...).weighted_total``
exactly (``==``, not approx) over thousands of random moves and every move
kind, and a seeded ``construct_placement`` returns the very same placement
with incremental scoring on and off.
"""

from __future__ import annotations

import math
import random
from dataclasses import replace

import numpy as np
import pytest

from eda_agent.design.pcb_placement import (
    ConstructOptions,
    DesignRules,
    ObjectiveWeights,
    _IncrementalObjective,
    _ORIENTATIONS,
    _apply_random_move,
    _match_group,
    _pair_decaps_to_power_pins,
    _restore,
    _seq_sum,
    _snapshot,
    construct_placement,
    construct_placement_best_of,
    score,
    size_board,
)
from eda_agent.placement import PlaceComp, PlaceNet, PlacePin


def _rich_board(seed: int = 0, n_pass: int = 24):
    """A board exercising every objective term: ICs with power pins and
    decaps, edge connectors (banded and unbanded), a pinless part, hot parts
    (thermal), two keep-out domains (separation), a matched group with roles
    (match / axis / centroid), flippable parts (via), and a net naming a ref
    that is not on the board."""
    rnd = random.Random(seed)
    comps: list[PlaceComp] = []
    nets: dict[str, list[str]] = {}

    def add(net, ref):
        nets.setdefault(net, []).append(ref)

    for k in range(3):
        ref = f"U{k + 1}"
        pins = []
        for i in range(6):
            off = -100 + i * 40
            for side, x in (("L", -160), ("R", 160)):
                nm = f"{ref}_{side}{i}"
                pins.append(PlacePin(x, off, nm))
                add(nm, ref)
        pins += [PlacePin(-160, 150, "VCC"), PlacePin(160, 150, "GND")]
        add("VCC", ref)
        add("GND", ref)
        c = PlaceComp(ref=ref, w=320, h=340, cx=rnd.uniform(-900, 900),
                      cy=rnd.uniform(-900, 900), rotatable=True,
                      pins=tuple(pins))
        c.role = "ic"
        c.flippable = True
        c.power_w = 1.5 + k
        c.keepout_group = "digital" if k < 2 else "analog"
        comps.append(c)

    signal = [n for n in nets if n not in ("VCC", "GND")]
    for i in range(n_pass):
        ref = f"R{i + 1}"
        if i % 5 == 0:
            a, b = "VCC", "GND"
        else:
            a, b = rnd.choice(signal), rnd.choice(signal + ["GND"])
        c = PlaceComp(ref=ref, w=40 + 20 * (i % 3), h=40,
                      cx=rnd.uniform(-900, 900), cy=rnd.uniform(-900, 900),
                      rotatable=True,
                      pins=(PlacePin(-20, 0, a), PlacePin(20, 0, b)))
        c.flippable = i % 2 == 0
        if i < 4:
            c.match_group = "diff"
            c.match_role = "A" if i % 2 else "B"
        if i in (5, 6):
            c.keepout_group = "analog"
        add(a, ref)
        add(b, ref)
        comps.append(c)

    j1 = PlaceComp(ref="J1", w=100, h=300, cx=-900, cy=0, rotatable=False,
                   pins=(PlacePin(0, 50, "VCC"), PlacePin(0, -50, "GND")))
    j1.edge = True
    j1.edge_band = "L"
    j2 = PlaceComp(ref="J2", w=300, h=100, cx=0, cy=900, rotatable=False,
                   pins=(PlacePin(50, 0, signal[0]), PlacePin(-50, 0, "GND")))
    j2.role = "connector"   # no band: nearest-edge pull
    mh = PlaceComp(ref="MH1", w=120, h=120, cx=500, cy=-500,
                   rotatable=False, pins=())
    for c, nm in ((j1, "VCC"), (j1, "GND"), (j2, signal[0]), (j2, "GND")):
        add(nm, c.ref)
    add(signal[1], "MH1")       # pinless part on a net: centroid fallback
    add(signal[2], "NOT_PLACED")
    comps += [j1, j2, mh]
    return comps, [PlaceNet(refs=tuple(v), name=k) for k, v in nets.items()]


def _setup(comps, nets, layers=2):
    rules = DesignRules(layers=layers)
    region = size_board(comps, nets, rules)
    pos = {c.ref: [c.cx, c.cy] for c in comps}
    rotations = {c.ref: c.rotation for c in comps}
    sides = {c.ref: 1 for c in comps}
    return rules, region, pos, rotations, sides


_TERMS = ("hpwl", "via", "cong", "clear", "edge", "decap", "conn", "therm",
          "sep", "match", "match_axis", "match_centroid")


def _full(comps, pos, rotations, sides, nets, region, rules, pairs):
    return score(comps, pos, rotations, sides, nets, region, rules,
                 ObjectiveWeights(), decap_pairs=pairs)


def _check(inc, comps, pos, rotations, sides, nets, region, rules, pairs,
           where=""):
    """Every unweighted term AND the weighted total must be bit-identical
    (a 1-ulp slip in a small term can vanish inside a large total, so the
    total alone is not a sharp enough check)."""
    got = inc.evaluate()
    want = _full(comps, pos, rotations, sides, nets, region, rules, pairs)
    for name in _TERMS:
        assert inc.last_terms[name] == getattr(want, name), (
            f"{where} term {name}: {inc.last_terms[name]!r} != "
            f"{getattr(want, name)!r}")
    assert got == want.weighted_total, f"{where}: {got!r} != {want!r}"
    return want


def _arbitrary_move(comps, pos, rotations, sides, region, rnd):
    """A wilder move than the polish makes: any part, any kind, possibly
    off-board or overlapping, possibly several parts at once."""
    for c in rnd.sample(comps, rnd.choice((1, 1, 2, 3))):
        kind = rnd.choice(("jump", "rot", "flip", "all"))
        if kind in ("jump", "all"):
            pos[c.ref][0] = rnd.uniform(region.x1 - 200, region.x2 + 200)
            pos[c.ref][1] = rnd.uniform(region.y1 - 200, region.y2 + 200)
        if kind in ("rot", "all"):
            rotations[c.ref] = rnd.choice(_ORIENTATIONS)
        if kind in ("flip", "all"):
            sides[c.ref] = -sides.get(c.ref, 1)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_incremental_equals_full_score_over_random_moves(seed):
    comps, nets = _rich_board(seed)
    rules, region, pos, rotations, sides = _setup(comps, nets)
    pairs = _pair_decaps_to_power_pins(comps, nets)
    assert pairs, "fixture must exercise the decap term"
    inc = _IncrementalObjective(comps, pos, rotations, sides, nets, region,
                                rules, ObjectiveWeights(), pairs)
    rng = np.random.default_rng(seed)
    rnd = random.Random(seed)
    movable = [c for c in comps if not c.fixed]
    by_ref = {c.ref: c for c in comps}
    match_groups: dict[str, list[str]] = {}
    for c in comps:
        if _match_group(c):
            match_groups.setdefault(_match_group(c), []).append(c.ref)
    member_to_group = {r: g for g, rs in match_groups.items() for r in rs}
    opts = ConstructOptions()

    # The base state itself.
    base = _check(inc, comps, pos, rotations, sides, nets, region, rules,
                  pairs, "base")
    # The fixture must make every term live, or its check proves nothing.
    # (via and match_axis start at zero: every part starts on top at 0 deg.)
    live = {name for name in _TERMS if getattr(base, name) != 0.0}
    for step in range(1500):
        snap = _snapshot(pos, rotations, sides)
        if step % 3 == 0:
            _arbitrary_move(comps, pos, rotations, sides, region, rnd)
        else:
            _apply_random_move(movable, by_ref, pos, rotations, sides, region,
                               rules, rules.grid, pairs, True, opts, rng,
                               match_groups, member_to_group)
        want = _check(inc, comps, pos, rotations, sides, nets, region,
                      rules, pairs, f"step {step}")
        live |= {name for name in _TERMS if getattr(want, name) != 0.0}
        if rnd.random() < 0.6:
            inc.commit()
        else:
            _restore(pos, rotations, sides, snap)
    assert live == set(_TERMS), set(_TERMS) - live
    # And the state it ended on, from a fresh rebuild.
    fresh = _IncrementalObjective(comps, pos, rotations, sides, nets, region,
                                  rules, ObjectiveWeights(), pairs)
    _check(fresh, comps, pos, rotations, sides, nets, region, rules, pairs,
           "fresh")
    _check(inc, comps, pos, rotations, sides, nets, region, rules, pairs,
           "end")


def test_unchanged_state_returns_same_total():
    comps, nets = _rich_board(3)
    rules, region, pos, rotations, sides = _setup(comps, nets)
    pairs = _pair_decaps_to_power_pins(comps, nets)
    inc = _IncrementalObjective(comps, pos, rotations, sides, nets, region,
                                rules, ObjectiveWeights(), pairs)
    a = inc.evaluate()
    assert inc.changed_refs() == []
    assert inc.evaluate() == a


def test_seq_sum_matches_plain_loop_not_compensated_sum():
    # builtins.sum compensates float error on Python >= 3.12; the scorer
    # must add left to right like the full-board loops do.
    vals = [1e16, 1.0, -1e16, 1.0] * 3
    total = 0.0
    for v in vals:
        total += v
    assert _seq_sum(vals) == total
    assert _seq_sum(iter(vals)) == total


def _result_key(res):
    return (
        res.region,
        sorted(res.centroids.items()),
        sorted(res.rotations.items()),
        sorted(res.sides.items()),
        res.report.as_dict(),
        res.report.legal,
        res.accepted,
        res.rejected,
        res.notes,
    )


@pytest.mark.parametrize("seed", [0, 1])
@pytest.mark.parametrize("mode", ["greedy", "spectral"])
def test_construct_placement_identical_with_and_without_incremental(seed,
                                                                   mode):
    comps, nets = _rich_board(seed, n_pass=16)
    rules = DesignRules(layers=2)
    base = ConstructOptions(seed=seed, seed_mode=mode, max_moves=3000)
    on = construct_placement(comps, nets, rules, base)
    off = construct_placement(comps, nets, rules,
                              replace(base, incremental_score=False))
    assert on.accepted + on.rejected > 0
    assert _result_key(on) == _result_key(off)


def test_best_of_identical_with_and_without_incremental():
    comps, nets = _rich_board(4, n_pass=10)
    rules = DesignRules(layers=1)
    base = ConstructOptions(max_moves=1500)
    on = construct_placement_best_of(comps, nets, rules, seeds=(0, 1),
                                     base_opts=base)
    off = construct_placement_best_of(
        comps, nets, rules, seeds=(0, 1),
        base_opts=replace(base, incremental_score=False))
    assert _result_key(on) == _result_key(off)


def test_duplicate_net_names_fall_back_to_full_scoring():
    # score() collapses same-named nets by name; the incremental scorer does
    # not model that, so sa_polish must fall back and still match.
    comps, nets = _rich_board(5, n_pass=8)
    nets = nets + [PlaceNet(refs=("R1", "R2"), name=nets[0].name)]
    rules = DesignRules(layers=2)
    base = ConstructOptions(seed=0, max_moves=800)
    on = construct_placement(comps, nets, rules, base)
    off = construct_placement(comps, nets, rules,
                              replace(base, incremental_score=False))
    assert _result_key(on) == _result_key(off)


def test_incremental_polish_is_faster():
    """The point of the change: the polish's scoring is several times
    cheaper. Generous bound so a loaded CI machine does not flake."""
    import time

    comps, nets = _rich_board(0, n_pass=30)
    rules = DesignRules(layers=2)
    base = ConstructOptions(seed=0, max_moves=2500)

    def run(o):
        t = time.perf_counter()
        construct_placement(comps, nets, rules, o)
        return time.perf_counter() - t

    off = run(replace(base, incremental_score=False))
    on = run(base)
    assert on < off * 0.75, (on, off)
    assert math.isfinite(on)
