# SPDX-License-Identifier: Apache-2.0
"""Dependency and feature status, without touching the real machine."""
from __future__ import annotations

import importlib.metadata as md
import sys

import pytest

from eda_agent.features import catalog as cat
from eda_agent.features import detect
from eda_agent.features.catalog import Dependency, Feature


@pytest.fixture
def versions(monkeypatch):
    table: dict[str, str] = {}

    def fake(name):
        if name in table:
            return table[name]
        raise md.PackageNotFoundError(name)
    monkeypatch.setattr(detect.md, "version", fake)
    return table


def py(id_, **kw):
    return Dependency(id_, id_, cat.PYTHON, "MIT", dist=id_, **kw)


def test_python_present_missing_and_outdated(versions):
    d = py("pkg", requirement="pkg>=2,<3")
    assert detect.dependency_status(d).state == detect.MISSING
    versions["pkg"] = "2.5"
    st = detect.dependency_status(d)
    assert st.state == detect.PRESENT and st.version == "2.5"
    versions["pkg"] = "1.9"
    st = detect.dependency_status(d)
    assert st.state == detect.OUTDATED and "1.9" in st.detail


def test_the_extra_decides_the_wanted_version(versions):
    f = cat.feature("render")
    d = cat.dependency("matplotlib")
    assert "matplotlib" in detect.requirement_for(d, f)
    assert "<4" in detect.requirement_for(d, f)
    versions["matplotlib"] = "4.1"
    assert detect.dependency_status(d, feature=f).state == detect.OUTDATED


def test_unsupported_python_and_platform(versions):
    newer = (sys.version_info.major, sys.version_info.minor + 1)
    assert detect.dependency_status(py("x", python_min=newer)).state == detect.UNSUPPORTED
    other = "nonexistent-os"
    d = Dependency("p", "p", cat.PROGRAM, "MIT", platforms=(other,))
    assert detect.dependency_status(d).state == detect.UNSUPPORTED


def test_extra_requirements_drop_extras_whose_marker_fails(monkeypatch):
    reqs = ["a>=1; extra == 'x'", "b; (python_version >= '9.0') and extra == 'x'",
            "c; extra == 'y'", "d>=2"]
    monkeypatch.setattr(detect.md, "requires", lambda dist: reqs)
    assert detect.extra_requirements("x") == ["a>=1"]
    assert detect.extra_requirements("y") == ["c"]


def test_venv_dependency_is_read_from_its_dist_info(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_ENGINES_DIR", str(tmp_path))
    d = Dependency("si", "SI", cat.VENV, "GPL-3.0", dist="SignalIntegrity", venv="si",
                   requirement="SignalIntegrity")
    assert detect.dependency_status(d).state == detect.MISSING
    py_exe = detect.venv_python("si")
    py_exe.parent.mkdir(parents=True)
    py_exe.write_text("")
    site = tmp_path / "si" / "venv" / ("Lib/site-packages" if sys.platform == "win32"
                                       else "lib/python3.12/site-packages")
    (site / "signalintegrity-1.5.6.dist-info").mkdir(parents=True)
    st = detect.dependency_status(d)
    assert st.state == detect.PRESENT and st.version == "1.5.6"


def test_program_finder_failure_is_missing_not_a_crash(monkeypatch):
    def boom(deep):
        raise OSError("broken")
    monkeypatch.setitem(detect.PROGRAMS, "ngspice", boom)
    st = detect.dependency_status(cat.dependency("ngspice"))
    assert st.state == detect.MISSING and "broken" in st.detail


def test_openems_is_found_by_the_folder_or_python_named(tmp_path, monkeypatch):
    monkeypatch.delenv("EDA_AGENT_OPENEMS_DIR", raising=False)
    monkeypatch.delenv("EDA_AGENT_OPENEMS_PYTHON", raising=False)
    monkeypatch.setattr(detect, "_which_any", lambda *a: "")
    assert detect.dependency_status(cat.dependency("openems")).state == detect.MISSING
    (tmp_path / "openEMS.exe").write_text("")
    monkeypatch.setenv("EDA_AGENT_OPENEMS_DIR", str(tmp_path))
    assert detect.dependency_status(cat.dependency("openems")).state == detect.PRESENT


def _st(**states):
    return {k: detect.DepStatus(k, v) for k, v in states.items()}


def test_feature_states():
    f = Feature("f", "F", "Core", "", needs=("a",), any_of=(("b", "c"),), adds=("d",))
    P, M, U = detect.PRESENT, detect.MISSING, detect.UNSUPPORTED
    assert detect.feature_status(f, _st(a=P, b=M, c=P, d=P), False).state == detect.READY
    assert detect.feature_status(f, _st(a=P, b=P, c=M, d=M), False).state == detect.PARTIAL
    s = detect.feature_status(f, _st(a=M, b=P, c=M, d=P), False)
    assert s.state == detect.NEEDS and s.missing == ["a"]
    s = detect.feature_status(f, _st(a=P, b=M, c=M, d=P), False)
    assert s.state == detect.NEEDS and s.missing == ["b or c"]
    assert detect.feature_status(f, _st(a=U, b=P, c=P, d=P), False).state == detect.UNAVAILABLE
    assert detect.feature_status(f, _st(a=P, b=P, c=P, d=P), True).state == detect.OFF
    core = Feature("c", "C", "Core", "", core=True)
    assert detect.feature_status(core, {}, False).state == detect.READY


def test_report_counts_every_feature(versions):
    from eda_agent.features import report
    rep = report()
    assert sum(rep["counts"].values()) == len(rep["features"])
    ids = {d["id"] for d in rep["dependencies"]}
    for f in rep["features"]:
        assert set(f["needs"]) <= ids
