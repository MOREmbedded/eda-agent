# SPDX-License-Identifier: Apache-2.0
"""`eda-agent update`: four things that go stale independently.

The checkout, the installed package, the deployed DelphiScript files,
and the copy Altium COMPILED can each be behind the others, and the
symptom of all four is the same -- a tool behaving like an older build.
Most of these tests are about the command saying WHICH one, and about
the two it deliberately will not do for you.

plan() is pure, so every branch is reachable without git, Altium, or a
network. That is the reason it is a separate function from the gather.
"""
from __future__ import annotations

import json

import pytest

from eda_agent import cli
from eda_agent.diag import update as mod
from eda_agent.diag.update import Step, format_report, plan


def _state(**over):
    base = {
        "repo_root": "/repo", "editable": True, "fetched": True,
        "behind": 0, "ahead": 0, "branch": "main", "dirty": False,
        "bundled_scripts": "/repo/scripts/altium",
        "deployed_scripts": "/home/scripts",
        "bundled_version": "2026.10.18.1",
        "deployed_version": "2026.10.18.1",
        "package_version": "0.6.1",
    }
    base.update(over)
    return base


def _by_name(steps):
    return {s.name: s for s in steps}


# ---- everything current -------------------------------------------------

def test_a_fully_current_install_leaves_nothing_to_do():
    steps = plan(_state(), compiled_version="2026.10.18.1")
    assert not any(s.needs_human for s in steps)
    assert "Nothing left to do" in format_report(steps)


# ---- the checkout -------------------------------------------------------

def test_being_behind_your_own_remote_is_reported_but_not_pulled():
    """A command called "update" silently rebasing someone's work is a
    bad trade, so this reports and hands the decision back."""
    steps = plan(_state(behind=3), compiled_version="2026.10.18.1")
    checkout = _by_name(steps)["your remote"]
    assert checkout.needs_human
    assert "3 commit(s) behind" in checkout.message
    assert "--pull" in checkout.action


def test_a_completed_pull_reports_done_rather_than_todo():
    steps = plan(_state(behind=2), pull=True,
                 compiled_version="2026.10.18.1")
    checkout = _by_name(steps)["your remote"]
    assert checkout.status == "done"
    assert not checkout.needs_human


def test_without_fetch_the_comparison_admits_it_may_be_stale():
    """"up to date" from a week-old fetch is not the same claim as "up
    to date", and presenting them identically is the whole problem."""
    stale = _by_name(plan(_state(fetched=False)))["your remote"]
    fresh = _by_name(plan(_state(fetched=True)))["your remote"]
    assert "--fetch" in stale.message
    assert "--fetch" not in fresh.message


def test_no_upstream_is_unknown_rather_than_current():
    checkout = _by_name(plan(_state(behind=None)))["your remote"]
    assert checkout.status == "unknown"
    assert not checkout.needs_human


def test_an_installed_copy_is_told_to_update_the_way_it_was_installed():
    """Not every run is from a checkout, and `git pull` is useless
    advice for a wheel."""
    checkout = _by_name(plan(_state(repo_root="")))["your remote"]
    assert checkout.status == "unknown"
    assert "pip install --upgrade" in checkout.action


# ---- another remote, only when asked -------------------------------------
#
# Checking a second remote every run was the first design, and it was
# wrong: a checkout can have your fork, the project it was forked from
# and a colleague's copy all configured, and reporting someone else's
# commits unasked turns the to-do list into something nobody reads.
# Name one with --from and it is checked; otherwise it is not.

def _other(**over):
    """State as gathered when --from named a remote."""
    base = {"upstream_remote": "upstream", "upstream_ref": "upstream/main",
            "upstream_behind": 24, "upstream_version": "0.7.1"}
    base.update(over)
    return _state(**base)


def test_no_other_remote_is_checked_unless_one_was_named():
    """The default question is about YOUR fork."""
    assert "original project" not in _by_name(plan(_state()))


def test_a_named_remote_that_is_ahead_is_reported():
    project = _by_name(plan(_other()))["original project"]
    assert project.needs_human
    assert "24 new commit(s)" in project.message


