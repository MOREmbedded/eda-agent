# SPDX-License-Identifier: Apache-2.0
"""The engines, submission, polling and comparison."""
from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from eda_agent import simulation as sim
from eda_agent.emc.touchstone import write_touchstone
from eda_agent.simulation import jobs
from eda_agent.simulation.builtin import s_quality
from eda_agent.simulation.engine import EXTERNAL, LIBRARY, Engine, EngineError, EngineInfo, metric

HAVE_SKRF = True
try:
    import skrf  # noqa: F401
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_SKRF = False


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_SIM_JOBS", str(tmp_path / "jobs"))
    monkeypatch.setenv("EDA_AGENT_FEATURES_FILE", str(tmp_path / "features.json"))
    monkeypatch.setattr(sim, "_loaded", None)
    yield
    sim._loaded = None


def line(n=41, loss=0.9, refl=0.1, gain=1.0, asym=0.0):
    f = np.linspace(1e8, 1e10, n)
    s = np.zeros((n, 2, 2), complex)
    ph = np.exp(-1j * 2 * np.pi * f / 3e9)
    s[:, 0, 0] = s[:, 1, 1] = refl
    s[:, 1, 0] = loss * gain * ph
    s[:, 0, 1] = loss * gain * ph * (1 + asym)
    return f, s


def write(tmp_path, name="line", **kw):
    f, s = line(**kw)
    return write_touchstone(tmp_path / name, list(f), s.tolist())


# -- the S-parameter checks ----------------------------------------------------

def test_lossless_line_is_passive_and_reciprocal():
    f = np.array([1e9])
    s = np.array([[[0, 1], [1, 0]]], complex)
    q = s_quality(f, s, 1e-6)
    assert q["metrics"]["passivity_max_sv"]["value"] == pytest.approx(1.0)
    assert all(v["pass"] for v in q["verdicts"])


def test_gain_fails_passivity_and_asymmetry_fails_reciprocity():
    f, s = line(gain=1.2)
    q = s_quality(f, s, 1e-3)
    assert {v["check"]: v["pass"] for v in q["verdicts"]}["passivity"] is False
    f, s = line(asym=0.05)
    q = s_quality(f, s, 1e-3)
    assert {v["check"]: v["pass"] for v in q["verdicts"]}["reciprocity"] is False
    assert q["metrics"]["reciprocity_max_err"]["value"] == pytest.approx(0.045, rel=1e-6)


def test_return_loss_and_insertion_loss_numbers():
    f, s = line(loss=0.5, refl=0.1)
    q = s_quality(f, s, 1e-3)["metrics"]
    assert q["worst_return_loss_db"]["value"] == pytest.approx(-20.0, abs=1e-3)
    assert q["s21_at_f_max_db"]["value"] == pytest.approx(20 * math.log10(0.5), abs=1e-3)
    f, s = line(refl=0.1)
    s[:, 1, 1] = 0.3
    worst = s_quality(f, s, 1e-3)["metrics"]["worst_return_loss_db"]["value"]
    assert worst == pytest.approx(20 * math.log10(0.3), abs=1e-3), "the worse port sets it"


# -- engines through submit ----------------------------------------------------

def test_builtin_touchstone_check(tmp_path):
    p = write(tmp_path)
    r = sim.submit("touchstone-check", "eda-agent", {}, {"touchstone": str(p)})
    assert r["ok"] and r["state"] == "done"
    m = r["results"]["metrics"]
    assert m["ports"]["value"] == 2 and m["points"]["value"] == 41
    assert m["z0_ohms"]["value"] == 50.0
    again = sim.submit("touchstone-check", "eda-agent", {}, {"touchstone": str(p)})
    assert again["cached"] and again["job"] == r["job"]
    forced = sim.submit("touchstone-check", "eda-agent", {}, {"touchstone": str(p)}, force=True)
    assert forced["state"] == "done" and "cached" not in forced
    # an answer kept from other code is computed again, not handed back
    again = sim.submit("touchstone-check", "eda-agent", {}, {"touchstone": str(p)})
    assert again["cached"]
    sim._code_stamp = "an-older-eda-agent"
    try:
        newer = sim.submit("touchstone-check", "eda-agent", {}, {"touchstone": str(p)})
        assert newer["state"] == "done" and "cached" not in newer
        assert sim.submit("touchstone-check", "eda-agent", {}, {"touchstone": str(p)})["cached"]
    finally:
        sim._code_stamp = None
    assert len(sim.code_stamp()) == 16 and sim.code_stamp() == sim.code_stamp()


@pytest.mark.skipif(not HAVE_SKRF, reason="scikit-rf not installed")
def test_scikit_rf_agrees_with_the_builtin_engine(tmp_path):
    p = write(tmp_path, loss=0.7, refl=0.2)
    a = sim.submit("touchstone-check", "eda-agent", {}, {"touchstone": str(p)})
    b = sim.submit("touchstone-check", "scikit-rf", {}, {"touchstone": str(p)})
    assert a["job"] == b["job"]
    c = sim.compare(a["job"])
    assert c["agree"] and c["engines"] == ["eda-agent", "scikit-rf"]
    names = {r["metric"] for r in c["metrics"]}
    assert {"passivity_max_sv", "reciprocity_max_err", "worst_return_loss_db"} <= names


