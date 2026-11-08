# SPDX-License-Identifier: Apache-2.0
"""Design modes (design/modes.py): presets for the kind of design run.

A mode narrows the 13-stage pipeline, adds rules and per-stage notes, and
names the inputs a run needs. These hold the parts a user relies on:

  - a run walks its mode's stages and no others, and finishes after them;
  - a run's mode is recorded, so editing the mode file later, or a resumed
    run, does not change what it walks;
  - a journal from before modes still walks all 13 stages;
  - a profile's modes/ extend or replace built-ins, and a broken mode file
    is reported, never raised;
  - selection order: the argument, EDA_AGENT_MODE, mode-use, full_design.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from eda_agent import cli
from eda_agent.design import modes as modes_mod
from eda_agent.design.modes import BUILT_IN, get_mode, load_modes, session_payload
from eda_agent.design.session import STAGES, SessionStore
from eda_agent.design.state_machine import COMPLETE, next_action


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("EDA_AGENT_PROFILE_DIR", raising=False)
    # Set empty rather than deleted, so teardown also removes a value the
    # --mode flag writes straight into os.environ.
    monkeypatch.setenv("EDA_AGENT_MODE", "")
    monkeypatch.chdir(tmp_path)


def _profile(tmp_path, monkeypatch, modes: dict) -> object:
    root = tmp_path / "profile"
    (root / "modes").mkdir(parents=True)
    for name, body in modes.items():
        text = body if isinstance(body, str) else json.dumps(body)
        (root / "modes" / f"{name}.json").write_text(text, encoding="utf-8")
    monkeypatch.setenv("EDA_AGENT_PROFILE_DIR", str(root))
    return root


def _start(tmp_path, mode_name: str = "", inputs: dict | None = None,
           mode=None):
    store = SessionStore(tmp_path / "sessions")
    chosen = mode or get_mode(mode_name)
    return store.start("req", session_id="s1",
                       mode=session_payload(chosen, inputs or {}))


def _walk(journal) -> list[str]:
    """Log ok for whatever stage the harness asks for, until it stops."""
    walked = []
    for _ in range(len(STAGES) + 2):
        action = next_action(journal.state())
        if action.status == COMPLETE:
            return walked
        walked.append(action.stage)
        journal.stage_result(action.stage, "ok")
    pytest.fail(f"never completed; walked {walked}")


# ------------------------------------------------------------ built-ins

def test_every_built_in_walks_stages_in_pipeline_order():
    for mode in BUILT_IN:
        assert mode.stages, mode.name
        assert list(mode.stages) == [s for s in STAGES if s in mode.stages], mode.name
        assert set(mode.stage_notes) <= set(STAGES), mode.name


def test_full_design_is_the_whole_pipeline_with_nothing_added():
    full = get_mode("full_design")
    assert full.stages == STAGES
    assert not full.rules and not full.stage_notes and not full.inputs


def test_reference_redraw_needs_the_reference_and_stops_at_the_schematic():
    mode = get_mode("reference_redraw")
    assert {"vendor", "board", "source"} <= {i.name for i in mode.inputs if i.required}
    assert mode.stages[-1] == "schematic_emit"
    assert "sch_to_pcb" not in mode.stages


# ------------------------------------------------------------ the run

def test_a_run_walks_its_modes_stages_and_completes_after_them(tmp_path):
    journal = _start(tmp_path, "reference_redraw")
    assert _walk(journal) == list(get_mode("reference_redraw").stages)
    assert journal.state().complete


def test_layout_only_starts_at_the_board(tmp_path):
    journal = _start(tmp_path, "layout_only")
    assert next_action(journal.state()).stage == "sch_to_pcb"


def test_the_stage_note_and_rules_reach_the_next_action(tmp_path):
    journal = _start(tmp_path, "reference_redraw")
    action = next_action(journal.state())
    mode = get_mode("reference_redraw")
    assert action.mode == "reference_redraw"
    assert action.mode_note == mode.stage_notes["requirement"]
    assert mode.stage_notes["requirement"] in action.guidance
    assert action.mode_rules == list(mode.rules)


def test_a_journal_from_before_modes_walks_all_thirteen(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    journal = store.start("req", session_id="old")
    state = journal.state()
    assert state.mode == "" and state.stages == STAGES
    action = next_action(state)
    assert action.mode == "" and action.mode_note == "" and action.mode_rules == []
    assert _walk(journal) == list(STAGES)


def test_editing_the_mode_file_does_not_change_a_run_under_way(tmp_path, monkeypatch):
    root = _profile(tmp_path, monkeypatch,
                    {"mine": {"stages": ["requirement", "plan"]}})
    journal = _start(tmp_path, "mine")
    (root / "modes" / "mine.json").write_text(
        json.dumps({"stages": ["outputs"]}), encoding="utf-8")
    assert _walk(journal) == ["requirement", "plan"]


def test_measured_gates_keep_to_the_modes_stages(tmp_path):
    """A failed gate sends the run back; the stage it then picks must still
    be one of the mode's."""
    from eda_agent.design.autonomy import measured_next_action

    journal = _start(tmp_path, "layout_only")
    for stage in ("sch_to_pcb", "rules_stackup"):
        journal.stage_result(stage, "ok")
    journal.stage_result("placement", "ok",
                         data={"placement_audit": {"overlaps": 3}})
    action = measured_next_action(journal.state(), journal.events())
    assert action["stage"] == "placement"


