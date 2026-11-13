# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A routing run as a background job: board in, placeable copper out.

Pure: no bridge. The board comes from the live read (``read_altium``) or
a fixture; what comes back is the copper the router adds, in the item
shapes ``pcb_place_tracks`` and ``pcb_place_vias`` take, plus the exact
DRC's verdict on the routed board. Nothing is applied here.
"""

from __future__ import annotations

import math
import numbers
import time
from typing import Any

from ..bench import copy_board
from ..drc import usual_via_mask, verdict
from ..model import LayoutBoard
from . import engine as _engine
from .router import route_adaptive
from .stages import finish_report


def route_job(params: dict[str, Any]) -> dict[str, Any]:
    board = params.get("board")
    if isinstance(board, dict):
        board = LayoutBoard.from_dict(board)
    if not isinstance(board, LayoutBoard):
        raise ValueError("layout_route job requires a 'board' (LayoutBoard or its dict)")
    nets = params.get("nets")
    # The engine is the user's setting (route/engine.py: EDA_AGENT_ROUTER,
    # then the profile's router.json), never the call's: `eda-agent
    # router-use euan` turns Euan's router on, nothing else does.
    setting = _engine.describe()
    which = setting["engine"]
    engine_notes: list[str] = []
    if setting["problem"]:
        engine_notes.append(f"The router setting was ignored ({setting['problem']}), so the "
                            "default engine routed.")
    stages_in, centre_asked = _pad_vias(params.get("stages"), which)
    stages_in = _check_stage_numbers(stages_in)
    euan_args: dict[str, Any] = {}
    if which == "euan":
        # Before anything runs: a bad switch fails the job as it does with
        # the default engine, rather than routing without it.
        euan_args, notes = euan_kwargs(stages_in, board, params)
        engine_notes += notes
    else:
        ignored = sorted(k for k in EUAN_ONLY_PARAMS if params.get(k) is not None)
        if ignored:
            engine_notes.append(", ".join(ignored) + " only steer the euan engine; the "
                                "default engine ignored them.")
    # Euan's job matched old pours by net as well (route_euan, 2026-10-18).
    board, old_pours = _without_old_pours(board, by_net=which == "euan")
    # The live view (design/live.py): the board before, progress while
    # routing (throttled), the routed board after.
    from ...design import live
    live.publish_safe(board, f"Routing {len(nets) if nets else 'all'} nets.", "route")
    t0 = time.perf_counter()
    if which == "euan":
        # The alternative router (route_euan). Its own gloss stays off, as
        # in Euan's job: it judges re-laid tracks by one clearance and
        # never sees keep-outs, cutouts or the outline. Ours runs below,
        # for both engines.
        from ..route_euan.router import route_adaptive as euan_adaptive
        from ..route_euan.stages import finish_report as euan_finish
        r = euan_adaptive(board, nets=frozenset(nets) if nets else None,
                          planes=bool(params.get("planes", True)), gloss=False,
                          log=live.progress_logger("route"), **euan_args)
        report_of = euan_finish
    else:
        r = route_adaptive(board, nets=frozenset(nets) if nets else None,
                           planes=bool(params.get("planes", True)),
                           stages=stages_in,
                           log=live.progress_logger("route"))
        report_of = finish_report
        if centre_asked is not None:
            # Written now, from the attempt kept: its fallback may have
            # widened "bga" to "ic".
            engine_notes.append(_pad_vias_note(centre_asked, r.options.pad_vias))
    out = r.apply()
    # The router's own tracks laid again as the shortest clean 45/90 runs;
    # the board's tracks before it stay as they are (gloss.py).
    glossed = None
    if params.get("gloss", True):
        from .gloss import gloss
        glossed = gloss(out, tracks=_added(board.tracks, out.tracks)).as_dict()
    new_tracks = _added(board.tracks, out.tracks)
    added_vias = _added(board.vias, out.vias)
    new_vias = _unique_vias(board.vias, added_vias)
    mask_note = _mask_like_board(board, added_vias)
    # Judged as written: a via on one already there is never sent, and
    # counted it is a hole drilled twice that no board will have. And what
    # is sent is the board's copper and these: a track of the board the
    # router took up would not be taken up on the board.
    lost = _lost(board, out)
    out.tracks = list(board.tracks) + new_tracks
    out.vias = list(board.vias) + new_vias
    v = verdict(out)
    drc = v["report"]
    stages = report_of(r, out)
    pub = live.publish_safe(out, f"Routed: {len(drc.unrouted)} nets still open, "
                      f"{len(drc.violations)} clearance violations, "
                      f"{v['counts']['mask_sliver']} mask slivers.", "route")
    rep = r.report
    wanted = set(nets) if nets else None
    unrouted = {n: k for n, k in drc.unrouted.items() if wanted is None or n in wanted}
    return {
        "board": board.name,
        "router": which,
        # pcb_autoroute_apply republishes this version once it is written.
        "live_version": (pub or {}).get("version"),
        "summary": {
            "completion": round(drc.completion, 4),
            "missing_connections": drc.missing_connections,
            "unrouted_nets": len(unrouted),
            # pass, fail or incomplete: see drc.verdict. Zero violations
            # with a rule left unevaluated is incomplete, not a pass.
            "drc_status": v["status"],
            "violations": len(v["violations"]),
            "mask_slivers": v["counts"]["mask_sliver"],
            "unevaluated_rules": len(v["unevaluated_rules"]),
            "tracks": len(new_tracks),
            "vias": len(new_vias),
            "routing_layers": list(r.grid.layers),
            "planes": sorted({f"{p.layer}:{p.net}" for p in r.pour_planes}),
            "pours": sorted({f"{r.grid.layers[p.li]}:{p.net}" for p in r.pours}),
            "released_planes": sorted(r.released),
            "old_pours": old_pours,
            "iterations": rep.iterations,
            "seconds": round(time.perf_counter() - t0, 1),
            "sharp_bends": stages["sharp_bends"],
            "unreached_pads": len(stages["unreached"]),
            "vias_in_pads": len(stages["via_in_pad"]),
            "gloss": glossed,
        },
        "unrouted": unrouted,
        # What the planned stages did (see stages.py): every via in a pad,
        # each pair and bus, and every pad left unreached with its place
        # and the nets in its way.
        "via_in_pad": stages["via_in_pad"],
        "pairs": stages["pairs"],
        "buses": stages["buses"],
        "unreached": stages["unreached"],
        "fanout": stages["fanout"],
        "pin_lanes": stages["pin_lanes"],
        "fallback": stages["fallback"],
        "violations": [x.as_dict() for x in v["violations"][:50]],
        "unevaluated_rules": v["unevaluated_rules"],
        "not_checked": v["not_checked"],
        "tracks": [{"x1": t.x1, "y1": t.y1, "x2": t.x2, "y2": t.y2, "width": t.width,
                    "layer": t.layer, "net_name": t.net} for t in new_tracks],
        "vias": [{"x": v.x, "y": v.y, "size": v.diameter, "hole_size": v.hole,
                  "low_layer": v.low_layer, "high_layer": v.high_layer, "net": v.net}
                 for v in new_vias],
        "notes": (engine_notes + _notes(r) + _stage_notes(stages) + v["notes"] + mask_note
                  + lost),
    }


#: Stage switches that are Router arguments in the euan engine.
_EUAN_ROUTER_ARGS = ("neck_pads", "join_existing", "exclude_layers", "reuse_vias")

#: Job params only Euan's router takes (as his own job did): the grid
#: pitch in mils, a preferred direction per layer and its strength, and
#: the cost of the outer layers.
EUAN_ONLY_PARAMS = ("pitch", "layer_dirs", "dir_strength", "outer_cost")

#: The range each euan-only number is taken in; outside it the job fails
#: before anything runs. Below -1, dir_strength makes a step across a
#: layer's direction cost less than nothing in his global router, whose
#: tile graph clamps no cost, and scipy's Dijkstra then ran the process
#: out of memory and aborted it: the MCP server, which no job error
#: catches. Between -1 and 0 it only turns the direction round, which
#: layer_dirs says plainly. At an outer_cost of 0 or below a step on an
#: outer layer would cost nothing. A pitch finer than the 1.5 mil his
#: router ever picks itself grows the grid with the inverse square (0.25
#: mil took 950 MB on a 15 x 10 mm board).
EUAN_RANGES = {"pitch": (1.5, 25.0), "dir_strength": (0.0, 10.0), "outer_cost": (0.1, 100.0)}


def _euan_param(key: str, value: Any, board: LayoutBoard | None) -> Any:
    """A euan-only job param as his router takes it, None for pitch 0
    (his router's own pitch), or ValueError."""
    if key == "layer_dirs":
        if not isinstance(value, dict):
            raise ValueError("layer_dirs takes each layer's name to 'h' or 'v'")
        dirs = {}
        for layer, way in value.items():
            word = str(way).strip().lower()
            if word not in ("h", "v", "horizontal", "vertical"):
                raise ValueError(f"layer_dirs gives {layer!r} {way!r}; it takes 'h' or 'v'")
            dirs[str(layer)] = word[0]
        unknown = set(dirs) - set(board.copper_layers()) if board is not None else set()
        if unknown:
            raise ValueError(f"layer_dirs names no copper layer: {sorted(unknown)}")
        return dirs
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} takes a number, not {value!r}") from None
    if key == "pitch" and number == 0:
        return None
    lo, hi = EUAN_RANGES[key]
    if not lo <= number <= hi:                  # NaN fails this as well
        span = (f"{lo:g} to {hi:g} mil, or 0 for the router's own" if key == "pitch"
                else f"{lo:g} to {hi:g}")
        raise ValueError(f"{key} takes {span}, not {value!r}")
    return number


