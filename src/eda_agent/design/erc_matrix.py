# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Reconcile a project's ERC matrix against the one a profile asks for.

Project Options > Error Reporting is a table of ~150 violation types, each
reported at ``no_report``, ``warning``, ``error`` or ``fatal``. It lives in
the project's own file, so every new project starts from Altium's defaults
and a house policy survives only as long as somebody remembers to re-enter
it. ``proj_get_erc_levels`` and ``proj_set_erc_levels`` could already read
and write the table a type at a time; this module is what turns a stored
policy into the list of writes that brings a project to it.

Three things make that less mechanical than it sounds.

**A type is named by Altium's own prose.** ``DM_ViolationTypeDescription``
is the only name there is, and a profile that spells one differently -- or
was written against an Altium version that called it something else --
names nothing. That is reported as ``unknown`` rather than skipped,
because a policy silently missing half its entries is worse than one that
says so.

**Matching has to agree with the writer.** The Pascal side resolves a type
by its kind number, else by an exact description match after trimming the
caller's text and folding both to lower case. The comparison here does the
same, so a diff that says "same" can never be followed by a write that
says "no violation type by that name".

**Unlisted types are left alone by default.** A profile naming 12 types is
a policy about those 12, not an assertion that the other 140 belong at
Altium's defaults. ``unlisted`` opts into pinning the whole matrix, for an
owner who wants exactly that.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

#: The levels, in Altium's own order, spelled as ``ErcLevelName`` writes them.
LEVEL_NAMES: tuple[str, ...] = ("no_report", "warning", "error", "fatal")

#: Every spelling ``ErcLevelFromText`` accepts, folded the way it folds them
#: (trim, lower case, spaces to underscores). Kept in step with the Pascal
#: by a test that reads the Pascal.
_LEVEL_FROM_TEXT: dict[str, int] = {
    "no_report": 0, "noreport": 0, "none": 0, "0": 0,
    "warning": 1, "1": 1,
    "error": 2, "2": 2,
    "fatal": 3, "fatal_error": 3, "3": 3,
}

#: ``unlisted``'s default: a type the set does not name is not touched.
UNLISTED_LEAVE = "leave"

#: Where named sets live, beside the profile's default one.
SETS_DIRNAME = "erc_levels"

#: The default set's file name, at the profile root.
DEFAULT_FILENAME = "erc_levels.json"

_KNOWN_KEYS = frozenset({"levels", "unlisted", "extends"})


def level_number(text: object) -> Optional[int]:
    """``"error"`` -> 2; None when the text names no level.

    Folds exactly as the Pascal ``ErcLevelFromText`` does, so a level this
    accepts is one the write will accept too.
    """
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, int):
        return text if 0 <= text <= 3 else None
    folded = str(text).strip().lower().replace(" ", "_")
    return _LEVEL_FROM_TEXT.get(folded)


