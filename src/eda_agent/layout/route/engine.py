# SPDX-License-Identifier: Apache-2.0
"""Which autorouter engine a routing run uses.

``default`` is the router in this package. ``euan`` is the alternative
router from Euan's fork (TechAUmNu/eda-agent, branch carrier-local),
kept in ``layout/route_euan``. It is off unless switched on:

- ``EDA_AGENT_ROUTER=euan`` (the environment wins), or
- ``eda-agent router-use euan``, which writes ``router.json`` in the
  active profile, so each workspace keeps its own choice.

``eda-agent router-use default`` (or deleting the file) turns it off. A
setting that cannot be used routes with the default engine (describe).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

ENV = "EDA_AGENT_ROUTER"
ENGINES = ("default", "euan")
DEFAULT = "default"
FILE = "router.json"
#: A router.json holds one name and perhaps a note; anything this big is
#: not one, and is not parsed.
MAX_BYTES = 64 * 1024


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
    if len(raw) > MAX_BYTES:
        raise ValueError(f"it is {len(raw)} bytes, too large for a router setting")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return json.loads(raw.decode("utf-16"))
    return json.loads(raw.decode("utf-8-sig"))


def _load(path: Path) -> tuple[Any, str]:
    """``path``'s JSON and "", None and "" when there is no such file, or
    None and why it cannot be read."""
    try:
        return _read(path), ""
    except FileNotFoundError:
        return None, ""
    # RecursionError: JSON nested deeper than the parser goes.
    except (OSError, ValueError, RecursionError, MemoryError) as exc:
        return None, f"{path} is unreadable: {exc}"


def _from_file(directory: Optional[Path]) -> tuple[str, str]:
    """The engine the profile's router.json names ("" when it names
    none), and a problem if any."""
    if directory is None:
        return "", ""
    path = Path(directory) / FILE
    data, problem = _load(path)
    if problem or data is None:
        return "", problem
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
    """The engine in force, which setting decided it, and any problem.

    The first setting present decides: EDA_AGENT_ROUTER, then the
    profile's router.json. When that one cannot be used (an unknown name,
    a file that is not JSON) the default engine routes, whatever a setting
    further down says: what the person asked for is unknown, and the
    default engine is the one that needs no asking. ``source`` is then
    "fallback" and ``problem`` says what was wrong.
    """
    directory = directory if directory is not None else _profile_dir()
    path = str(Path(directory) / FILE) if directory is not None else ""
    raw = os.environ.get(ENV, "").strip().lower()
    if raw:
        if raw in ENGINES:
            return {"engine": raw, "source": "environment", "path": path, "problem": ""}
        return {"engine": DEFAULT, "source": "fallback", "path": path,
                "problem": f"{ENV}={raw!r} is not one of {list(ENGINES)}"}
    name, problem = _from_file(directory)
    if name:
        return {"engine": name, "source": "profile", "path": path, "problem": ""}
    return {"engine": DEFAULT, "source": "fallback" if problem else "default",
            "path": path, "problem": problem}


def write(directory: Path, engine: str) -> dict[str, Any]:
    """Record ``engine`` in the profile's router.json, other keys kept.

    Written to a temporary file and moved into place, so a routing run
    that reads it meanwhile sees the old file or the new one, never half
    of one. A file that is present but unreadable is left alone: writing
    over it would throw away whatever else is in it.
    """
    from ...atomicfile import discard, replace_with_retry

    name = str(engine).strip().lower()
    if name not in ENGINES:
        return {"ok": False, "reason": f"{engine!r} is not one of {list(ENGINES)}"}
    directory = Path(directory)
    if not directory.is_dir():
        return {"ok": False, "reason": f"{directory} is not a profile folder"}
    path = directory / FILE
    stored, problem = _load(path)
    if problem or (stored is not None and not isinstance(stored, dict)):
        why = problem or f"{path} holds no JSON object"
        return {"ok": False, "reason": f"{why}. Fix or delete that file first; writing "
                                       "over it would throw away whatever else is in it."}
    data = dict(stored or {})
    data["engine"] = name
    fd, tmp = tempfile.mkstemp(prefix=".router.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        replace_with_retry(tmp, path)
    except OSError as exc:
        discard(tmp)
        return {"ok": False, "reason": f"cannot write {path}: {exc}"}
    return {"ok": True, "path": str(path), "engine": name}