#: The numbers the stage options take, the same for both engines; outside
#: its range a value fails the job before anything runs. Unchecked, a
#: negative lane_cost gave scipy's Dijkstra negative weights, which ran
#: the server process out of memory and aborted it, and a lane_depth of
#: 1e7 mil walked each fine-pitch pin's lane back half a cell at a time
#: for hours. A lane costs at least an open cell (inf makes it a wall),
#: and a bus is two nets or more.
STAGE_RANGES = {"pair_skew": (0.0, 1000.0), "pair_width": (1.0, 1000.0),
                "pair_gap": (1.0, 1000.0), "lane_depth": (0.0, 1000.0),
                "fine_pitch": (1.0, 1000.0), "lane_cost": (1.0, math.inf),
                "bus_min": (2, 10000)}


def _check_stage_numbers(stages: Any) -> Any:
    """``stages`` with each numeric stage option as the float the engines
    compute with (bus_min as an int), in the form it came in; ValueError
    for one out of STAGE_RANGES, not a number at all, or too large for a
    float. pair_width and pair_gap may be None (the board's rule).

    Passed on as given, a lane_cost of 10**400 was in range and then
    overflowed in the router's cost arrays, and a Fraction made them
    arrays of Python objects."""
    from dataclasses import asdict, is_dataclass, replace

    given = stages if is_dataclass(stages) and not isinstance(stages, type) else None
    if given is not None:
        stages = asdict(given)
    if not isinstance(stages, dict):
        return stages
    fixed: dict[str, Any] = {}
    for key, (lo, hi) in STAGE_RANGES.items():
        if key not in stages or (stages[key] is None and key in ("pair_width", "pair_gap")):
            continue
        value = stages[key]
        whole = key == "bus_min"
        if isinstance(value, bool) or not isinstance(value, numbers.Real):
            raise ValueError(f"{key} takes {'a whole number' if whole else 'a number'}, "
                             f"not {value!r}")
        # Compared exactly first, so a huge int or Fraction meets the range
        # message rather than an overflow.
        if not lo <= value <= hi:                  # NaN fails this as well
            raise ValueError(f"{key} takes {lo:g} to {hi:g}, not {value!r}")
        if whole and not (isinstance(value, numbers.Integral) or float(value).is_integer()):
            raise ValueError(f"{key} takes a whole number, not {value!r}")
        try:
            fixed[key] = int(value) if whole else float(value)
        except OverflowError:                       # only lane_cost reaches inf
            raise ValueError(f"{key} takes {lo:g} to {hi:g}, not a number too large to "
                             "compute with (inf makes each pin lane a wall)") from None
    return replace(given, **fixed) if given is not None else {**stages, **fixed}


