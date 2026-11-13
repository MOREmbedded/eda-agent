# SPDX-License-Identifier: Apache-2.0
"""Switches, plugins and the command line."""
from __future__ import annotations

import json

import pytest

from eda_agent import cli
from eda_agent.features import catalog as cat
from eda_agent.features import registry, state


@pytest.fixture
def switches(tmp_path, monkeypatch):
    path = tmp_path / "features.json"
    monkeypatch.setenv("EDA_AGENT_FEATURES_FILE", str(path))
    return path


def test_switch_off_and_on_again(switches):
    assert state.is_on("openems")
    assert state.set_switch("openems", False) == {"openems"}
    assert not state.is_on("openems")
    assert json.loads(switches.read_text())["off"] == ["openems"]
    state.set_switch("openems", True)
    assert state.is_on("openems")


def test_only_switchable_features_switch(switches):
    with pytest.raises(ValueError):
        state.set_switch("render", False)
    with pytest.raises(KeyError):
        state.set_switch("nope", False)


def test_a_broken_switch_file_means_everything_is_on(switches):
    switches.write_text("{not json")
    assert state.switched_off() == set()


class EP:
    def __init__(self, name, fn):
        self.name, self.value, self._fn = name, f"pkg:{name}", fn

    def load(self):
        return self._fn


@pytest.fixture
def plugins(monkeypatch):
    found = []
    monkeypatch.setattr(registry, "_entry_points", lambda: found)
    monkeypatch.setattr(registry, "_cache", None)
    yield found
    registry._cache = None


def _plugin(fid="extra-solver", dep_id="extra-dep", license_="MIT", needs=None, core=False):
    dep = cat.Dependency(dep_id, "Extra", cat.PYTHON, license_, dist=dep_id)
    feat = cat.Feature(fid, "Extra solver", "Field solvers and signal integrity", "",
                       needs=tuple(needs or (dep_id,)), core=core)
    return cat.Plugin(feat, [dep])


def test_a_plugin_adds_a_feature(plugins):
    plugins.append(EP("good", lambda: _plugin()))
    loaded = registry.load(refresh=True)
    assert "extra-solver" in {f.id for f in loaded["features"]}
    assert "extra-dep" in loaded["dependencies"] and loaded["errors"] == []


@pytest.mark.parametrize("make, why", [
    (lambda: _plugin(fid="render"), "already taken"),
    (lambda: _plugin(dep_id="flask"), "already taken"),
    (lambda: _plugin(license_=""), "no license"),
    (lambda: _plugin(needs=["ghost"]), "nothing declares"),
    (lambda: _plugin(fid="Bad Id"), "lower case"),
    (lambda: _plugin(core=True), "cannot be core"),
    (lambda: "not a plugin", "catalog.Plugin"),
])
def test_a_bad_plugin_is_reported_and_left_out(plugins, make, why):
    plugins.append(EP("bad", make))
    loaded = registry.load(refresh=True)
    assert loaded["errors"] and why in loaded["errors"][0]["error"]
    assert len(loaded["features"]) == len(cat.FEATURES)


def test_a_plugin_that_raises_does_not_hide_the_rest(plugins):
    def boom():
        raise RuntimeError("kaput")
    plugins.append(EP("boom", boom))
    loaded = registry.load(refresh=True)
    assert "kaput" in loaded["errors"][0]["error"] and len(loaded["features"]) == len(cat.FEATURES)


def test_cli_features_lists_groups_and_switches(switches, capsys):
    assert cli.cmd_features() == 0
    out = capsys.readouterr().out
    assert "Field solvers and signal integrity" in out and "openems" in out
    assert cli.cmd_features(off="openems") == 0
    assert "openems is now off" in capsys.readouterr().out
    assert cli.cmd_features(off="render") == 2


def test_cli_features_json(switches, capsys):
    assert cli.cmd_features(as_json=True) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] and {f["id"] for f in data["features"]} >= {"core", "openems"}


def test_cli_install_deps_needs_names_and_dry_run_changes_nothing(capsys, monkeypatch):
    from eda_agent.features import install
    assert cli.cmd_install_deps([]) == 2
    assert cli.cmd_install_deps(["nope"]) == 2
    monkeypatch.setattr(install, "execute", lambda *a, **k: pytest.fail("ran"))
    assert cli.cmd_install_deps(["openems", "render"], dry_run=True) == 0
    assert "openEMS" in capsys.readouterr().out


def test_cli_install_deps_asks_first(capsys, monkeypatch):
    from eda_agent.features import install
    plan = install.Plan(["x"], [install.Step("pip", ["a"], "A", argv=[["py"]])], [])
    monkeypatch.setattr(install, "plan", lambda *a, **k: plan)
    monkeypatch.setattr(install, "execute", lambda *a, **k: pytest.fail("ran without a yes"))
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli.cmd_install_deps(["render"]) == 1
    called = {}
    monkeypatch.setattr(install, "execute", lambda p, on_line=None: called.setdefault(
        "r", {"ok": True, "log": "l", "steps": [{"title": "A", "ok": True, "seconds": 0.1}],
              "manual": []}))
    assert cli.cmd_install_deps(["render"], assume_yes=True) == 0 and called
