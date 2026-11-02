# SPDX-License-Identifier: Apache-2.0
"""The help listing and the parser have to describe the same CLI.

`eda-agent --help` is the only listing of the 43 commands anyone reads,
and argparse prints whatever order add_parser happened to run in.
command_groups() lays that out in sections. Two lists of names can
drift, so these hold them together: a command added to the parser and
not to a group, or left in a group after it was removed, fails here
rather than quietly landing under "Other" or nowhere at all.
"""
from __future__ import annotations

import argparse
import io
from collections import Counter

import pytest

from eda_agent.server import GroupedCommandHelp, build_parser, command_groups


def registered_commands() -> set[str]:
    """Every subcommand the real parser accepts."""
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    pytest.fail("build_parser() registered no subcommands")


def grouped_names() -> list[str]:
    return [name for _title, names in command_groups() for name in names]


def test_every_command_is_listed_in_exactly_one_group():
    counted = Counter(grouped_names())
    missing = sorted(registered_commands() - set(counted))
    assert not missing, (
        "these commands print under 'Other' because command_groups() "
        f"does not name them: {missing}")
    twice = sorted(name for name, n in counted.items() if n > 1)
    assert not twice, f"listed in more than one group: {twice}"


def test_no_group_names_a_command_that_does_not_exist():
    stale = sorted(set(grouped_names()) - registered_commands())
    assert not stale, (
        f"command_groups() names commands the parser does not have: {stale}")


def test_the_generated_calculators_are_grouped_without_being_retyped():
    """Adding a calculator must not also mean editing the group list."""
    from eda_agent import cli

    calculators = dict(command_groups())["Engineering calculators"]
    for verb in cli.CALC_COMMANDS:
        assert verb in calculators, verb


def test_the_rendered_help_shows_the_groups_in_order():
    text = build_parser().format_help()
    titles = [title for title, _names in command_groups()]
    positions = []
    for title in titles:
        heading = f"  {title}:"
        assert heading in text, f"missing heading: {title}"
        positions.append(text.index(heading))
    assert positions == sorted(positions)
    assert "Other:" not in text


def test_each_command_still_appears_under_its_own_heading():
    text = build_parser().format_help()
    for name in registered_commands():
        assert f"    {name}" in text, name


def test_a_command_in_no_group_still_prints():
    """The fallback, because hiding a command is worse than misfiling it.

    Built against a throwaway parser rather than the real one: the real
    one has no ungrouped command, and the test above is what keeps it
    that way.
    """
    parser = argparse.ArgumentParser(
        prog="toy",
        formatter_class=lambda prog: GroupedCommandHelp(
            prog, groups=[("Known", ("named",))]),
    )
    subs = parser.add_subparsers(dest="command", title="commands")
    subs.add_parser("named", help="In a group.")
    subs.add_parser("forgotten", help="In no group at all.")

    text = parser.format_help()
    assert "  Known:" in text
    assert "  Other:" in text
    assert "forgotten" in text


def test_the_formatter_leaves_options_alone():
    """Only the subcommand listing is reformatted."""
    parser = build_parser()
    text = parser.format_help()
    assert "  -h, --help" in text
    assert "--backend {altium,kicad,easyeda,both}" in text
    assert "usage: eda-agent" in text


def test_a_subcommands_own_help_is_untouched():
    parser = build_parser()
    buf = io.StringIO()
    sub = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            sub = action.choices["perf"]
    assert sub is not None
    sub.print_help(buf)
    text = buf.getvalue()
    assert "usage: eda-agent perf" in text
    assert "--limit" in text
    # No group headings leak into a subcommand that has no subcommands.
    assert "Diagnostics:" not in text


# ---- and the README says the same thing --------------------------------
# The table there was a flat 40 rows in roughly parser order, five
# commands short. Grouping it the same way only helps if it stays that
# way, which is what these two are for.

def _cli_section() -> str:
    from pathlib import Path

    text = Path(__file__).resolve().parents[1].joinpath(
        "README.md").read_text(encoding="utf-8")
    start = text.index("\n## CLI\n")
    end = text.index("\n## Configuration\n", start)
    return text[start:end]


def test_the_readme_table_lists_every_command():
    import re

    section = _cli_section()
    listed = set(re.findall(r"`eda-agent ([a-z0-9-]+)", section))
    missing = sorted(registered_commands() - listed)
    assert not missing, f"commands with no row in the README CLI table: {missing}"


def test_the_readme_uses_the_same_section_titles():
    section = _cli_section()
    for title, _names in command_groups():
        assert f"### {title}" in section, title
