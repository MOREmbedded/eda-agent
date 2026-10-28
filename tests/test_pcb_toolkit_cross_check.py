# SPDX-License-Identifier: Apache-2.0
"""Cross-checking a calculator against pcb-toolkit.

The binary is optional and is not on this machine, so the easy thing
would be to test only the not-installed path and call it covered. That
would leave the part that matters -- the comparison -- unexercised. A
stub binary stands in for it here, which also keeps the tests honest
about what this project has and has not run: it has never seen the
real pcb-toolkit reply.
"""
from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from eda_agent.engineering import pcb_toolkit
from eda_agent.engineering.pcb_toolkit import UNMAPPED, cross_check


# ---- refusals that cost nothing to get right ---------------------------

def test_without_the_binary_it_says_how_to_get_one(monkeypatch):
    """"not found" helps nobody when the reason is that upstream does
    not publish a binary at all."""
    monkeypatch.setattr(pcb_toolkit, "find_binary", lambda: None)
    result = cross_check("ipc2221_spacing", {"voltage": 100})
    assert result["ok"] is False
    assert "cargo build" in result["reason"]
    assert "not a dependency" in result["reason"]


@pytest.mark.parametrize("calculator", sorted(UNMAPPED))
def test_a_deliberately_unmapped_calculator_says_why(calculator):
    """"No mapping" and "I forgot" look identical unless one of them
    explains itself."""
    result = cross_check(calculator, {})
    assert result["ok"] is False
    assert UNMAPPED[calculator] in result["reason"]


def test_an_unknown_calculator_lists_both_sets():
    result = cross_check("nope", {})
    assert result["ok"] is False
    assert "ipc2221_spacing" in result["reason"]
    assert "via_parasitics" in result["reason"]


def test_a_local_refusal_stops_before_the_subprocess(monkeypatch):
    """No point running the other implementation when this one already
    says the input is impossible."""
    monkeypatch.setattr(pcb_toolkit, "find_binary", lambda: "/nope")
    called = []
    monkeypatch.setattr(pcb_toolkit.subprocess, "run",
                        lambda *a, **k: called.append(a))
    result = cross_check("ipc2221_spacing", {"voltage": -5})
    assert result["ok"] is False
    assert "refused before anything was compared" in result["reason"]
    assert not called


def test_the_environment_variable_wins_over_path(monkeypatch, tmp_path):
    binary = tmp_path / "pcb-toolkit"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setenv("EDA_AGENT_PCB_TOOLKIT", str(binary))
    monkeypatch.setattr(pcb_toolkit.shutil, "which",
                        lambda _n: "/somewhere/else")
    assert pcb_toolkit.find_binary() == str(binary)


def test_an_override_pointing_at_nothing_is_not_silently_ignored(
        monkeypatch, tmp_path):
    """Falling back to PATH here would run a DIFFERENT binary from the
    one the user named, and report results as if it were theirs."""
    monkeypatch.setenv("EDA_AGENT_PCB_TOOLKIT", str(tmp_path / "absent"))
    monkeypatch.setattr(pcb_toolkit.shutil, "which", lambda _n: "/on/path")
    assert pcb_toolkit.find_binary() is None


# ---- the comparison, against a stub ------------------------------------

def _stub(monkeypatch, payload, returncode=0, stderr=""):
    """Stand in for the binary, recording the argv it was given."""
    seen = {}

    class _Completed:
        def __init__(self):
            self.returncode = returncode
            self.stdout = (payload if isinstance(payload, str)
                           else json.dumps(payload))
            self.stderr = stderr

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr(pcb_toolkit, "find_binary", lambda: "pcb-toolkit")
    monkeypatch.setattr(pcb_toolkit.subprocess, "run", fake_run)
    return seen


def test_matching_numbers_are_reported_as_agreements(monkeypatch):
    _stub(monkeypatch, {"spacing_mm": 0.6, "voltage_v": 48.0})
    result = cross_check("ipc2221_spacing",
                         {"voltage": 48, "environment": "B2"})
    assert result["ok"]
    fields = {row["field"] for row in result["agreements"]}
    assert "spacingmm" in fields
    assert result["disagreements"] == []


def test_a_real_disagreement_is_surfaced(monkeypatch):
    """The entire reason for running two implementations."""
    _stub(monkeypatch, {"spacing_mm": 1.25})
    result = cross_check("ipc2221_spacing",
                         {"voltage": 280, "environment": "B2"})
    disagreement = next(r for r in result["disagreements"]
                        if r["field"] == "spacingmm")
    assert disagreement["ours"] == 2.5
    assert disagreement["theirs"] == 1.25


