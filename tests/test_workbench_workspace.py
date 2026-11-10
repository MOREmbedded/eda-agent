# SPDX-License-Identifier: Apache-2.0
"""The dashboard's Workspace view: which profile is in effect, why, what it
declares, and the one write it makes (switching the central profile).

Run against a temp HOME and cwd, isolated from any real ~/.eda-agent, the
same discipline as test_profile_cli.py.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from eda_agent import cli, profile as profile_mod
from eda_agent.web import workspace_view
from eda_agent.web.dashboard import create_app

_FAB = {
    "name": "housefab", "source": "a fixture, not a real fab page",
    "copper_layer_counts": [2], "min_track_mils": 6.0, "min_gap_mils": 7.0,
    "min_drill_mils": 12.0, "min_annular_ring_mils": 5.0,
    "min_hole_to_hole_mils": 10.0, "min_mask_sliver_mils": 3.0,
    "min_silk_width_mils": 5.0,
}


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    for var in ("EDA_AGENT_PROFILE_DIR", "EDA_AGENT_IPC_DIR", "ALTIUM_AI_IPC_DIR",
                "EDA_AGENT_PHASES"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")


def _write(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def _central(name: str, title: str = "", files: dict | None = None) -> pathlib.Path:
    """A central profile with FILES, keyed by their path inside it."""
    directory = profile_mod.central_profiles_root() / name
    _write(directory / "profile.json", {"name": title or name, "description": f"{name} boards"})
    for rel, data in (files or {}).items():
        _write(directory / rel, data)
    return directory


@pytest.fixture
def client(tmp_path):
    app = create_app(workspace_dir=tmp_path / "ws")
    app.config["TESTING"] = True
    return app.test_client()


# -- where the profile comes from --------------------------------------------


def test_no_profile_says_so_and_lists_none(client):
    got = client.get("/api/wb/workspace").get_json()
    assert got["ok"] and got["profile"] is None
    assert got["where"]["source"] == "none"
    assert got["profiles"] == []


def test_central_choice_is_shown_with_its_contents(client):
    _central("acme", "Acme Instruments", files={
        "design_rules.json": {"fab_profile": "housefab", "net_classes": {"VBUS": "power"}},
        "fab_profiles/housefab.json": _FAB,
        "erc_levels.json": {"levels": {"Floating power object": "fatal"}},
        "rules/company.json": {"ruleset": "company", "description": "house checks", "rules": [
            {"id": "mpn", "type": "component_parameter_required", "severity": "error",
             "description": "MPN", "parameters": ["MPN"]}]},
        "templates/default/Project.PrjPcb": "[Design]\n",
        "templates/documents/A4.SchDoc": "x"})
    _central("globex")
    profile_mod.use_central_profile("acme")

    got = client.get("/api/wb/workspace").get_json()
    p = got["profile"]
    assert got["where"]["source"] == "central"
    assert [(x["name"], x["central_active"], x["in_effect"]) for x in got["profiles"]] == [
        ("acme", True, True), ("globex", False, False)]
    assert p["name"] == "Acme Instruments" and p["problems"] == []
    assert p["design_rules"]["fab_profile"] == "housefab"
    assert p["rulesets"] == [{"name": "company", "file": "rules/company.json",
                              "description": "house checks", "rules": 1,
                              "severities": {"error": 1}, "readable": True}]
    assert p["erc"]["sets"][0]["default"] and p["erc"]["sets"][0]["types"] == 1
    assert p["fab_profiles"][0]["limits"]["min_track_mils"] == 6.0
    assert p["templates"]["projects"] == [{"name": "default", "files": 1,
                                           "sample": ["Project.PrjPcb"]}]
    assert [d["name"] for d in p["templates"]["documents"]] == ["A4"]


def test_a_project_profile_wins_and_the_view_says_which_step_decided(client, tmp_path):
    _central("acme")
    profile_mod.use_central_profile("acme")
    project = tmp_path / "cwd" / ".eda-agent" / "profile"
    _write(project / "profile.json", {"name": "Project rules"})

    got = client.get("/api/wb/workspace").get_json()
    assert got["where"]["source"] == "project"
    assert got["profile"]["name"] == "Project rules"
    # The central choice is still reported, but not as the one in effect.
    assert [(x["central_active"], x["in_effect"]) for x in got["profiles"]] == [(True, False)]


def test_an_environment_override_that_is_not_a_folder_is_reported(client, tmp_path, monkeypatch):
    _central("acme")
    profile_mod.use_central_profile("acme")
    monkeypatch.setenv("EDA_AGENT_PROFILE_DIR", str(tmp_path / "nowhere"))
    got = client.get("/api/wb/workspace").get_json()
    assert got["profile"] is None and got["where"]["source"] == "none"
    assert got["where"]["environment"]["value"] and not got["where"]["environment"]["usable"]


def test_problems_are_the_ones_profile_validate_reports(client, capsys):
    _central("acme", files={"rules/bad.json": {"ruleset": "bad name!", "rules": []},
                            "document_settings.json": {"schematic_units": "furlong"}})
    profile_mod.use_central_profile("acme")
    problems = client.get("/api/wb/workspace").get_json()["profile"]["problems"]
    assert any("schematic_units" in x for x in problems)
    # The reason travels with "ruleset invalid", so the line to fix is named.
    assert any("rules/bad.json" in x and "ruleset name missing or invalid" in x for x in problems)
    assert cli.cmd_profile_validate(name="acme", here=False) == 1
    err = capsys.readouterr().err
    assert all(x in err for x in problems)


# -- switching ----------------------------------------------------------------


def test_switching_writes_the_same_pointer_profile_use_does(client):
    _central("acme")
    _central("globex")
    profile_mod.use_central_profile("acme")
    got = client.post("/api/wb/workspace/use", json={"name": "globex"}).get_json()
    assert got["ok"] and got["in_effect"] and got["source"] == "central"
    assert profile_mod.central_active_name() == "globex"
    assert client.get("/api/wb/workspace").get_json()["profile"]["name"] == "globex"


@pytest.mark.parametrize("name", ["", "..", "../acme", "a/b", "a\\b", "C:", "missing"])
def test_switching_refuses_what_is_not_a_central_profile(client, name):
    _central("acme")
    profile_mod.use_central_profile("acme")
    # A folder a path could reach, so only the name check stands in the way:
    # the pointer holds a name, never a path out of the profiles folder.
    (profile_mod.central_profiles_root().parent / "acme").mkdir()
    (profile_mod.central_profiles_root() / "a" / "b").mkdir(parents=True)
    got = client.post("/api/wb/workspace/use", json={"name": name}).get_json()
    assert not got["ok"] and got["reason"]
    assert profile_mod.central_active_name() == "acme"


def test_switching_under_a_project_profile_says_it_does_not_apply_here(client, tmp_path):
    _central("acme")
    _write(tmp_path / "cwd" / ".eda-agent" / "profile" / "profile.json", {"name": "p"})
    got = client.post("/api/wb/workspace/use", json={"name": "acme"}).get_json()
    assert got["ok"] and not got["in_effect"] and got["source"] == "project"
    assert "project profile" in got["reason"]


def test_switching_moves_the_naming_pointer_only_when_startup_set_it(client, monkeypatch):
    acme = _central("acme", files={"naming.json": {}})
    globex = _central("globex", files={"naming.json": {}})
    plain = _central("plain")
    profile_mod.use_central_profile("acme")
    profile_mod.apply_env()
    assert os.environ["EDA_AGENT_IPC_DIR"] == str(acme)

    assert client.post("/api/wb/workspace/use", json={"name": "globex"}).get_json()["env_moved"]
    assert os.environ["EDA_AGENT_IPC_DIR"] == str(globex)
    # A profile with no naming drops the pointer, so the bundled default applies.
    client.post("/api/wb/workspace/use", json={"name": "plain"})
    assert "EDA_AGENT_IPC_DIR" not in os.environ
    assert plain.is_dir()

    # One the person set themselves is never touched.
    monkeypatch.setenv("EDA_AGENT_IPC_DIR", "/their/own")
    assert not client.post("/api/wb/workspace/use", json={"name": "acme"}).get_json()["env_moved"]
    assert os.environ["EDA_AGENT_IPC_DIR"] == "/their/own"


def test_switching_to_different_phase_gates_says_a_restart_is_needed(client):
    _central("acme", files={"phase_gates.json": {"stages": ["evt"]}})
    _central("globex")
    profile_mod.use_central_profile("acme")
    assert "restarts" in client.post("/api/wb/workspace/use", json={"name": "globex"}).get_json()["note"]


def test_switching_needs_a_same_origin_request(client):
    _central("acme")
    got = client.post("/api/wb/workspace/use", json={"name": "acme"},
                      headers={"Origin": "http://evil.example"})
    assert got.status_code == 403
    assert profile_mod.central_active_name() == ""


# -- reading files and the rule plan -------------------------------------------


def test_a_profile_file_is_read_and_parsed(client):
    _central("acme", files={"erc_levels/strict.json": {"levels": {"X": "error"}}})
    profile_mod.use_central_profile("acme")
    got = client.get("/api/wb/workspace/file?path=erc_levels/strict.json").get_json()
    assert got["ok"] and got["json"] == {"levels": {"X": "error"}}


@pytest.mark.parametrize("path", ["../globex/profile.json", "../../active_profile",
                                  "/etc/passwd", "..\\globex\\profile.json", "", "rules"])
def test_a_path_outside_the_profile_is_refused(client, path):
    _central("acme", files={"rules/x.json": {"rules": []}})
    _central("globex")
    profile_mod.use_central_profile("acme")
    got = client.get("/api/wb/workspace/file", query_string={"path": path}).get_json()
    assert not got["ok"], got


def test_a_template_board_is_described_not_sent(client):
    _central("acme", files={"templates/documents/Big.PcbDoc": "x" * 10})
    profile_mod.use_central_profile("acme")
    got = client.get("/api/wb/workspace/file?path=templates/documents/Big.PcbDoc").get_json()
    assert got["ok"] and got["binary"] and "text" not in got


def test_the_rule_plan_is_the_one_pcb_rules_plan_returns(client):
    from eda_agent.tools.rules import plan_rules
    acme = _central("acme", files={"design_rules.json": {"fab_profile": "housefab"},
                                   "fab_profiles/housefab.json": _FAB})
    profile_mod.use_central_profile("acme")
    got = client.get("/api/wb/workspace/rules-plan").get_json()
    assert got["ok"] and got["rules"]
    assert got == json.loads(json.dumps(plan_rules(None, str(acme))))
    assert {r["name"]: r["value"] for r in got["rules"]}["Width_Fab_Min"] == 6


def test_the_rule_plan_without_design_rules_says_why(client):
    _central("acme")
    profile_mod.use_central_profile("acme")
    got = client.get("/api/wb/workspace/rules-plan").get_json()
    assert not got["ok"] and "design_rules.json" in got["reason"]


# -- the page --------------------------------------------------------------------


def test_the_page_has_the_workspace_mode_and_chip():
    from eda_agent.web.workbench_strings import PAGE
    page = PAGE.read_text(encoding="utf-8")
    assert 'data-mode="workspace"' in page and 'id="wsbtn"' in page
    assert '"tools", "workspace", "usage"]' in page and "/^[1-9]$/" in page
    for url in ("/api/wb/workspace", "/api/wb/workspace/use", "/api/wb/workspace/file",
                "/api/wb/workspace/rules-plan"):
        assert url in page
    # Applying writes to Altium, so the page asks for a second click first.
    assert "Click again to write to Altium" in page


def test_describe_runs_without_flask(tmp_path):
    _central("acme")
    profile_mod.use_central_profile("acme")
    assert workspace_view.describe()["profile"]["directory"].endswith("acme")


def test_a_rules_diff_with_an_empty_skipped_list_is_not_called_absent():
    """Measured live 2026-11-06: a rules diff that ran carries skipped: []
    (the rules the schematic owns), [] is truthy in JavaScript, and the page
    labelled a profile WITH design_rules.json "Not in this profile". Only a
    skipped section, {skipped: "<why>"}, may read that way."""
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / "src" / "eda_agent" / "web"
            / "dashboard_static" / "workbench.html").read_text(encoding="utf-8")
    assert 'else if (typeof s.skipped === "string") state = T("Not in this profile");' in page
    assert 'else if (s.skipped) state = T("Not in this profile");' not in page
