# SPDX-License-Identifier: Apache-2.0
"""The user's choices about features: which switchable ones are on, and
where programs eda-agent did not find on its own are.

Kept per user in ``~/.eda-agent/features.json`` (``EDA_AGENT_FEATURES_FILE``
names another file)::

    {"off": ["openems"], "on": ["compute"],
     "paths": {"EDA_AGENT_OPENEMS_DIR": "F:\\\\openEMS\\\\openEMS"}}

Only features marked ``switchable`` can be switched. A feature that is on
by default is listed under ``off`` when the user turns it off; one that is
off by default (``default_on=False``, the Rust kernels) is listed under
``on`` when the user turns it on.

``paths`` holds the environment variables that point eda-agent at a
program (the ones the catalogue's dependencies name). :func:`apply`
copies them into this process's environment at startup, so a location
saved once on the Features page holds for every later session. A
variable already set in the environment wins: the saved value is a
default, never an override.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def state_file() -> Path:
    named = os.environ.get("EDA_AGENT_FEATURES_FILE", "").strip()
    return Path(named) if named else Path.home() / ".eda-agent" / "features.json"


def _read() -> dict[str, Any]:
    try:
        data = json.loads(state_file().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _ids(data: dict, key: str) -> set[str]:
    raw = data.get(key)
    return {str(x) for x in raw} if isinstance(raw, list) else set()


def _write(data: dict[str, Any]) -> None:
    from ..atomicfile import replace_with_retry
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    out = {"off": sorted(_ids(data, "off")), "on": sorted(_ids(data, "on"))}
    paths = data.get("paths")
    if isinstance(paths, dict) and paths:
        out["paths"] = {str(k): str(v) for k, v in sorted(paths.items())}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1), encoding="utf-8")
    replace_with_retry(tmp, path)


def switched_off() -> set[str]:
    """Features on by default that the user turned off."""
    return _ids(_read(), "off")


def switched_on() -> set[str]:
    """Features off by default that the user turned on."""
    return _ids(_read(), "on")


def _feature(feature_id: str):
    from .registry import all_features
    feats = {f.id: f for f in all_features()}
    if feature_id not in feats:
        raise KeyError(f"unknown feature {feature_id!r}")
    f = feats[feature_id]
    if not f.switchable:
        raise ValueError(f"{feature_id!r} cannot be switched off: it is on whenever "
                         "it is installed")
    return f


def is_on(feature_id: str) -> bool:
    try:
        f = _feature(feature_id)
    except (KeyError, ValueError):
        return True
    if f.switch_env:
        name, _on, off_value = f.switch_env
        current = os.environ.get(name, "").strip()
        if current and _APPLIED.get(name) != os.environ.get(name):
            return current.lower() != off_value.lower()   # the user's own setting
    data = _read()
    if f.default_on:
        return feature_id not in _ids(data, "off")
    return feature_id in _ids(data, "on")


def set_switch(feature_id: str, on: bool) -> set[str]:
    """Turn a switchable feature on or off; returns the features the user
    has switched off (the ones on by default that are now off)."""
    f = _feature(feature_id)
    data = _read()
    off, ons = _ids(data, "off"), _ids(data, "on")
    if f.default_on:
        (off.discard if on else off.add)(feature_id)
        ons.discard(feature_id)
    else:
        (ons.add if on else ons.discard)(feature_id)
        off.discard(feature_id)
    data["off"], data["on"] = sorted(off), sorted(ons)
    _write(data)
    if f.switch_env:
        name, value = f.switch_env[0], f.switch_env[1]
        if on and not os.environ.get(name, "").strip():
            os.environ[name] = value
            _APPLIED[name] = value
        elif not on and _APPLIED.get(name) == os.environ.get(name):
            os.environ.pop(name, None)
            _APPLIED.pop(name, None)
    return off


def off_features() -> set[str]:
    """Every switchable feature that is off now, by default or by choice."""
    from .registry import all_features
    return {f.id for f in all_features() if f.switchable and not is_on(f.id)}


# ------------------------------------------------------------------ paths

def path_variables() -> dict[str, str]:
    """{environment variable: dependency id} for every variable a
    dependency names, the ones a saved location may set."""
    from .registry import load
    loaded = load()
    # A variable a switch sets (EDA_AGENT_COMPUTE) is a mode, not a place.
    modes = {f.switch_env[0] for f in loaded["features"] if f.switch_env}
    out: dict[str, str] = {}
    for d in loaded["dependencies"].values():
        for name in d.env:
            if name not in modes:
                out.setdefault(name, d.id)
    return out


def saved_paths() -> dict[str, str]:
    raw = _read().get("paths")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if str(v).strip()}


def set_path(name: str, value: str) -> dict[str, str]:
    """Save where a program is (``value`` empty forgets it); returns the
    saved locations. The path must exist: a typo saved for good would
    only resurface as a missing program in some later session."""
    name = str(name).strip()
    known = path_variables()
    if name not in known:
        raise KeyError(f"{name!r} is not a location eda-agent reads; known: "
                       + ", ".join(sorted(known)))
    value = str(value).strip().strip('"')
    data = _read()
    paths = dict(saved_paths())
    if value:
        if not Path(value).exists():
            raise ValueError(f"{value} does not exist")
        paths[name] = value
    else:
        paths.pop(name, None)
    data["paths"] = paths
    _write(data)
    # This process: a saved value replaces one it applied itself, never
    # one the user set in the environment.
    if name in _APPLIED or not os.environ.get(name, "").strip():
        if value:
            os.environ[name] = value
            _APPLIED[name] = value
        else:
            os.environ.pop(name, None)
            _APPLIED.pop(name, None)
    return paths


#: Variables this process set from the state file, and their values.
_APPLIED: dict[str, str] = {}


def path_source(name: str) -> str:
    """"environment", "saved" (applied from the state file) or ""."""
    if not os.environ.get(name, "").strip():
        return ""
    return "saved" if _APPLIED.get(name) == os.environ.get(name) else "environment"


def apply() -> dict[str, str]:
    """Copy the saved locations and the switches that set a variable into
    this process's environment, where nothing set them already. Returns
    what it set. Cheap and safe to call more than once."""
    done: dict[str, str] = {}
    try:
        known = path_variables()
        for name, value in saved_paths().items():
            if name in known and not os.environ.get(name, "").strip():
                os.environ[name] = value
                _APPLIED[name] = done[name] = value
        from .registry import all_features
        for f in all_features():
            if f.switchable and f.switch_env and is_on(f.id):
                name, value = f.switch_env[0], f.switch_env[1]
                if not os.environ.get(name, "").strip():
                    os.environ[name] = value
                    _APPLIED[name] = done[name] = value
    except Exception:  # noqa: BLE001 - a bad state file must not stop startup
        pass
    return done
