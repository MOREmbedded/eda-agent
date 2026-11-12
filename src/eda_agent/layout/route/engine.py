# SPDX-License-Identifier: Apache-2.0
"""Which autorouter engine a routing run uses.

``default`` is the router in this package. ``euan`` is the alternative
router from Euan's fork (TechAUmNu/eda-agent, branch carrier-local),
kept in ``layout/route_euan``. It is off unless switched on:

- ``EDA_AGENT_ROUTER=euan`` (the environment wins), or
- ``eda-agent router-use euan``, which writes ``router.json`` in the
  active profile, so each workspace keeps its own choice.

``eda-agent router-use default`` (or deleting the file) turns it off.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

ENV = "EDA_AGENT_ROUTER"
ENGINES = ("default", "euan")
DEFAULT = "default"
FILE = "router.json"


def _profile_dir() -> Optional[Path]:
    try:
        from ...profile import resolve_profile_dir
        return resolve_profile_dir()
    except Exception:                                   # noqa: BLE001
        return None


def _read(path: Path) -> Any:
    """The JSON in ``path``: UTF-8 with or without a byte order mark, or
    UTF-16 as Windows PowerShell 5.1 writes it with ``>``."""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return json.loads(raw.decode("utf-16"))
    return json.loads(raw.decode("utf-8-sig"))


def _from_file(directory: Optional[Path]) -> tuple[str, str]:
    """The engine the profile's router.json names ("" when it names
    none), and a problem if any."""
    if directory is None:
        return "", ""
    path = Path(directory) / FILE
    try:
        data = _read(path)
    except FileNotFoundError:
        return "", ""
    except (OSError, ValueError) as exc:
        return "", f"{path} is unreadable: {exc}"
    if not isinstance(data, dict):
        return "", f"{path} holds no JSON object"
    raw = data.get("engine")
    if raw is None or str(raw).strip() == "":
        return "", ""                       # a file of notes only sets nothing
    name = str(raw).strip().lower()
    if name not in ENGINES:
        return "", f"{path} names engine {raw!r}, not one of {list(ENGINES)}"
    return name, ""


def describe(directory: Optional[Path] = None) -> dict[str, Any]:
    """The engine in force, which setting decided it, and any problem."""
    directory = directory if directory is not None else _profile_dir()
    path = str(Path(directory) / FILE) if directory is not None else ""
    raw = os.environ.get(ENV, "").strip().lower()
    if raw:
        if raw in ENGINES:
            return {"engine": raw, "source": "environment", "path": path, "problem": ""}
        problem = f"{ENV}={raw!r} is not one of {list(ENGINES)}"
    else:
        problem = ""
    name, file_problem = _from_file(directory)
    problem = "; ".join(p for p in (problem, file_problem) if p)
    if name:
        return {"engine": name, "source": "profile", "path": path, "problem": problem}
    return {"engine": DEFAULT, "source": "default", "path": path, "problem": problem}


def current(override: Optional[str] = None) -> str:
    """The engine to use: ``override`` when it names one, else the setting."""
    if override:
        name = str(override).strip().lower()
        if name not in ENGINES:
            raise ValueError(f"router engine {override!r} is not one of {list(ENGINES)}")
        return name
    # The variable decides without a look for the profile.
    raw = os.environ.get(ENV, "").strip().lower()
    if raw in ENGINES:
        return raw
    return describe()["engine"]


def write(directory: Path, engine: str) -> dict[str, Any]:
    """Record ``engine`` in the profile's router.json (other keys kept)."""
    name = str(engine).strip().lower()
    if name not in ENGINES:
        return {"ok": False, "reason": f"{engine!r} is not one of {list(ENGINES)}"}
    path = Path(directory) / FILE
    data: dict[str, Any] = {}
    try:
        stored = _read(path)
        if isinstance(stored, dict):
            data = stored
    except (OSError, ValueError):
        pass
    data["engine"] = name
    try:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "reason": f"cannot write {path}: {exc}"}
    return {"ok": True, "path": str(path), "engine": name}
