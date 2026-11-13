# SPDX-License-Identifier: Apache-2.0
"""The catalogue describes every optional part once, and agrees with
pyproject.toml, the repository and the program finders."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from eda_agent.features import catalog as cat
from eda_agent.features import detect

ROOT = Path(__file__).resolve().parents[2]

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    tomllib = None


def _pyproject():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_every_pyproject_extra_belongs_to_one_feature():
    extras = set(_pyproject()["project"]["optional-dependencies"])
    owners = {}
    for f in cat.FEATURES:
        if f.extra:
            assert f.extra not in owners, f"extra {f.extra} named by {owners[f.extra]} and {f.id}"
            owners[f.extra] = f.id
    assert extras == set(owners), (
        f"extras with no feature: {sorted(extras - set(owners))}; "
        f"features naming no such extra: {sorted(set(owners) - extras)}")


def test_every_package_an_extra_installs_is_a_dependency_of_its_feature():
    extras = _pyproject()["project"]["optional-dependencies"]
    by_extra = {f.extra: f for f in cat.FEATURES if f.extra}
    for extra, reqs in extras.items():
        f = by_extra[extra]
        names = {cat.dependency(d).dist or d for d in f.all_deps()}
        for r in reqs:
            name = re.split(r"[<>=!~;\[ ]", r, maxsplit=1)[0].strip().lower()
            assert name in {n.lower() for n in names}, (extra, name)


def test_ids_are_unique_and_every_reference_resolves():
    assert len({d.id for d in cat.DEPENDENCIES}) == len(cat.DEPENDENCIES)
    assert len({f.id for f in cat.FEATURES}) == len(cat.FEATURES)
    for f in cat.FEATURES:
        assert f.group in cat.GROUPS, f.id
        for d in f.all_deps():
            cat.dependency(d)


def test_every_dependency_is_used_and_named_with_its_license():
    used = {d for f in cat.FEATURES for d in f.all_deps()}
    for d in cat.DEPENDENCIES:
        assert d.id in used, f"{d.id} belongs to no feature"
        assert d.kind in cat.KINDS
        assert d.license.strip(), d.id


def test_programs_have_steps_and_a_finder():
    for d in cat.DEPENDENCIES:
        if d.kind == cat.PROGRAM:
            assert d.manual.strip(), d.id
            assert d.id in detect.PROGRAMS, d.id


def test_local_wheels_point_at_folders_that_exist():
    for d in cat.DEPENDENCIES:
        if d.kind == cat.LOCAL:
            assert (ROOT / d.source / "pyproject.toml").is_file(), d.id


def test_copyleft_programs_are_never_python_imports():
    """GPL code must never load into this Apache-2.0 process: anything
    GPL is a program or lives in its own environment."""
    for d in cat.DEPENDENCIES:
        if "GPL" in d.license and "LGPL" not in d.license:
            assert d.runs_apart, d.id


def test_core_needs_nothing_and_cannot_be_switched_off():
    core = cat.feature("core")
    assert core.core and not core.all_deps() and not core.switchable


def test_unknown_names_say_what_is_known():
    with pytest.raises(KeyError, match="known:"):
        cat.feature("nope")
    with pytest.raises(KeyError):
        cat.dependency("nope")