def test_a_version_gap_is_named_when_there_is_one():
    project = _by_name(plan(_other()))["original project"]
    assert "0.7.1" in project.message and "0.6.1" in project.message


def test_no_version_gap_is_not_invented():
    project = _by_name(plan(_other(upstream_version="0.6.1")))[
        "original project"]
    assert "declares" not in project.message


def test_merging_from_another_remote_is_never_done_for_you():
    """Twenty-odd commits into a fork with its own work on top is a
    real merge, not a fast-forward, and it can conflict."""
    project = _by_name(plan(_other()))["original project"]
    assert "NOT done for you" in project.action
    assert "can conflict" in project.action


def test_being_level_with_the_named_remote_is_current():
    project = _by_name(plan(_other(upstream_behind=0)))["original project"]
    assert project.status == "current"
    assert not project.needs_human


def test_the_two_remote_steps_are_not_confusable():
    """They both say "behind" and answer different questions."""
    names = _by_name(plan(_other(behind=3)))
    assert "push to" in names["your remote"].message
    assert "upstream/main" in names["original project"].message


# ---- updating from a local folder ----------------------------------------
#
# The case with no network in it: a copy on a share, a release folder,
# another checkout on the same machine.

def _from_folder(**over):
    base = {"source_dir": "/some/copy", "source_ok": True,
            "source_problem": "", "source_version": "0.9.9",
            "source_script_version": "2099.12.31.9",
            "source_scripts": "/some/copy/scripts/altium"}
    base.update(over)
    return _state(**base)


def test_a_folders_scripts_are_what_current_means():
    """With --from, the folder is the thing being updated FROM.
    Comparing the deployed scripts against THIS tree instead would
    answer a question nobody asked."""
    scripts = _by_name(plan(_from_folder(), source_dir="/some/copy"))[
        "altium scripts"]
    assert scripts.needs_human
    assert "2099.12.31.9" in scripts.message


def test_the_fix_for_a_folder_is_not_install_scripts():
    """install-scripts always copies from THIS tree, so suggesting it
    after --from would deploy the wrong files and look like the update
    had failed."""
    scripts = _by_name(plan(_from_folder(), source_dir="/some/copy"))[
        "altium scripts"]
    assert "install-scripts" not in scripts.action
    assert "--from" in scripts.action


def test_a_version_difference_in_the_folder_is_reported():
    steps = plan(_from_folder(), source_dir="/some/copy")
    package = _by_name(steps)["python package"]
    assert package.needs_human
    assert "0.9.9" in package.message and "0.6.1" in package.message


def test_installing_a_package_is_never_done_for_you():
    """A bigger change than copying a script, and one that can replace
    the interpreter's idea of what eda-agent is."""
    package = _by_name(plan(_from_folder(), source_dir="/some/copy"))[
        "python package"]
    assert "Not done for you" in package.action
    assert "pip install" in package.action


def test_a_matching_folder_leaves_nothing_to_do():
    steps = plan(_from_folder(source_version="0.6.1",
                              source_script_version="2026.10.18.1"),
                 source_dir="/some/copy", compiled_version="2026.10.18.1")
    assert not any(st.needs_human for st in steps)


def test_a_folder_that_is_not_an_eda_agent_checkout_says_so():
    steps = plan(_from_folder(source_ok=False,
                              source_problem="/x is not a checkout"),
                 source_dir="/x")
    folder = _by_name(steps)["source folder"]
    assert folder.needs_human
    assert "--from at an eda-agent checkout" in folder.action


def test_a_folder_with_no_scripts_is_not_a_failure():
    """A copy of just the Python package is a legitimate source; it
    simply has no scripts to deploy."""
    steps = plan(_from_folder(source_script_version="", source_scripts=""),
                 source_dir="/some/copy")
    scripts = _by_name(steps)["altium scripts"]
    assert scripts.status == "unknown"
    assert not scripts.needs_human


def test_the_local_trees_own_package_step_is_not_also_shown():
    """Two "python package" lines answering different questions would
    be worse than either alone."""
    steps = plan(_from_folder(), source_dir="/some/copy")
    assert len([st for st in steps if st.name == "python package"]) == 1


# ---- the working tree ---------------------------------------------------

