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


def test_validate_reports_an_unreadable_profile_file(tmp_path, capsys):
    """Loading degrades instead of raising, so this command is the place a
    malformed profile file has to become visible."""
    cli.cmd_profile_init("acme", here=False)
    profile_dir = tmp_path / "home" / ".eda-agent" / "profiles" / "acme"
    (profile_dir / "footprint_defaults.json").write_text("{oops}", encoding="utf-8")
    capsys.readouterr()

    rc = cli.cmd_profile_validate(name="acme", here=False)
    assert rc == 1
    assert "footprint_defaults.json" in capsys.readouterr().err


def test_show_still_works_with_a_broken_profile(tmp_path, capsys):
    cli.cmd_profile_init("acme", here=False)
    cli.cmd_profile_use("acme")
    profile_dir = tmp_path / "home" / ".eda-agent" / "profiles" / "acme"
    (profile_dir / "footprint_defaults.json").write_text("{oops}", encoding="utf-8")
    capsys.readouterr()

    rc = cli.cmd_profile_show(as_json=True)
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["active"] is True
    assert data["footprint_defaults"] == {}
    assert any("footprint_defaults.json" in e for e in data["errors"])


@pytest.mark.parametrize("filename,content,needle", [
    ("footprint_defaults.json", '{"pad_corner_radius_pct": "one"}', "pad_corner_radius_pct"),
    ("document_settings.json", '{"schematic_units": "millimeters"}', "schematic_units"),
    ("document_settings.json", '{"schematic_snap_grid_mil": "wide"}', "schematic_snap_grid_mil"),
])
def test_validate_applies_the_same_checks_the_tools_do(tmp_path, capsys, filename, content, needle):
    """"Valid" has to mean the consumers will accept it, or this command
    passes a profile that then gets refused at the point of use."""
    cli.cmd_profile_init("acme", here=False)
    profile_dir = tmp_path / "home" / ".eda-agent" / "profiles" / "acme"
    (profile_dir / filename).write_text(content, encoding="utf-8")
    capsys.readouterr()

    rc = cli.cmd_profile_validate(name="acme", here=False)
    assert rc == 1
    assert needle in capsys.readouterr().err


def test_validate_names_a_misspelled_setting(tmp_path, capsys):
    """A typo'd key is the quietest way for a profile to do nothing."""
    cli.cmd_profile_init("acme", here=False)
    profile_dir = tmp_path / "home" / ".eda-agent" / "profiles" / "acme"
    (profile_dir / "document_settings.json").write_text(
        '{"schematic_unit": "mm"}', encoding="utf-8")
    capsys.readouterr()

    rc = cli.cmd_profile_validate(name="acme", here=False)
    assert rc == 1
    err = capsys.readouterr().err
    assert "schematic_unit" in err and "unknown setting" in err


def test_validate_allows_underscore_comment_keys(tmp_path):
    """The scaffold writes _comment into these files."""
    cli.cmd_profile_init("acme", here=False)
    profile_dir = tmp_path / "home" / ".eda-agent" / "profiles" / "acme"
    (profile_dir / "document_settings.json").write_text(
        '{"_comment": "ours", "schematic_units": "mm"}', encoding="utf-8")
    assert cli.cmd_profile_validate(name="acme", here=False) == 0


def test_the_scaffolded_profile_passes_its_own_validation(tmp_path):
    """The example files profile-init writes must themselves be valid."""
    cli.cmd_profile_init("acme", here=False)
    assert cli.cmd_profile_validate(name="acme", here=False) == 0


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