def test_the_report_marks_stages_outside_the_mode(tmp_path):
    from eda_agent.design.report import build_report

    journal = _start(tmp_path, "reference_redraw",
                     {"vendor": "NXP", "board": "EVK", "source": "x.pdf"})
    text = build_report(journal)
    assert "## Mode" in text and "vendor: NXP" in text
    assert "| routing | not in mode |" in text
    unreached = next(line for line in text.splitlines()
                     if line.startswith("- Stages not reached"))
    assert "routing" not in unreached and "requirement" in unreached


# ------------------------------------------------------------ profile modes

def test_a_profile_mode_extends_a_built_in(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch, {"nxp_redraw": {
        "extends": "reference_redraw",
        "inputs": [{"name": "vendor", "description": "v", "default": "NXP"},
                   {"name": "board", "description": "b"},
                   {"name": "source", "description": "s"}],
        "rules": ["Use the ACME title block."],
        "stage_notes": {"plan": "One sheet per NXP sheet."},
        "profile_hints": {"erc_set": "strict"},
    }})
    mode = get_mode("nxp_redraw")
    base = get_mode("reference_redraw")
    assert mode.stages == base.stages
    assert mode.rules == base.rules + ("Use the ACME title block.",)
    assert mode.stage_notes["plan"] == "One sheet per NXP sheet."
    assert mode.stage_notes["requirement"] == base.stage_notes["requirement"]
    assert mode.profile_hints == {"erc_set": "strict"}
    assert mode.missing_inputs({"board": "b", "source": "s"}) == []
    assert mode.resolve_inputs({"board": "b"})["vendor"] == "NXP"


def test_a_profile_mode_without_extends_replaces_a_built_in(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch, {"layout_only": {"stages": ["routing"]}})
    mode = get_mode("layout_only")
    assert mode.stages == ("routing",) and mode.rules == ()


def test_stages_are_put_in_pipeline_order_whatever_the_file_says(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch, {"m": {"stages": ["outputs", "requirement"]}})
    assert get_mode("m").stages == ("requirement", "outputs")


@pytest.mark.parametrize("body, needle", [
    ("{not json", "modes/bad.json"),
    ("[1, 2]", "expected a JSON object"),
    (json.dumps({"stages": ["routing", "pcb_magic"]}), "pcb_magic"),
    (json.dumps({"stage_notes": {"nope": "x"}}), "nope"),
    (json.dumps({"titel": "x"}), "titel"),
    (json.dumps({"extends": "nothing_here"}), "nothing_here"),
    (json.dumps({"profile_hints": {"color": "red"}}), "color"),
    (json.dumps({"inputs": [{"name": "Bad Name"}]}), "inputs[0]"),
])
def test_a_broken_mode_file_is_reported_not_raised(tmp_path, monkeypatch, body, needle):
    _profile(tmp_path, monkeypatch, {"bad": body})
    modes, problems = load_modes()
    assert any(needle in p for p in problems), problems
    assert "full_design" in modes


def test_an_extends_loop_is_reported(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch, {"a": {"extends": "b"}, "b": {"extends": "a"}})
    modes, problems = load_modes()
    assert any("loop" in p for p in problems), problems
    assert "a" not in modes and "b" not in modes


def test_profile_validate_reports_a_bad_mode(tmp_path, monkeypatch, capsys):
    _profile(tmp_path, monkeypatch, {"bad": {"stages": ["warp"]}})
    assert cli.cmd_profile_validate(name=None, here=False) == 1
    assert "warp" in capsys.readouterr().err


# ------------------------------------------------------------ selection

def test_an_unknown_mode_is_refused_not_replaced_by_the_full_pipeline():
    with pytest.raises(ValueError, match="no design mode named 'nope'"):
        get_mode("nope")


def test_selection_order_env_then_mode_use_then_default(monkeypatch, capsys):
    assert modes_mod.selected_mode_name() == ("full_design", "default")
    assert cli.cmd_mode_use("schematic_only", clear=False) == 0
    assert get_mode().name == "schematic_only"
    monkeypatch.setenv("EDA_AGENT_MODE", "layout_only")
    assert get_mode().name == "layout_only"
    assert get_mode("reference_redraw").name == "reference_redraw"
    monkeypatch.delenv("EDA_AGENT_MODE")
    assert cli.cmd_mode_use(None, clear=True) == 0
    assert get_mode().name == "full_design"