def test_fields_that_do_not_pair_are_listed_not_dropped(monkeypatch):
    """Silently comparing only what happened to match would report
    agreement on a comparison that barely happened."""
    _stub(monkeypatch, {"spacing_mm": 0.6, "something_of_theirs": 42})
    result = cross_check("ipc2221_spacing",
                         {"voltage": 48, "environment": "B2"})
    assert "somethingoftheirs" in result["unpaired_theirs"]
    assert result["unpaired_ours"]


def test_pairing_is_by_name_and_the_result_says_so(monkeypatch):
    _stub(monkeypatch, {"spacing_mm": 0.6})
    result = cross_check("ipc2221_spacing",
                         {"voltage": 48, "environment": "B2"})
    assert "pair by NAME" in result["note"]


def test_a_text_field_cannot_be_compared_as_a_number(monkeypatch):
    _stub(monkeypatch, {"spacing_mm": "0.6"})
    result = cross_check("ipc2221_spacing",
                         {"voltage": 48, "environment": "B2"})
    assert not any(r["field"] == "spacingmm"
                   for r in result["agreements"] + result["disagreements"])


def test_a_boolean_is_not_treated_as_one_or_zero(monkeypatch):
    """bool is an int in Python, so an `ok: true` on both sides would
    otherwise pair up and report a meaningless agreement."""
    _stub(monkeypatch, {"ok": True, "spacing_mm": 0.6})
    result = cross_check("ipc2221_spacing",
                         {"voltage": 48, "environment": "B2"})
    assert not any(r["field"] == "ok"
                   for r in result["agreements"] + result["disagreements"])


# ---- the argv it builds -------------------------------------------------

def test_the_flags_come_from_upstreams_own_definitions(monkeypatch):
    """Every flag here was read from pcb-toolkit's clap structs. A
    guessed name fails at the moment a user runs it, which is the one
    place this feature must not fail."""
    seen = _stub(monkeypatch, {"spacing_mm": 0.6})
    cross_check("ipc2221_spacing", {"voltage": 48, "environment": "B2"})
    argv = seen["argv"]
    assert argv[1] == "--json"
    assert argv[2] == "spacing"
    assert "--voltage" in argv and "--device-type" in argv
    # pcb-toolkit takes the class lowercase.
    assert argv[argv.index("--device-type") + 1] == "b2"


def test_a_length_argument_carries_its_unit(monkeypatch):
    """pcb-toolkit's Length type parses a suffix; a bare number means
    whatever that flag's default unit is, which is not something to
    assume."""
    seen = _stub(monkeypatch, {"fusing_current_a": 5.1})
    cross_check("fusing_current", {"width_mils": 20, "copper_oz": 1.0,
                                   "time_s": 1.0, "ambient_c": 25.0})
    argv = seen["argv"]
    assert argv[argv.index("--width") + 1] == "20mil"
    assert argv[argv.index("--copper") + 1] == "1.0oz"


def test_optional_reactance_arguments_are_only_sent_when_given(monkeypatch):
    seen = _stub(monkeypatch, {"capacitive_reactance_ohm": 1.59})
    cross_check("reactance", {"frequency_hz": 1e6, "capacitance_f": 1e-7,
                              "inductance_h": 0.0})
    assert "--cap" in seen["argv"]
    assert "--ind" not in seen["argv"]


def test_padstack_uses_the_nested_subcommand(monkeypatch):
    seen = _stub(monkeypatch, {"pad_diameter_mils": 24})
    cross_check("padstack_geometry", {"drill_mils": 12,
                                      "annular_ring_mils": 6,
                                      "clearance_mils": 8})
    assert seen["argv"][2:4] == ["padstack", "thru-hole"]


def test_a_missing_required_argument_is_named(monkeypatch):
    _stub(monkeypatch, {})
    result = cross_check("ipc2221_spacing", {"environment": "B2"})
    assert result["ok"] is False
    assert "voltage" in result["reason"]


# ---- failures from the other side ---------------------------------------

def test_a_nonzero_exit_reports_its_stderr(monkeypatch):
    _stub(monkeypatch, {}, returncode=2, stderr="error: invalid value")
    result = cross_check("ipc2221_spacing",
                         {"voltage": 48, "environment": "B2"})
    assert result["ok"] is False
    assert "invalid value" in result["reason"]
    # Our own answer is still handed back rather than thrown away.
    assert result["ours"]["ok"] is True