def test_profile_show_lists_document_templates_apart_from_projects(tmp_path, capsys):
    """A document template is not a starter project.

    profile-new-project refuses anything that is not a directory, so
    showing the two under one heading would advertise a template that
    the command it implies then rejects.
    """
    cli.cmd_profile_init("acme", here=False)
    cli.cmd_profile_use("acme")
    root = tmp_path / "home" / ".eda-agent" / "profiles" / "acme" / "templates"
    (root / "StarterProject").mkdir(parents=True, exist_ok=True)
    (root / "documents").mkdir(parents=True, exist_ok=True)
    (root / "documents" / "A4_Titleblock.SchDoc").write_text("x", encoding="utf-8")

    assert cli.cmd_profile_show(as_json=False) == 0
    out = capsys.readouterr().out
    assert "StarterProject" in out
    assert "A4_Titleblock.SchDoc" in out
    templates_line = next(ln for ln in out.splitlines()
                          if ln.strip().startswith("templates:"))
    assert "A4_Titleblock" not in templates_line


# --- phases-show / phases-enable / phases-disable --------------------------
#
# The NPI gate's setting lives in the profile, so its CLI lives here with
# the rest of the profile glue rather than in its own file.

def test_phases_are_off_in_a_fresh_profile(capsys):
    """The scaffold writes the file and leaves it empty.

    Writing the file matters: an owner editing their profile by hand
    should find the setting rather than have to learn it exists. Leaving
    it empty matters more -- scaffolding a gate switched on would turn a
    default into a decision nobody made.
    """
    cli.cmd_profile_init("acme", here=False)
    cli.cmd_profile_use("acme")
    capsys.readouterr()

    assert cli.cmd_phases_show(as_json=True) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["stages"] == []
    # "profile", not "default": the scaffold wrote an empty list, and
    # saying so points the owner at the file to edit rather than at a
    # default they would then go looking for.
    assert found["source"] == "profile"
    assert found["path"].endswith("phase_gates.json")

    assert cli.cmd_profile_validate(name="acme", here=False) == 0


def test_enable_records_the_stages_and_keeps_the_rest_of_the_file(tmp_path,
                                                                  capsys):
    """An owner's own note in the file must survive a settings command.

    The scaffold writes a _comment into it, and a command that silently
    dropped one is the kind of small betrayal nobody reports.
    """
    cli.cmd_profile_init("acme", here=False)
    cli.cmd_profile_use("acme")
    path = (tmp_path / "home" / ".eda-agent" / "profiles" / "acme"
            / "phase_gates.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_comment"] = "agreed at kickoff"
    path.write_text(json.dumps(data), encoding="utf-8")
    capsys.readouterr()

    assert cli.cmd_phases_enable(["dvt", "evt"]) == 0
    written = json.loads(path.read_text(encoding="utf-8"))
    # Recorded in stage order, not the order they were typed: running
    # DVT before EVT is the mistake the gate exists to prevent.
    assert written["stages"] == ["evt", "dvt"]
    assert written["_comment"] == "agreed at kickoff"
    assert profile_mod.get_phase_stages() == ("evt", "dvt")


def test_enable_with_no_stages_means_all_five(capsys):
    cli.cmd_profile_init("acme", here=False)
    cli.cmd_profile_use("acme")
    capsys.readouterr()

    assert cli.cmd_phases_enable([]) == 0
    from eda_agent.design.phase_gates import STAGES
    assert profile_mod.get_phase_stages() == STAGES


def test_disable_turns_them_off_without_touching_the_ledger(tmp_path, capsys):
    cli.cmd_profile_init("acme", here=False)
    cli.cmd_profile_use("acme")
    cli.cmd_phases_enable(["pvt"])
    capsys.readouterr()

    assert cli.cmd_phases_disable() == 0
    assert profile_mod.get_phase_stages() == ()
    out = capsys.readouterr().out
    assert "Answers already recorded are untouched" in out


def test_a_stage_that_is_not_a_stage_is_refused_before_anything_is_written(
        tmp_path, capsys):
    cli.cmd_profile_init("acme", here=False)
    cli.cmd_profile_use("acme")
    path = (tmp_path / "home" / ".eda-agent" / "profiles" / "acme"
            / "phase_gates.json")
    before = path.read_text(encoding="utf-8")
    capsys.readouterr()

    assert cli.cmd_phases_enable(["evt", "bananas"]) == 2
    assert "bananas" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == before


