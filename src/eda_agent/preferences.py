# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The Config tab on the Altium status panel, as the Python side reads it.

The user's choices live in ``preferences.json`` in the workspace, written
by the panel and kept across restarts. Only the user changes them; the
server reads them. A missing file, a missing key or a value this version
does not know means the default.

``value_format`` is how a component value is written when it is cleaned
up: ``none`` (left as written, the default), ``rkm`` (4K7, 2u2) or ``si``
(4.7k, 2.2u). ``value_unit`` is ``with`` to add the unit symbol under SI
(100nF, 10uH, 3.3V; never on resistors) or ``without`` (100n, the default).
The RKM code of IEC 60062 has no unit, so under RKM it is always
``without``.

``edits`` is ``readonly`` to refuse every command that is not a read
(default ``allowed``). ``deleting`` and ``closing`` are ``refused`` to turn
away a command that deletes or removes, or that closes a document or
project (default ``allowed``). ``ui_automation`` is ``off`` to refuse menus,
dialogs and keystrokes (default ``on``). ``keep_in_view`` is ``on`` to fit
the board or sheet in view after each edit (default ``off``).
``dialogs`` is ``manual`` to leave the dialogs tools open (the change order,
the Teardrops options) for the user (default ``auto``: the tool answers
them). ``usage_stats`` is ``off`` to stop counting the tokens each tool
call costs (default ``on``; see eda_agent.diag.usage). ``theme`` is how
the web dashboard looks: ``system`` (the default) follows the operating
system's light or dark mode, ``light`` and ``dark`` fix it. The Altium panel
has no switch for the theme; it is set in the profile (the dashboard's
Settings menu or Workspace view write it there). ``length_units`` is the
unit a new AI session gives and reads lengths in, ``mil`` (the default) or
``mm``; each session may change its own (lengths.py). ``checks_off`` names
the checks switched off, comma separated (see :data:`CHECKS`; default none:
every check runs). Each of the others also has an environment variable in
the AI client's config; for the first two
the stricter of the two wins, for the third either one turns it on.

The owner profile in effect (see eda_agent.profile) may carry its own
``preferences.json`` with any of the same keys, so each company or client
keeps its own value format. Where both say something:

- ``value_format``/``value_unit``, ``keep_in_view``, ``dialogs``,
  ``usage_stats`` and ``theme``: the
  profile wins over the panel's file. ``value_format`` and ``value_unit``
  travel as a pair: a profile that sets the format also decides the unit
  (``without`` unless it says ``with``), so a format from one layer is
  never combined with a unit from the other.
- ``edits``, ``deleting``, ``closing`` and ``ui_automation`` are safety
  switches: the stricter of the two wins. A profile can switch one on,
  never off; the panel's choice always holds as well.

The same profile file may name ``default_mode``, the design mode a run uses
when it names none (see eda_agent.design.modes). It is not a panel setting
and is reported, not resolved, here.

The Altium panel shows and writes only its own file: it does not know the
profile. ``resolved`` says, per key, which layer won, which is what the
dashboard's Workspace view shows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

PREFERENCES_FILE = "preferences.json"

VALUE_FORMATS = ("none", "rkm", "si")
VALUE_UNITS = ("without", "with")

#: Each key, its allowed values; the first is the default.
CHOICES: dict[str, tuple[str, ...]] = {
    "value_format": VALUE_FORMATS,
    "value_unit": VALUE_UNITS,
    "edits": ("allowed", "readonly"),
    "deleting": ("allowed", "refused"),
    "closing": ("allowed", "refused"),
    "ui_automation": ("on", "off"),
    "keep_in_view": ("off", "on"),
    "dialogs": ("auto", "manual"),
    "usage_stats": ("on", "off"),
    "theme": ("system", "light", "dark"),
    "length_units": ("mil", "mm"),
}

DEFAULTS: dict[str, str] = {key: allowed[0] for key, allowed in CHOICES.items()}

#: Safety switches and the value that makes each one strict. For these the
#: stricter of the panel's file and the profile wins, so a profile can never
#: loosen what the user switched on in Altium.
STRICT: dict[str, str] = {
    "edits": "readonly",
    "deleting": "refused",
    "closing": "refused",
    "ui_automation": "off",
}

#: The profile file's name, and the one key it has beyond CHOICES.
PROFILE_FILE = PREFERENCES_FILE
DEFAULT_MODE_KEY = "default_mode"