def level_name(value: object) -> str:
    """0..3 as its name; anything else as written.

    Altium answers ``-1`` for a type whose level it would not report, and
    the bridge passes that through as the string ``"-1"``. Turning that
    into a level name here would invent an answer nobody gave.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return LEVEL_NAMES[value] if 0 <= value <= 3 else str(value)
    return str(value or "").strip()


def _kind_of(key: object) -> Optional[int]:
    """The kind number a key names, or None when it names a description.

    ``ErcKindFromText`` takes any non-negative integer as a kind number
    before it looks at a single description, so the same text has to mean
    the same thing here.
    """
    text = str(key).strip()
    return int(text) if text.isdigit() else None


def _fold(description: object) -> str:
    """A description as both sides compare it."""
    return str(description or "").strip().lower()


def bad_set_name(name: str) -> str:
    """Why this is not a usable set name, or "" if it is.

    A set is named, not pathed. ``extends`` is written by hand in a file
    that may be committed to a project's repo and shared, so it reaches
    only the profile's own ``erc_levels/`` and never ``../`` its way out.
    """
    text = str(name or "")
    if not text.strip():
        return "a set name cannot be empty"
    if "/" in text or "\\" in text or Path(text).is_absolute():
        return (f"a set is named by a plain file name, not a path: "
                f"{text!r}")
    if text.strip(".") == "":
        return f"a set is named by a plain file name: {text!r}"
    return ""


# ------------------------------------------------------------------- files

def check_erc_levels(data: Mapping[str, Any],
                     filename: str = DEFAULT_FILENAME) -> list[str]:
    """What is wrong with one ERC set file, as ``profile-validate`` says it."""
    problems: list[str] = []
    unknown = sorted(str(k) for k in data
                     if not str(k).startswith("_") and k not in _KNOWN_KEYS)
    for key in unknown:
        problems.append(f"{filename}: unknown setting {key!r} "
                        f"(known: {sorted(_KNOWN_KEYS)})")

    extends = data.get("extends")
    if extends is not None:
        if not isinstance(extends, str) or not extends.strip():
            problems.append(
                f"{filename}: extends must name another set, got {extends!r}")
        else:
            unusable = bad_set_name(extends)
            if unusable:
                problems.append(f"{filename}: {unusable}")

    levels = data.get("levels")
    if levels is None:
        problems.append(f"{filename}: no levels, so the file sets nothing")
    elif not isinstance(levels, dict):
        problems.append(
            f"{filename}: levels must be an object of {{violation type: "
            f"level}}, got {type(levels).__name__}")
    else:
        for key, value in levels.items():
            # null drops an entry an extended set made; see read_set.
            if value is not None and level_number(value) is None:
                problems.append(
                    f"{filename}: {key!r} is {value!r}; a level is one of "
                    f"{list(LEVEL_NAMES)}")

    unlisted = data.get("unlisted")
    if (unlisted is not None
            and str(unlisted).strip().lower() != UNLISTED_LEAVE
            and level_number(unlisted) is None):
        problems.append(
            f"{filename}: unlisted is {unlisted!r}; it is {UNLISTED_LEAVE!r} "
            f"(the default) or one of {list(LEVEL_NAMES)}, applied to every "
            f"type the set does not name")
    return problems


def set_path(directory: Path, name: str = "") -> Path:
    """Where one set lives: the default at the profile root, named ones under
    ``erc_levels/``."""
    if not name:
        return Path(directory) / DEFAULT_FILENAME
    stem = name if name.lower().endswith(".json") else f"{name}.json"
    return Path(directory) / SETS_DIRNAME / stem


def set_names(directory: Path) -> list[str]:
    """The named sets in ``erc_levels/``, for ``profile_show`` and the CLI."""
    folder = Path(directory) / SETS_DIRNAME
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.json"))


def read_set(directory: Path, name: str = "") -> dict[str, Any]:
    """One resolved ERC set, with ``extends`` followed and the nearest file
    winning.

    Returns ``{"ok": True, "levels", "unlisted", "chain", "path"}``, or
    ``{"ok": False, "reason"}`` naming the file at fault. ``chain`` lists
    the files that were merged, base first, so a level nobody expected can
    be traced to the set that asked for it.

    A level of ``null`` drops an inherited entry: "this workspace does not
    manage that type" is a different statement from "set it to no_report",
    and without it a company set could be overridden but never narrowed.
    """
    directory = Path(directory)
    chain: list[Path] = []
    files: list[dict[str, Any]] = []
    current = name

    while True:
        unusable = bad_set_name(current) if current else ""
        if unusable:
            return {"ok": False, "reason": unusable}
        path = set_path(directory, current)
        if any(p == path for p in chain):
            loop = " extends ".join(p.name for p in chain) + f" extends {path.name}"
            return {"ok": False, "reason": f"the sets loop: {loop}"}
        chain.append(path)
        if not path.is_file():
            if len(chain) > 1:
                return {"ok": False, "reason": (
                    f"{chain[-2].name} extends {current!r}, and {path} is "
                    f"not a file")}
            return {"ok": False, "reason": f"{path} is not a file"}
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            return {"ok": False, "reason": f"{path}: {exc}"}
        if not isinstance(data, dict):
            return {"ok": False, "reason": (
                f"{path}: expected a JSON object, got {type(data).__name__}")}
        problems = check_erc_levels(data, path.name)
        if problems:
            return {"ok": False, "reason": "; ".join(problems)}
        files.append(data)

        extends = str(data.get("extends") or "").strip()
        if not extends:
            break
        current = extends

    levels: dict[str, Any] = {}
    unlisted = UNLISTED_LEAVE
    # Base first: the file read last is the deepest base, and each file
    # read before it overrides what it set.
    for data in reversed(files):
        for key, value in (data.get("levels") or {}).items():
            # Both the key as written and any other spelling of the same
            # type have to go, or a base's "Nets with multiple names" would
            # survive a child's "nets with multiple names": null.
            for existing in [k for k in levels if _fold(k) == _fold(key)]:
                del levels[existing]
            if value is not None:
                levels[key] = value
        if data.get("unlisted") is not None:
            unlisted = str(data["unlisted"]).strip()

    return {"ok": True, "levels": levels, "unlisted": unlisted,
            "chain": [p.name for p in reversed(chain)],
            "path": str(chain[0])}


# ------------------------------------------------------------- reconciling

def reconcile(wanted: Mapping[str, Any],
              on_project: Iterable[Mapping[str, Any]],
              unlisted: str = UNLISTED_LEAVE) -> dict[str, Any]:
    """What an apply would have to write, matching the way the writer does.

    ``on_project`` is the ``levels`` list ``proj_get_erc_levels`` returns:
    ``[{kind, description, level}]``.

    Returns ``change`` (what to write, each with the kind the write will
    use), ``same``, ``unknown`` (named by the set, absent from this
    Altium), ``invalid`` (a level that is not a level), ``unmanaged``
    (present, not named by the set) and ``in_step``.
    """
    by_kind: dict[int, dict[str, Any]] = {}
    by_description: dict[str, dict[str, Any]] = {}
    for item in on_project:
        kind = item.get("kind")
        state = {"kind": kind, "description": item.get("description") or "",
                 "level": level_name(item.get("level"))}
        if isinstance(kind, int) and not isinstance(kind, bool):
            by_kind.setdefault(kind, state)
        elif str(kind or "").strip().isdigit():
            state["kind"] = int(str(kind).strip())
            by_kind.setdefault(state["kind"], state)
        if state["description"]:
            by_description.setdefault(_fold(state["description"]), state)

    change: list[dict[str, Any]] = []
    same: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    matched: set[int] = set()

    for key, value in wanted.items():
        want = level_number(value)
        if want is None:
            invalid.append({"type": str(key), "level": value,
                            "reason": f"a level is one of {list(LEVEL_NAMES)}"})
            continue
        kind = _kind_of(key)
        state = by_kind.get(kind) if kind is not None \
            else by_description.get(_fold(key))
        if state is None:
            unknown.append({"type": str(key), "level": LEVEL_NAMES[want],
                            "reason": ("no violation type by that number or "
                                       "exact description; proj_get_erc_levels "
                                       "lists the names this Altium has")})
            continue
        if isinstance(state.get("kind"), int):
            matched.add(state["kind"])
        entry = {"type": str(key), "kind": state["kind"],
                 "description": state["description"],
                 "want": LEVEL_NAMES[want], "got": state["level"]}
        if level_number(state["level"]) == want:
            same.append(entry)
        else:
            change.append(entry)

    unmanaged = [s for k, s in sorted(by_kind.items()) if k not in matched]
    pin = None if str(unlisted).strip().lower() == UNLISTED_LEAVE \
        else level_number(unlisted)
    if str(unlisted).strip().lower() != UNLISTED_LEAVE and pin is None:
        invalid.append({"type": "unlisted", "level": unlisted,
                        "reason": (f"unlisted is {UNLISTED_LEAVE!r} or one of "
                                   f"{list(LEVEL_NAMES)}")})
    elif pin is not None:
        for state in unmanaged:
            entry = {"type": state["description"] or str(state["kind"]),
                     "kind": state["kind"],
                     "description": state["description"],
                     "want": LEVEL_NAMES[pin], "got": state["level"],
                     "unlisted": True}
            if level_number(state["level"]) == pin:
                same.append(entry)
            else:
                change.append(entry)

    return {"change": change, "same": same, "unknown": unknown,
            "invalid": invalid, "unmanaged": unmanaged,
            "unmanaged_count": len(unmanaged),
            "in_step": not change and not unknown and not invalid}


def write_items(change: Iterable[Mapping[str, Any]]) -> list[str]:
    """``kind=level`` for each change, as ``project.set_erc_levels`` takes it.

    The kind NUMBER rather than the description, because the command packs
    its items into one string with ``~~`` and splits each at its first
    ``=``: a description carrying either would be mangled, and the diff has
    already resolved every type to the number Altium answered with.
    """
    items: list[str] = []
    for entry in change:
        kind = entry.get("kind")
        if not isinstance(kind, int) or isinstance(kind, bool):
            continue
        items.append(f"{kind}={entry['want']}")
    return items