@pytest.mark.skipif(not HAVE_SKRF, reason="scikit-rf not installed")
def test_scikit_rf_catches_an_active_file_too(tmp_path):
    p = write(tmp_path, gain=1.3)
    r = sim.submit("touchstone-check", "scikit-rf", {}, {"touchstone": str(p)})
    v = {x["check"]: x["pass"] for x in r["results"]["verdicts"]}
    assert v["passivity"] is False and v["passivity (scikit-rf is_passive)"] is False


def test_xsection_engine_is_the_toolkit_calculator():
    from eda_agent.engineering import toolkit
    want = next(o["value"] for o in toolkit.run("microstrip", {})["outputs"] if o["key"] == "z0")
    r = sim.submit("xsection-2d", "eda-agent", {"calculator": "microstrip"})
    assert r["ok"], r
    z = r["results"]["metrics"]["z0"]
    assert z["value"] == pytest.approx(want) and z["unit"] == "ohm"


def test_unknown_calculator_and_wrong_kind_are_refused():
    r = sim.submit("xsection-2d", "eda-agent", {"calculator": "no_such"})
    assert not r["ok"] and "no calculator" in r["error"]
    r = sim.submit("em-3d", "eda-agent", {"nets": ["X"]}, {})
    assert not r["ok"] and "does not run em-3d" in r["error"]
    with pytest.raises(KeyError):
        sim.submit("touchstone-check", "no-such-engine", {}, {})


def test_a_switched_off_engine_is_not_used(tmp_path):
    from eda_agent.features import state
    state.set_switch("scikit-rf", False)
    p = write(tmp_path)
    r = sim.submit("touchstone-check", "scikit-rf", {}, {"touchstone": str(p)})
    assert not r["ok"] and "switched off" in r["error"]


def test_a_bad_file_fails_the_run_with_the_reason(tmp_path):
    p = tmp_path / "bad.s2p"
    p.write_text("# GHz S RI R 50\n1 2 3\n")
    r = sim.submit("touchstone-check", "eda-agent", {}, {"touchstone": str(p)})
    assert not r["ok"] and r["state"] == "failed" and "whole number" in r["error"]
    st = sim.refresh(r["job"])
    assert st["runs"]["eda-agent"]["state"] == "failed"


# -- plugins, background runs, comparison ----------------------------------------

class Slow(Engine):
    info = EngineInfo("slow", "Slow", "core", "MIT", LIBRARY, ("xsection-2d",))

    def run(self, job, folder, progress):
        progress("thinking", 0.5)
        time.sleep(0.2)
        return {"metrics": {"z0": metric(51.0, "ohm")}}


class Crash(Engine):
    info = EngineInfo("crash", "Crash", "core", "MIT", LIBRARY, ("xsection-2d",))

    def run(self, job, folder, progress):
        raise ZeroDivisionError("oops")


class EP:
    def __init__(self, fn):
        self.name, self.value, self._fn = "x", "pkg:x", fn

    def load(self):
        return self._fn


def _with_plugins(monkeypatch, *makers):
    monkeypatch.setattr(sim.md, "entry_points", lambda group: [EP(m) for m in makers])
    sim._loaded = None


def test_plugin_engines_and_their_errors(monkeypatch):
    bad = EngineInfo("bad", "Bad", "no-feature", "MIT", LIBRARY, ("xsection-2d",))
    odd = EngineInfo("odd", "Odd", "core", "MIT", LIBRARY, ("no-kind",))

    def mk(info):
        e = Engine()
        e.info = info
        return e
    _with_plugins(monkeypatch, lambda: Slow(), lambda: [mk(bad), mk(odd)],
                  lambda: object(), lambda: (_ for _ in ()).throw(RuntimeError("load")))
    ids = {e.info.id for e in sim.engines()}
    assert "slow" in ids and "bad" not in ids and "odd" not in ids
    errors = " | ".join(e["error"] for e in sim._load()["errors"])
    assert "no-feature" in errors and "no-kind" in errors and "not an Engine" in errors
    assert "load" in errors


def test_a_background_run_finishes_and_compares(monkeypatch):
    _with_plugins(monkeypatch, lambda: Slow())
    spec = {"calculator": "microstrip"}
    r = sim.submit("xsection-2d", "slow", spec, wait=False)
    assert r["state"] == "running"
    st = sim.refresh(r["job"])
    assert st["runs"]["slow"]["state"] == "running", "a live thread is not a dead run"
    for _ in range(100):
        if sim.refresh(r["job"])["runs"]["slow"]["state"] != "running":
            break
        time.sleep(0.05)
    assert sim.refresh(r["job"])["runs"]["slow"]["state"] == "done"
    sim.submit("xsection-2d", "eda-agent", spec)
    c = sim.compare(r["job"], rel_tol=0.1)
    row = next(x for x in c["metrics"] if x["metric"] == "z0")
    assert row["values"]["slow"] == 51.0 and row["agree"]
    z = row["values"]["eda-agent"]
    assert row["spread"] == pytest.approx(abs(51.0 - z) / max(51.0, z), rel=1e-9)
    tight = sim.compare(r["job"], rel_tol=row["spread"] / 2)
    assert not tight["agree"]