def _read(workspace: Optional[Path]) -> dict[str, Any]:
    if workspace is None:
        from .config import get_config
        workspace = Path(get_config().workspace_dir)
    try:
        stored = json.loads((Path(workspace) / PREFERENCES_FILE)
                            .read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def _valid(stored: dict[str, Any]) -> dict[str, str]:
    """The keys of ``stored`` that are known, with a value they may take."""
    out: dict[str, str] = {}
    for key, allowed in CHOICES.items():
        value = str(stored.get(key, "")).lower()
        if value in allowed:
            out[key] = value
    return out


def profile_directory() -> Optional[Path]:
    """The profile in effect, or None. Never raises: this runs before every
    bridge command (the safety checks), where a profile problem must not
    stop the server."""
    try:
        from .profile import resolve_profile_dir
        return resolve_profile_dir()
    except Exception:                                   # noqa: BLE001
        return None


def read_profile_file(directory: Optional[Path]) -> dict[str, Any]:
    """The profile's preferences.json as written ({} when absent or broken;
    profile-validate names a broken one)."""
    if directory is None:
        return {}
    try:
        stored = json.loads((Path(directory) / PROFILE_FILE)
                            .read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def _profile_layer() -> dict[str, Any]:
    return read_profile_file(profile_directory())


def resolved(workspace: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """Each preference with the layer it came from.

    ``{key: {"value", "source", "panel", "profile"}}``: ``source`` is
    ``profile``, ``panel`` or ``default``; ``panel`` and ``profile`` are what
    each layer says (None when it says nothing usable).
    """
    panel = _valid(_read(workspace))
    profile = _valid(_profile_layer())
    out: dict[str, dict[str, Any]] = {}
    for key in CHOICES:
        a, b = panel.get(key), profile.get(key)
        if key in STRICT:
            strict = STRICT[key]
            if b == strict and a != strict:
                value, source = b, "profile"
            elif a is not None:
                value, source = a, "panel"
            elif b is not None:
                value, source = b, "profile"
            else:
                value, source = DEFAULTS[key], "default"
        elif key == "value_unit":
            continue                       # decided with value_format below
        elif b is not None:
            value, source = b, "profile"
        elif a is not None:
            value, source = a, "panel"
        else:
            value, source = DEFAULTS[key], "default"
        out[key] = {"value": value, "source": source, "panel": a, "profile": b}
    # The unit comes from the layer the format came from, never mixed.
    fmt = out["value_format"]
    layer = profile if fmt["source"] == "profile" else panel if fmt["source"] == "panel" else {}
    unit = layer.get("value_unit") or DEFAULTS["value_unit"]
    if fmt["value"] != "si":
        unit = "without"
    out["value_unit"] = {"value": unit, "source": fmt["source"],
                         "panel": panel.get("value_unit"),
                         "profile": profile.get("value_unit")}
    return {key: out[key] for key in CHOICES}


def preferences(workspace: Optional[Path] = None) -> dict[str, str]:
    """Every preference in effect: the panel's file and the profile's,
    combined as the module docstring says, or the default."""
    return {key: r["value"] for key, r in resolved(workspace).items()}


def check_profile_preferences(data: Any, mode_names: Optional[set[str]] = None,
                              filename: str = PROFILE_FILE) -> list[str]:
    """What profile-validate says about a profile's preferences.json."""
    if not isinstance(data, dict):
        return [f"{filename}: expected a JSON object, got {type(data).__name__}"]
    problems: list[str] = []
    known = set(CHOICES) | {DEFAULT_MODE_KEY}
    for key in sorted(k for k in data if not str(k).startswith("_") and k not in known):
        problems.append(f"{filename}: unknown setting {key!r} (known: {sorted(known)})")
    for key, allowed in CHOICES.items():
        if key in data and str(data[key]).lower() not in allowed:
            problems.append(f"{filename}: {key} {data[key]!r} is not one of {list(allowed)}")
    if "value_unit" in data and "value_format" not in data:
        problems.append(f"{filename}: value_unit does nothing without value_format "
                        f"in the same file (the two travel as a pair)")
    elif (str(data.get("value_unit", "")).lower() == "with"
          and str(data.get("value_format", "")).lower() != "si"):
        problems.append(f"{filename}: value_unit 'with' needs value_format 'si' "
                        f"(the RKM code has no unit)")
    mode = data.get(DEFAULT_MODE_KEY)
    if mode is not None:
        if not isinstance(mode, str) or not mode.strip():
            problems.append(f"{filename}: {DEFAULT_MODE_KEY} must be a mode's name")
        elif mode_names is not None and mode not in mode_names:
            problems.append(f"{filename}: {DEFAULT_MODE_KEY} {mode!r} is not a mode "
                            f"(the modes are {sorted(mode_names)})")
    return problems


def value_format(workspace: Optional[Path] = None) -> str:
    """'none', 'rkm' or 'si', as chosen on the Config tab."""
    return preferences(workspace)["value_format"]


def value_unit(workspace: Optional[Path] = None) -> str:
    """'with' or 'without' the unit symbol, as chosen on the Config tab."""
    return preferences(workspace)["value_unit"]


def edits_read_only(workspace: Optional[Path] = None) -> bool:
    """True when edits are set to read-only on the Config tab."""
    return preferences(workspace)["edits"] == "readonly"


def deleting_refused(workspace: Optional[Path] = None) -> bool:
    """True when deleting is switched off on the Config tab."""
    return preferences(workspace)["deleting"] == "refused"


def closing_refused(workspace: Optional[Path] = None) -> bool:
    """True when closing documents is switched off on the Config tab."""
    return preferences(workspace)["closing"] == "refused"


def ui_automation_off(workspace: Optional[Path] = None) -> bool:
    """True when menus, dialogs and keys are switched off on the Config tab."""
    return preferences(workspace)["ui_automation"] == "off"


def keep_in_view(workspace: Optional[Path] = None) -> bool:
    """True when the board is to be kept in view after each edit."""
    return preferences(workspace)["keep_in_view"] == "on"


def dialogs_auto(workspace: Optional[Path] = None) -> bool:
    """True when a tool answers the dialog it opens (the change order, the
    Teardrops options) itself."""
    return preferences(workspace)["dialogs"] == "auto"


def usage_stats_on(workspace: Optional[Path] = None) -> bool:
    """True when the token cost of each tool call is counted."""
    return preferences(workspace)["usage_stats"] == "on"


def theme(workspace: Optional[Path] = None) -> str:
    """'system', 'light' or 'dark': how the web dashboard looks."""
    return preferences(workspace)["theme"]


def save_profile_preferences(directory: Path, changes: dict[str, Any],
                             mode_names: Optional[set[str]] = None) -> dict[str, Any]:
    """Set or clear keys of a profile's preferences.json.

    ``changes`` maps a key to its new value, or to None (or "") to remove it
    from the profile, so the panel's choice or the default applies again.
    Every change is checked before anything is written; the file's other
    keys (a ``_comment``, keys this version does not know) are kept. Clearing
    ``value_format`` clears ``value_unit`` with it, and a ``value_unit``
    with no format beside it (which does nothing) is dropped. A file left
    with nothing in it is removed. Written whole through a temporary file,
    so a reader never sees half of it.
    """
    import os
    import tempfile

    from .atomicfile import discard, replace_with_retry

    directory = Path(directory)
    if not directory.is_dir():
        return {"ok": False, "reason": f"{directory} is not a profile folder"}
    if not isinstance(changes, dict) or not changes:
        return {"ok": False, "reason": "nothing to change"}
    path = directory / PROFILE_FILE
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            return {"ok": False, "reason": f"{PROFILE_FILE} in the profile is unreadable, "
                                           f"so it was left alone: {exc}"}
        if not isinstance(current, dict):
            return {"ok": False, "reason": f"{PROFILE_FILE} in the profile is not a JSON "
                                           f"object, so it was left alone"}
    else:
        current = {}
    known = set(CHOICES) | {DEFAULT_MODE_KEY}
    updated = dict(current)
    for key, value in changes.items():
        if key not in known:
            return {"ok": False, "reason": f"{key!r} is not a setting (known: {sorted(known)})"}
        if value is None or value == "":
            updated.pop(key, None)
            continue
        if key == DEFAULT_MODE_KEY:
            if not isinstance(value, str) or (mode_names is not None and value not in mode_names):
                return {"ok": False, "reason": f"no design mode named {value!r}"
                        + (f"; the modes are {sorted(mode_names)}" if mode_names else "")}
            updated[key] = value
            continue
        value = str(value).lower()
        if value not in CHOICES[key]:
            return {"ok": False, "reason": f"{key} {value!r} is not one of {list(CHOICES[key])}"}
        updated[key] = value
    # The unit travels with the format: none without one, none beside RKM.
    if str(updated.get("value_format", "")).lower() != "si":
        updated.pop("value_unit", None)
    if not updated:
        if path.exists():
            path.unlink()
        return {"ok": True, "file": str(path), "stored": {}, "removed": True}
    fd, tmp = tempfile.mkstemp(prefix=".preferences.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(updated, fh, indent=2)
            fh.write("\n")
        replace_with_retry(tmp, path)
    except OSError as exc:
        discard(tmp)
        return {"ok": False, "reason": f"could not write {path}: {exc}"}
    return {"ok": True, "file": str(path), "stored": updated}


_PANEL_WHERE = "on the Config tab of the bridge panel in Altium"


def where_set(key: str, workspace: Optional[Path] = None) -> tuple[str, str]:
    """(where KEY's value in effect was chosen, how the user changes it), for
    a refusal or a note that names the switch. The panel when it decided, or
    the profile's file when that did."""
    if resolved(workspace)[key]["source"] == "profile":
        directory = profile_directory()
        path = Path(directory) / PROFILE_FILE if directory else Path(PROFILE_FILE)
        return (f"by the active profile's {PROFILE_FILE} ({path})",
                "The user can change it in that file or in the dashboard's "
                "Workspace view (Settings).")
    return (_PANEL_WHERE, "The user can change it there.")
#: The checks the Config tab can switch off, and what each one checks. Every
#: check is on unless the user turns it off there.
CHECKS: dict[str, str] = {
    "footprint_drawing": "the footprint against its datasheet drawing",
    "symbol_pins": "the symbol's pins against the datasheet pin table",
    "pins_pads": "the symbol's pin numbers against the footprint's pads",
    "model_body": "the 3D body against the datasheet outline",
    "spice_model": "the SPICE model file",
    "fab_export": "the board and parts against the fab before export",
}


def checks_off(workspace: Optional[Path] = None) -> set[str]:
    """The checks switched off on the Config tab (names from CHECKS)."""
    raw = str(_read(workspace).get("checks_off", "") or "").lower()
    return {n.strip() for n in raw.split(",") if n.strip() in CHECKS}


def check_on(name: str, workspace: Optional[Path] = None) -> bool:
    """True unless check ``name`` is switched off on the Config tab."""
    if name not in CHECKS:
        raise KeyError(f"no check named {name!r}")
    return name not in checks_off(workspace)


#: Each check's row on the Config tab, as the panel labels it.
CHECK_LABELS: dict[str, str] = {
    "footprint_drawing": "Footprint vs drawing",
    "symbol_pins": "Symbol vs pin table",
    "pins_pads": "Symbol pins vs pads",
    "model_body": "3D body vs drawing",
    "spice_model": "SPICE model file",
    "fab_export": "Fab export",
}

#: When a user may rightly want a part that does not pass the check.
CHECK_REASONS: dict[str, str] = {
    "footprint_drawing": ("lands deliberately changed from the vendor's "
                          "pattern (longer for hand soldering, a house rule)"),
    "symbol_pins": ("a pin table that is a picture, or names deliberately "
                    "written another way"),
    "pins_pads": ("a footprint held in an installed library or vault, or "
                  "named pads deliberately left off the symbol"),
    "model_body": ("a deliberately simplified body, or a part with no "
                   "outline drawing"),
    "spice_model": ("the user's own model (measured, or written in-house) "
                    "rather than a vendor's"),
    "fab_export": ("a limit the fab agreed for this order (a fab with no "
                   "data here takes fab \"custom\" with the user's own limits "
                   "instead)"),
}


def check_skipped(name: str) -> dict[str, str]:
    """What a result carries when check ``name`` was switched off, so a part
    built without it is never taken for a checked one."""
    return {"check": name, "skipped": (
        f"not checked: {CHECKS[name]} is switched off on the Config tab")}


def if_the_user_insists(name: str) -> str:
    """What a refusal by check ``name`` tells the agent to do when the
    user wants to go ahead anyway."""
    return (f"Show the user what differs. If they still want it built as it "
            f"is, ask them to switch \"{CHECK_LABELS[name]}\" off on the Config "
            f"tab of the Altium panel, then build it again and switch it back "
            f"on after. Never work around the check another way: not another "
            f"tool, not an edit after the build. See docs/CONFIG.md.")


def checks_guide() -> str:
    """Every check, what it checks, when a user may want to go ahead, and
    the switch: the text tool_guide and docs/CONFIG.md give."""
    rows = "; ".join(
        f"\"{CHECK_LABELS[n]}\" checks {CHECKS[n]} (a user may want past it "
        f"for {CHECK_REASONS[n]})" for n in CHECKS)
    return ("A refused build is the check working: report what differs and "
            "let the user decide. Only the user switches a check off, on the "
            "Config tab of the Altium panel; it takes effect on the next "
            "command, and every result built with it off carries "
            "\"unchecked\". Never pass a check by another route (a different "
            "tool, pads or pins placed one by one, an edit after the build). "
            f"The checks: {rows}.")
