# SPDX-License-Identifier: Apache-2.0
"""The autorouter engine is a choice: the usual router unless the user
turns on Euan's (``eda-agent router-use euan`` or EDA_AGENT_ROUTER)."""
from __future__ import annotations

import json
import re

import pytest

from eda_agent.design import live
from eda_agent.layout.model import Component, Layer, LayoutBoard, Pad, PadCopper, Rule
from eda_agent.layout.route import engine
from eda_agent.layout.route.job import euan_kwargs, route_job

LAYERS = [Layer("TopLayer", "signal", 0), Layer("BottomLayer", "signal", 1)]
CLEARANCE = [Rule("Clearance", "0", "All", "All", 1, True, {"gap": 6.0},
                  "clearance")]


@pytest.fixture(autouse=True)
def _no_profile(monkeypatch, tmp_path):
    monkeypatch.delenv(engine.ENV, raising=False)
    monkeypatch.setattr(engine, "_profile_dir", lambda: None)
    monkeypatch.setattr(live, "publish_safe", lambda *a, **k: None)


def _board() -> LayoutBoard:
    b = LayoutBoard(name="r", outline=[(0, 0), (600, 0), (600, 400), (0, 400)],
                    layers=list(LAYERS), rules=list(CLEARANCE))
    b.pads = [Pad(c, "1", x, 200, net="A", copper=[PadCopper("TopLayer", "rect", 20, 20)])
              for c, x in (("R1", 100), ("R2", 500))]
    b.components = [Component("R1", x=100, y=200), Component("R2", x=500, y=200)]
    return b


def test_the_default_is_the_usual_router():
    assert engine.DEFAULT == "default" == engine.ENGINES[0]
    assert engine.describe()["engine"] == "default"
    res = route_job({"board": _board()})
    assert res["router"] == "default"
    assert res["summary"]["completion"] == 1.0
    assert not any("euan" in n for n in res["notes"])


def test_the_environment_turns_on_euans_router(monkeypatch):
    import eda_agent.layout.route_euan.router as euan
    called = []
    real = euan.route_adaptive
    monkeypatch.setattr(euan, "route_adaptive",
                        lambda *a, **k: called.append(k) or real(*a, **k))
    monkeypatch.setenv(engine.ENV, "euan")
    res = route_job({"board": _board()})
    assert called, "the euan engine ran"
    assert res["router"] == "euan"
    assert res["summary"]["completion"] == 1.0
    assert res["summary"]["drc_status"] == "pass" or res["summary"]["violations"] == 0
    assert "Routed with the euan engine." in res["notes"]


def test_the_profile_file_chooses_and_the_cli_writes_it(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_profile_dir", lambda: tmp_path)
    (tmp_path / engine.FILE).write_text(json.dumps({"_comment": "keep me"}))
    assert engine.write(tmp_path, "euan")["ok"]
    stored = json.loads((tmp_path / engine.FILE).read_text())
    assert stored == {"_comment": "keep me", "engine": "euan"}
    assert engine.describe() == {"engine": "euan", "source": "profile",
                                 "path": str(tmp_path / engine.FILE), "problem": ""}
    # The environment overrules the profile.
    monkeypatch.setenv(engine.ENV, "default")
    assert engine.describe()["engine"] == "default"
    assert not engine.write(tmp_path, "bogus")["ok"]


def test_a_bad_setting_falls_back_to_the_default(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_profile_dir", lambda: tmp_path)
    (tmp_path / engine.FILE).write_text('{"engine": "fast"}')
    found = engine.describe()
    assert (found["engine"], found["source"]) == ("default", "fallback")
    assert "fast" in found["problem"]
    monkeypatch.setenv(engine.ENV, "nope")
    found = engine.describe()
    assert (found["engine"], found["source"]) == ("default", "fallback")
    assert "nope" in found["problem"]


def test_a_bad_setting_in_force_is_not_passed_over_for_the_next(tmp_path, monkeypatch):
    """EDA_AGENT_ROUTER decides when it is set. Set to a name no engine
    has, the default engine routes, not the profile's choice beneath it:
    what the person meant is unknown, and the default needs no asking."""
    monkeypatch.setattr(engine, "_profile_dir", lambda: tmp_path)
    (tmp_path / engine.FILE).write_text('{"engine": "euan"}')
    monkeypatch.setenv(engine.ENV, "eaun")
    found = engine.describe()
    assert (found["engine"], found["source"]) == ("default", "fallback")
    res = route_job({"board": _board()})
    assert res["router"] == "default"
    assert any(n.startswith("The router setting was ignored") and "eaun" in n
               and "the default engine routed" in n for n in res["notes"]), res["notes"]


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '"euan"'])
def test_router_use_leaves_a_file_it_cannot_read_alone(tmp_path, content):
    path = tmp_path / engine.FILE
    path.write_text(content)
    res = engine.write(tmp_path, "euan")
    assert not res["ok"] and "Fix or delete that file first" in res["reason"]
    assert path.read_text() == content
    assert not engine.write(tmp_path / "missing", "euan")["ok"]