def test_an_engine_that_crashes_leaves_a_trace(monkeypatch):
    _with_plugins(monkeypatch, lambda: Crash())
    r = sim.submit("xsection-2d", "crash", {"calculator": "microstrip"})
    assert not r["ok"] and "ZeroDivisionError" in r["error"]
    job = jobs.get(r["job"])
    assert "Traceback" in (job.run_folder("crash") / "error.log").read_text()


def test_a_run_whose_process_died_is_marked_failed(monkeypatch):
    r = sim.submit("xsection-2d", "eda-agent", {"calculator": "microstrip"})
    folder = jobs.get(r["job"]).run_folder("eda-agent")
    jobs.set_status(folder, jobs.RUNNING, owner_pid=2 ** 22 + 12345)
    st = sim.refresh(r["job"])
    assert st["runs"]["eda-agent"]["state"] == "failed"
    assert "interrupted" in st["runs"]["eda-agent"]["error"]


def test_compare_reports_verdict_disagreement(tmp_path):
    job = jobs.create("touchstone-check", {}, {"touchstone": str(write(tmp_path))})
    for name, ok in (("a", True), ("b", False)):
        folder = job.run_folder(name)
        jobs.write_json(folder / "results.json", {"ok": True, "metrics": {"x": metric(1.0)},
                                                  "verdicts": [{"check": "passivity", "pass": ok}]})
        jobs.set_status(folder, jobs.DONE)
    c = sim.compare(job.id)
    assert not c["agree"] and c["verdict_disagreements"][0]["votes"] == {"a": True, "b": False}


# -- openEMS through the stand-in ----------------------------------------------

FAKE = Path(__file__).resolve().parents[1] / "emc" / "fake_openems"


def test_openems_engine_runs_and_reports_through_the_stand_in(tmp_path, monkeypatch):
    from tests.emc.boards import STACKUP, microstrip_board
    import json
    board = tmp_path / "b.json"
    microstrip_board().save(board)
    stack = tmp_path / "s.json"
    stack.write_text(json.dumps(STACKUP))
    monkeypatch.setenv("PYTHONPATH", str(FAKE) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("EDA_AGENT_OPENEMS_PYTHON", sys.executable)
    monkeypatch.delenv("EDA_AGENT_OPENEMS_DIR", raising=False)
    spec = {"nets": ["RF"], "ports": ["J1.1", "J2.1"], "f_max_mhz": 3000}
    r = sim.submit("em-3d", "openems", spec, {"board": str(board), "stackup": str(stack)})
    assert r["ok"] and r["state"] == "running", r
    for _ in range(300):
        st = sim.refresh(r["job"])["runs"]["openems"]
        if st["state"] != "running":
            break
        time.sleep(0.2)
    assert st["state"] == "done", st
    res = st["results"]
    assert res["files"]["touchstone"].endswith(".s2p")
    assert res["metrics"]["s21_worst_db"]["value"] == pytest.approx(20 * math.log10(0.9), abs=0.01)


def test_openems_engine_without_openems_is_unavailable(monkeypatch, tmp_path):
    from eda_agent.features import detect
    monkeypatch.delenv("EDA_AGENT_OPENEMS_DIR", raising=False)
    monkeypatch.delenv("EDA_AGENT_OPENEMS_PYTHON", raising=False)
    monkeypatch.setattr(detect, "_which_any", lambda *a: "")
    r = sim.submit("em-3d", "openems", {"nets": ["RF"]}, {"board": str(tmp_path / "x.json")})
    assert not r["ok"] and "not available" in r["error"]


def test_a_run_that_finishes_while_being_polled_stays_done(monkeypatch):
    # The poll read "running", then the worker wrote "done" and ended
    # before the owner check: the finished result must not become failed.
    import os
    import threading
    r = sim.submit("xsection-2d", "eda-agent", {"calculator": "microstrip"})
    job = jobs.get(r["job"])
    assert jobs.runs(job)["eda-agent"]["state"] == "done"
    ended = threading.Thread(target=lambda: None)
    ended.start()
    ended.join()
    monkeypatch.setitem(sim._threads, f"{job.id}/eda-agent", ended)
    stale = {"eda-agent": {"state": jobs.RUNNING, "owner_pid": os.getpid()}}
    real_runs = jobs.runs
    calls = []

    def runs_once_stale(j):
        calls.append(1)
        return stale if len(calls) == 1 else real_runs(j)
    monkeypatch.setattr(jobs, "runs", runs_once_stale)
    st = sim.refresh(job.id)
    assert st["runs"]["eda-agent"]["state"] == "done"
