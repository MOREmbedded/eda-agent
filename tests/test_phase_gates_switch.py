# SPDX-License-Identifier: Apache-2.0
"""The NPI gate is off until an owner asks for it, and says so.

Everything about what the 42 checks ASK is measured in
test_phase_gates_conventions_and_altium365.py, with the gate forced on.
This file measures the switch: what a fresh install serves, where the
setting is read from, and what the tools do about a stage that is off.

Two failures this exists to prevent, both of them quiet:

  - A default install advertising three tools for a process the owner
    does not run, which is how the feature was shipped.
  - A profile naming only some stages and the product being reported as
    reaching production by clearing them, with nothing in the reply
    saying which stages that claim rests on.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from eda_agent import profile as profile_mod
from eda_agent.design import phase_gates
from eda_agent.tools.registry import ToolRegistry

PHASE_TOOLS = ("design_phase_gate", "design_phase_answer",
               "design_phase_standing")


@pytest.fixture
def no_profile(monkeypatch, tmp_path):
    """A machine with no profile anywhere and no variable set.

    resolve_profile_dir walks up from the current directory and then
    falls back to a central pointer in the home directory, so a test run
    from inside a checkout that has its own .eda-agent/profile/ would
    otherwise read the developer's settings and pass whatever they say.
    """
    monkeypatch.delenv("EDA_AGENT_PHASES", raising=False)
    monkeypatch.setattr(profile_mod, "resolve_profile_dir",
                        lambda start=None: None)
    return tmp_path


def _surface(backend: str = "altium") -> set[str]:
    from eda_agent.tools import register_backend

    registry = ToolRegistry()
    register_backend(registry, backend)
    return {t.name for t in asyncio.run(registry.list_tools())}


# --- what a fresh install serves ------------------------------------------

def test_a_machine_with_no_profile_runs_no_stages(no_profile):
    assert profile_mod.get_phase_stages() == ()
    assert phase_gates.DEFAULT_STAGES == ()


def test_the_three_tools_are_not_advertised_by_default(no_profile):
    """Hidden, not present-and-refusing.

    A tool that exists only to say it is switched off still costs a call
    and a round trip to teach what its absence teaches for free, and it
    reads to a model as a capability that is there.
    """
    surface = _surface()
    assert not (set(PHASE_TOOLS) & surface), (
        f"the default surface advertises {sorted(set(PHASE_TOOLS) & surface)}; "
        f"the gate is meant to be off until an owner asks for it")
    # And the surface is not empty, which would make the line above pass
    # for the wrong reason.
    assert len(surface) > 100


def test_turning_it_on_brings_all_three_back(monkeypatch):
    monkeypatch.setenv("EDA_AGENT_PHASES", "all")
    assert set(PHASE_TOOLS) <= _surface()


def test_the_backend_check_for_a_renamed_tool_still_bites(monkeypatch):
    """The exemption lasts exactly as long as the gate is off.

    register_offline_design_tools raises when OFFLINE_DESIGN_TOOLS names
    a tool that no longer exists, which is what catches a rename. Three
    of those names are now allowed to be absent, and if that exemption
    applied while the gate was ON a rename inside the phase tools would
    stop being caught at all.
    """
    from eda_agent.tools import (OFFLINE_DESIGN_TOOLS, PHASE_GATE_TOOLS,
                                 register_offline_design_tools)

    assert set(PHASE_GATE_TOOLS) <= set(OFFLINE_DESIGN_TOOLS)

    monkeypatch.setenv("EDA_AGENT_PHASES", "all")

    # Bound before patching, or the stand-in calls itself: the real
    # function is reached through a late import of the name being
    # replaced.
    from eda_agent.tools.design import register_design_tools as real

    def _as_if_renamed(mcp):
        real(mcp)
        for name in PHASE_GATE_TOOLS:
            mcp._tools.pop(name, None)

    monkeypatch.setattr("eda_agent.tools.design.register_design_tools",
                        _as_if_renamed)
    with pytest.raises(RuntimeError, match="no longer exist"):
        register_offline_design_tools(ToolRegistry())


# --- where the setting is read from ---------------------------------------

def test_a_profile_turns_it_on(monkeypatch, tmp_path):
    monkeypatch.delenv("EDA_AGENT_PHASES", raising=False)
    (tmp_path / "phase_gates.json").write_text(
        json.dumps({"stages": ["evt", "dvt"]}), encoding="utf-8")
    monkeypatch.setattr(profile_mod, "resolve_profile_dir",
                        lambda start=None: tmp_path)
    assert profile_mod.get_phase_stages() == ("evt", "dvt")
    assert profile_mod.describe_phase_stages()["source"] == "profile"


def test_the_variable_overrules_the_profile(monkeypatch, tmp_path):
    """Including turning it OFF, which an empty value cannot say.

    An empty EDA_AGENT_PHASES means "the profile decides", so without the
    word "none" a shell could enable stages over a profile but never
    disable them.
    """
    (tmp_path / "phase_gates.json").write_text(
        json.dumps({"stages": ["all"]}), encoding="utf-8")
    monkeypatch.setattr(profile_mod, "resolve_profile_dir",
                        lambda start=None: tmp_path)

    monkeypatch.setenv("EDA_AGENT_PHASES", "evt")
    assert profile_mod.get_phase_stages() == ("evt",)
    monkeypatch.setenv("EDA_AGENT_PHASES", "none")
    assert profile_mod.get_phase_stages() == ()
    monkeypatch.setenv("EDA_AGENT_PHASES", "")
    assert profile_mod.get_phase_stages() == phase_gates.STAGES


def test_a_typo_degrades_quietly_and_is_reported_loudly(monkeypatch, tmp_path):
    """Both halves, because either alone is a bug.

    Raising would leave the server without a tool surface over a
    misspelled word. Degrading alone would make the stages vanish with
    nothing anywhere saying why.
    """
    monkeypatch.delenv("EDA_AGENT_PHASES", raising=False)
    (tmp_path / "phase_gates.json").write_text(
        json.dumps({"stages": ["evt", "bananas"]}), encoding="utf-8")
    monkeypatch.setattr(profile_mod, "resolve_profile_dir",
                        lambda start=None: tmp_path)

    assert profile_mod.get_phase_stages() == ()
    found = profile_mod.describe_phase_stages()
    assert "bananas" in found["problem"]
    assert profile_mod.check_phase_gates({"stages": ["evt", "bananas"]})


def test_an_unknown_key_in_the_file_is_named(monkeypatch):
    """A misspelled key is the quietest way for a profile to do nothing."""
    problems = profile_mod.check_phase_gates({"stage": ["evt"]})
    assert problems and "stage" in problems[0]
    assert not profile_mod.check_phase_gates({"stages": ["evt"], "_comment": "x"})


# --- what the tools do about a stage that is off --------------------------

@pytest.fixture
def some_stages(monkeypatch, tmp_path):
    """A profile running feasibility and prototype, and nothing later."""
    monkeypatch.setenv("EDA_AGENT_PHASES", "feasibility prototype")
    from eda_agent import config
    monkeypatch.setattr(config.get_config(), "workspace_dir", tmp_path,
                        raising=False)
    from eda_agent.tools import register_backend
    registry = ToolRegistry()
    register_backend(registry, "altium")
    return registry


def test_a_stage_that_is_off_is_refused_rather_than_answered(some_stages):
    reply = asyncio.run(some_stages.get("design_phase_gate").fn("dvt"))
    assert reply["ok"] is False
    assert "feasibility, prototype" in reply["reason"]
    assert asyncio.run(some_stages.get("design_phase_gate").fn(
        "feasibility"))["stage"] == "feasibility"


def test_an_answer_for_a_stage_that_is_off_is_refused(some_stages):
    """Evidence nothing reviews is worse than no evidence.

    design_phase_standing skips a stage that is off, so an answer
    recorded against one would sit in the ledger looking like progress
    and counting for nothing.
    """
    reply = asyncio.run(some_stages.get("design_phase_answer").fn(
        "dvt:lifecycle", "met", detail="checked"))
    assert reply["ok"] is False and "dvt" in reply["reason"]


def test_production_means_the_stages_this_profile_runs(some_stages):
    """And the reply says which, because it is the weaker claim.

    Clearing two stages is not clearing five, and a bare
    in_production=true would read as the second.
    """
    answer = some_stages.get("design_phase_answer").fn
    for check in phase_gates.CHECKS.values():
        if check.stage in ("feasibility", "prototype"):
            asyncio.run(answer(check.key, "met", detail="x", product="p"))
    standing = asyncio.run(some_stages.get("design_phase_standing").fn(
        product="p"))
    assert standing["in_production"] is True
    assert standing["stages_run"] == ["feasibility", "prototype"]
    assert [s["stage"] for s in standing["stages"]] == ["feasibility",
                                                        "prototype"]


def test_an_answer_outlives_the_stage_being_switched_off(monkeypatch, tmp_path):
    """Turning the gate off must not destroy a programme's audit trail.

    The ledger is a file, and switching stages is a setting; nothing in
    the switch touches the file, so the evidence comes back with the
    stages. Measured rather than asserted, because "it should be
    obvious" is how data gets deleted.
    """
    ledger = phase_gates.Ledger(tmp_path / "p.jsonl")
    ledger.add("dvt:lifecycle", phase_gates.MET, detail="all in production")

    off = phase_gates.standing(ledger.answers(), stages=("feasibility",))
    assert [r.stage for r in off[1]] == ["feasibility"]

    back = phase_gates.review("dvt", ledger.answers())
    line = next(l for l in back.lines if l.check.key == "dvt:lifecycle")
    assert line.outcome == phase_gates.MET


# --- the guide does not offer what the server is not serving --------------

def test_the_guide_withholds_the_recipe_while_the_gate_is_off(no_profile):
    """The failure the guide exists to prevent, in its own house.

    tool_guide was built because capabilities were reported absent when
    they were present. Naming three tools this server is not advertising
    is that failure with more confidence behind it.
    """
    from eda_agent.tools.guidance import guidance_for

    out = guidance_for("is this board ready for dvt", "", "altium", {})
    named = {t for r in out["recipes"] for t in r.get("use", [])}
    assert not (named & set(PHASE_TOOLS))


def test_the_guide_offers_it_again_once_the_gate_is_on(monkeypatch):
    from eda_agent.tools.guidance import guidance_for

    monkeypatch.setenv("EDA_AGENT_PHASES", "all")
    out = guidance_for("is this board ready for dvt", "", "altium", {})
    named = {t for r in out["recipes"] for t in r.get("use", [])}
    assert named & set(PHASE_TOOLS)