def test_router_use_writes_the_whole_file_or_nothing(tmp_path, monkeypatch):
    from eda_agent import atomicfile
    path = tmp_path / engine.FILE
    path.write_text('{"engine": "default", "note": "mine"}')

    def fail(tmp, target):
        raise OSError("disk full")
    monkeypatch.setattr(atomicfile, "replace_with_retry", fail)
    res = engine.write(tmp_path, "euan")
    assert not res["ok"] and "disk full" in res["reason"]
    assert json.loads(path.read_text()) == {"engine": "default", "note": "mine"}
    assert [p.name for p in tmp_path.iterdir()] == [engine.FILE]     # no temporary left


def test_router_use_reports_a_folder_that_takes_no_new_file(tmp_path, monkeypatch, capsys):
    """The new file is staged beside router.json; where none can be made
    router-use says so and stops, with no traceback."""
    from eda_agent import cli, profile
    path = tmp_path / engine.FILE
    path.write_text('{"engine": "default"}')

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(engine.tempfile, "mkstemp", refuse)
    res = engine.write(tmp_path, "euan")
    assert not res["ok"] and "cannot write" in res["reason"] and "Permission denied" in res["reason"]
    monkeypatch.setattr(profile, "resolve_profile_dir", lambda: tmp_path)
    assert cli.cmd_router_use("euan") == 2
    assert "ERROR: cannot write" in capsys.readouterr().err
    assert json.loads(path.read_text()) == {"engine": "default"}
    assert [p.name for p in tmp_path.iterdir()] == [engine.FILE]


def test_router_use_keeps_half_an_emoji_the_file_already_held(tmp_path, monkeypatch, capsys):
    """A lone surrogate read from router.json (an emoji cut in half by
    some other tool) goes back as the escape it was. Written as UTF-8 it
    raised UnicodeEncodeError: a traceback, and a .tmp file left behind
    on every try."""
    from eda_agent import cli, profile
    path = tmp_path / engine.FILE
    path.write_text('{"engine": "default", "note": "\\ud83d"}')
    monkeypatch.delenv(engine.ENV, raising=False)
    found = engine.describe(tmp_path)
    assert (found["engine"], found["source"]) == ("default", "profile"), found
    monkeypatch.setattr(profile, "resolve_profile_dir", lambda: tmp_path)
    assert cli.cmd_router_use("euan") == 0
    capsys.readouterr()
    assert json.loads(path.read_text()) == {"engine": "euan", "note": "\ud83d"}
    assert [p.name for p in tmp_path.iterdir()] == [engine.FILE]
    # Whatever else a write cannot encode is reported, not raised, and
    # leaves no temporary file.
    def circular(*args, **kwargs):
        raise ValueError("Circular reference detected")
    monkeypatch.setattr(engine.json, "dumps", circular)
    res = engine.write(tmp_path, "default")
    assert not res["ok"] and "cannot write" in res["reason"] and "Circular" in res["reason"]
    assert [p.name for p in tmp_path.iterdir()] == [engine.FILE]
    assert json.loads(path.read_text())["engine"] == "euan"


@pytest.mark.parametrize("content", [
    "[" * 30000 + "]" * 30000,                                  # deeper than the parser goes
    " " * (engine.MAX_BYTES + 1) + '{"engine": "euan"}',        # too big to be a setting
])
def test_a_router_file_that_cannot_be_parsed_is_a_problem_not_a_crash(tmp_path, monkeypatch,
                                                                     content):
    monkeypatch.setattr(engine, "_profile_dir", lambda: tmp_path)
    (tmp_path / engine.FILE).write_text(content)
    found = engine.describe()
    assert (found["engine"], found["source"]) == ("default", "fallback")
    assert "unreadable" in found["problem"]


