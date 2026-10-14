# SPDX-License-Identifier: Apache-2.0
"""The per-workspace profile: discovery order, parsing, and the
EDA_AGENT_IPC_DIR hand-off to the existing naming/ipc7351 readers."""
from __future__ import annotations

import json
import os

import pytest

from eda_agent import profile as profile_mod


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Every test gets its own fake HOME and starts with no profile env set.

    apply_env() under test sets os.environ["EDA_AGENT_IPC_DIR"] directly as
    its real, intended side effect (see profile.py) -- monkeypatch only
    tracks changes IT makes, so a test that triggers that mutation would
    otherwise leak the var into every test run afterward (it did: it broke
    ipc7351.load_goals() in test_engineering_integration.py when the two
    files ran in the same session). Force an explicit, unconditional
    restore after every test here instead of trusting monkeypatch to catch
    a mutation it never performed itself.
    """
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("EDA_AGENT_PROFILE_DIR", raising=False)
    monkeypatch.delenv("EDA_AGENT_IPC_DIR", raising=False)
    monkeypatch.delenv("ALTIUM_AI_IPC_DIR", raising=False)
    yield
    os.environ.pop("EDA_AGENT_IPC_DIR", None)
    os.environ.pop("ALTIUM_AI_IPC_DIR", None)


def test_no_profile_resolves_to_none(tmp_path):
    assert profile_mod.resolve_profile_dir(tmp_path) is None
    assert profile_mod.get_active_profile(tmp_path) is None
    assert profile_mod.get_footprint_defaults(tmp_path) == {}


def test_env_override_wins_over_everything(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit_profile"
    explicit.mkdir()
    (explicit / "footprint_defaults.json").write_text('{"pad_corner_radius_pct": 7}', encoding="utf-8")
    central = tmp_path / "home" / ".eda-agent" / "profiles" / "other"
    central.mkdir(parents=True)
    (central / "footprint_defaults.json").write_text('{"pad_corner_radius_pct": 99}', encoding="utf-8")
    (tmp_path / "home" / ".eda-agent" / "active_profile").write_text("other", encoding="utf-8")

    monkeypatch.setenv("EDA_AGENT_PROFILE_DIR", str(explicit))
    assert profile_mod.resolve_profile_dir(tmp_path) == explicit
    assert profile_mod.get_footprint_defaults(tmp_path) == {"pad_corner_radius_pct": 7}


def test_project_profile_is_found_by_walking_up_from_a_subdirectory(tmp_path):
    project = tmp_path / "company_a" / "boards" / "widget"
    project.mkdir(parents=True)
    profile_dir = tmp_path / "company_a" / ".eda-agent" / "profile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "footprint_defaults.json").write_text('{"pad_corner_radius_pct": 1}', encoding="utf-8")

    found = profile_mod.resolve_profile_dir(project)
    assert found == profile_dir
    assert profile_mod.get_footprint_defaults(project) == {"pad_corner_radius_pct": 1}


def test_project_profile_beats_the_central_one(tmp_path, monkeypatch):
    project = tmp_path / "company_a"
    project.mkdir()
    project_profile = project / ".eda-agent" / "profile"
    project_profile.mkdir(parents=True)
    (project_profile / "footprint_defaults.json").write_text('{"pad_corner_radius_pct": 1}', encoding="utf-8")

    central = tmp_path / "home" / ".eda-agent" / "profiles" / "acme"
    central.mkdir(parents=True)
    (central / "footprint_defaults.json").write_text('{"pad_corner_radius_pct": 50}', encoding="utf-8")
    (tmp_path / "home" / ".eda-agent" / "active_profile").write_text("acme", encoding="utf-8")

    assert profile_mod.resolve_profile_dir(project) == project_profile


def test_central_active_profile_is_used_when_no_project_profile_exists(tmp_path):
    central = tmp_path / "home" / ".eda-agent" / "profiles" / "acme"
    central.mkdir(parents=True)
    (central / "footprint_defaults.json").write_text('{"pad_corner_radius_pct": 1}', encoding="utf-8")
    (tmp_path / "home" / ".eda-agent" / "active_profile").write_text("acme\n", encoding="utf-8")

    found = profile_mod.resolve_profile_dir(tmp_path)
    assert found == central
    assert profile_mod.get_footprint_defaults(tmp_path) == {"pad_corner_radius_pct": 1}


def test_active_profile_pointer_naming_a_missing_profile_is_ignored(tmp_path):
    (tmp_path / "home" / ".eda-agent").mkdir(parents=True)
    (tmp_path / "home" / ".eda-agent" / "active_profile").write_text("nope", encoding="utf-8")
    assert profile_mod.resolve_profile_dir(tmp_path) is None


def test_load_profile_parses_every_optional_piece(tmp_path):
    d = tmp_path / "profile"
    d.mkdir()
    (d / "profile.json").write_text(json.dumps({"name": "acme", "description": "test"}), encoding="utf-8")
    (d / "footprint_defaults.json").write_text(json.dumps({"pad_corner_radius_pct": 1}), encoding="utf-8")
    (d / "footprint_policy.json").write_text(json.dumps({"courtyard": True}), encoding="utf-8")
    (d / "document_settings.json").write_text(json.dumps({"schematic_units": "mm"}), encoding="utf-8")
    (d / "naming.json").write_text("{}", encoding="utf-8")
    (d / "company_goals.json").write_text("{}", encoding="utf-8")
    (d / "rules").mkdir()
    (d / "rules" / "company.json").write_text("{}", encoding="utf-8")
    (d / "fab_profiles").mkdir()
    (d / "fab_profiles" / "jlc_4l.json").write_text("{}", encoding="utf-8")
    (d / "templates").mkdir()
    (d / "templates" / "default").mkdir()

    p = profile_mod.load_profile(d)
    assert p.meta == {"name": "acme", "description": "test"}
    assert p.footprint_defaults == {"pad_corner_radius_pct": 1}
    assert p.footprint_policy == {"courtyard": True}
    assert p.document_settings == {"schematic_units": "mm"}
    assert p.naming_path == d / "naming.json"
    assert p.company_goals_path == d / "company_goals.json"
    assert p.rules_dir == d / "rules"
    assert p.fab_profiles_dir == d / "fab_profiles"
    assert p.templates_dir == d / "templates"
    assert p.rulesets() == ["company"]
    assert p.fab_profile_names() == ["jlc_4l"]
    assert p.template_names() == ["default"]


def test_load_profile_with_nothing_present_is_all_empty(tmp_path):
    d = tmp_path / "empty_profile"
    d.mkdir()
    p = profile_mod.load_profile(d)
    assert p.meta == {} and p.footprint_defaults == {} and p.footprint_policy == {}
    assert p.naming_path is None and p.rules_dir is None
    assert p.rulesets() == [] and p.fab_profile_names() == [] and p.template_names() == []


def test_apply_env_sets_ipc_dir_only_when_a_naming_or_goals_file_exists(tmp_path):
    project = tmp_path / "company_a"
    d = project / ".eda-agent" / "profile"
    d.mkdir(parents=True)
    (d / "footprint_defaults.json").write_text("{}", encoding="utf-8")

    profile_mod.apply_env(project)
    assert "EDA_AGENT_IPC_DIR" not in os.environ


def test_apply_env_sets_ipc_dir_when_naming_json_is_present(tmp_path, monkeypatch):
    project = tmp_path / "company_a"
    d = project / ".eda-agent" / "profile"
    d.mkdir(parents=True)
    (d / "naming.json").write_text("{}", encoding="utf-8")

    monkeypatch.delenv("EDA_AGENT_IPC_DIR", raising=False)
    result = profile_mod.apply_env(project)
    assert result is not None
    assert os.environ["EDA_AGENT_IPC_DIR"] == str(d)


def test_apply_env_never_overrides_an_already_set_ipc_dir(tmp_path, monkeypatch):
    project = tmp_path / "company_a"
    d = project / ".eda-agent" / "profile"
    d.mkdir(parents=True)
    (d / "naming.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("EDA_AGENT_IPC_DIR", "/already/set/path")
    profile_mod.apply_env(project)
    assert os.environ["EDA_AGENT_IPC_DIR"] == "/already/set/path"


def test_apply_env_with_no_profile_is_a_noop(tmp_path):
    assert profile_mod.apply_env(tmp_path) is None
    assert "EDA_AGENT_IPC_DIR" not in os.environ


# ---------------------------------------------------------------- broken profiles
#
# A profile is read from places that must not care whether it is any good:
# apply_env() runs during server startup, and get_footprint_defaults() runs
# on the pad-authoring path. Reading one used to raise, so a single stray
# comma took down the whole MCP server, and a JSON file that parsed to a
# list reached a caller's .get() as an AttributeError.

def _broken_profile(tmp_path, filename: str, text: str):
    d = tmp_path / "profile"
    d.mkdir(exist_ok=True)
    (d / filename).write_text(text, encoding="utf-8")
    return d


def test_malformed_json_is_recorded_rather_than_raised(tmp_path):
    d = _broken_profile(tmp_path, "footprint_defaults.json", "{oops}")
    p = profile_mod.load_profile(d)
    assert p.footprint_defaults == {}
    assert len(p.errors) == 1
    assert "footprint_defaults.json" in p.errors[0]


def test_json_that_is_not_an_object_is_recorded_rather_than_raised(tmp_path):
    d = _broken_profile(tmp_path, "footprint_defaults.json", "[1, 2, 3]")
    p = profile_mod.load_profile(d)
    assert p.footprint_defaults == {}
    assert "expected a JSON object" in p.errors[0]


def test_every_optional_file_reports_its_own_problem(tmp_path):
    d = tmp_path / "profile"
    d.mkdir()
    for name in ("profile.json", "footprint_defaults.json",
                 "footprint_policy.json", "document_settings.json"):
        (d / name).write_text("{nope}", encoding="utf-8")
    p = profile_mod.load_profile(d)
    assert len(p.errors) == 4
    assert p.meta == {} and p.footprint_policy == {} and p.document_settings == {}


def test_a_broken_profile_does_not_break_startup(tmp_path):
    """apply_env() runs in server.py's main(): raising here took the server down."""
    d = tmp_path / "company" / ".eda-agent" / "profile"
    d.mkdir(parents=True)
    (d / "profile.json").write_text("{oops}", encoding="utf-8")

    result = profile_mod.apply_env(tmp_path / "company")
    assert result is not None
    assert result.errors


def test_a_broken_profile_yields_empty_footprint_defaults(tmp_path, monkeypatch):
    d = _broken_profile(tmp_path, "footprint_defaults.json", "{oops}")
    monkeypatch.setenv("EDA_AGENT_PROFILE_DIR", str(d))
    assert profile_mod.get_footprint_defaults() == {}


def test_summary_carries_the_errors(tmp_path):
    d = _broken_profile(tmp_path, "footprint_defaults.json", "{oops}")
    summary = profile_mod.load_profile(d).summary()
    assert summary["active"] is True
    assert summary["errors"] and "footprint_defaults.json" in summary["errors"][0]


def test_a_clean_profile_reports_no_errors(tmp_path):
    d = _broken_profile(tmp_path, "footprint_defaults.json", '{"pad_corner_radius_pct": 1}')
    p = profile_mod.load_profile(d)
    assert p.errors == ()
    assert p.summary()["errors"] == []