#: Where a via may go into a pad (the stage option pad_vias), by engine.
PAD_VIAS = {"default": ("bga", "ic", "any"), "euan": ("bga", "ic", "any", "centre")}


def _pad_vias(stages: Any, which: str) -> tuple[Any, Any]:
    """``stages`` with its pad_vias as the engine that routes takes it, and
    the value asked for when that engine cannot take it (else None). A
    value neither engine knows fails the job before anything runs. The
    euan engine's "centre" is "bga" to the default engine, its own
    default; route_job's notes say so (_pad_vias_note): routed as "bga" in
    silence, the person was never told that what they asked for was not
    applied. Options given as a StageOptions object are checked the same.
    """
    from dataclasses import asdict, is_dataclass

    if is_dataclass(stages) and not isinstance(stages, type):
        stages = asdict(stages)
    if not isinstance(stages, dict) or "pad_vias" not in stages:
        return stages, None
    raw = stages["pad_vias"]
    if raw is None:                 # not given: the engine's own default
        return {k: v for k, v in stages.items() if k != "pad_vias"}, None
    word = str(raw).strip().lower()
    word = "centre" if word == "center" else word
    if word not in PAD_VIAS["euan"]:
        raise ValueError("pad_vias takes 'bga', 'ic' or 'any', or 'centre' with the euan "
                         f"engine; not {raw!r}")
    if word not in PAD_VIAS.get(which, PAD_VIAS["default"]):
        return {**stages, "pad_vias": "bga"}, raw
    return {**stages, "pad_vias": word}, None


