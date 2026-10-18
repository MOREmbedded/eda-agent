# SPDX-License-Identifier: Apache-2.0
"""The calculators as CLI verbs.

The parser for these is GENERATED from cli.CALC_COMMANDS rather than
written out ten times, so the risk this file exists to cover is the
table and the functions drifting apart: a renamed parameter, a flag
that fills nothing, a verb pointing at a function that moved. argparse
reports every one of those as a TypeError at the moment a user runs
the command, which is the worst place to find out.
"""
from __future__ import annotations

import inspect
import json

import pytest

from eda_agent import cli
from eda_agent.engineering import pcb_calc


# ---- the table and the functions must agree ----------------------------

def test_every_verb_points_at_a_function_that_exists():
    for verb, (fn_name, _help, _params) in cli.CALC_COMMANDS.items():
        assert hasattr(pcb_calc, fn_name), f"{verb} -> {fn_name}"
        assert callable(getattr(pcb_calc, fn_name)), verb


def test_every_flag_fills_a_real_parameter():
    """A flag named for a parameter that was renamed produces a
    TypeError when the command runs, and nothing before then."""
    for verb, (fn_name, _help, params) in cli.CALC_COMMANDS.items():
        signature = inspect.signature(getattr(pcb_calc, fn_name))
        for name, _type, _default, _phelp in params:
            assert name in signature.parameters, f"{verb}: --{name}"


def test_every_required_parameter_has_a_flag():
    """The other direction: a parameter with no default and no flag
    cannot be supplied, so the verb can never succeed."""
    for verb, (fn_name, _help, params) in cli.CALC_COMMANDS.items():
        signature = inspect.signature(getattr(pcb_calc, fn_name))
        flags = {name for name, _t, _d, _h in params}
        for name, parameter in signature.parameters.items():
            if parameter.default is inspect.Parameter.empty:
                assert name in flags, f"{verb}: {name} has no flag"


def test_the_tables_defaults_match_the_functions_defaults():
    """Two places stating the same default is the same defect as any
    fact stated twice: nothing enforces agreement, and the CLI then
    quietly computes something the library would not."""
    mismatched = []
    for verb, (fn_name, _help, params) in cli.CALC_COMMANDS.items():
        signature = inspect.signature(getattr(pcb_calc, fn_name))
        for name, _type, default, _phelp in params:
            if default is None:
                continue
            actual = signature.parameters[name].default
            if actual is not inspect.Parameter.empty and actual != default:
                mismatched.append(
                    f"{verb} --{name}: CLI says {default!r}, "
                    f"{fn_name} says {actual!r}")
    assert not mismatched, "\n  ".join([""] + mismatched)


def test_every_verb_has_help_text():
    for verb, (_fn, help_text, params) in cli.CALC_COMMANDS.items():
        assert help_text and not help_text.endswith("."), verb
        for name, _t, _d, phelp in params:
            assert phelp, f"{verb} --{name}"


def test_the_verbs_follow_this_clis_flat_hyphenated_shape():
    """Every other command here is a flat hyphenated verb. These would
    be the only nested ones."""
    for verb in cli.CALC_COMMANDS:
        assert verb.startswith("calc-"), verb
        assert " " not in verb and verb.islower(), verb


# ---- running them ------------------------------------------------------

def _run(verb, values, as_json=False):
    return cli.cmd_calc(verb, values, as_json=as_json)


def test_a_calculator_prints_its_answer_and_exits_zero(capsys):
    """B1 at 100 V is the "up to 100" row, 0.10 mm."""
    assert _run("calc-spacing", {"voltage": 100, "environment": "B1"}) == 0
    out = capsys.readouterr().out
    assert "spacing_mm" in out and "0.1" in out
    assert "[published]" in out