def test_our_stage_switches_become_euans_router_arguments():
    kwargs, notes = euan_kwargs({"diff_pairs": True, "axis_entry": True,
                                 "neck_pads": True, "join_existing": True,
                                 "exclude_layers": ("Mid1",)})
    assert kwargs["pad_entry"] == "axis"
    assert kwargs["neck_pads"] is True and kwargs["join_existing"] is True
    assert kwargs["exclude_layers"] == frozenset({"Mid1"})
    assert kwargs["stages"] == {"diff_pairs": True}
    assert any("neck_pads" in n for n in notes)
    assert euan_kwargs(False)[0] == {"stages": False}
    # One layer named as a string is that layer, not its letters.
    assert euan_kwargs({"exclude_layers": "MidLayer1"})[0]["exclude_layers"] == \
        frozenset({"MidLayer1"})
    # Our StageOptions object, as the default engine takes it.
    from eda_agent.layout.route.stages import StageOptions
    assert euan_kwargs(StageOptions(neck_pads=True))[0]["neck_pads"] is True
    # Euan's own job params reach his router.
    kw = euan_kwargs(None, params={"pitch": "5", "layer_dirs": {"TopLayer": "h"}})[0]
    assert kw["pitch"] == 5.0 and kw["layer_dirs"] == {"TopLayer": "h"}
    kw = euan_kwargs(None, _board(), params={"pitch": 0, "dir_strength": 0,
                                             "layer_dirs": {"BottomLayer": "Vertical"}})[0]
    assert "pitch" not in kw                    # 0: his router picks its own
    assert kw["dir_strength"] == 0.0 and kw["layer_dirs"] == {"BottomLayer": "v"}


@pytest.mark.parametrize("key, value, match", [
    # Below -1 a move across a layer's direction costs less than nothing
    # in his global router, and scipy's Dijkstra then ran the server
    # process out of memory and aborted it.
    ("dir_strength", -1.2, "dir_strength takes 0 to 10, not -1.2"),
    ("dir_strength", float("nan"), "dir_strength takes 0 to 10"),
    ("outer_cost", 0, "outer_cost takes 0.1 to 100, not 0"),
    ("pitch", 0.25, "pitch takes 1.5 to 25 mil, or 0 for the router's own, not 0.25"),
    ("pitch", "fine", "pitch takes a number, not 'fine'"),
    ("layer_dirs", {"TopLayer": "diagonal"}, "it takes 'h' or 'v'"),
    ("layer_dirs", ["TopLayer"], "layer_dirs takes each layer's name to 'h' or 'v'"),
    ("layer_dirs", {"Mid9": "h"}, "layer_dirs names no copper layer"),
])
def test_euans_own_params_are_checked_before_his_router_runs(key, value, match, monkeypatch):
    import eda_agent.layout.route_euan.router as euan
    with pytest.raises(ValueError, match=re.escape(match)):
        euan_kwargs(None, _board(), params={key: value})
    ran = []
    monkeypatch.setattr(euan, "route_adaptive", lambda *a, **k: ran.append(k))
    monkeypatch.setenv(engine.ENV, "euan")
    with pytest.raises(ValueError):
        route_job({"board": _board(), key: value})
    assert not ran, "the job failed before his router ran"


def test_a_bad_stage_switch_fails_as_it_does_with_the_default_engine():
    with pytest.raises(ValueError, match="unknown stage options: bus_lane"):
        euan_kwargs({"bus_lane": True})
    b = _board()
    with pytest.raises(ValueError, match="names no copper layer"):
        euan_kwargs({"exclude_layers": ["Nope"]}, b)
    with pytest.raises(ValueError, match="surface pad"):
        euan_kwargs({"exclude_layers": ["TopLayer"]}, b)


@pytest.mark.parametrize("which", engine.ENGINES)
def test_pad_vias_is_checked_for_the_engine_that_routes(which, monkeypatch):
    """pad_vias takes 'bga', 'ic' or 'any', and 'centre' with the euan
    engine. The default engine routes 'centre' as 'bga' and says so; a
    value neither engine knows fails the job before anything runs."""
    import eda_agent.layout.route.job as job_mod
    import eda_agent.layout.route_euan.router as euan
    where = euan if which == "euan" else job_mod
    got = []
    real = where.route_adaptive
    monkeypatch.setattr(where, "route_adaptive", lambda *a, **k: got.append(k) or real(*a, **k))
    monkeypatch.setenv(engine.ENV, which)
    for value in ("everywhere", "", "bga ic"):
        with pytest.raises(ValueError, match="pad_vias takes 'bga', 'ic' or 'any'"):
            route_job({"board": _board(), "stages": {"pad_vias": value}})
    assert not got, "the job failed before the router ran"
    res = route_job({"board": _board(), "stages": {"pad_vias": "Center"}})
    assert res["summary"]["completion"] == 1.0
    noted = [n for n in res["notes"] if "pad_vias" in n]
    if which == "euan":
        assert got[-1]["stages"]["pad_vias"] == "centre" and not noted
    else:
        assert got[-1]["stages"]["pad_vias"] == "bga"
        assert noted == ["pad_vias='Center' only steers the euan engine; the default engine "
                         "routed with 'bga', a via only in a BGA's ball or an exposed pad."]
    route_job({"board": _board(), "stages": {"pad_vias": None, "bus_lanes": False}})
    assert got[-1]["stages"] == {"bus_lanes": False}, "None leaves the engine's own default"


