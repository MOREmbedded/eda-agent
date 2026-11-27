# SPDX-License-Identifier: Apache-2.0
"""The Hertzon engine: finding hertzon-cli, the spec and its command line,
the project copy, and whole runs through a stand-in that follows Hertzon's
documented command-line contract (tests/simulation/fake_hertzon)."""
from __future__ import annotations

import asyncio
import json
import math
import os
import stat
import sys
import time
import zipfile
from pathlib import Path

import pytest

from eda_agent import simulation as sim
from eda_agent.simulation import hertzon as hz

FAKE = Path(__file__).resolve().parent / "fake_hertzon" / "hertzon_cli.py"

PROJECT = {"designs": [
    {"name": "Line50", "variables": {"w": 0.003, "L": 0.02}, "setups": ["Setup1"]},
    {"name": "Stub", "variables": {"ls": 0.01}, "setups": ["Setup1"]},
]}


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_SIM_JOBS", str(tmp_path / "jobs"))
    monkeypatch.setenv("EDA_AGENT_FEATURES_FILE", str(tmp_path / "features.json"))
    monkeypatch.setenv("EDA_AGENT_ENGINES_DIR", str(tmp_path / "engines"))
    monkeypatch.delenv("FAKE_HERTZON_MODE", raising=False)
    monkeypatch.setattr(sim, "_loaded", None)
    hz._version_cache.clear()
    yield
    sim._loaded = None


def make_launcher(folder: Path) -> Path:
    """An executable ``hertzon-cli`` that runs the stand-in."""
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        p = folder / "hertzon-cli.cmd"
        p.write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8")
    else:
        p = folder / "hertzon-cli"
        p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


@pytest.fixture
def cli(tmp_path, monkeypatch):
    p = make_launcher(tmp_path / "hertzon-bin")
    monkeypatch.setenv(hz.ENV_CLI, str(p))
    return p


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj" / "Filter.hzn"
    p.parent.mkdir()
    p.write_text(json.dumps(PROJECT), encoding="utf-8")
    return p


def wait_done(job_id, engine="hertzon", timeout=60.0):
    end = time.time() + timeout
    while True:
        st = sim.refresh(job_id)["runs"][engine]
        if st["state"] != "running" or time.time() > end:
            return st
        time.sleep(0.1)


# -- finding hertzon-cli -------------------------------------------------------

def test_the_named_program_is_used(cli):
    found = hz.find_cli()
    assert found == {"available": True, "cli": str(cli), "how": hz.ENV_CLI}


def test_a_named_hertzon_folder_is_searched(tmp_path, monkeypatch):
    exe = ".exe" if sys.platform == "win32" else ""
    p = tmp_path / "Hertzon" / ".venv" / ("Scripts" if exe else "bin") / f"hertzon-cli{exe}"
    p.parent.mkdir(parents=True)
    p.write_text("")
    monkeypatch.setenv(hz.ENV_CLI, str(tmp_path / "Hertzon"))
    assert hz.find_cli()["cli"] == str(p)


def test_a_wrong_name_is_reported_not_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv(hz.ENV_CLI, str(tmp_path / "nowhere"))
    monkeypatch.setattr(hz.shutil, "which", lambda n: "/usr/bin/hertzon-cli")
    found = hz.find_cli()
    assert not found["available"] and hz.ENV_CLI in found["how"]