def test_json_output_is_the_whole_reply(capsys):
    assert _run("calc-wire", {"awg": 24, "length_m": 1.0,
                              "temperature_c": 20.0}, as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["diameter_mm"] == pytest.approx(0.5106, abs=0.001)


def test_a_refusal_exits_nonzero_so_a_script_can_tell(capsys):
    """"this geometry is impossible" and "here is your answer" must not
    both exit 0, or a shell pipeline cannot branch on it."""
    code = _run("calc-spacing", {"voltage": -5, "environment": "B1"})
    assert code == 1
    assert "Cannot compute" in capsys.readouterr().err


def test_a_refusal_in_json_mode_also_exits_nonzero(capsys):
    code = _run("calc-spacing", {"voltage": -5, "environment": "B1"},
                as_json=True)
    assert code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_an_unknown_verb_exits_two(capsys):
    assert _run("calc-nonsense", {}) == 2
    assert "unknown calculator" in capsys.readouterr().err


def test_caveats_go_to_stderr_so_stdout_is_just_the_figures(capsys):
    """A shell reading the numbers should not have to filter prose out
    of them."""
    _run("calc-fusing", {"width_mils": 20, "copper_oz": 1.0,
                         "time_s": 30.0, "ambient_c": 25.0})
    captured = capsys.readouterr()
    assert "fusing_current_a" in captured.out
    assert "DESTROYS" not in captured.out
    assert "DESTROYS" in captured.err
    assert "OUT OF RANGE" in captured.err


def test_a_disputed_table_cell_is_reported_on_stderr(capsys):
    _run("calc-spacing", {"voltage": 280, "environment": "B2"})
    assert "DISPUTED" in capsys.readouterr().err


@pytest.mark.parametrize("verb", sorted(cli.CALC_COMMANDS))
def test_every_verb_runs_end_to_end_with_plausible_inputs(verb, capsys):
    """One real invocation each, so a verb cannot be registered,
    documented and broken."""
    plausible = {
        "voltage": 100.0, "environment": "B1",
        "width_mils": 20.0, "copper_oz": 1.0, "time_s": 1.0,
        "ambient_c": 25.0,
        "drill_mils": 12.0, "length_mils": 63.0, "pad_mils": 24.0,
        "antipad_mils": 40.0, "dielectric_constant": 4.3,
        "plating_mils": 1.0,
        "turns": 8.0, "outer_diameter_mils": 300.0,
        "inner_diameter_mils": 120.0, "shape": "square",
        "frequency_hz": 1e6, "capacitance_f": 1e-7, "inductance_h": 1e-6,
        "voltage_v": 3.3, "ripple_percent": 5.0,
        "transient_current_a": 2.0,
        "annular_ring_mils": 6.0, "pitch_mils": 39.4,
        "clearance_mils": 8.0,
        "awg": 24.0, "length_m": 1.0, "temperature_c": 20.0,
    }
    _fn, _help, params = cli.CALC_COMMANDS[verb]
    values = {name: plausible[name] for name, _t, _d, _h in params}
    assert _run(verb, values) == 0, verb
    assert capsys.readouterr().out.strip()


# ---- the argparse wiring -----------------------------------------------

def test_the_parser_registers_every_verb_from_the_table(monkeypatch):
    """Built by a loop over the table, so this checks the loop actually
    runs rather than that someone remembered ten add_parser calls.

    Driven through main() with a real argv, because the parser is built
    inside it: anything that only inspected the table would pass while
    the wiring was absent.
    """
    import argparse
    import sys

    from eda_agent import server

    registered: list[str] = []
    original = argparse._SubParsersAction.add_parser

    def spy(self, name, **kwargs):
        registered.append(name)
        return original(self, name, **kwargs)

    monkeypatch.setattr(argparse._SubParsersAction, "add_parser", spy)
    # A verb missing its required flag: argparse exits 2 during parsing,
    # which is after every subcommand has been registered and before any
    # of them runs.
    monkeypatch.setattr(sys, "argv", ["eda-agent", "calc-spacing"])
    with pytest.raises(SystemExit) as exit_info:
        server.main()
    assert exit_info.value.code == 2

    for verb in cli.CALC_COMMANDS:
        assert verb in registered, verb


def test_a_verb_runs_through_main_end_to_end(monkeypatch, capsys):
    """The parser, the dispatch and the calculator in one pass. The
    dispatch reads its values back off the namespace by parameter name,
    so a dest that does not match silently passes None."""
    import sys

    from eda_agent import server

    monkeypatch.setattr(sys, "argv",
                        ["eda-agent", "calc-wire", "--awg", "24"])
    assert server.main() == 0
    assert "0.5105" in capsys.readouterr().out


def test_a_hyphenated_flag_reaches_an_underscored_parameter(monkeypatch,
                                                            capsys):
    """--width-mils has to arrive as width_mils. argparse only does that
    because every flag sets an explicit dest, and dropping it would make
    every multi-word flag unreachable."""
    import sys

    from eda_agent import server

    monkeypatch.setattr(sys, "argv", ["eda-agent", "calc-padstack",
                                      "--drill-mils", "12",
                                      "--annular-ring-mils", "6"])
    assert server.main() == 0
    assert "pad_diameter_mils            24.0" in capsys.readouterr().out
