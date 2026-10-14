# SPDX-License-Identifier: Apache-2.0
"""eda-agent profile-*: scaffold/list/use/show/validate/new-project.

These exercise the CLI glue against a temp HOME and temp cwd, isolated
from whatever real ~/.eda-agent a developer running the suite might
have (same isolation discipline as test_profile.py).
"""
from __future__ import annotations

import json

import pytest

from eda_agent import cli, profile as profile_mod


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("EDA_AGENT_PROFILE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)


def test_init_then_list_then_use_then_show(tmp_path, capsys):
    rc = cli.cmd_profile_init("acme", here=False)
    assert rc == 0
    assert (tmp_path / "home" / ".eda-agent" / "profiles" / "acme" / "profile.json").exists()
    capsys.readouterr()

    rc = cli.cmd_profile_list()
    assert rc == 0
    assert "acme" in capsys.readouterr().out

    rc = cli.cmd_profile_use("acme")
    assert rc == 0
    assert profile_mod.active_profile_pointer().read_text(encoding="utf-8") == "acme"
    capsys.readouterr()

    rc = cli.cmd_profile_show(as_json=True)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["active"] is True
    assert data["meta"]["name"] == "acme"
    assert data["footprint_defaults"]["pad_corner_radius_pct"] == 25


def test_init_refuses_a_nonempty_existing_directory(tmp_path):
    rc = cli.cmd_profile_init("acme", here=False)
    assert rc == 0
    rc = cli.cmd_profile_init("acme", here=False)
    assert rc == 1


def test_init_here_scaffolds_a_project_local_profile_with_the_given_name(tmp_path, capsys):
    rc = cli.cmd_profile_init("widgetco", here=True)
    assert rc == 0
    profile_dir = tmp_path / ".eda-agent" / "profile"
    assert (profile_dir / "profile.json").exists()
    meta = json.loads((profile_dir / "profile.json").read_text(encoding="utf-8"))
    assert meta["name"] == "widgetco"


def test_project_profile_overrides_the_central_one_in_show(tmp_path, capsys):
    cli.cmd_profile_init("acme", here=False)
    cli.cmd_profile_use("acme")
    capsys.readouterr()
    cli.cmd_profile_init("widgetco", here=True)
    capsys.readouterr()

    cli.cmd_profile_show(as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert data["meta"]["name"] == "widgetco"


def test_use_refuses_an_unknown_profile(capsys):
    rc = cli.cmd_profile_use("nope")
    assert rc == 1
    assert "profile-init" in capsys.readouterr().err


def test_show_with_no_active_profile(capsys):
    rc = cli.cmd_profile_show(as_json=True)
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"active": False}


def test_validate_reports_a_malformed_ruleset(tmp_path, capsys):
    cli.cmd_profile_init("acme", here=False)
    rules_dir = tmp_path / "home" / ".eda-agent" / "profiles" / "acme" / "rules"
    (rules_dir / "bad.json").write_text('{"ruleset": "bad", "rules": [{"id": "r1", "type": "nonsense"}]}',
                                        encoding="utf-8")
    capsys.readouterr()

    rc = cli.cmd_profile_validate(name="acme", here=False)
    assert rc == 1
    assert "bad.json" in capsys.readouterr().err


def test_validate_passes_a_well_formed_profile(tmp_path):
    cli.cmd_profile_init("acme", here=False)
    rc = cli.cmd_profile_validate(name="acme", here=False)
    assert rc == 0


def test_validate_reports_an_invalid_fab_profile(tmp_path, capsys):
    cli.cmd_profile_init("acme", here=False)
    fab_dir = tmp_path / "home" / ".eda-agent" / "profiles" / "acme" / "fab_profiles"
    (fab_dir / "bad.json").write_text('{"name": "x", "layers": []}', encoding="utf-8")
    capsys.readouterr()

    rc = cli.cmd_profile_validate(name="acme", here=False)
    assert rc == 1
    assert "bad.json" in capsys.readouterr().err


def test_new_project_copies_a_template(tmp_path):
    cli.cmd_profile_init("acme", here=False)
    cli.cmd_profile_use("acme")
    templates = tmp_path / "home" / ".eda-agent" / "profiles" / "acme" / "templates" / "default"
    templates.mkdir(parents=True)
    (templates / "stub.PrjPcb").write_text("[Design]\n", encoding="utf-8")

    dest = tmp_path / "new_board"
    rc = cli.cmd_profile_new_project("default", str(dest), force=False)
    assert rc == 0
    assert (dest / "stub.PrjPcb").read_text(encoding="utf-8") == "[Design]\n"


def test_new_project_refuses_an_unknown_template(tmp_path):
    cli.cmd_profile_init("acme", here=False)
    cli.cmd_profile_use("acme")
    (tmp_path / "home" / ".eda-agent" / "profiles" / "acme" / "templates").mkdir(exist_ok=True)

    rc = cli.cmd_profile_new_project("missing", str(tmp_path / "dest"), force=False)
    assert rc == 1


def test_new_project_refuses_a_nonempty_destination_without_force(tmp_path):
    cli.cmd_profile_init("acme", here=False)
    cli.cmd_profile_use("acme")
    templates = tmp_path / "home" / ".eda-agent" / "profiles" / "acme" / "templates" / "default"
    templates.mkdir(parents=True)
    (templates / "stub.PrjPcb").write_text("x", encoding="utf-8")

    dest = tmp_path / "new_board"
    dest.mkdir()
    (dest / "existing.txt").write_text("keep me", encoding="utf-8")

    rc = cli.cmd_profile_new_project("default", str(dest), force=False)
    assert rc == 1
    assert (dest / "existing.txt").exists()

    rc = cli.cmd_profile_new_project("default", str(dest), force=True)
    assert rc == 0
    assert (dest / "stub.PrjPcb").exists()