def test_with_no_profile_it_says_where_to_put_the_setting(capsys):
    """Refusing is right; refusing without a way forward is not.

    There is deliberately no "scaffold one for you": a profile is a whole
    directory with its own resolution order, and making one from a
    settings command would leave an owner with two.
    """
    assert cli.cmd_phases_enable(["evt"]) == 1
    err = capsys.readouterr().err
    assert "profile-init" in err and "EDA_AGENT_PHASES" in err


def test_show_names_the_variable_when_it_is_the_one_deciding(monkeypatch,
                                                             capsys):
    """Which setting won is the whole question when two disagree."""
    cli.cmd_profile_init("acme", here=False)
    cli.cmd_profile_use("acme")
    cli.cmd_phases_enable(["pvt"])
    capsys.readouterr()

    monkeypatch.setenv("EDA_AGENT_PHASES", "evt")
    assert cli.cmd_phases_show(as_json=False) == 0
    out = capsys.readouterr().out
    assert "evt" in out and "EDA_AGENT_PHASES" in out
    assert "pvt" not in out


def test_show_reports_a_value_it_had_to_ignore(tmp_path, monkeypatch, capsys):
    """get_phase_stages degrades silently so a typo cannot break startup.

    This is the command that has to say it out loud, or the stages vanish
    with nothing anywhere explaining why.
    """
    cli.cmd_profile_init("acme", here=False)
    cli.cmd_profile_use("acme")
    monkeypatch.setenv("EDA_AGENT_PHASES", "evt, bananas")
    capsys.readouterr()

    assert cli.cmd_phases_show(as_json=False) == 1
    assert "bananas" in capsys.readouterr().err


def test_the_scaffold_makes_room_for_erc_sets_but_asserts_no_policy(tmp_path):
    """A scaffolded set would name violation types by Altium's own prose,
    and a different Altium version calls some of them something else, so a
    brand-new profile would start out reporting half its policy unknown."""
    assert cli.cmd_profile_init("acme", here=True) == 0
    profile_dir = tmp_path / ".eda-agent" / "profile"
    assert (profile_dir / "erc_levels").is_dir()
    assert not (profile_dir / "erc_levels.json").exists()
    assert cli.cmd_profile_validate(name=None, here=True) == 0


def test_validate_reports_a_broken_erc_set(tmp_path, capsys):
    cli.cmd_profile_init("acme", here=True)
    profile_dir = tmp_path / ".eda-agent" / "profile"
    (profile_dir / "erc_levels" / "house.json").write_text(
        json.dumps({"levels": {"Duplicate Net Names": "loud"}}),
        encoding="utf-8")
    assert cli.cmd_profile_validate(name=None, here=True) == 1
    assert "house.json" in capsys.readouterr().err


def test_a_base_set_is_reported_once_however_many_sets_extend_it(tmp_path, capsys):
    cli.cmd_profile_init("acme", here=True)
    sets = tmp_path / ".eda-agent" / "profile" / "erc_levels"
    (sets / "company.json").write_text(
        json.dumps({"levels": {"Duplicate Net Names": "loud"}}),
        encoding="utf-8")
    for name in ("house", "project"):
        (sets / f"{name}.json").write_text(
            json.dumps({"extends": "company", "levels": {}}), encoding="utf-8")
    assert cli.cmd_profile_validate(name=None, here=True) == 1
    reported = [line for line in capsys.readouterr().err.splitlines()
                if "company.json" in line]
    assert len(reported) == 1, reported


def test_show_names_the_erc_sets(tmp_path, capsys):
    cli.cmd_profile_init("acme", here=True)
    profile_dir = tmp_path / ".eda-agent" / "profile"
    (profile_dir / "erc_levels.json").write_text(
        json.dumps({"levels": {"Duplicate Net Names": "fatal"}}),
        encoding="utf-8")
    (profile_dir / "erc_levels" / "strict.json").write_text(
        json.dumps({"levels": {}}), encoding="utf-8")
    assert cli.cmd_profile_show(as_json=False) == 0
    out = capsys.readouterr().out
    assert "erc_levels:         1 type(s)" in out
    assert "strict" in out