def test_output_that_is_not_json_is_reported_with_what_was_seen(monkeypatch):
    _stub(monkeypatch, "not json at all")
    result = cross_check("ipc2221_spacing",
                         {"voltage": 48, "environment": "B2"})
    assert result["ok"] is False
    assert "not json at all" in result["stdout"]


def test_a_binary_that_cannot_be_run_is_refused_not_raised(monkeypatch):
    monkeypatch.setattr(pcb_toolkit, "find_binary", lambda: "pcb-toolkit")

    def boom(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(pcb_toolkit.subprocess, "run", boom)
    result = cross_check("ipc2221_spacing",
                         {"voltage": 48, "environment": "B2"})
    assert result["ok"] is False
    assert "permission denied" in result["reason"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_no_console_window_flashes_on_windows(monkeypatch):
    """Same treatment the LTspice runner needed: a short-lived child
    that pops a window is disruptive when something calls it in a
    loop."""
    seen = _stub(monkeypatch, {"spacing_mm": 0.6})
    cross_check("ipc2221_spacing", {"voltage": 48, "environment": "B2"})
    # noconsole.no_window() is the one helper for this now; it hides the
    # window with CREATE_NO_WINDOW rather than a STARTUPINFO record.
    assert seen["kwargs"].get("creationflags")


# ---- the mapping table itself -------------------------------------------

def test_no_calculator_is_both_mapped_and_unmapped():
    assert not set(pcb_toolkit._MAPPING) & set(UNMAPPED)


def test_every_mapped_calculator_exists():
    from eda_agent.engineering import pcb_calc

    for name in pcb_toolkit._MAPPING:
        assert hasattr(pcb_calc, name), name


def test_every_calculator_is_either_mapped_or_explained():
    """A calculator added later must not quietly fall out of both
    lists, which would read as "cross-checking is not possible" rather
    than "nobody has looked"."""
    from eda_agent.engineering import pcb_calc

    covered = set(pcb_toolkit._MAPPING) | set(UNMAPPED)
    missing = [name for name in pcb_calc.__all__ if name not in covered]
    assert not missing, (
        f"these calculators are neither mapped to a pcb-toolkit command "
        f"nor listed in UNMAPPED with a reason: {missing}")


def test_the_spacing_mapping_warns_about_the_revision_difference():
    """pcb-toolkit targets IPC-2221C and takes b1-b5/a6-a8; this
    carries the A/B table with b1-b4/a5-a7, and C REDEFINES those
    columns. A disagreement there may be the revision rather than an
    error, and a reader has to be told that before they go hunting."""
    caveat = pcb_toolkit._MAPPING["ipc2221_spacing"]["caveat"]
    assert "IPC-2221C" in caveat and "REDEFINES" in caveat


# ---- the CLI verb --------------------------------------------------------

def test_the_cli_exits_one_when_the_two_disagree(monkeypatch, capsys):
    """A script running this in CI needs to branch on the outcome."""
    from eda_agent import cli

    _stub(monkeypatch, {"spacing_mm": 1.25})
    code = cli.cmd_calc_cross_check(
        "ipc2221_spacing", ["voltage=280", "environment=B2"])
    assert code == 1
    assert "DISAGREE" in capsys.readouterr().out


def test_the_cli_exits_zero_when_they_agree(monkeypatch, capsys):
    from eda_agent import cli

    _stub(monkeypatch, {"spacing_mm": 2.5})
    code = cli.cmd_calc_cross_check(
        "ipc2221_spacing", ["voltage=280", "environment=B2"])
    assert code == 0
    assert "agree" in capsys.readouterr().out


def test_a_malformed_cli_argument_exits_two(monkeypatch, capsys):
    from eda_agent import cli

    assert cli.cmd_calc_cross_check("ipc2221_spacing", ["nonsense"]) == 2
    assert capsys.readouterr().err


def test_a_non_numeric_argument_is_passed_through(monkeypatch):
    """environment=B2 is a string and must not be mangled into a
    float."""
    from eda_agent import cli

    seen = _stub(monkeypatch, {"spacing_mm": 0.6})
    cli.cmd_calc_cross_check("ipc2221_spacing",
                             ["voltage=48", "environment=B2"])
    assert seen["argv"][seen["argv"].index("--device-type") + 1] == "b2"
