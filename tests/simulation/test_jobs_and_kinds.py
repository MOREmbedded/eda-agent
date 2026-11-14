# SPDX-License-Identifier: Apache-2.0
"""Job folders and job-kind specs."""
from __future__ import annotations

import time

import pytest

from eda_agent.simulation import jobs
from eda_agent.simulation.kinds import check_spec


def test_the_same_problem_is_the_same_job(tmp_path):
    f = tmp_path / "a.s2p"
    f.write_text("# Hz S RI R 50\n")
    a = jobs.create("touchstone-check", {"tolerance": 0.01}, {"touchstone": str(f)}, base=tmp_path / "j")
    b = jobs.create("touchstone-check", {"tolerance": 0.01}, {"touchstone": str(f)}, base=tmp_path / "j")
    assert a.id == b.id and a.folder == b.folder
    assert a.input_path("touchstone").read_text() == "# Hz S RI R 50\n"
    c = jobs.create("touchstone-check", {"tolerance": 0.02}, {"touchstone": str(f)}, base=tmp_path / "j")
    assert c.id != a.id
    f.write_text("# Hz S RI R 75\n")
    d = jobs.create("touchstone-check", {"tolerance": 0.01}, {"touchstone": str(f)}, base=tmp_path / "j")
    assert d.id != a.id, "a changed input file must be a new job"


def test_spec_key_order_does_not_make_a_new_job(tmp_path):
    a = jobs.create("xsection-2d", {"calculator": "m", "inputs": {"a": 1, "b": 2}}, base=tmp_path)
    b = jobs.create("xsection-2d", {"inputs": {"b": 2, "a": 1}, "calculator": "m"}, base=tmp_path)
    assert a.id == b.id


def test_a_missing_input_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        jobs.create("touchstone-check", {}, {"touchstone": str(tmp_path / "none.s2p")}, base=tmp_path)


def test_jobs_are_listed_newest_first_with_their_runs(tmp_path):
    a = jobs.create("xsection-2d", {"calculator": "a"}, base=tmp_path)
    time.sleep(0.01)
    b = jobs.create("xsection-2d", {"calculator": "b"}, base=tmp_path)
    jobs.set_status(b.run_folder("eda-agent"), jobs.RUNNING)
    st = jobs.set_status(b.run_folder("eda-agent"), jobs.DONE)
    assert st["seconds"] >= 0
    rows = jobs.list_jobs(base=tmp_path)
    assert [r["id"] for r in rows] == [b.id, a.id]
    assert rows[0]["runs"]["eda-agent"]["state"] == jobs.DONE


def test_get_refuses_path_tricks(tmp_path):
    with pytest.raises(KeyError):
        jobs.get("../etc", base=tmp_path)
    with pytest.raises(KeyError):
        jobs.get("", base=tmp_path)


def test_delete_removes_only_a_job_folder(tmp_path):
    a = jobs.create("xsection-2d", {"calculator": "a"}, base=tmp_path)
    jobs.delete(a)
    assert not a.folder.exists()
    fake = jobs.Job("x", "k", "t", {}, folder=tmp_path)
    with pytest.raises(ValueError):
        jobs.delete(fake)
    assert tmp_path.exists()


@pytest.mark.parametrize("kind, spec, inputs, needle", [
    ("touchstone-check", {"tolerence": 1}, {"touchstone": "x"}, "unknown spec key 'tolerence'"),
    ("touchstone-check", {}, {}, "input 'touchstone'"),
    ("touchstone-check", {"tolerance": "big"}, {"touchstone": "x"}, "must be a number"),
    ("touchstone-check", {"tolerance": True}, {"touchstone": "x"}, "must be a number"),
    ("touchstone-check", {}, {"touchstone": "x", "board": "y"}, "unknown input 'board'"),
    ("xsection-2d", {}, {}, "'calculator' is required"),
    ("em-3d", {"nets": "RF"}, {"board": "b.json"}, "must be a list"),
    ("channel-eye", {"data_rate_gbps": 1, "ctle_db": [3]}, {"touchstone": "x"},
     "'ctle_db' must be a number or a text"),
    ("channel-eye", {"data_rate_gbps": 1, "ctle_db": True}, {"touchstone": "x"},
     "'ctle_db' must be a number or a text"),
    ("channel-eye", {"data_rate_gbps": 1, "tx_ffe": 0.5}, {"touchstone": "x"},
     "'tx_ffe' must be a list or a text"),
])
def test_check_spec(kind, spec, inputs, needle):
    problems = check_spec(kind, spec, inputs)
    assert any(needle in p for p in problems), problems


def test_check_spec_accepts_a_good_spec_and_an_optional_input():
    assert check_spec("em-3d", {"nets": ["RF"], "f_max_mhz": 3000}, {"board": "b.json"}) == []
    assert check_spec("touchstone-check", {"tolerance": 0.01}, {"touchstone": "x"}) == []
    with pytest.raises(KeyError, match="known"):
        check_spec("nope", {}, {})


def test_a_status_being_replaced_is_read_not_taken_for_missing(tmp_path, monkeypatch):
    """Windows refuses to open a file in the instant it is being replaced."""
    from pathlib import Path
    path = tmp_path / "status.json"
    jobs.write_json(path, {"state": "running"})
    real, refused = Path.read_text, []

    def read_text(self, *a, **k):
        if self == path and len(refused) < 3:
            refused.append(1)
            raise PermissionError(13, "being replaced")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(jobs, "READ_RETRY_S", 0.0)
    assert jobs.read_json(path) == {"state": "running"}
    assert len(refused) == 3


def test_a_file_that_stays_refused_or_is_missing_reads_as_none(tmp_path, monkeypatch):
    from pathlib import Path
    path = tmp_path / "status.json"
    jobs.write_json(path, {"state": "running"})
    calls = []

    def read_text(self, *a, **k):
        calls.append(self)
        raise PermissionError(13, "locked") if self == path else FileNotFoundError(2, "gone")

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(jobs, "READ_RETRY_S", 0.0)
    assert jobs.read_json(path) is None
    assert len(calls) == jobs.READ_RETRIES + 1
    del calls[:]
    assert jobs.read_json(tmp_path / "none.json") is None
    assert len(calls) == 1, "a missing file is not waited for"