def _pad_vias_note(asked: Any, routed: str) -> str:
    """What the default engine did with a pad_vias only Euan's takes:
    "bga", or "ic" where its fallback widened that (route_adaptive)."""
    did = ("a via only in a BGA's ball or an exposed pad" if routed == "bga" else
           "its fallback having widened 'bga' to IC pins, where a via may stand anywhere "
           "in the pin" if routed == "ic" else "")
    return (f"pad_vias={asked!r} only steers the euan engine; the default engine routed "
            f"with {routed!r}" + (f", {did}." if did else "."))


def euan_kwargs(stages: Any, board: LayoutBoard | None = None,
                params: dict | None = None) -> tuple[dict[str, Any], list[str]]:
    """Our ``stages`` value (and the job's own euan-only params) as the
    euan engine's arguments, and notes.

    That router takes neck_pads, join_existing, exclude_layers and
    reuse_vias as Router arguments rather than stage options, and
    axis_entry as ``pad_entry="axis"``. A stage option neither router
    knows fails as it does with the default engine; with ``board``, so
    does an exclude_layers entry that names no copper layer or one a
    net's surface pad is on.
    """
    from dataclasses import asdict, is_dataclass

    from ..route_euan.stages import StageOptions as EuanStageOptions
    from .stages import StageOptions

    notes = ["Routed with the euan engine."]
    if is_dataclass(stages) and not isinstance(stages, type):
        stages = asdict(stages)
    kwargs: dict[str, Any] = {}
    for key in EUAN_ONLY_PARAMS:
        value = (params or {}).get(key)
        if value is not None:
            value = _euan_param(key, value, board)
            if value is not None:
                kwargs[key] = value
    if not isinstance(stages, dict):
        kwargs["stages"] = stages
        return kwargs, notes
    stages = dict(stages)
    unknown = set(stages) - set(StageOptions.__dataclass_fields__)
    if unknown:
        raise ValueError(f"unknown stage options: {', '.join(sorted(unknown))}")
    for key in _EUAN_ROUTER_ARGS:
        if key in stages:
            value = stages.pop(key)
            if key == "exclude_layers":
                value = frozenset((value,) if isinstance(value, str) else value or ())
                if board is not None:
                    _check_excluded(board, value)
            kwargs[key] = value if key == "exclude_layers" else bool(value)
    if stages.pop("axis_entry", False):
        kwargs["pad_entry"] = "axis"
    left = set(stages) - set(EuanStageOptions.__dataclass_fields__)
    if left:                      # every other option of ours is one of his
        raise ValueError(f"the euan engine has no stage option {', '.join(sorted(left))}")
    kwargs["stages"] = stages
    if kwargs.get("neck_pads"):
        notes.append("neck_pads in the euan engine ends the net's full-width track on "
                     "the narrow pad's copper and necks down only for the last mils; "
                     "the default engine stops it clear of the pad.")
    return kwargs, notes


def _check_excluded(board: LayoutBoard, excluded: frozenset) -> None:
    """The default router's own check (route/router.py) for the euan one."""
    unknown = excluded - set(board.copper_layers())
    if unknown:
        raise ValueError(f"exclude_layers names no copper layer: {sorted(unknown)}")
    padded = {c.layer for p in board.pads if p.net and p.is_smd for c in p.copper}
    if excluded & padded:
        raise ValueError("exclude_layers takes a layer no net's surface pad is on; "
                         f"these carry some: {sorted(excluded & padded)}")


def _key(x) -> tuple:
    """What a track or via is, for telling the board's own from new ones."""
    if hasattr(x, "x1"):
        return ("t", x.layer, round(x.x1, 4), round(x.y1, 4), round(x.x2, 4), round(x.y2, 4),
                round(x.width, 4), x.net)
    return ("v", round(x.x, 4), round(x.y, 4), round(x.diameter, 4), x.net)


def _added(before: list, after: list) -> list:
    """The items of ``after`` that ``before`` does not hold, counted: the
    board's own list as a prefix in the usual case, matched by what each
    item is when a pass took one of them up."""
    n = len(before)
    if len(after) >= n and all(a is b or _key(a) == _key(b) for a, b in zip(before, after)):
        return list(after[n:])
    from collections import Counter
    left = Counter(_key(x) for x in before)
    out = []
    for x in after:
        k = _key(x)
        if left[k]:
            left[k] -= 1
        else:
            out.append(x)
    return out


