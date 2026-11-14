# SPDX-License-Identifier: Apache-2.0
"""The registry covers everything eda-agent has, and the choices a user
makes on the Features page (switches, program locations) hold.

Covers: the built-in analysis features and the core packages, tool and
document names that must exist, default-off switches that work through an
environment variable, saved program locations, the program finders the
report shares with the tools, the core repair and the cargo build in the
installer, and the dashboard endpoints and page."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

from eda_agent import cli
from eda_agent.features import catalog as cat
from eda_agent.features import detect, install, registry, report, state

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def choices(tmp_path, monkeypatch):
    """A features file of the test's own, and an environment that the
    switches and locations may change without leaking into other tests."""
    path = tmp_path / "features.json"
    monkeypatch.setenv("EDA_AGENT_FEATURES_FILE", str(path))
    names = {n for d in cat.DEPENDENCIES for n in d.env} | {"EDA_AGENT_COMPUTE"}
    before = {n: os.environ.get(n) for n in names}
    for n in names:
        monkeypatch.delenv(n, raising=False)
    state._APPLIED.clear()
    yield path
    state._APPLIED.clear()
    for n, v in before.items():
        if v is None:
            os.environ.pop(n, None)
        else:
            os.environ[n] = v


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# ---------------------------------------------------------------- catalogue

def test_every_core_requirement_is_a_core_dependency_and_back():
    import tomllib
    reqs = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    names = {_norm(re.split(r"[<>=!~;\s\[]", r, maxsplit=1)[0]) for r in reqs}
    core = {_norm(d.dist) for d in cat.CORE_DEPENDENCIES}
    assert names == core, (f"in pyproject only: {sorted(names - core)}; "
                           f"in the catalogue only: {sorted(core - names)}")


def test_every_core_dependency_belongs_to_a_core_feature():
    used = {d for f in cat.FEATURES if f.core for d in f.needs}
    assert {d.id for d in cat.CORE_DEPENDENCIES} <= used


def test_the_analysis_features_are_listed():
    ids = {f.id for f in cat.FEATURES if f.core}
    assert {"layout", "signal-integrity", "power-integrity", "thermal", "emi",
            "calculators"} <= ids


def _tool_names() -> set[str]:
    text = (ROOT / "docs" / "TOOL_REFERENCE.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\| `([a-z0-9_]+)`", text, re.M))


def _commands() -> set[str]:
    from eda_agent.server import build_parser
    parser = build_parser()
    sub = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    return set(sub.choices)


def test_every_tool_a_feature_names_exists():
    tools, commands = _tool_names(), _commands()
    assert len(tools) > 500 and "features" in commands
    for f in cat.FEATURES:
        for t in f.tools:
            assert t in tools or t in commands, (f.id, t)


def test_every_document_and_related_feature_exists():
    ids = {f.id for f in cat.FEATURES}
    for f in cat.FEATURES:
        if f.docs:
            assert (ROOT / f.docs).is_file(), (f.id, f.docs)
        for r in f.related:
            assert r in ids and r != f.id, (f.id, r)


def test_every_variable_a_dependency_reads_is_documented():
    from eda_agent.settings_catalog import ENV_VARS
    documented = {v.name for v in ENV_VARS}
    for d in cat.DEPENDENCIES:
        for name in d.env:
            assert name in documented, (d.id, name)


def test_every_program_has_a_finder_and_cargo_only_for_programs():
    for d in cat.DEPENDENCIES:
        if d.kind == cat.PROGRAM:
            assert d.id in detect.PROGRAMS, d.id
        assert not d.cargo or d.kind == cat.PROGRAM, d.id


def test_a_switch_through_a_variable_names_three_parts():
    for f in cat.FEATURES:
        if f.switch_env:
            assert f.switchable and len(f.switch_env) == 3, f.id
        if not f.default_on:
            assert f.switchable, f.id


# ---------------------------------------------------------------- detection

def test_a_platform_only_need_is_no_need_elsewhere(monkeypatch):
    win = cat.Dependency("winpkg", "W", cat.PYTHON, "MIT", dist="winpkg", platforms=("win32",))
    other = "linux" if sys.platform == "win32" else sys.platform
    monkeypatch.setattr(detect.sys, "platform", other)
    st = detect.dependency_status(win)
    assert st.applies is False and st.to_dict()["applies"] is False
    f = cat.Feature("x", "X", "Core", "", needs=("winpkg",), core=True)
    assert detect.feature_status(f, {"winpkg": st}, False).state == detect.READY
    g = cat.Feature("y", "Y", "Views and pictures", "", needs=("winpkg",))
    assert detect.feature_status(g, {"winpkg": st}, False).state == detect.READY


def test_a_core_feature_that_lost_a_package_says_how_to_repair():
    f = cat.Feature("x", "X", "Core", "", needs=("numpy",), core=True)
    st = detect.DepStatus("numpy", detect.MISSING)
    fs = detect.feature_status(f, {"numpy": st}, False)
    assert fs.state == detect.NEEDS and fs.missing == ["numpy"]
    assert "install-deps --all" in fs.detail


def test_the_core_requirement_carries_its_range():
    dep = cat.dependency("numpy")
    assert detect.requirement_for(dep).replace(" ", "").startswith("numpy")
    assert "<" in detect.requirement_for(dep)


def test_ngspice_is_found_where_it_is_named(tmp_path, choices, monkeypatch):
    from eda_agent.sim import ngspice as ng
    monkeypatch.setattr(ng.shutil, "which", lambda name: None)
    exe = tmp_path / "Spice64" / "bin" / "ngspice_con.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("EDA_AGENT_NGSPICE", str(exe))
    assert ng.find_ngspice() == str(exe)
    monkeypatch.setenv("EDA_AGENT_NGSPICE", str(tmp_path / "Spice64"))   # the folder
    assert ng.find_ngspice() == str(exe)
    monkeypatch.delenv("EDA_AGENT_NGSPICE")
    monkeypatch.setattr(ng.sys, "platform", "win32")
    monkeypatch.setattr(ng.Path, "home", lambda: tmp_path / "nohome")
    monkeypatch.setenv("SystemDrive", str(tmp_path / "nodrive"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    for k in ("ProgramW6432", "LOCALAPPDATA"):
        monkeypatch.delenv(k, raising=False)
    assert ng.find_ngspice() == str(exe)


def test_freecad_and_kicad_cli_follow_their_variables(tmp_path, choices, monkeypatch):
    from eda_agent.convert.altium_kicad import pipeline
    from eda_agent.design import models3d
    fc, kc = tmp_path / "freecadcmd.exe", tmp_path / "kicad-cli.exe"
    fc.write_text("")
    kc.write_text("")
    monkeypatch.setenv("EDA_AGENT_FREECADCMD", str(fc))
    monkeypatch.setenv("EDA_AGENT_KICAD_CLI", str(kc))
    assert models3d._freecadcmd() == str(fc)
    assert pipeline.find_kicad_cli() == str(kc)
    assert detect.dependency_status(cat.dependency("freecad")).where == str(fc)
    assert detect.dependency_status(cat.dependency("kicad")).where == str(kc)


def test_pcb_toolkit_built_by_the_installer_is_found(tmp_path, monkeypatch):
    from eda_agent.engineering import pcb_toolkit
    monkeypatch.setenv("EDA_AGENT_ENGINES_DIR", str(tmp_path))
    monkeypatch.delenv("EDA_AGENT_PCB_TOOLKIT", raising=False)
    monkeypatch.setattr(pcb_toolkit.shutil, "which", lambda name: None)
    assert pcb_toolkit.find_binary() is None
    exe = tmp_path / "pcb-toolkit" / "bin" / ("pcb-toolkit.exe" if sys.platform == "win32" else "pcb-toolkit")
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert pcb_toolkit.find_binary() == str(exe)


# ----------------------------------------------------------------- switches

def test_the_rust_kernels_are_off_until_switched_on(choices):
    assert not state.is_on("compute")
    state.set_switch("compute", True)
    assert state.is_on("compute") and os.environ["EDA_AGENT_COMPUTE"] == "auto"
    assert json.loads(choices.read_text())["on"] == ["compute"]
    state.set_switch("compute", False)
    assert not state.is_on("compute") and "EDA_AGENT_COMPUTE" not in os.environ


def test_a_switch_on_is_applied_in_a_later_session(choices):
    choices.write_text(json.dumps({"on": ["compute"]}))
    assert "EDA_AGENT_COMPUTE" not in os.environ
    assert state.apply() == {"EDA_AGENT_COMPUTE": "auto"}
    assert os.environ["EDA_AGENT_COMPUTE"] == "auto"


def test_the_users_own_variable_decides_and_is_never_touched(choices, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_COMPUTE", "python")
    choices.write_text(json.dumps({"on": ["compute"]}))
    assert state.apply() == {}
    assert not state.is_on("compute")
    state.set_switch("compute", False)
    assert os.environ["EDA_AGENT_COMPUTE"] == "python"
    monkeypatch.setenv("EDA_AGENT_COMPUTE", "rust")
    assert state.is_on("compute")


def test_an_engine_stays_on_by_default_and_off_by_choice(choices):
    assert state.is_on("spice") and state.is_on("openems")
    assert state.set_switch("spice", False) == {"spice"}
    assert not state.is_on("spice")


def test_the_report_shows_off_only_once_installed(choices, monkeypatch):
    monkeypatch.setattr(detect.md, "version", lambda name: (_ for _ in ()).throw(
        detect.md.PackageNotFoundError(name)))
    rep = {f["id"]: f for f in report()["features"]}
    assert rep["compute"]["state"] == detect.NEEDS      # not installed says more
    monkeypatch.setattr(detect.md, "version", lambda name: "1.0")
    rep = {f["id"]: f for f in report()["features"]}
    assert rep["compute"]["state"] == detect.OFF
    state.set_switch("compute", True)
    rep = {f["id"]: f for f in report()["features"]}
    assert rep["compute"]["state"] == detect.READY


# ---------------------------------------------------------------- locations

def test_a_saved_location_is_applied_and_forgotten(tmp_path, choices):
    folder = tmp_path / "openEMS"
    folder.mkdir()
    saved = state.set_path("EDA_AGENT_OPENEMS_DIR", str(folder))
    assert saved == {"EDA_AGENT_OPENEMS_DIR": str(folder)}
    assert os.environ["EDA_AGENT_OPENEMS_DIR"] == str(folder)
    assert state.path_source("EDA_AGENT_OPENEMS_DIR") == "saved"
    os.environ.pop("EDA_AGENT_OPENEMS_DIR")
    state._APPLIED.clear()                           # a new session
    assert state.apply() == {"EDA_AGENT_OPENEMS_DIR": str(folder)}
    state.set_path("EDA_AGENT_OPENEMS_DIR", "")
    assert "EDA_AGENT_OPENEMS_DIR" not in os.environ
    assert "paths" not in json.loads(choices.read_text())


def test_a_location_set_in_the_environment_wins(tmp_path, choices, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_OPENEMS_DIR", str(tmp_path))
    other = tmp_path / "other"
    other.mkdir()
    state.set_path("EDA_AGENT_OPENEMS_DIR", str(other))
    assert os.environ["EDA_AGENT_OPENEMS_DIR"] == str(tmp_path)
    assert state.path_source("EDA_AGENT_OPENEMS_DIR") == "environment"


def test_bad_locations_are_refused(tmp_path, choices):
    with pytest.raises(KeyError):
        state.set_path("EDA_AGENT_BOGUS", str(tmp_path))
    with pytest.raises(KeyError):                    # a mode, not a place
        state.set_path("EDA_AGENT_COMPUTE", str(tmp_path))
    with pytest.raises(ValueError):
        state.set_path("EDA_AGENT_NGSPICE", str(tmp_path / "nope"))
    assert not choices.exists()


def test_a_broken_file_does_not_stop_startup(choices):
    choices.write_text('{"paths": [1, 2], "on": "compute"}')
    assert state.apply() == {}
    choices.write_text("not json")
    assert state.apply() == {} and state.saved_paths() == {}


def test_the_report_lists_locations_and_notes(tmp_path, choices):
    state.set_path("EDA_AGENT_NGSPICE", str(tmp_path))
    rep = report()
    ng = next(d for d in rep["dependencies"] if d["id"] == "ngspice")
    assert ng["locations"] == [{"name": "EDA_AGENT_NGSPICE", "value": str(tmp_path),
                                "source": "saved", "saved": str(tmp_path)}]
    comp = next(d for d in rep["dependencies"] if d["id"] == "eda-agent-compute")
    assert comp["locations"] == []
    feats = {f["id"]: f for f in rep["features"]}
    assert "router-use" in feats["layout"]["note"]
    assert "EDA_AGENT_COMPUTE" in feats["compute"]["note"]
    assert rep["built_in"] == sum(1 for f in rep["features"] if f["core"] and f["state"] == "ready")


def test_cli_saves_and_forgets_a_location(tmp_path, choices, capsys):
    assert cli.cmd_features(set_path=f"EDA_AGENT_NGSPICE={tmp_path}") == 0
    assert state.saved_paths() == {"EDA_AGENT_NGSPICE": str(tmp_path)}
    assert cli.cmd_features(set_path="EDA_AGENT_NGSPICE=") == 2
    assert cli.cmd_features(set_path=f"EDA_AGENT_BOGUS={tmp_path}") == 2
    assert cli.cmd_features(clear_path="EDA_AGENT_NGSPICE") == 0
    assert state.saved_paths() == {}
    assert "forgotten" in capsys.readouterr().out


# ---------------------------------------------------------------- installer

def _nothing(monkeypatch):
    monkeypatch.setattr(detect.md, "version", lambda name: (_ for _ in ()).throw(
        detect.md.PackageNotFoundError(name)))


def test_all_repairs_a_lost_core_package(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_ENGINES_DIR", str(tmp_path))
    real = detect.md.version

    def version(name):
        if _norm(name) == "pyamg":
            raise detect.md.PackageNotFoundError(name)
        return real(name)
    monkeypatch.setattr(detect.md, "version", version)
    p = install.plan(install.select(everything=True))
    pips = [s for s in p.steps if s.kind == "pip" and "pyamg" in s.deps]
    assert pips and any(a.startswith("pyamg") for a in pips[0].argv[0])
    assert "thermal" in p.features or "core" in p.features
    # The healthy core features are not part of what the plan is about.
    assert "signal-integrity" not in p.features


def test_a_platform_only_package_is_never_installed_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_ENGINES_DIR", str(tmp_path))
    _nothing(monkeypatch)
    other = "linux" if sys.platform == "win32" else sys.platform
    monkeypatch.setattr(detect.sys, "platform", other)
    p = install.plan([cat.feature("core")])
    ids = [d for s in p.steps for d in s.deps]
    assert "numpy" in ids and "pywin32" not in ids and "comtypes" not in ids


def test_pcb_toolkit_is_built_with_cargo_when_there_is_one(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_ENGINES_DIR", str(tmp_path))
    from eda_agent.engineering import pcb_toolkit
    monkeypatch.setattr(pcb_toolkit, "find_binary", lambda: None)
    monkeypatch.setattr(install.shutil, "which", lambda n: "/bin/cargo" if n == "cargo" else None)
    p = install.plan([cat.feature("pcb-toolkit")])
    (step,) = p.steps
    assert step.kind == "cargo" and step.automatic
    argv = step.argv[0]
    assert argv[:5] == ["cargo", "install", "--locked", "--root", str(tmp_path / "pcb-toolkit")]
    assert argv[-1] == "pcb-toolkit-cli" and "--git" in argv
    monkeypatch.setattr(install.shutil, "which", lambda n: None)
    (step,) = install.plan([cat.feature("pcb-toolkit")]).steps
    assert step.kind == "manual" and "rustup.rs" in step.text


def test_select_names_each_feature_once_and_takes_extra_names():
    assert [f.id for f in install.select(["pdf", "pdf", "render"])] == ["pdf", "render"]
    assert [f.id for f in install.select(["si", "web"])] == ["scikit-rf", "dashboard"]
    with pytest.raises(KeyError):
        install.select(["nope"])


# ---------------------------------------------------------------- dashboard

@pytest.fixture
def client(choices):
    from flask import Flask
    from eda_agent.web import features_view
    app = Flask("t")
    features_view.register(app)
    return app.test_client()


def test_the_path_endpoint_saves_refuses_and_forgets(tmp_path, client):
    r = client.post("/api/wb/features/path", json={"name": "EDA_AGENT_NGSPICE",
                                                   "value": str(tmp_path)}).get_json()
    assert r["ok"] and r["paths"] == {"EDA_AGENT_NGSPICE": str(tmp_path)}
    r = client.post("/api/wb/features/path", json={"name": "EDA_AGENT_NGSPICE",
                                                   "value": str(tmp_path / "x")}).get_json()
    assert not r["ok"] and "does not exist" in r["error"]
    r = client.post("/api/wb/features/path", json={"name": "PATH", "value": "/"}).get_json()
    assert not r["ok"]
    r = client.post("/api/wb/features/path", json={"name": "EDA_AGENT_NGSPICE", "value": ""}).get_json()
    assert r["ok"] and r["paths"] == {}


def test_a_selection_of_features_plans_together(client):
    r = client.post("/api/wb/features/plan", json={"features": ["openems", "kicad"]}).get_json()
    assert r["ok"] and set(r["plan"]["features"]) == {"openems", "kicad"}


def test_the_page_offers_selection_filters_locations_and_the_full_list():
    page = (ROOT / "src" / "eda_agent" / "web" / "dashboard_static" / "workbench.html").read_text(encoding="utf-8")
    for needle in ('data-ftpick', 'data-ftplan="sel"', 'data-ftfilter', 'data-ftlocsave',
                   '"/api/wb/features/path"', 'function ftAllDeps', 'data-ftgoto',
                   'applies !== false'):
        assert needle in page, needle
