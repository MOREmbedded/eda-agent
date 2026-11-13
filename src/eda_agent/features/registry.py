# SPDX-License-Identifier: Apache-2.0
"""The built-in catalogue plus what other installed packages contribute.

A package adds a feature by declaring an entry point in the group
``eda_agent.features`` that names a callable returning a
``catalog.Plugin`` (or a list of them). A plugin that fails to load is
reported, never fatal: a broken add-on must not hide the rest.
"""
from __future__ import annotations

import importlib.metadata as md
import logging
import re
from typing import Iterable

from . import catalog as cat
from .catalog import KINDS, Dependency, Feature, Plugin

log = logging.getLogger(__name__)

GROUP = "eda_agent.features"
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

_cache: dict | None = None


def _entry_points() -> Iterable:
    try:
        return md.entry_points(group=GROUP)
    except Exception:  # noqa: BLE001 - broken metadata is not our failure
        return ()


def validate(plugin: Plugin, known_deps: set[str], known_features: set[str]) -> list[str]:
    """What is wrong with a plugin; empty when it can be used."""
    problems = []
    f = plugin.feature
    if not isinstance(f, Feature):
        return ["feature is not a catalog.Feature"]
    if not _ID.match(f.id):
        problems.append(f"feature id {f.id!r} must be lower case letters, digits, . _ -")
    if f.id in known_features:
        problems.append(f"feature id {f.id!r} is already taken")
    if f.core:
        problems.append("a plugin cannot be core")
    own = set()
    for d in plugin.dependencies:
        if not isinstance(d, Dependency):
            problems.append("a dependency is not a catalog.Dependency")
            continue
        if d.kind not in KINDS:
            problems.append(f"dependency {d.id!r} has unknown kind {d.kind!r}")
        if d.id in known_deps:
            problems.append(f"dependency id {d.id!r} is already taken")
        if not d.license:
            problems.append(f"dependency {d.id!r} names no license")
        own.add(d.id)
    for d in f.all_deps():
        if d not in own and d not in known_deps:
            problems.append(f"feature needs {d!r}, which nothing declares")
    return problems


def load(refresh: bool = False) -> dict:
    """{"features": [...], "dependencies": {id: Dependency}, "errors": [...]}."""
    global _cache
    if _cache is not None and not refresh:
        return _cache
    feats = list(cat.FEATURES)
    deps = {d.id: d for d in cat.DEPENDENCIES}
    errors = []
    for ep in _entry_points():
        origin = f"{ep.name} ({ep.value})"
        try:
            made = ep.load()()
            plugins = made if isinstance(made, list) else [made]
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            errors.append({"plugin": origin, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for p in plugins:
            if not isinstance(p, Plugin):
                errors.append({"plugin": origin, "error": "did not return a catalog.Plugin"})
                continue
            problems = validate(p, set(deps), {f.id for f in feats})
            if problems:
                errors.append({"plugin": origin, "error": "; ".join(problems)})
                continue
            p.origin = origin
            feats.append(p.feature)
            for d in p.dependencies:
                deps[d.id] = d
    _cache = {"features": feats, "dependencies": deps, "errors": errors}
    return _cache


def all_features(include_dev: bool = True) -> list[Feature]:
    return [f for f in load()["features"] if include_dev or not f.dev]


def get_feature(feature_id: str) -> Feature:
    for f in load()["features"]:
        if f.id == feature_id:
            return f
    known = ", ".join(sorted(f.id for f in load()["features"]))
    raise KeyError(f"unknown feature {feature_id!r}; known: {known}")


def get_dependency(dep_id: str) -> Dependency:
    try:
        return load()["dependencies"][dep_id]
    except KeyError:
        raise KeyError(f"unknown dependency {dep_id!r}") from None