def _lost(board: LayoutBoard, out: LayoutBoard) -> list[str]:
    """A note when the routed board lacks tracks or vias the board had:
    nothing is ever taken up on the board, so it is judged with them."""
    gone = len(_added(out.tracks, board.tracks)) + len(_added(out.vias, board.vias))
    if not gone:
        return []
    return [f"The router took up {gone} of the board's own tracks or vias; nothing on the "
            "board is removed, so the result was judged with them in place."]


def _stage_notes(stages: dict) -> list[str]:
    notes = []
    if stages["fallback"]:
        notes.append("The planned stages left connections unmade, so the board was "
                     + stages["fallback"] + ".")
    if stages["via_in_pad"]:
        notes.append(f"{len(stages['via_in_pad'])} vias sit in pads (listed in via_in_pad, "
                     "each with its kind): the fabrication notes must ask for them filled "
                     "and capped.")
    if stages["unreached"]:
        notes.append(f"{len(stages['unreached'])} pads could not be reached; unreached gives "
                     "each one's place and the nets in its way.")
    return notes


def _mask_like_board(board: LayoutBoard, new) -> list[str]:
    """Give the router's vias the solder mask most of the board's vias have.

    A via placed later takes its tenting and expansion from Altium, not
    from the router, so the sliver check judges new vias like the board's
    own. With no vias to go by they stay open, which can only find more
    slivers, never fewer.
    """
    usual = usual_via_mask(board)
    if not new or usual is None:
        return []
    if not [x for x in board.vias if not x.comp]:
        return ["New vias were judged with open solder mask on both sides: the board "
                "has no vias of its own to say whether vias are tented."]
    tt, tb, fh, manual = usual
    for x in new:
        x.tent_top, x.tent_bottom, x.mask_from_hole = tt, tb, fh
        x.mask_manual = list(manual) if manual else None
    sides = {(True, True): "tented on both sides", (False, False): "open on both sides",
             (True, False): "tented on top only", (False, True): "tented on the bottom only"}
    return [f"New vias were judged {sides[(tt, tb)]}, like most of the board's vias; check "
            "the mask of the placed vias if Altium's via defaults differ."]


def _unique_vias(existing, new) -> list:
    """The new vias less any that repeat one already there or listed.

    A second (plane fan-out) run on a live take left two vias on one
    spot, which Altium then reported as a short to itself. Not
    reproduced; a via is never wanted twice in the same place, so the
    list sent to the board holds each once.
    """
    seen = {(round(v.x, 3), round(v.y, 3), v.net) for v in existing}
    out = []
    for v in new:
        key = (round(v.x, 3), round(v.y, 3), v.net)
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _without_old_pours(board: LayoutBoard,
                       by_net: bool = False) -> tuple[LayoutBoard, list[str]]:
    """The board without its polygons' poured copper.

    A polygon's copper is where its last pour left it, round routing that
    may since have been taken up or moved. The router pours from the
    polygon's outline and the apply step repours, so the old copper is not
    something new routing has to clear. Kept, it was: on a public board,
    live, un-routed and re-placed, new vias found room on the inner layers only
    in the old vias' voids: 137 s and 76 violations. Without it the same
    board routed in 8 s with none.
    """
    outlines = {(r.layer, r.source) for r in board.regions if r.kind == "pour_boundary"}
    # ``by_net`` (the euan engine, as Euan's job did): also a pour whose
    # source differs from its boundary's, on the boundary's layer and net.
    nets = {(r.layer, r.net) for r in board.regions
            if by_net and r.kind == "pour_boundary" and r.net}
    old = [r for r in board.regions if r.kind == "pour"
           and ((r.layer, r.source) in outlines or (r.layer, r.net) in nets)]
    if not old:
        return board, []
    out = copy_board(board)
    out.regions = [r for r in board.regions if not any(r is o for o in old)]
    return out, sorted({f"{r.layer}:{r.net}" for r in old})


def _notes(r) -> list[str]:
    notes = []
    if r.pour_planes:
        notes.append(
            "The routes rely on these inner-layer pours as planes: "
            + ", ".join(sorted({f"{p.layer} ({p.net})" for p in r.pour_planes}))
            + ". Nothing was routed on them. Repour the polygons after "
              "applying (pcb_repour_polygons) so they clear the new vias.")
    if r.pours:
        notes.append(
            "These nets are joined by the board's own pours, poured round the "
            "routes; their tracks the pours make redundant were taken up: "
            + ", ".join(sorted({f"{r.grid.layers[p.li]} ({p.net})" for p in r.pours}))
            + ". Their connections hold once the polygons are repoured "
              "(pcb_autoroute_apply does it, or pcb_repour_polygons).")
    return notes