def test_a_dirty_tree_matters_only_when_a_pull_is_in_play():
    """Uncommitted changes are the ordinary state of a checkout being
    worked in. A report that always carries a to-do item is a report
    whose to-do list nobody reads."""
    quiet = _by_name(plan(_state(dirty=True, behind=0)))["working tree"]
    assert quiet.status == "current"
    assert not quiet.needs_human

    loud = _by_name(plan(_state(dirty=True, behind=2)))["working tree"]
    assert loud.needs_human
    assert "stash" in loud.action


def test_a_clean_tree_is_not_mentioned_at_all():
    assert "working tree" not in _by_name(plan(_state(dirty=False)))


# ---- the installed package ----------------------------------------------

def test_an_editable_install_needs_no_reinstall():
    package = _by_name(plan(_state(editable=True)))["python package"]
    assert package.status == "current"
    assert "no reinstall" in package.message


def test_a_copied_install_is_told_a_pull_will_not_take_effect():
    """The trap: `git pull` succeeds, nothing changes, and the reason
    is that the running code is a copy."""
    package = _by_name(plan(_state(editable=False)))["python package"]
    assert package.needs_human
    assert "pip install -e ." in package.action


# ---- the deployed scripts -----------------------------------------------

def test_matching_script_versions_are_current():
    scripts = _by_name(plan(_state()))["altium scripts"]
    assert scripts.status == "current"


def test_a_stale_deploy_names_both_versions():
    scripts = _by_name(plan(_state(deployed_version="2026.10.13.5"),
                            compiled_version=None))["altium scripts"]
    assert scripts.needs_human
    assert "2026.10.13.5" in scripts.message
    assert "2026.10.18.1" in scripts.message


def test_a_redeploy_this_command_performed_reports_done():
    scripts = _by_name(plan(_state(deployed_version="2026.10.13.5"),
                            scripts_deployed=True))["altium scripts"]
    assert scripts.status == "done"
    assert not scripts.needs_human


def test_a_redeploy_still_reports_done_once_the_state_is_re_read():
    """The shape run_update actually produces, which the test above
    does not.

    run_update re-reads the state after copying the files, so by the
    time plan() sees it the two versions MATCH. Testing equality before
    the redeploy flag reported a redeploy as "nothing to do" and hid
    the one fact that matters: the scripts just changed under a running
    Altium, so it has to reload.
    """
    scripts = _by_name(plan(_state(deployed_version="2026.10.18.1"),
                            scripts_deployed=True))["altium scripts"]
    assert scripts.status == "done", (
        "a performed redeploy reported as 'current'; the equality check "
        "is running before the scripts_deployed branch again")


def test_a_redeploy_tells_a_matching_altium_session_to_reload():
    """The consequence of the bug above, and the reason it mattered: a
    session that matched a moment ago is stale the instant the files
    are replaced."""
    steps = plan(_state(deployed_version="2026.10.18.1"),
                 scripts_deployed=True,
                 compiled_version="2026.10.13.5")
    assert _by_name(steps)["altium session"].needs_human


def test_scripts_never_installed_are_an_action_not_a_crash():
    scripts = _by_name(plan(_state(deployed_version=None)))["altium scripts"]
    assert scripts.needs_human
    assert "install-scripts" in scripts.action


# ---- the copy Altium compiled -------------------------------------------

def test_a_stale_altium_session_is_the_last_step_and_a_gui_action():
    """The one fix nothing here can perform: there is no API to reload
    a script project, so it has to be handed over explicitly rather
    than quietly omitted."""
    steps = plan(_state(), compiled_version="2026.10.13.5")
    session = _by_name(steps)["altium session"]
    assert session.needs_human
    assert "File > Run Script" in session.action
    assert "no API" in session.action
    # Last, so it reads as what is left once the disk is in order.
    assert steps[-1].name == "altium session"


def test_altium_being_closed_is_unknown_not_a_failure():
    """Running this with Altium shut is normal."""
    session = _by_name(plan(_state(), compiled_version=None))[
        "altium session"]
    assert session.status == "unknown"
    assert not session.needs_human


