# SPDX-License-Identifier: Apache-2.0
"""Per-profile settings: a profile's preferences.json and default_mode.

The Config tab on the Altium panel keeps one preferences.json per machine.
The owner profile in effect may carry its own, so each company or client
keeps its own value format, safety switches and the design mode a run
starts in. How the two combine is in eda_agent.preferences; this file holds
each rule to it, through the code that consumes it (the safety checks, the
value-format tool, the mode selection, profile-validate and the dashboard's
Workspace view), not only through the resolver.

Run against a temp HOME and cwd, isolated from any real ~/.eda-agent.
"""
from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

from eda_agent import cli, preferences, profile as profile_mod, safety
from eda_agent.design import modes as modes_mod
from eda_agent.web.dashboard import create_app

pytestmark = pytest.mark.profile_preferences


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    for var in ("EDA_AGENT_PROFILE_DIR", "EDA_AGENT_IPC_DIR", "ALTIUM_AI_IPC_DIR",
                safety.READONLY_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EDA_AGENT_MODE", "")
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    from eda_agent.config import get_config
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr(get_config(), "workspace_dir", tmp_path / "ws", raising=False)


def _panel(tmp_path, **prefs) -> None:
    (tmp_path / "ws" / "preferences.json").write_text(json.dumps(prefs), encoding="utf-8")


def _central(name: str, prefs: dict | str | None = None) -> pathlib.Path:
    """A central profile, made the active one, with PREFS as its file."""
    directory = profile_mod.central_profiles_root() / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "profile.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    if prefs is not None:
        text = prefs if isinstance(prefs, str) else json.dumps(prefs)
        (directory / "preferences.json").write_text(text, encoding="utf-8")
    profile_mod.use_central_profile(name)
    return directory


# -- how the two layers combine ----------------------------------------------


def test_with_no_profile_the_panel_decides(tmp_path):
    _panel(tmp_path, value_format="rkm", edits="readonly")
    got = preferences.resolved()
    assert got["value_format"] == {"value": "rkm", "source": "panel", "panel": "rkm", "profile": None}
    assert got["edits"]["value"] == "readonly" and got["edits"]["source"] == "panel"
    assert got["dialogs"] == {"value": "auto", "source": "default", "panel": None, "profile": None}


def test_the_profile_value_format_wins_over_the_panel(tmp_path):
    _panel(tmp_path, value_format="rkm")
    _central("acme", {"value_format": "si", "value_unit": "with"})
    assert preferences.value_format() == "si" and preferences.value_unit() == "with"
    assert preferences.resolved()["value_format"]["source"] == "profile"


def test_the_unit_travels_with_the_format_and_is_never_mixed(tmp_path):
    # Panel asks for the unit; the profile's SI says nothing about one. The
    # profile's format must not borrow the panel's unit.
    _panel(tmp_path, value_format="si", value_unit="with")
    _central("acme", {"value_format": "si"})
    assert preferences.value_unit() == "without"
    # A unit alone in the profile does nothing: the panel's pair holds.
    _central("acme", {"value_unit": "without"})
    assert (preferences.value_format(), preferences.value_unit()) == ("si", "with")
    # RKM has no unit, whatever a file says.
    _central("acme", {"value_format": "rkm", "value_unit": "with"})
    assert preferences.value_unit() == "without"


def test_switching_profile_switches_the_value_format(tmp_path):
    _central("acme", {"value_format": "rkm"})
    _central("globex", {"value_format": "si"})
    assert preferences.value_format() == "si"
    profile_mod.use_central_profile("acme")
    assert preferences.value_format() == "rkm"


@pytest.mark.parametrize("key, strict, loose", [
    ("edits", "readonly", "allowed"), ("deleting", "refused", "allowed"),
    ("closing", "refused", "allowed"), ("ui_automation", "off", "on")])
def test_a_safety_switch_takes_the_stricter_of_the_two(tmp_path, key, strict, loose):
    _panel(tmp_path, **{key: loose})
    _central("acme", {key: strict})
    assert preferences.preferences()[key] == strict
    assert preferences.resolved()[key]["source"] == "profile"
    # A profile can never loosen what the panel switched on.
    _panel(tmp_path, **{key: strict})
    _central("acme", {key: loose})
    assert preferences.preferences()[key] == strict
    assert preferences.resolved()[key]["source"] == "panel"


@pytest.mark.parametrize("key, value", [("keep_in_view", "on"), ("dialogs", "manual")])
def test_workflow_settings_follow_the_profile(tmp_path, key, value):
    _panel(tmp_path, **{key: preferences.DEFAULTS[key]})
    _central("acme", {key: value})
    assert preferences.preferences()[key] == value


def test_a_broken_profile_file_leaves_the_panel_in_charge(tmp_path):
    _panel(tmp_path, value_format="rkm")
    _central("acme", "{not json")
    assert preferences.value_format() == "rkm"
    _central("acme", "[1, 2]")
    assert preferences.value_format() == "rkm"


# -- the consumers see it ------------------------------------------------------


def test_a_profile_read_only_switch_refuses_edits_and_says_where(tmp_path):
    _panel(tmp_path, edits="allowed")
    directory = _central("acme", {"edits": "readonly"})
    why = safety.refuse_command("pcb.place_tracks")
    assert why and "read-only" in why and str(directory / "preferences.json") in why
    assert "Config tab" not in why
    assert safety.refuse_command("pcb.get_components") is None
    assert safety.readonly_mode()


def test_a_profile_deleting_switch_refuses_deletes(tmp_path):
    _panel(tmp_path, deleting="allowed")
    _central("acme", {"deleting": "refused"})
    assert "deleting is switched off by the active profile" in safety.refuse_command("pcb.delete_object")


def test_a_profile_ui_automation_switch_stops_the_primitives(tmp_path, monkeypatch):
    from eda_agent.ui import windows
    monkeypatch.delenv(windows.UI_AUTOMATION_ENV, raising=False)
    _panel(tmp_path, ui_automation="on")
    _central("acme", {"ui_automation": "off"})
    assert not windows.automation_enabled()
    with pytest.raises(windows.AutomationDisabled, match="active profile"):
        windows._gate("a click")


def test_the_value_format_tool_writes_the_profile_format(tmp_path, monkeypatch):
    from eda_agent.tools import application

    class _Bridge:
        async def send_command_async(self, command, params=None, **_):
            return {"text": "2u2", "kind": "farad", "value": "0.0000022", "rest": "", "notes": []}

    class _Mcp:
        tools: dict = {}

        def tool(self, *a, **k):
            def deco(fn):
                self.tools[fn.__name__] = fn
                return fn
            return deco

    monkeypatch.setattr(application, "get_bridge", lambda: _Bridge())
    mcp = _Mcp()
    application.register_application_tools(mcp)
    _panel(tmp_path, value_format="rkm")
    _central("acme", {"value_format": "si", "value_unit": "with"})
    out = asyncio.run(mcp.tools["app_format_value"](action="cleanup", text="2.2 uF"))
    assert out["text"] == "2.2uF"
    _central("acme", {"value_format": "none"})
    out = asyncio.run(mcp.tools["app_format_value"](action="tostring", kind="R", value="4700"))
    assert out["ok"] is False and "active profile" in out["reason"]


# -- default_mode ----------------------------------------------------------------


def test_the_profile_default_mode_sits_between_the_variable_and_mode_use(monkeypatch, capsys):
    assert cli.cmd_mode_use("schematic_only", clear=False) == 0
    _central("acme", {"default_mode": "reference_redraw"})
    assert modes_mod.selected_mode_name() == ("reference_redraw", "profile")
    assert modes_mod.get_mode().name == "reference_redraw"
    monkeypatch.setenv("EDA_AGENT_MODE", "layout_only")
    assert modes_mod.get_mode().name == "layout_only"
    monkeypatch.setenv("EDA_AGENT_MODE", "")
    _central("globex")                      # no preferences.json: mode-use applies
    assert modes_mod.selected_mode_name() == ("schematic_only", "mode-use")


def test_mode_use_says_when_the_profile_overrules_it(capsys):
    _central("acme", {"default_mode": "reference_redraw"})
    assert cli.cmd_mode_use("schematic_only", clear=False) == 0
    out = capsys.readouterr().out
    assert "default_mode 'reference_redraw'" in out and "overrules" in out
    assert cli.cmd_mode_list() == 0
    assert "Selected: reference_redraw (from profile)" in capsys.readouterr().out


def test_an_unknown_default_mode_is_refused_not_replaced(capsys):
    _central("acme", {"default_mode": "warp"})
    with pytest.raises(ValueError, match="no design mode named 'warp'"):
        modes_mod.get_mode()


def test_a_profile_mode_can_be_the_default(tmp_path):
    directory = _central("acme", {"default_mode": "acme_nxp"})
    (directory / "modes").mkdir()
    (directory / "modes" / "acme_nxp.json").write_text(
        json.dumps({"extends": "reference_redraw"}), encoding="utf-8")
    assert modes_mod.get_mode().name == "acme_nxp"


# -- profile-validate ------------------------------------------------------------


def test_profile_validate_names_every_mistake(tmp_path):
    directory = _central("acme", {"value_format": "metric", "colour": "red",
                                  "default_mode": "warp", "edits": "maybe"})
    problems = profile_mod.validate_profile(directory)
    text = "\n".join(problems)
    assert "value_format 'metric'" in text and "'colour'" in text
    assert "default_mode 'warp' is not a mode" in text and "edits 'maybe'" in text
    _central("acme", {"value_unit": "with"})
    assert any("does nothing without value_format" in p for p in profile_mod.validate_profile(directory))
    _central("acme", {"value_format": "rkm", "value_unit": "with"})
    assert any("needs value_format 'si'" in p for p in profile_mod.validate_profile(directory))
    _central("acme", {"_comment": "ok", "value_format": "si", "value_unit": "with",
                      "default_mode": "reference_redraw", "edits": "readonly"})
    assert profile_mod.validate_profile(directory) == []


def test_profile_show_carries_the_preferences(tmp_path):
    directory = _central("acme", {"value_format": "rkm"})
    assert profile_mod.load_profile(directory).summary()["preferences"] == {"value_format": "rkm"}


# -- writing the profile file ----------------------------------------------------


def test_save_sets_clears_and_keeps_what_it_does_not_own(tmp_path):
    directory = _central("acme", {"_comment": "Acme house style", "value_format": "rkm"})
    got = preferences.save_profile_preferences(directory, {"value_format": "si", "value_unit": "with"})
    assert got["ok"]
    stored = json.loads((directory / "preferences.json").read_text(encoding="utf-8"))
    assert stored == {"_comment": "Acme house style", "value_format": "si", "value_unit": "with"}
    # Clearing the format clears its unit with it.
    assert preferences.save_profile_preferences(directory, {"value_format": None})["ok"]
    assert json.loads((directory / "preferences.json").read_text(encoding="utf-8")) == {"_comment": "Acme house style"}
    assert not list(directory.glob(".preferences.*.tmp"))


def test_save_refuses_a_bad_change_and_writes_nothing(tmp_path):
    directory = _central("acme", {"value_format": "rkm"})
    before = (directory / "preferences.json").read_text(encoding="utf-8")
    for changes in ({"value_format": "metric"}, {"colour": "red"}, {"edits": "maybe"}, {}):
        got = preferences.save_profile_preferences(directory, changes)
        assert not got["ok"], changes
    got = preferences.save_profile_preferences(directory, {"default_mode": "warp"}, {"full_design"})
    assert not got["ok"] and "no design mode named 'warp'" in got["reason"]
    assert (directory / "preferences.json").read_text(encoding="utf-8") == before


def test_save_leaves_an_unreadable_file_alone(tmp_path):
    directory = _central("acme", "{hand-written, half done")
    got = preferences.save_profile_preferences(directory, {"value_format": "si"})
    assert not got["ok"] and "unreadable" in got["reason"]
    assert (directory / "preferences.json").read_text(encoding="utf-8") == "{hand-written, half done"


def test_save_removes_a_file_left_with_nothing_in_it(tmp_path):
    directory = _central("acme", {"keep_in_view": "on"})
    got = preferences.save_profile_preferences(directory, {"keep_in_view": None})
    assert got["ok"] and got["removed"]
    assert not (directory / "preferences.json").exists()


# -- the dashboard's Workspace view ---------------------------------------------


@pytest.fixture
def client(tmp_path):
    app = create_app(workspace_dir=tmp_path / "ws")
    app.config["TESTING"] = True
    return app.test_client()


def test_the_workspace_view_shows_each_setting_and_where_it_comes_from(tmp_path, client):
    _panel(tmp_path, value_format="rkm", deleting="refused")
    _central("acme", {"value_format": "si", "default_mode": "reference_redraw"})
    st = client.get("/api/wb/workspace").get_json()["settings"]
    assert st["can_save"] and st["profile_file"] == "preferences.json"
    assert st["panel_file"] == str(tmp_path / "ws" / "preferences.json")
    assert st["items"]["value_format"]["value"] == "si"
    assert st["items"]["value_format"]["source"] == "profile"
    assert st["items"]["value_format"]["panel"] == "rkm"
    assert st["items"]["deleting"]["value"] == "refused"
    assert st["items"]["deleting"]["source"] == "panel"
    assert st["mode"]["selected"] == "reference_redraw" and st["mode"]["source"] == "profile"
    assert {"reference_redraw", "devkit_derivative"} <= {m["name"] for m in st["mode"]["modes"]}
    assert st["problems"] == []


def test_the_workspace_view_reads_the_panel_file_of_its_own_bridge_folder(tmp_path, client):
    # The app's bridge folder, not whatever the global config says.
    from eda_agent.config import get_config
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "preferences.json").write_text(json.dumps({"value_format": "rkm"}), encoding="utf-8")
    pathlib.Path(get_config().workspace_dir, "preferences.json").write_text("{}", encoding="utf-8")
    app = create_app(workspace_dir=other)
    app.config["TESTING"] = True
    st = app.test_client().get("/api/wb/workspace").get_json()["settings"]
    assert st["items"]["value_format"] == {"value": "rkm", "source": "panel", "panel": "rkm", "profile": None}


