# SPDX-License-Identifier: Apache-2.0
"""The openEMS model, the runner (on a stand-in for openEMS) and the
results coming back as Touchstone."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from eda_agent.emc import openems as em
from eda_agent.emc.stack import stack_from_stackup
from eda_agent.emc.touchstone import read_touchstone
from eda_agent.layout import geom
from eda_agent.layout.model import Region

from .boards import LAYERS, STACKUP, microstrip_board

FAKE = Path(__file__).with_name("fake_openems")


def _model(**kw):
    b = microstrip_board()
    return em.build_model(b, ["RF"], ["J1.1", "J2.1"], stack_from_stackup(STACKUP, LAYERS),
                          f_max_hz=kw.pop("f_max_hz", 3e9), **kw)


# -- geometry helpers -------------------------------------------------------

def test_clip_keeps_the_inside_part():
    sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
    out = em.clip_rect(sq, 5, -1, 20, 20)
    assert sorted(out) == sorted([(5, 0), (10, 0), (10, 10), (5, 10)])
    assert em.clip_rect(sq, 20, 20, 30, 30) == []


def test_keyhole_keeps_the_hole_out():
    outer = [(0, 0), (100, 0), (100, 100), (0, 100)]
    hole = [(40, 40), (60, 40), (60, 60), (40, 60)]
    ring = em.keyhole(outer, [hole])
    poly = geom.polygon(ring)
    assert geom.point_in_poly(10, 10, poly)
    assert not geom.point_in_poly(50, 50, poly)
    assert geom.ring_area(ring) == pytest.approx(100 * 100 - 20 * 20)


def test_track_polygon_covers_the_capsule():
    poly = em.shape_polygon(geom.capsule(0, 0, 100, 0, 10))
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    assert min(xs) == pytest.approx(-5) and max(xs) == pytest.approx(105)
    assert min(ys) == pytest.approx(-5) and max(ys) == pytest.approx(5)


# -- model --------------------------------------------------------------------

def test_microstrip_model_geometry():
    m = _model()
    assert m["nets"] == ["RF"] and m["mode"] == "sparams"
    # box: the line's copper plus 200 mil margin, centred
    assert m["box_mils"] == [0.0, 100.0, 1400.0, 500.0]
    roles = sorted({s["role"] for s in m["sheets"]})
    assert roles == ["return", "signal"]
    z = {s["role"]: s["z"] for s in m["sheets"]}
    assert z["signal"] == pytest.approx(56 * 0.0254)
    assert z["return"] == pytest.approx(48 * 0.0254)
    p1, p2 = m["ports"]
    assert (p1["name"], p1["reference_layer"]) == ("J1.1", "MidLayer1")
    # port spans the 8 mil prepreg, from the GND layer up to the pad
    assert p1["start"][2] == pytest.approx(48 * 0.0254)
    assert p1["stop"][2] == pytest.approx(56 * 0.0254)
    # a vertical line at the pad centre, with mesh lines through it
    assert p1["start"][:2] == p1["stop"][:2] == pytest.approx([-500 * 0.0254, 0.0])
    assert p1["start"][0] in m["mesh"]["keep_x"] and 0.0 in m["mesh"]["keep_y"]
    assert 20_000 <= m["max_steps"] <= 2_000_000
    # dielectrics fill the box, er from the stackup
    assert [d["er"] for d in m["dielectrics"]] == [4.2, 4.6, 4.2]
    assert m["boundary"] == ["MUR"] * 6 and not m["nf2ff"]
    # every layer height is a mesh line
    for s in m["sheets"]:
        assert s["z"] in m["mesh"]["z"]
    # mesh: 20 cells per wavelength in er 4.6 at 3 GHz
    assert m["mesh"]["res_mm"] == pytest.approx(299792458 / 3e9 * 1e3 / 4.6 ** 0.5 / 20, rel=1e-4)


def test_the_model_refuses_what_it_cannot_build():
    b = microstrip_board()
    s = stack_from_stackup(STACKUP, LAYERS)
    with pytest.raises(em.ModelError, match="not on the board"):
        em.build_model(b, ["NOPE"], [], s)
    with pytest.raises(em.ModelError, match="no pad"):
        em.build_model(b, ["RF"], ["J9.1"], s)
    b.regions.clear()
    with pytest.raises(em.ModelError, match="no return copper"):
        em.build_model(b, ["RF"], ["J1.1"], s)
    with pytest.raises(em.ModelError, match="M cells"):
        _model(f_max_hz=200e9)


def test_default_ports_are_the_net_pads():
    b = microstrip_board()
    m = em.build_model(b, ["RF"], [], stack_from_stackup(STACKUP, LAYERS), f_max_hz=3e9)
    assert [p["name"] for p in m["ports"]] == ["J1.1", "J2.1"]


def test_plane_holes_become_keyholes():
    b = microstrip_board()
    b.regions[0] = Region("MidLayer1", list(b.regions[0].outline),
                          [[(600, 250), (700, 250), (700, 350), (600, 350)]], "GND", "pour")
    m = em.build_model(b, ["RF"], ["J1.1", "J2.1"], stack_from_stackup(STACKUP, LAYERS),
                       f_max_hz=3e9)
    ret = [s for s in m["sheets"] if s["role"] == "return"][0]
    assert len(ret["pts"]) == 4 + 5 + 1


def test_antenna_mode_adds_air_pml_and_far_field():
    m = _model(mode="antenna", f_min_hz=2e9)
    assert m["nf2ff"] and m["boundary"] == ["PML_8"] * 6
    air = 299792458 / 2e9 * 1e3 / 4
    assert m["sim_box"]["z"][0] == pytest.approx(-air)


# -- runner on the stand-in ---------------------------------------------------

def _run(folder: Path):
    env = dict(os.environ, PYTHONPATH=str(FAKE))
    return subprocess.run([sys.executable, str(folder / "openems_runner.py"),
                           str(folder / "model.json")], env=env, capture_output=True,
                          text=True, timeout=120)


def test_runner_end_to_end_on_the_stand_in(tmp_path):
    m = _model()
    m["points"] = 11
    folder = em.write_model(m, tmp_path)
    proc = _run(folder)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert em.run_status(folder)["state"] == "done"
    calls = json.loads((folder / "calls.json").read_text())["calls"]
    ports = [c for c in calls if c[0] == "port"]
    # two excitations, two ports each; port 2 driven in the second
    assert [(c[1], c[6]) for c in ports] == [(1, 1.0), (2, 0.0), (1, 0.0), (2, 1.0)]
    assert sum(c[0] == "polygon" for c in calls) == 2 * len(m["sheets"])
    out = em.collect_results(folder, plot=False)
    assert out["ok"] and out["settled"] is True and "warning" not in out
    assert out["port_decay_db"] == [pytest.approx(-60.0)] * 2
    ts = read_touchstone(out["touchstone"])
    assert ts["ports"] == 2 and len(ts["freq_hz"]) == 11
    assert abs(ts["s"][0][0][0]) == pytest.approx(0.1)
    assert abs(ts["s"][0][1][0]) == pytest.approx(0.9)
    assert out["summary"]["s21"]["worst_db"] == pytest.approx(20 * __import__("math").log10(0.9), abs=0.01)


def test_runner_failure_is_reported(tmp_path):
    m = _model()
    folder = em.write_model(m, tmp_path)
    env = dict(os.environ, PYTHONPATH=str(tmp_path / "nothing"))
    subprocess.run([sys.executable, str(folder / "openems_runner.py"),
                    str(folder / "model.json")], env=env, capture_output=True, timeout=120)
    st = em.run_status(folder)
    assert st["state"] == "failed" and "ModuleNotFoundError" in st["error"]


def test_antenna_run_reports_directivity_and_efficiency(tmp_path):
    m = _model(mode="antenna", f_min_hz=2e9)
    m["points"] = 5
    folder = em.write_model(m, tmp_path)
    assert _run(folder).returncode == 0
    out = em.collect_results(folder, plot=False)
    ant = out["antenna"]
    assert ant["directivity_dbi"] == pytest.approx(10 * __import__("math").log10(2.0))
    assert ant["efficiency"] == pytest.approx(0.8)
    assert max(ant["pattern"]["phi0"]) == pytest.approx(ant["directivity_dbi"])


def test_start_run_without_openems_starts_nothing(tmp_path, monkeypatch):
    folder = em.write_model(_model(), tmp_path)
    monkeypatch.setattr(em, "_python_can_import", lambda p: False)
    monkeypatch.delenv("EDA_AGENT_OPENEMS_PYTHON", raising=False)
    out = em.start_run(folder)
    assert not out["ok"] and "openEMS" in out["error"] and out["how"]
    assert not (folder / "run.json").exists()


def test_start_run_runs_in_the_background(tmp_path, monkeypatch):
    folder = em.write_model(dict(_model(), points=5), tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(FAKE))
    out = em.start_run(folder, python=sys.executable)
    assert out["ok"]
    import time
    for _ in range(100):
        if em.run_status(folder)["state"] != "running":
            break
        time.sleep(0.2)
    assert em.run_status(folder)["state"] == "done"


def test_runner_retries_a_rename_a_scanner_blocks(tmp_path, monkeypatch):
    """The runner carries its own copy of atomicfile's retry."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("runner", em.RUNNER)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    real = os.replace
    state = {"n": 0}

    def flaky(a, b):
        state["n"] += 1
        if state["n"] <= 2:
            raise PermissionError(32, "held by a scanner")
        return real(a, b)

    monkeypatch.setattr(runner.os, "replace", flaky)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    runner._write(str(tmp_path / "status.json"), {"x": 1})
    assert json.loads((tmp_path / "status.json").read_text()) == {"x": 1}
    assert state["n"] == 3