def test_path_then_the_installer_folders(tmp_path, monkeypatch):
    monkeypatch.delenv(hz.ENV_CLI, raising=False)
    monkeypatch.setattr(hz.shutil, "which", lambda n: "/opt/bin/hertzon-cli")
    assert hz.find_cli() == {"available": True, "cli": "/opt/bin/hertzon-cli", "how": "PATH"}
    monkeypatch.setattr(hz.shutil, "which", lambda n: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(hz.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert not hz.find_cli()["available"]
    win = tmp_path / "local" / "Programs" / "Hertzon" / ".venv" / "Scripts" / "hertzon-cli.exe"
    win.parent.mkdir(parents=True)
    win.write_text("")
    assert hz.find_cli()["cli"] == str(win)
    win.unlink()
    lin = tmp_path / "home" / ".local" / "share" / "hertzon" / ".venv" / "bin" / "hertzon-cli"
    lin.parent.mkdir(parents=True)
    lin.write_text("")
    assert hz.find_cli()["cli"] == str(lin)


def test_the_child_does_not_inherit_this_pythons_paths(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    monkeypatch.setenv("PYTHONHOME", "/else")
    env = hz.child_env()
    assert "PYTHONPATH" not in env and "PYTHONHOME" not in env


def test_version_and_describe_through_the_stand_in(cli, project):
    assert hz.cli_version() == "0.4.8"
    d = hz.describe(project, "Line50")
    assert d["ok"] and d["cli_schema"] == 1 and "warning" not in d
    line = d["designs"][0]
    assert line["name"] == "Line50" and [v["name"] for v in line["variables"]] == ["w", "L"]


def test_describe_says_why_it_failed(cli, tmp_path):
    bad = tmp_path / "broken.hzn"
    bad.write_text("not json")
    with pytest.raises(hz.HertzonError, match="bad input"):
        hz.describe(bad)
    with pytest.raises(hz.HertzonError, match="no project file"):
        hz.describe(tmp_path / "missing.hzn")


def test_without_hertzon_describe_says_where_it_looked(monkeypatch, project):
    monkeypatch.setenv(hz.ENV_CLI, str(project.parent / "nothing-here"))
    with pytest.raises(hz.HertzonError, match="not installed or not found"):
        hz.describe(project)


# -- the spec and the command line ---------------------------------------------

def test_solve_args_carry_every_option(tmp_path):
    spec = {"design": "Line50", "setup": "Setup1", "sweep": "Sweep",
            "variables": {"w": "1.2mm", "$eps": 4.4, "L": 20},
            "f_start_hz": 1e9, "f_stop_hz": 5.5e9, "f_points": 401,
            "sweep_type": "Interpolating", "cores": 4, "validate": True,
            "require_convergence": True, "fields": True}
    args = hz.solve_args("P.hzn", spec, tmp_path)
    assert args == ["solve", "P.hzn", "--design", "Line50", "--setup", "Setup1",
                    "--sweep", "Sweep", "--set", "w=1.2mm", "--set", "$eps=4.4",
                    "--set", "L=20", "--freq", "1000000000:5500000000:401",
                    "--sweep-type", "interpolating", "--cores", "4", "--validate",
                    "--require-convergence", "--fields", "-o", str(tmp_path),
                    "--progress", "json", "--json"]


def test_points_in_place_of_a_band(tmp_path):
    args = hz.solve_args("P.hzn", {"frequencies_hz": [2.4e9, 5.8e9, 1.5e3]}, tmp_path)
    assert args[args.index("--points") + 1] == "2400000000,5800000000,1500"
    assert "--freq" not in args and "--design" not in args


@pytest.mark.parametrize("spec, says", [
    ({"f_start_hz": 1e9, "f_stop_hz": 2e9}, "needs all of"),
    ({"f_start_hz": 2e9, "f_stop_hz": 1e9, "f_points": 11}, "f_start_hz < f_stop_hz"),
    ({"f_start_hz": 1e9, "f_stop_hz": 2e9, "f_points": 1}, "f_points"),
    ({"f_start_hz": 1e9, "f_stop_hz": 2e9, "f_points": 10.5}, "f_points"),
    ({"f_start_hz": 1e9, "f_stop_hz": 2e9, "f_points": 3, "frequencies_hz": [1e9]},
     "not both"),
    ({"frequencies_hz": []}, "frequencies_hz"),
    ({"frequencies_hz": [0]}, "frequencies_hz"),
    ({"sweep_type": "adaptive"}, "sweep_type"),
    ({"cores": -1}, "cores"),
    ({"variables": {"w mm": 1}}, "variable name"),
    ({"variables": {"w=": 1}}, "variable name"),
    ({"variables": {"w": True}}, "needs a number"),
    ({"variables": {"w": float("nan")}}, "needs a number"),
    ({"variables": {"w": " "}}, "needs a number"),
    ({"design": " Line50"}, "spaces"),
])
def test_bad_specs_are_refused_before_anything_runs(spec, says, tmp_path):
    problems = hz.check_spec(spec)
    assert any(says in p for p in problems), problems
    with pytest.raises(hz.HertzonError):
        hz.solve_args("P.hzn", spec, tmp_path)


def test_the_kind_checks_types_and_the_engine_checks_values():
    from eda_agent.simulation.kinds import check_spec
    assert check_spec("hertzon-solve", {"cores": "four"}, {"project": "x.hzn"})
    assert check_spec("hertzon-solve", {"colour": 1}, {"project": "x.hzn"})
    assert check_spec("hertzon-solve", {}, {})            # the project is required
    eng = sim.get_engine("hertzon")
    assert eng.check("hertzon-solve", {}, {"project": "board.PcbDoc"})
    assert not eng.check("hertzon-solve", {}, {"project": "x.zip"})


# -- the project copy ----------------------------------------------------------

def test_a_hzn_is_copied_under_its_own_name(tmp_path, project):
    target = hz.prepare_project(project, tmp_path / "w", name="Filter.hzn")
    assert target == tmp_path / "w" / "project" / "Filter.hzn"
    assert target.read_text() == project.read_text()
    with pytest.raises(hz.HertzonError, match="project_file is for a .zip"):
        hz.prepare_project(project, tmp_path / "w", project_file="Filter.hzn")


def _zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return path


def test_a_zip_is_unpacked_with_its_assets(tmp_path):
    z = _zip(tmp_path / "p.zip", {"Ant/Ant.hzn": "{}", "Ant/models/drone.stl": "solid x",
                                  "Ant/Ant.hzn.hznresults/old/Old.hzn": "{}"})
    target = hz.prepare_project(z, tmp_path / "w")
    assert target.relative_to(tmp_path / "w" / "project").as_posix() == "Ant/Ant.hzn"
    assert (target.parent / "models" / "drone.stl").is_file()


def test_a_zip_with_two_projects_needs_project_file(tmp_path):
    z = _zip(tmp_path / "p.zip", {"a/A.hzn": "{}", "b/B.hzn": "{}"})
    with pytest.raises(hz.HertzonError, match="name one with project_file"):
        hz.prepare_project(z, tmp_path / "w")
    assert hz.prepare_project(z, tmp_path / "w", project_file="b/B.hzn").name == "B.hzn"
    with pytest.raises(hz.HertzonError, match="no single"):
        hz.prepare_project(z, tmp_path / "w", project_file="C.hzn")


def test_a_zip_cannot_write_outside_its_folder(tmp_path):
    z = _zip(tmp_path / "p.zip", {"../evil.hzn": "{}"})
    with pytest.raises(hz.HertzonError, match="outside"):
        hz.prepare_project(z, tmp_path / "w")
    assert not (tmp_path / "w" / "evil.hzn").exists()


def test_other_files_are_not_projects(tmp_path):
    other = tmp_path / "board.json"
    other.write_text("{}")
    with pytest.raises(hz.HertzonError, match=".hzn file or a .zip"):
        hz.prepare_project(other, tmp_path / "w")
    notzip = tmp_path / "x.zip"
    notzip.write_text("no")
    with pytest.raises(hz.HertzonError, match="not a readable zip"):
        hz.prepare_project(notzip, tmp_path / "w")


# -- whole runs ----------------------------------------------------------------

def test_a_solve_runs_to_checked_s_parameters(cli, project):
    spec = {"design": "Line50", "variables": {"w": "1.2mm"},
            "f_start_hz": 1e9, "f_stop_hz": 5e9, "f_points": 9}
    r = sim.submit("hertzon-solve", "hertzon", spec, {"project": str(project)})
    assert r["ok"] and r["state"] == "running", r
    st = wait_done(r["job"])
    assert st["state"] == "done", st
    res = st["results"]
    m = res["metrics"]
    assert m["ports"]["value"] == 2 and m["points"]["value"] == 9
    assert m["f_max_hz"]["value"] == pytest.approx(5e9)
    assert m["s21_worst_db"]["value"] == pytest.approx(20 * math.log10(0.9), abs=1e-4)
    assert m["worst_return_loss_db"]["value"] == pytest.approx(-20.0, abs=1e-4)
    assert m["adaptive_passes"]["value"] == 3 and m["tetrahedra"]["value"] == 3000
    assert m["z0_ohms"]["value"] == 50.0
    checks = {v["check"]: v["pass"] for v in res["verdicts"]}
    assert checks == {"passivity": True, "reciprocity": True, "convergence": True}
    assert res["files"]["touchstone"] == "Line50_Setup1_Sweep.s2p"
    assert any("Hertzon 0.4.8" in n for n in res["notes"])
    assert any("port 2 is close" in n for n in res["notes"])
    work = Path(r["folder"]) / "work"
    assert (work / res["files"]["touchstone"]).is_file()
    argv = json.loads((work / "fake_argv.json").read_text())
    assert argv[0] == str(work / "project" / "Filter.hzn")
    assert ["--set", "w=1.2mm"] == argv[argv.index("--set"):argv.index("--set") + 2]
    assert "1000000000:5000000000:9" in argv
    # The user's project folder gets no results folder.
    assert sorted(p.name for p in project.parent.iterdir()) == ["Filter.hzn"]
    again = sim.submit("hertzon-solve", "hertzon", spec, {"project": str(project)})
    assert again["state"] == "done" and again.get("cached")


def test_an_unconverged_mesh_is_a_failing_verdict(cli, project, monkeypatch):
    monkeypatch.setenv("FAKE_HERTZON_MODE", "noconverge")
    r = sim.submit("hertzon-solve", "hertzon", {"design": "Line50"},
                   {"project": str(project)})
    st = wait_done(r["job"])
    assert st["state"] == "done", st
    conv = [v for v in st["results"]["verdicts"] if v["check"] == "convergence"][0]
    assert not conv["pass"] and "did NOT converge" in conv["detail"] and "0.5" in conv["detail"]


def test_required_convergence_keeps_the_data_and_fails_the_verdict(cli, project, monkeypatch):
    monkeypatch.setenv("FAKE_HERTZON_MODE", "noconverge")
    r = sim.submit("hertzon-solve", "hertzon", {"design": "Line50",
                                                "require_convergence": True},
                   {"project": str(project)})
    st = wait_done(r["job"])
    assert st["state"] == "done", st
    assert not [v for v in st["results"]["verdicts"] if v["check"] == "convergence"][0]["pass"]
    assert st["results"]["metrics"]["points"]["value"] == 11


def test_a_solver_failure_says_hertzons_reason(cli, project, monkeypatch):
    monkeypatch.setenv("FAKE_HERTZON_MODE", "solverfail")
    r = sim.submit("hertzon-solve", "hertzon", {}, {"project": str(project)})
    st = wait_done(r["job"])
    assert st["state"] == "failed"
    assert "solver failed" in st["error"] and "meshing failed on face 12" in st["error"]


def test_an_unknown_variable_is_bad_input(cli, project):
    r = sim.submit("hertzon-solve", "hertzon", {"design": "Line50", "variables": {"q": 1}},
                   {"project": str(project)})
    st = wait_done(r["job"])
    assert st["state"] == "failed"
    assert "bad input" in st["error"] and "No variable 'q'" in st["error"]


def test_a_program_that_does_not_start_is_reported(tmp_path, project, monkeypatch):
    ghost = tmp_path / "ghost" / "hertzon-cli"
    ghost.parent.mkdir()
    ghost.write_text("")            # found, but not a program
    monkeypatch.setenv(hz.ENV_CLI, str(ghost))
    r = sim.submit("hertzon-solve", "hertzon", {}, {"project": str(project)})
    st = wait_done(r["job"])
    assert st["state"] == "failed" and "could not start" in st["error"]


def test_a_running_solve_can_be_cancelled(cli, project, monkeypatch):
    monkeypatch.setenv("FAKE_HERTZON_MODE", "slow")
    r = sim.submit("hertzon-solve", "hertzon", {"design": "Stub"}, {"project": str(project)})
    assert r["state"] == "running"
    end = time.time() + 20
    while True:              # until Hertzon's first pass shows (a slow start on Windows)
        st = sim.refresh(r["job"])["runs"]["hertzon"]
        if st.get("progress", 0) >= 0.25 or st["state"] != "running" or time.time() > end:
            break
        time.sleep(0.1)
    assert st["state"] == "running"
    # Hertzon's progress events show while it runs.
    assert 0.25 <= st["progress"] < 1.0 and "Adaptive pass" in st["message"], st
    assert "tetrahedra" in st["message"]
    assert sim.cancel(r["job"], "hertzon")["ok"]
    assert sim.refresh(r["job"])["runs"]["hertzon"]["state"] == "cancelled"


def test_without_hertzon_the_engine_is_unavailable(monkeypatch, project):
    monkeypatch.setenv(hz.ENV_CLI, str(project.parent / "none"))
    r = sim.submit("hertzon-solve", "hertzon", {}, {"project": str(project)})
    assert not r["ok"] and "not available" in r["error"] and r["feature"] == "hertzon"


def test_switched_off_on_the_features_page_it_does_not_run(cli, project):
    from eda_agent.features import state
    state.set_switch("hertzon", False)
    r = sim.submit("hertzon-solve", "hertzon", {}, {"project": str(project)})
    assert not r["ok"] and "switched off" in r["error"]


def test_the_status_of_a_run_that_vanished(tmp_path):
    folder = tmp_path / "w"
    folder.mkdir()
    (folder / "run.json").write_text(json.dumps({"pid": 0, "started": time.time()}))
    st = hz.solve_status(folder)
    assert st["state"] == "failed" and "without saying" in st["error"]
    assert hz.solve_status(tmp_path / "nothing")["state"] == "not_started"


# -- features and tools ----------------------------------------------------------

def test_the_feature_reports_hertzon_and_its_version(cli):
    from eda_agent.features import detect, registry
    dep = registry.get_dependency("hertzon")
    assert dep.runs_apart and "GPL" in dep.license
    st = detect.dependency_status(dep, deep=True)
    assert st.where == str(cli) and st.version == "0.4.8"
    status = sim.engine_status(sim.get_engine("hertzon"))
    assert status["available"] and status["process"] == "external"


def test_the_tools_describe_and_solve(cli, project):
    from mcp.server.fastmcp import FastMCP
    from eda_agent.tools.simulation import register_simulation_tools
    m = FastMCP("t")
    register_simulation_tools(m)

    def call(name, args):
        out = asyncio.run(m.call_tool(name, args))
        blocks = out[0] if isinstance(out, tuple) else out
        return json.loads(blocks[0].text)

    d = call("sim_hertzon_describe", {"project": str(project)})
    assert d["ok"] and [x["name"] for x in d["designs"]] == ["Line50", "Stub"]
    r = call("sim_hertzon_solve", {"project": str(project), "design": "Stub",
                                   "variables": {"ls": "8mm"}, "frequencies_hz": [2.4e9, 5e9]})
    assert r["ok"] and r["state"] == "running" and "sim_job_status" in r["next_step"]
    st = wait_done(r["job"])
    assert st["state"] == "done" and st["results"]["metrics"]["points"]["value"] == 2
    bad = call("sim_hertzon_solve", {"project": str(project), "sweep_type": "magic"})
    assert not bad["ok"] and "sweep_type" in bad["error"]
    missing = call("sim_hertzon_describe", {"project": str(project.parent / "x.hzn")})
    assert not missing["ok"] and missing["feature"] == "hertzon"
