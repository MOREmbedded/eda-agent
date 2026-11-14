# SPDX-License-Identifier: Apache-2.0
"""Install plans and their execution, with no pip run for real."""
from __future__ import annotations

import importlib.metadata as md
import io

import pytest

from eda_agent.features import catalog as cat
from eda_agent.features import detect, install


@pytest.fixture
def nothing_installed(monkeypatch, tmp_path):
    def missing(name):
        raise md.PackageNotFoundError(name)
    monkeypatch.setattr(detect.md, "version", missing)
    for k in list(detect.PROGRAMS):
        monkeypatch.setitem(detect.PROGRAMS, k, lambda deep: ("", ""))
    monkeypatch.setenv("EDA_AGENT_ENGINES_DIR", str(tmp_path / "engines"))


def test_select_all_checks_core_first_and_leaves_out_development():
    """--all puts back a core package the environment lost, so the core
    features come first; the development ones only with include_dev."""
    chosen = install.select(everything=True)
    ids = [f.id for f in chosen]
    assert ids[0] == "core" and all(f.core for f in chosen[:ids.index("dashboard")])
    assert "dev" not in ids and "site" not in ids
    assert "dev" in {f.id for f in install.select(everything=True, include_dev=True)}
    with pytest.raises(KeyError):
        install.select(["nope"])


def test_one_pip_call_per_feature(nothing_installed):
    p = install.plan(install.select(["gerber", "pdf"]))
    pips = [s for s in p.steps if s.kind == "pip"]
    assert [s.deps for s in pips] == [["gerbonara"], ["pdfplumber"]] or \
        [s.deps for s in pips] == [["pdfplumber"]]  # gerbonara needs Python 3.12
    for s in pips:
        assert s.argv[0][1:4] == ["-m", "pip", "install"]


def test_present_dependencies_are_not_installed_again(nothing_installed, monkeypatch):
    monkeypatch.setattr(detect.md, "version", lambda n: "3.9" if n == "matplotlib" else
                        (_ for _ in ()).throw(md.PackageNotFoundError(n)))
    p = install.plan(install.select(["render"]))
    assert p.already == ["matplotlib"] and p.steps == []


def test_alternatives_install_the_one_pip_can(nothing_installed):
    p = install.plan(install.select(["models3d"]))
    assert any(s.kind == "pip" and s.deps == ["cadquery"] for s in p.steps)
    assert not any("freecad" in s.deps for s in p.steps)


def test_programs_are_manual_steps(nothing_installed):
    p = install.plan(install.select(["openems"]))
    assert [s.kind for s in p.steps] == ["manual"]
    assert "EDA_AGENT_OPENEMS_DIR" in p.steps[0].text and p.automatic == []


def test_a_local_wheel_without_cargo_is_manual(nothing_installed, monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda n: None)
    p = install.plan(install.select(["compute"]))
    assert p.steps[0].kind == "manual" and "rustup" in p.steps[0].text
    monkeypatch.setattr(install.shutil, "which", lambda n: "/usr/bin/cargo")
    p = install.plan(install.select(["compute"]))
    assert p.steps[0].kind == "local"
    assert p.steps[0].argv[0][-1].replace("\\", "/").endswith("rust/eda_agent_compute")


def test_a_venv_engine_gets_its_own_environment(nothing_installed, monkeypatch):
    dep = cat.Dependency("gplpkg", "GPL pkg", cat.VENV, "GPL-3.0", dist="gplpkg",
                         venv="gplpkg", requirement="gplpkg>=1")
    feat = cat.Feature("gplf", "GPL feature", "Field solvers and signal integrity", "",
                       needs=("gplpkg",))
    from eda_agent.features import registry
    monkeypatch.setattr(registry, "_cache", {"features": [*cat.FEATURES, feat],
                                             "dependencies": {**{d.id: d for d in cat.DEPENDENCIES},
                                                              "gplpkg": dep},
                                             "errors": []})
    p = install.plan([feat], python="py")
    (step,) = p.steps
    assert step.kind == "venv"
    assert step.argv[0][:3] == ["py", "-m", "venv"]
    assert step.argv[1][1:] == ["-m", "pip", "install", "gplpkg>=1"]
    assert str(detect.venv_python("gplpkg")) == step.argv[1][0]


class FakeProc:
    def __init__(self, rc, text):
        self.stdout = io.StringIO(text)
        self.rc = rc

    def wait(self):
        return self.rc


def test_execute_runs_steps_in_order_and_isolates_failures(tmp_path):
    steps = [install.Step("pip", ["a"], "A", argv=[["x", "fail"], ["x", "never"]]),
             install.Step("manual", ["m"], "M", text="by hand"),
             install.Step("pip", ["b"], "B", argv=[["x", "ok"]])]
    plan = install.Plan(["f"], steps, [])
    ran = []

    def runner(cmd, **kw):
        ran.append(cmd[1])
        return FakeProc(1 if cmd[1] == "fail" else 0, f"output of {cmd[1]}\n")

    lines = []
    res = install.execute(plan, on_line=lines.append, log_path=tmp_path / "i.log", runner=runner)
    assert ran == ["fail", "ok"]
    assert [s["ok"] for s in res["steps"]] == [False, True] and res["ok"] is False
    assert res["steps"][0]["returncode"] == 1 and "output of fail" in res["steps"][0]["tail"]
    assert res["manual"][0]["title"] == "M"
    log = (tmp_path / "i.log").read_text(encoding="utf-8")
    assert "output of ok" in log and any("output of fail" in x for x in lines)


def test_execute_reports_a_command_that_cannot_start(tmp_path):
    plan = install.Plan(["f"], [install.Step("pip", ["a"], "A", argv=[["nope"]])], [])

    def runner(cmd, **kw):
        raise FileNotFoundError("no such program")
    res = install.execute(plan, log_path=tmp_path / "l.log", runner=runner)
    assert not res["ok"] and "no such program" in res["steps"][0]["tail"]


def test_commands_are_quoted_for_a_shell():
    s = install.Step("pip", ["a"], "A", argv=[["py", "-m", "pip", "install", "a<2,>=1"]])
    assert s.to_dict()["commands"] == ['py -m pip install "a<2,>=1"']