def test_after_a_redeploy_altium_is_compared_against_the_new_version():
    """The sequence that matters: redeploy bumps what is on disk, so a
    session still running the old one has to be flagged even though it
    matched a moment ago."""
    steps = plan(_state(deployed_version="2026.10.13.5"),
                 scripts_deployed=True,
                 compiled_version="2026.10.13.5")
    session = _by_name(steps)["altium session"]
    assert session.needs_human
    assert session.details["on_disk"] == "2026.10.18.1"


def test_a_matching_session_is_current():
    session = _by_name(plan(_state(), compiled_version="2026.10.18.1"))[
        "altium session"]
    assert session.status == "current"


# ---- the report ---------------------------------------------------------

def test_the_actions_are_gathered_at_the_end():
    """"What do I do now" is why someone ran this; hunting for it among
    the passes is what a report should save them."""
    report = format_report(plan(_state(behind=1, editable=False),
                                compiled_version="2026.10.13.5"))
    assert "thing(s) left for you:" in report
    assert "1." in report and "2." in report


def test_every_action_step_actually_carries_an_action():
    """A to-do with no instruction is just an alarm."""
    for state, compiled in [
        (_state(behind=2), "2026.10.18.1"),
        (_state(editable=False), "2026.10.18.1"),
        (_state(deployed_version="2026.10.13.5"), None),
        (_state(), "2026.10.13.5"),
        (_state(dirty=True, behind=1), "2026.10.18.1"),
    ]:
        for step in plan(state, compiled_version=compiled):
            if step.needs_human:
                assert step.action, step.name


# ---- the runner and the CLI ---------------------------------------------

def test_check_only_changes_nothing(monkeypatch):
    """--check has to be safe to run at any time, including mid-edit."""
    called = []
    monkeypatch.setattr(mod, "gather_state",
                        lambda **k: _state(deployed_version="2026.10.13.5",
                                           behind=2))
    monkeypatch.setattr(mod, "altium_version", lambda: None)
    monkeypatch.setattr(mod, "_run_git",
                        lambda *a, **k: called.append(a) or "")
    import eda_agent.cli as cli_mod
    monkeypatch.setattr(cli_mod, "cmd_install_scripts",
                        lambda **k: called.append("install") or 0)

    steps, code = mod.run_update(check_only=True, pull=True)
    assert called == []
    assert code == 1


def test_a_pull_is_refused_on_a_dirty_tree(monkeypatch):
    """Pulling onto uncommitted work and handing back a conflict is
    worse than not pulling."""
    git_calls = []

    def fake_git(args, cwd, timeout=30.0):
        git_calls.append(args[0])
        return ""

    monkeypatch.setattr(mod, "gather_state", lambda **k: _state(dirty=True,
                                                                behind=2))
    monkeypatch.setattr(mod, "altium_version", lambda: None)
    monkeypatch.setattr(mod, "_run_git", fake_git)
    mod.run_update(pull=True)
    assert "pull" not in git_calls


def test_a_failed_redeploy_exits_two(monkeypatch):
    monkeypatch.setattr(mod, "gather_state",
                        lambda **k: _state(deployed_version="2026.10.13.5"))
    monkeypatch.setattr(mod, "altium_version", lambda: None)
    import eda_agent.cli as cli_mod
    monkeypatch.setattr(cli_mod, "cmd_install_scripts", lambda **k: 1)

    steps, code = mod.run_update()
    assert code == 2
    assert steps[0].needs_human


def test_the_exit_code_distinguishes_current_from_needs_you(monkeypatch):
    """A script has to branch on this without parsing prose."""
    monkeypatch.setattr(mod, "altium_version", lambda: "2026.10.18.1")
    monkeypatch.setattr(mod, "gather_state", lambda **k: _state())
    assert mod.run_update(check_only=True)[1] == 0

    monkeypatch.setattr(mod, "gather_state", lambda **k: _state(behind=4))
    assert mod.run_update(check_only=True)[1] == 1


