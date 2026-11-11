# SPDX-License-Identifier: Apache-2.0
"""docs/OPTIONS.md lists every setting, and stays generated from the code.

Two ways it could go wrong, both checked:

* the page drifts from the code it was generated from (a flag added, a
  help text changed, a Config-tab choice added) -> the freshness test;
* a setting is added that the generator cannot see, because environment
  variables are read with ``os.environ.get`` all over the source and their
  descriptions are hand-written in ``eda_agent.settings_catalog`` -> the
  scan, which reads the source for every ``EDA_AGENT_*`` name and fails on
  one with no catalog entry, and on a catalog entry nothing reads any more.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "gen_options_reference", ROOT / "scripts" / "gen_options_reference.py")
gen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen)

from eda_agent import settings_catalog as catalog  # noqa: E402

#: Where settings are read: the Python package, the Altium scripts and the
#: EasyEDA extension.
_SOURCES = [ROOT / "src" / "eda_agent", ROOT / "scripts" / "altium",
            ROOT / "extensions" / "easyeda"]
_SUFFIXES = {".py", ".pas", ".js"}
_NAME = re.compile(r"\b_?(EDA_AGENT_[A-Z0-9_]+)")


def _source_files():
    for base in _SOURCES:
        for path in base.rglob("*"):
            if (path.suffix in _SUFFIXES and "__pycache__" not in path.parts
                    and path.name != "settings_catalog.py"):
                yield path


def names_in_source() -> set[str]:
    found: set[str] = set()
    for path in _source_files():
        found |= set(_NAME.findall(path.read_text(encoding="utf-8", errors="replace")))
    return found


def _source_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in _source_files())


def test_the_generated_page_is_current():
    current = (ROOT / "docs" / "OPTIONS.md").read_text(encoding="utf-8")
    assert current == gen.build_reference(), (
        "docs/OPTIONS.md is stale. Run: python scripts/gen_options_reference.py")


def test_every_setting_in_the_source_is_described():
    described = {v.name for v in catalog.ENV_VARS} | set(catalog.NOT_SETTINGS)
    missing = sorted(names_in_source() - described)
    assert not missing, (
        f"the code reads {missing} but src/eda_agent/settings_catalog.py "
        "does not describe them, so docs/OPTIONS.md cannot list them. Add an "
        "EnvVar entry (or a NOT_SETTINGS one saying what it really is) and "
        "regenerate.")


def test_no_described_setting_is_stale():
    in_source = names_in_source()
    stale = sorted(v.name for v in catalog.ENV_VARS if v.name not in in_source)
    assert not stale, f"documented but no longer read anywhere: {stale}"
    text = _source_text()
    gone = sorted(v.name for v in catalog.OTHER_ENV_VARS if v.name not in text)
    assert not gone, f"documented but no longer read anywhere: {gone}"


def test_every_variable_is_in_a_known_group():
    groups = set(catalog.GROUPS)
    stray = [v.name for v in catalog.ENV_VARS + catalog.OTHER_ENV_VARS
             if v.group not in groups]
    assert not stray, f"variables in no section of the page: {stray}"


def test_the_structured_settings_match_the_code():
    from eda_agent import profile
    from eda_agent.preferences import CHOICES
    from eda_agent.workspace_start import KEYS

    assert set(catalog.PREFERENCES) == set(CHOICES)
    assert set(catalog.WORKSPACE_KEYS) == set(KEYS)
    assert set(catalog.PROFILE_KEYS["footprint_defaults.json"]) == set(
        profile._FOOTPRINT_DEFAULT_KEYS)
    assert set(catalog.PROFILE_KEYS["document_settings.json"]) == set(
        profile._DOCUMENT_SETTING_KEYS)
    assert set(catalog.PROFILE_KEYS["phase_gates.json"]) == set(
        profile._PHASE_GATE_KEYS)


def test_every_cli_command_and_flag_is_on_the_page():
    import argparse

    from eda_agent.server import build_parser

    page = (ROOT / "docs" / "OPTIONS.md").read_text(encoding="utf-8")
    parser = build_parser()
    sub = next(a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction))
    missing = []
    for name, cmd in sub.choices.items():
        if f"#### `eda-agent {name}`" not in page:
            missing.append(name)
        for action in cmd._actions:
            for flag in action.option_strings:
                if flag not in ("-h", "--help") and f"`{flag}`" not in page:
                    missing.append(f"{name} {flag}")
    assert not missing, f"not on docs/OPTIONS.md: {missing}"


def test_the_scan_finds_a_new_setting(tmp_path):
    """Guard the guard: the pattern sees each way a name is written."""
    text = ('os.environ.get("EDA_AGENT_NEW_THING")\n'
            "ENV = 'EDA_AGENT_OTHER'\n"
            'os.environ["_EDA_AGENT_PRIVATE"] = "1"\n')
    assert set(_NAME.findall(text)) == {
        "EDA_AGENT_NEW_THING", "EDA_AGENT_OTHER", "EDA_AGENT_PRIVATE"}