@pytest.mark.parametrize("which", engine.ENGINES)
def test_pad_vias_in_a_stage_options_object_is_checked_the_same(which, monkeypatch):
    """Our StageOptions object reached either router unchecked: an unknown
    value routed, 'centre' went to the default engine without a word, and
    'Center' reached Euan's router, which compares with case, so his
    centre mode never ran."""
    import eda_agent.layout.route.job as job_mod
    import eda_agent.layout.route_euan.router as euan
    from eda_agent.layout.route.stages import StageOptions
    where = euan if which == "euan" else job_mod
    got = []
    real = where.route_adaptive
    monkeypatch.setattr(where, "route_adaptive", lambda *a, **k: got.append(k) or real(*a, **k))
    monkeypatch.setenv(engine.ENV, which)
    with pytest.raises(ValueError, match="pad_vias takes 'bga', 'ic' or 'any'"):
        route_job({"board": _board(), "stages": StageOptions(pad_vias="everywhere")})
    assert not got, "the job failed before the router ran"
    with pytest.raises(ValueError, match="lane_cost takes 1 to inf"):
        route_job({"board": _board(), "stages": StageOptions(lane_cost=-1.0)})
    assert not got
    res = route_job({"board": _board(), "stages": StageOptions(pad_vias="Center")})
    assert res["summary"]["completion"] == 1.0
    noted = [n for n in res["notes"] if "pad_vias" in n]
    passed = got[-1]["stages"]
    passed = passed if isinstance(passed, dict) else passed.__dict__
    if which == "euan":
        assert passed["pad_vias"] == "centre" and not noted
    else:
        assert passed["pad_vias"] == "bga" and len(noted) == 1


def _boxed_ic_pin() -> LayoutBoard:
    """IC pin U1.1 (net A) boxed in on top by a keep-out with a window just
    round it; its other end, J1.1, is on the bottom. Only a via in the pin
    joins them, which 'bga' does not allow and the fallback's 'ic' does."""
    from eda_agent.layout.model import Region

    def ring(x0, y0, x1, y1):
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

    def pad(comp, name, x, y, net, layer="TopLayer"):
        return Pad(comp, name, x, y, net=net, copper=[PadCopper(layer, "rect", 30, 30)])

    b = _board()
    b.pads = [pad("U1", "1", 150, 200, "A"), pad("U1", "2", 150, 350, "B"),
              pad("U1", "3", 150, 50, "C"), pad("J1", "1", 450, 200, "A", "BottomLayer"),
              pad("J1", "2", 450, 350, "B"), pad("J1", "3", 450, 50, "C")]
    b.components = [Component("U1", x=150, y=200), Component("J1", x=450, y=200)]
    b.regions.append(Region("TopLayer", ring(-5, -5, 605, 405),
                            [ring(130, 180, 170, 220), ring(120, 320, 480, 380),
                             ring(120, 20, 480, 80)], kind="keepout", keepout=True))
    return b


def test_the_centre_note_tells_of_the_fallbacks_ic_pins(monkeypatch):
    """The default engine routes 'centre' as 'bga'. When its fallback then
    widens that to IC pins and keeps the result, the note says so: written
    before routing, it said the via went only into a BGA's ball or an
    exposed pad, beside a via 11.8 mil off the IC pin's centre."""
    monkeypatch.setenv(engine.ENV, "default")
    res = route_job({"board": _boxed_ic_pin(), "stages": {"pad_vias": "centre"}})
    assert res["summary"]["completion"] == 1.0 and res["fallback"]
    assert [v["kind"] for v in res["via_in_pad"]] == ["ic pin"]
    noted = [n for n in res["notes"] if "pad_vias" in n]
    assert noted == ["pad_vias='centre' only steers the euan engine; the default engine routed "
                     "with 'ic', its fallback having widened 'bga' to IC pins, where a via may "
                     "stand anywhere in the pin."]
    # Euan's engine takes 'centre' itself: the via stands at the pin's centre.
    monkeypatch.setenv(engine.ENV, "euan")
    res = route_job({"board": _boxed_ic_pin(), "stages": {"pad_vias": "centre"}})
    assert res["summary"]["completion"] == 1.0
    assert [(v["x"], v["y"]) for v in res["via_in_pad"]] == [(150.0, 200.0)]