def test_the_cli_json_is_parseable_and_carries_the_exit_code(
        monkeypatch, capsys):
    monkeypatch.setattr(mod, "gather_state", lambda **k: _state(behind=2))
    monkeypatch.setattr(mod, "altium_version", lambda: "2026.10.18.1")
    code = cli.cmd_update(check_only=True, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == code == 1
    assert any(s["action"] for s in payload["steps"])


def test_the_ping_does_not_save_anything():
    """doctor's canary calls application.save_all, which saves every
    open document. An update that silently did that to check a version
    would be a far worse trade than not checking."""
    import inspect

    source = inspect.getsource(mod.altium_version)
    assert "save_all" not in source
    assert "ping_with_version" in source


def test_the_ping_log_is_silenced_under_the_right_logger_name():
    """The bridge logs under "eda_agent.bridge", not its own dotted
    module path. Silencing the longer name silenced nothing, and the
    line landed in the middle of the report."""
    import inspect

    source = inspect.getsource(mod.altium_version)
    assert '"eda_agent.bridge"' in source
    assert "altium_bridge" not in source


def test_a_real_folder_is_read_end_to_end(tmp_path):
    """_source_dir_state against actual files, since every test above
    hands it a dict and would pass with the reader broken."""
    repo = tmp_path / "copy"
    (repo / "src" / "eda_agent").mkdir(parents=True)
    (repo / "src" / "eda_agent" / "__init__.py").write_text(
        '__version__ = "9.9.9"\n', encoding="utf-8")
    (repo / "scripts" / "altium").mkdir(parents=True)
    (repo / "scripts" / "altium" / "Main.pas").write_text(
        "    SCRIPT_VERSION = '2099.01.01.1';\n", encoding="utf-8")

    state = mod._source_dir_state(str(repo))
    assert state["source_ok"] is True
    assert state["source_version"] == "9.9.9"
    assert state["source_script_version"] == "2099.01.01.1"


def test_an_unpacked_package_layout_is_accepted_too(tmp_path):
    """Not every source is a checkout; a wheel unpacks without src/."""
    repo = tmp_path / "unpacked"
    (repo / "eda_agent").mkdir(parents=True)
    (repo / "eda_agent" / "__init__.py").write_text(
        '__version__ = "1.2.3"\n', encoding="utf-8")
    assert mod._source_dir_state(str(repo))["source_version"] == "1.2.3"


def test_a_missing_folder_is_refused_with_a_reason(tmp_path):
    state = mod._source_dir_state(str(tmp_path / "nope"))
    assert state["source_ok"] is False
    assert "not a directory" in state["source_problem"]


def test_deploying_from_a_folder_copies_only_files(tmp_path, monkeypatch):
    """The script project is flat. Recursing would copy whatever else
    happens to live in that folder."""
    source = tmp_path / "scripts"
    source.mkdir()
    (source / "Main.pas").write_text("x", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "deep.pas").write_text("no", encoding="utf-8")
    dest = tmp_path / "deployed"

    import eda_agent.cli as cli_mod
    monkeypatch.setattr(cli_mod, "get_default_scripts_dest", lambda: dest)
    assert mod._deploy_from(str(source)) == 0
    assert (dest / "Main.pas").is_file()
    assert not (dest / "nested").exists()


def test_the_options_survive_the_re_read_after_an_action(monkeypatch):
    """run_update re-reads state after it acts. Dropping --from there
    made the report describe this tree instead of the folder, so a
    redeploy from a folder announced the wrong version."""
    import inspect

    source = inspect.getsource(mod.run_update)
    for call in ("compare_remote=compare_remote", "source_dir=source_dir"):
        assert source.count(call) >= 2, (
            f"{call} is not carried through every gather_state call")


def test_checking_another_remote_does_not_import_its_tags():
    """Fetching a remote normally brings its TAGS too, so a fork
    checking the project it came from acquires that project's release
    tags. This repo quietly ended up holding a v0.7.0 it had never
    released, which broke the guard asserting the declared version is
    ahead of every tag. Comparing commits needs no tags.
    """
    import inspect

    source = inspect.getsource(mod._remote_state)
    fetch_line = next(l for l in source.splitlines()
                      if '"fetch"' in l or "'fetch'" in l)
    assert "--no-tags" in source, (
        f"the named-remote fetch does not pass --no-tags, so it will "
        f"import that remote's release tags:\n  {fetch_line.strip()}")
