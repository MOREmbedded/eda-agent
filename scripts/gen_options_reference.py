#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate docs/OPTIONS.md: every setting, from the code that reads it.

Usage:
    python scripts/gen_options_reference.py          # write docs/OPTIONS.md
    python scripts/gen_options_reference.py --check  # exit 1 if it is stale

Sources, so the page cannot drift from the program:

* command-line flags: the real argparse parser (``server.build_parser``),
  walked command by command in the ``--help`` grouping;
* environment variables: ``eda_agent.settings_catalog`` (descriptions),
  which a test holds to the names the source actually reads, plus each
  part provider's own ``env_vars``;
* the Config tab / ``preferences.json``: ``eda_agent.preferences.CHOICES``;
* ``workspace.json``: ``eda_agent.workspace_start.KEYS``;
* profile files: the key sets ``eda_agent.profile`` validates.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "OPTIONS.md"
sys.path.insert(0, str(ROOT / "src"))


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _flag_values(action: argparse.Action) -> str:
    if action.choices:
        return ", ".join(f"`{c}`" for c in action.choices)
    if action.nargs == 0:
        return "switch"
    if action.metavar:
        return f"`{action.metavar}`"
    return f"`{action.dest.upper()}`"


def _arguments(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    return [a for a in parser._actions
            if not isinstance(a, (argparse._HelpAction, argparse._VersionAction,
                                  argparse._SubParsersAction))]


def _arg_table(actions: list[argparse.Action]) -> list[str]:
    rows = ["| Argument | Takes | What it does |", "|---|---|---|"]
    for a in actions:
        name = ", ".join(f"`{o}`" for o in a.option_strings) or f"`{a.metavar or a.dest}`"
        help_text = ((a.help or "").replace("%(default)s", str(a.default))
                     .replace("%%", "%"))
        if help_text == argparse.SUPPRESS:
            continue
        rows.append(f"| {name} | {_cell(_flag_values(a))} | {_cell(help_text)} |")
    return rows


def cli_section() -> list[str]:
    from eda_agent.server import build_parser, command_groups

    parser = build_parser()
    sub = next(a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction))
    helps = {c.dest: c.help for c in sub._choices_actions}
    out = ["## Command line", "",
           "Flags that apply to every command (put them before the command):", ""]
    out += _arg_table(_arguments(parser))
    out.append("")
    listed: set[str] = set()
    for title, names in command_groups():
        out += [f"### {title}", ""]
        for name in names:
            if name not in sub.choices:
                continue
            listed.add(name)
            out += _command(name, sub.choices[name], helps.get(name))
    rest = sorted(set(sub.choices) - listed)
    if rest:
        out += ["### Other commands", ""]
        for name in rest:
            out += _command(name, sub.choices[name], helps.get(name))
    return out


def _command(name: str, parser: argparse.ArgumentParser, help_text) -> list[str]:
    out = [f"#### `eda-agent {name}`", ""]
    if help_text and help_text != argparse.SUPPRESS:
        out += [_cell(help_text), ""]
    args = [a for a in _arguments(parser) if (a.help or "") != argparse.SUPPRESS]
    nested = next((a for a in parser._actions
                   if isinstance(a, argparse._SubParsersAction)), None)
    if args:
        out += _arg_table(args) + [""]
    if nested:
        for sub_name, sub_parser in nested.choices.items():
            out += [f"`eda-agent {name} {sub_name}`", ""]
            sub_args = _arguments(sub_parser)
            if sub_args:
                out += _arg_table(sub_args) + [""]
    if not args and not nested:
        out += ["No arguments.", ""]
    return out