def test_mode_use_refuses_an_unknown_mode(tmp_path, capsys):
    assert cli.cmd_mode_use("nope", clear=False) == 1
    assert not modes_mod.active_mode_pointer().exists()


def test_the_mode_flag_sets_the_variable_for_the_command(monkeypatch, capsys):
    from eda_agent import server

    monkeypatch.setattr("sys.argv", ["eda-agent", "--mode", "layout_only",
                                     "mode-show", "--json"])
    assert server.main() == 0
    assert json.loads(capsys.readouterr().out)["name"] == "layout_only"


def test_mode_init_scaffolds_a_mode_that_loads(tmp_path, monkeypatch, capsys):
    root = _profile(tmp_path, monkeypatch, {})
    assert cli.cmd_mode_init("acme_redraw", extends="reference_redraw",
                             force=False) == 0
    assert (root / "modes" / "acme_redraw.json").is_file()
    modes, problems = load_modes()
    assert problems == []
    assert modes["acme_redraw"].stages == get_mode("reference_redraw").stages
    assert cli.cmd_mode_init("acme_redraw", extends=None, force=False) == 1


# ------------------------------------------------------------ through the tools

@pytest.fixture
def mcp(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    from eda_agent.tools import register_all_tools

    monkeypatch.setattr("eda_agent.config.get_config",
                        lambda: SimpleNamespace(workspace_dir=tmp_path))
    m = FastMCP("test")
    register_all_tools(m)
    return m


def _call(mcp, name, args):
    result = asyncio.run(mcp.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


def test_session_start_refuses_a_mode_whose_inputs_are_missing(mcp):
    out = _call(mcp, "design_session_start",
                {"requirement": "r", "mode": "reference_redraw",
                 "inputs": {"vendor": "NXP"}})
    assert "board" in out["error"] and "source" in out["error"]


def test_session_start_in_a_mode_then_next_action(mcp):
    out = _call(mcp, "design_session_start",
                {"requirement": "r", "mode": "reference_redraw",
                 "inputs": json.dumps({"vendor": "NXP", "board": "EVK",
                                       "source": "evk.pdf"})})
    assert out["state"]["mode"] == "reference_redraw"
    assert out["state"]["mode_inputs"]["vendor"] == "NXP"
    action = _call(mcp, "design_next_action", {"session_id": out["session_id"]})
    assert action["stage"] == "requirement"
    assert action["mode"] == "reference_redraw" and action["mode_rules"]


def test_session_start_with_no_mode_uses_the_selected_one(mcp, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_MODE", "layout_only")
    out = _call(mcp, "design_session_start", {"requirement": "r"})
    assert out["state"]["mode"] == "layout_only"
    assert out["state"]["next_stage"] == "sch_to_pcb"


def test_list_and_get_modes(mcp):
    listed = _call(mcp, "design_list_modes", {})
    assert {m["name"] for m in listed["modes"]} >= {b.name for b in BUILT_IN}
    assert listed["selected"] == "full_design"
    got = _call(mcp, "design_get_mode", {"name": "layout_only"})
    assert got["ok"] and "requirement" in got["skipped_stages"]
    assert _call(mcp, "design_get_mode", {"name": "nope"})["ok"] is False


def test_an_unknown_selected_mode_is_flagged(mcp, monkeypatch):
    """Seen 2026-11-08: EDA_AGENT_MODE=no_such_mode was listed as selected
    with no problem, though a run that names no mode is then refused."""
    monkeypatch.setenv("EDA_AGENT_MODE", "no_such_mode")
    listed = _call(mcp, "design_list_modes", {})
    assert listed["selected"] == "no_such_mode"
    assert listed["selected_known"] is False
    assert any("no_such_mode" in p and "not a mode" in p
               for p in listed["problems"])


def test_a_known_selected_mode_raises_no_problem(mcp, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_MODE", "layout_only")
    listed = _call(mcp, "design_list_modes", {})
    assert listed["selected_known"] is True
    assert not any("selected mode" in p for p in listed["problems"])


def test_each_mode_is_a_prompt_that_starts_a_run_in_it():
    from mcp.server.fastmcp import FastMCP

    from eda_agent.tools.requests import register_mode_prompts

    m = FastMCP("test")
    names = register_mode_prompts(m)
    assert set(names) == {f"mode_{b.name}" for b in BUILT_IN}
    got = asyncio.run(m.get_prompt("mode_reference_redraw",
                                   {"vendor": "ST", "board": "NUCLEO-H753ZI"}))
    text = got.messages[0].content.text
    assert 'mode="reference_redraw"' in text
    assert "- vendor: ST" in text and "source: (not given: ask me)" in text
    assert '"board": "NUCLEO-H753ZI"' in text