def test_near_duplicate_mesh_lines_are_merged():
    """A hair-thin cell sets a hair-thin time step and the solve crawls."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("runner2", em.RUNNER)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    assert runner.merge_lines([0.17780, 0.1778049, -1.0, 0.5, 0.17781], 0.01) == [-1.0, 0.1778, 0.5]
    # a port's line always stays; the neighbour that crowds it goes
    assert runner.merge_lines([0.0, 0.004, 1.0], 0.01, keep=[0.005]) == [0.005, 1.0]
    m = _model()
    assert 0.005 <= m["mesh"]["min_gap_mm"] <= 14 * 0.0254 / 3 + 1e-9


def _runner_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("runner3", em.RUNNER)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def test_port_decay_reads_the_probe_files(tmp_path):
    runner = _runner_module()
    (tmp_path / "port_ut1").write_text("% time voltage\n0 0\n1 1.0\n2 0.5\n"
                                       + "".join(f"{k} 1e-4\n" for k in range(3, 23)))
    (tmp_path / "port_ut2").write_text("% t u\n0 2\n1 1e-3\n" + "".join(
        f"{k} 2e-6\n" for k in range(2, 22)))
    assert runner.port_decay(str(tmp_path), 2) == pytest.approx(-80.0)
    (tmp_path / "port_ut2").write_text("% t u\n0 2\n" + "".join(f"{k} 0.2\n" for k in range(1, 21)))
    assert runner.port_decay(str(tmp_path), 2) == pytest.approx(-20.0)
    assert runner.port_decay(str(tmp_path), 3) is None


def test_an_unsettled_run_is_flagged(tmp_path):
    m = dict(_model(), points=5)
    folder = em.write_model(m, tmp_path)
    assert _run(folder).returncode == 0
    res = json.loads((folder / "results.json").read_text())
    res["settled"], res["port_decay_db"] = False, [-12.0, -15.0]
    (folder / "results.json").write_text(json.dumps(res))
    out = em.collect_results(folder, plot=False)
    assert out["settled"] is False and "unconverged" in out["warning"]