def env_section() -> list[str]:
    from eda_agent.libimport.providers import available_providers
    from eda_agent.settings_catalog import ENV_VARS, GROUPS, OTHER_ENV_VARS

    out = ["## Environment variables", "",
           "Set them in your MCP client's server config (an `env` block), or "
           "for your Windows user with `setx NAME value`. The server reads "
           "your saved user environment too, so no restart of the client is "
           "needed after `setx` for the Altium 365 settings.", ""]
    everything = list(ENV_VARS) + list(OTHER_ENV_VARS)
    for group in GROUPS:
        rows = [v for v in everything if v.group == group]
        if not rows:
            continue
        out += [f"### {group}", "",
                "| Variable | Default | What it does |", "|---|---|---|"]
        out += [f"| `{v.name}` | {_cell(v.default)} | {_cell(v.description)} |"
                for v in rows]
        out.append("")
    out += ["### Part provider credentials", "",
            "Each catalogue provider reads its own credentials. Enable the "
            "provider with `EDA_AGENT_PART_PROVIDERS` as well.", "",
            "| Provider | Variables |", "|---|---|"]
    for p in available_providers():
        names = getattr(p, "env_vars", ())
        if names:
            cell = ', '.join(f'`{n}`' for n in names)
            fallback = getattr(p, "fallback_env_vars", ())
            if fallback:
                cell += (", or else " + ', '.join(f'`{n}`' for n in fallback))
            out.append(f"| `{p.name}` | {cell} |")
    out.append("")
    return out


def preferences_section() -> list[str]:
    from eda_agent.preferences import CHOICES, STRICT
    from eda_agent.settings_catalog import PREFERENCES

    out = ["## Config tab and `preferences.json`", "",
           "The Config tab on the Altium status window writes these to "
           "`preferences.json` in the workspace. A profile may carry its own "
           "`preferences.json` with the same keys plus `default_mode`. For "
           "the safety switches the stricter of the two wins; for the rest "
           "the profile wins. The first value listed is the default.", "",
           "| Key | Values | Safety switch | What it does |", "|---|---|---|---|"]
    for key, allowed in CHOICES.items():
        values = ", ".join(f"`{v}`" for v in allowed)
        strict = "yes" if key in STRICT else ""
        out.append(f"| `{key}` | {values} | {strict} | {_cell(PREFERENCES[key])} |")
    out += ["| `default_mode` | a design mode name | | Profile only: the "
            "design mode a run uses when it names none. |", ""]
    return out


def workspace_section() -> list[str]:
    from eda_agent.settings_catalog import WORKSPACE_KEYS
    from eda_agent.workspace_start import KEYS

    out = ["## `workspace.json`", "",
           "`<workspace>/.eda-agent/workspace.json` is what `eda-agent start` "
           "reads to bring a design workspace up. Unknown keys are refused, "
           "and so is anything that looks like a secret: keep secrets in "
           "your user environment.", "",
           "| Key | What it does |", "|---|---|"]
    out += [f"| `{k}` | {_cell(WORKSPACE_KEYS[k])} |" for k in sorted(KEYS)]
    out.append("")
    return out


def profile_section() -> list[str]:
    from eda_agent import profile
    from eda_agent.settings_catalog import PROFILE_KEYS

    declared = {
        "footprint_defaults.json": profile._FOOTPRINT_DEFAULT_KEYS,
        "document_settings.json": profile._DOCUMENT_SETTING_KEYS,
        "phase_gates.json": profile._PHASE_GATE_KEYS,
    }
    out = ["## Profile files with fixed keys", "",
           "A profile also holds naming, design rules, ERC levels, fab "
           "profiles, modes and templates, described in "
           "[Profiles](PROFILES.md). These files have a fixed set of keys, "
           "and `eda-agent profile-validate` refuses any other:", ""]
    for name, keys in declared.items():
        out += [f"### `{name}`", "", "| Key | What it does |", "|---|---|"]
        out += [f"| `{k}` | {_cell(PROFILE_KEYS[name][k])} |" for k in sorted(keys)]
        out.append("")
    return out


def build_reference() -> str:
    lines = [
        "<!-- Generated by scripts/gen_options_reference.py. Do not edit by hand: -->",
        "<!-- change the code or src/eda_agent/settings_catalog.py and regenerate. -->",
        "",
        "# All options",
        "",
        "Every setting eda-agent has, in one place: command-line flags, "
        "environment variables, the Config tab, `workspace.json` and the "
        "profile files with fixed keys. This page is generated from the "
        "code, and a test fails when a setting is added without a line "
        "here.",
        "",
        "For a short tour of the ones most people change, see "
        "[Configuration](CONFIGURATION.md).",
        "",
    ]
    for section in (env_section, preferences_section, workspace_section,
                    profile_section, cli_section):
        lines += section()
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="exit 1 when docs/OPTIONS.md is out of date")
    args = ap.parse_args()
    text = build_reference()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print("docs/OPTIONS.md is stale: run "
                  "python scripts/gen_options_reference.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
