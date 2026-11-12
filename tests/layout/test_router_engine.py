# SPDX-License-Identifier: Apache-2.0
"""The autorouter engine is a choice: the usual router unless the user
turns on Euan's (``eda-agent router-use euan`` or EDA_AGENT_ROUTER)."""
from __future__ import annotations

import json

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
    assert found["engine"] == "default" and "fast" in found["problem"]
    monkeypatch.setenv(engine.ENV, "nope")
    found = engine.describe()
    assert found["engine"] == "default" and "nope" in found["problem"]
    with pytest.raises(ValueError):
        engine.current("nope")


def test_our_stage_switches_become_euans_router_arguments():
    kwargs, notes = euan_kwargs({"diff_pairs": True, "axis_entry": True,
                                 "neck_pads": True, "join_existing": True,
                                 "exclude_layers": ("Mid1",), "made_up": 1})
    assert kwargs["pad_entry"] == "axis"
    assert kwargs["neck_pads"] is True and kwargs["join_existing"] is True
    assert kwargs["exclude_layers"] == frozenset({"Mid1"})
    assert kwargs["stages"] == {"diff_pairs": True}
    assert any("made_up" in n for n in notes)
    assert euan_kwargs(False)[0] == {"stages": False}


def test_the_cli_commands_exist(tmp_path, monkeypatch, capsys):
    from eda_agent import cli, profile
    monkeypatch.setattr(profile, "resolve_profile_dir", lambda *a, **k: tmp_path)
    assert cli.cmd_router_use("euan") == 0
    assert json.loads((tmp_path / engine.FILE).read_text())["engine"] == "euan"
    monkeypatch.setattr(engine, "_profile_dir", lambda: tmp_path)
    assert cli.cmd_router_show() == 0
    assert "euan" in capsys.readouterr().out
    assert cli.cmd_router_use("default") == 0
    assert engine.describe()["engine"] == "default"