@pytest.mark.parametrize("key, value, match", [
    # A negative lane cost gave scipy's Dijkstra negative weights, which
    # ran the server process out of memory and aborted it.
    ("lane_cost", -1, "lane_cost takes 1 to inf, not -1"),
    ("lane_cost", 0.5, "lane_cost takes 1 to inf, not 0.5"),
    ("lane_cost", float("nan"), "lane_cost takes 1 to inf, not nan"),
    ("lane_cost", "8", "lane_cost takes a number, not '8'"),
    # Walked back half a cell at a time, this lane took hours.
    ("lane_depth", 1e7, "lane_depth takes 0 to 1000, not 10000000.0"),
    ("pair_skew", -1.0, "pair_skew takes 0 to 1000, not -1.0"),
    ("pair_width", 0, "pair_width takes 1 to 1000, not 0"),
    ("pair_gap", True, "pair_gap takes a number, not True"),
    ("fine_pitch", float("inf"), "fine_pitch takes 1 to 1000, not inf"),
    ("bus_min", 1, "bus_min takes 2 to 10000, not 1"),
    ("bus_min", 2.5, "bus_min takes a whole number, not 2.5"),
])
@pytest.mark.parametrize("which", engine.ENGINES)
def test_stage_numbers_are_checked_before_either_engine_runs(which, key, value, match,
                                                              monkeypatch):
    import eda_agent.layout.route.job as job_mod
    import eda_agent.layout.route_euan.router as euan
    ran = []
    for where in (job_mod, euan):
        monkeypatch.setattr(where, "route_adaptive", lambda *a, **k: ran.append(k))
    monkeypatch.setenv(engine.ENV, which)
    with pytest.raises(ValueError, match=re.escape(match)):
        route_job({"board": _board(), "stages": {key: value}})
    assert not ran, "the job failed before the router ran"


@pytest.mark.parametrize("which", engine.ENGINES)
def test_stage_numbers_in_range_are_taken(which, monkeypatch):
    monkeypatch.setenv(engine.ENV, which)
    res = route_job({"board": _board(), "stages": {
        "lane_cost": float("inf"), "bus_min": 4, "pair_width": None, "pair_gap": None,
        "lane_depth": 0, "pair_skew": 0.2, "fine_pitch": 25.6}})
    assert res["router"] == which and res["summary"]["completion"] == 1.0


def test_the_cli_commands_exist(tmp_path, monkeypatch, capsys):
    from eda_agent import cli, profile
    monkeypatch.setattr(profile, "resolve_profile_dir", lambda *a, **k: tmp_path)
    assert cli.cmd_router_use("euan") == 0
    assert json.loads((tmp_path / engine.FILE).read_text())["engine"] == "euan"
    monkeypatch.setattr(engine, "_profile_dir", lambda: tmp_path)
    assert cli.cmd_router_show() == 0
    assert "Router engine: euan  (from " + str(tmp_path / engine.FILE) in capsys.readouterr().out
    assert cli.cmd_router_show(as_json=True) == 0
    assert json.loads(capsys.readouterr().out)["engine"] == "euan"
    assert cli.cmd_router_use("default") == 0
    assert engine.describe()["engine"] == "default"


def test_router_use_says_when_the_environment_decides_instead(tmp_path, monkeypatch, capsys):
    from eda_agent import cli, profile
    monkeypatch.setattr(profile, "resolve_profile_dir", lambda *a, **k: tmp_path)
    monkeypatch.setenv(engine.ENV, "default")
    assert cli.cmd_router_use("euan") == 0
    assert f"{engine.ENV}=default is set here and decides" in capsys.readouterr().out
    monkeypatch.setenv(engine.ENV, "eaun")
    assert cli.cmd_router_use("euan") == 0
    out = capsys.readouterr().out
    assert "'eaun' is not one of" in out and "use the default engine until" in out
    monkeypatch.setattr(engine, "_profile_dir", lambda: tmp_path)
    assert cli.cmd_router_show() == 1                       # the setting in force is unusable
    assert "Router engine: default  (from the default, because" in capsys.readouterr().out


def test_the_cli_offers_exactly_the_engines():
    from eda_agent import server
    parser = server.build_parser()
    sub = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    use = sub.choices["router-use"]
    arg = next(a for a in use._actions if a.dest == "engine")
    assert tuple(arg.choices) == engine.ENGINES