def test_the_settings_card_saves_into_the_profile(tmp_path, client):
    directory = _central("acme")
    got = client.post("/api/wb/workspace/settings",
                      json={"changes": {"value_format": "si", "value_unit": "with",
                                        "default_mode": "devkit_derivative"}}).get_json()
    assert got["ok"], got
    assert json.loads((directory / "preferences.json").read_text(encoding="utf-8")) == {
        "value_format": "si", "value_unit": "with", "default_mode": "devkit_derivative"}
    st = client.get("/api/wb/workspace").get_json()["settings"]
    assert st["items"]["value_unit"]["value"] == "with"
    assert st["mode"]["selected"] == "devkit_derivative"
    # The panel's own file is never written from here.
    assert not (tmp_path / "ws" / "preferences.json").exists()


def test_the_settings_card_refuses_what_it_cannot_keep(tmp_path, client):
    got = client.post("/api/wb/workspace/settings", json={"changes": {"value_format": "si"}}).get_json()
    assert not got["ok"] and "no profile is in effect" in got["reason"]
    _central("acme")
    got = client.post("/api/wb/workspace/settings", json={"changes": {"default_mode": "warp"}}).get_json()
    assert not got["ok"] and "warp" in got["reason"]
    got = client.post("/api/wb/workspace/settings", json={"changes": "si"}).get_json()
    assert not got["ok"]


