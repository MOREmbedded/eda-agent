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
    # Below zero his search's step costs go negative, and scipy's Dijkstra
    # then ran the server process out of memory and aborted it.
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