def test_a_foreign_page_cannot_change_the_settings(tmp_path, client):
    directory = _central("acme")
    resp = client.post("/api/wb/workspace/settings", json={"changes": {"edits": "readonly"}},
                       headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403
    assert not (directory / "preferences.json").exists()


def test_the_page_has_the_settings_card_wired():
    page = (pathlib.Path(preferences.__file__).parent / "web" / "dashboard_static"
            / "workbench.html").read_text(encoding="utf-8")
    assert "function wsSettingsCard(" in page and "${wsSettingsCard(w)}" in page
    assert '"/api/wb/workspace/settings"' in page and "data-wsset" in page
    assert 'addEventListener("change", e => { if (S.mode === "workspace"' in page


def test_save_works_beside_a_key_this_version_does_not_know(tmp_path):
    """A key from a newer version, or a hand-written note, must not make
    every save from the dashboard fail."""
    directory = _central("acme", {"colour": "red", "value_format": "si", "value_unit": "with"})
    assert preferences.save_profile_preferences(directory, {"keep_in_view": "on"})["ok"]
    stored = json.loads((directory / "preferences.json").read_text(encoding="utf-8"))
    assert stored == {"colour": "red", "value_format": "si", "value_unit": "with", "keep_in_view": "on"}
    # Moving to RKM drops the unit, which RKM does not have.
    assert preferences.save_profile_preferences(directory, {"value_format": "rkm"})["ok"]
    assert "value_unit" not in json.loads((directory / "preferences.json").read_text(encoding="utf-8"))


def test_the_settings_table_cannot_grow_past_its_card():
    """Measured in Chromium: before these rules the table was 624 px wide in
    a 332 px card (a mode's long title widened its menu), so the pane
    scrolled sideways. A fixed layout holds the columns to the card, the
    menus take their column's width, and a narrow card stacks each row."""
    page = (pathlib.Path(preferences.__file__).parent / "web" / "dashboard_static"
            / "workbench.html").read_text(encoding="utf-8")
    assert ".wsset table.grid { table-layout:fixed; }" in page
    assert ".wsset select { font-size:12px; width:100%; min-width:0; }" in page
    assert "container-type:inline-size" in page and "@container (max-width: 520px)" in page
