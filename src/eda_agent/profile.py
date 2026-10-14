# SPDX-License-Identifier: Apache-2.0
"""Per-workspace "profile": an owner's house style, read from files.

A profile is a directory of optional files declaring how one owner (a
company, a client, a personal convention set) wants their libraries and
projects built: naming templates, design rules, fab/stackup profiles,
footprint-authoring defaults (pad corner rounding and the like),
footprint-policy pins, document unit/grid settings, and project
templates. None of this is new storage format invention where an
existing one already fits:

  - naming.json            -- engineering.naming.NamingPolicy's own format
  - company_goals.json      -- engineering.ipc7351.load_goals's own format
  - rules/*.json            -- engineering.rules.RuleStore's own format
  - fab_profiles/*.json     -- design.fab_profile.load_fab_profile's own format

Two file shapes are genuinely new here (nothing already reads them):

  - footprint_defaults.json -- {"pad_corner_radius_pct": 1}
  - footprint_policy.json   -- design.footprint_policy's explicit "policy"
                               dict shape, just loaded from a file instead
                               of only ever passed inline
  - document_settings.json  -- {"schematic_units", "pcb_units",
                               "schematic_snap_grid_mil",
                               "schematic_visible_grid_mil"}
  - templates/<name>/       -- plain files, copied as-is by
                               `eda-agent profile-new-project`

Resolution order (first match wins), matching the "env override, then
walk up, then a central per-machine default" idiom already used by
engineering.naming._default_path and design.preferences' log-path
helpers:

  1. EDA_AGENT_PROFILE_DIR env var, if set.
  2. Walking up from the start path (default: cwd) for `.eda-agent/profile/`
     -- a project-local profile, meant to be committed to that project's
     own repo so a whole team shares it. Unlike `.eda-local/` (gitignored,
     per-developer-machine scratch), this is NOT gitignored by default.

     Being cwd-relative, this reaches the CLI (run from inside a project)
     but usually NOT the MCP server, which its client launches from
     whatever directory it likes. Nothing here can do better: Python never
     learns the active Altium project's path (scope resolution happens on
     the Pascal side), so there is no project directory to walk up from.
     An MCP setup wants (3) or (1); see docs/PROFILES.md.
  3. The centrally active profile: ``~/.eda-agent/active_profile`` names
     one of ``~/.eda-agent/profiles/<name>/``.
  4. None found -> no profile active; every caller keeps today's bundled
     defaults. Zero-config stays zero-surprise.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_PROFILE_DIRNAME = ".eda-agent"
PROJECT_PROFILE_SUBDIR = "profile"
_ENV_OVERRIDE = "EDA_AGENT_PROFILE_DIR"
_IPC_DIR_ENV_VARS = ("EDA_AGENT_IPC_DIR", "ALTIUM_AI_IPC_DIR")


def _home() -> Path:
    # Same precedent as design.preferences' log-path helpers: USERPROFILE
    # first (Windows, and more predictable under test/CI), Path.home() as
    # the cross-platform fallback.
    userprofile = os.environ.get("USERPROFILE")
    return Path(userprofile) if userprofile else Path.home()


def central_profiles_root() -> Path:
    """Where named, centrally-switchable profiles live: ~/.eda-agent/profiles/."""
    return _home() / PROJECT_PROFILE_DIRNAME / "profiles"


def active_profile_pointer() -> Path:
    """One-line file naming the centrally active profile, written by `profile-use`."""
    return _home() / PROJECT_PROFILE_DIRNAME / "active_profile"


def find_project_profile(start: Path) -> Path | None:
    """Walk up from `start` looking for `.eda-agent/profile/`. Public (not
    just an internal step of resolve_profile_dir) so a caller like
    `profile-list` can report "a project-local profile overrides this
    here" separately from which one is centrally active."""
    current = start.resolve()
    while True:
        candidate = current / PROJECT_PROFILE_DIRNAME / PROJECT_PROFILE_SUBDIR
        if candidate.is_dir():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def resolve_profile_dir(start: Path | None = None) -> Path | None:
    """The profile directory in effect right now, or None if no profile applies."""
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        p = Path(override)
        return p if p.is_dir() else None

    project = find_project_profile(start or Path.cwd())
    if project:
        return project

    pointer = active_profile_pointer()
    if pointer.exists():
        name = pointer.read_text(encoding="utf-8").strip()
        if name:
            central = central_profiles_root() / name
            if central.is_dir():
                return central
    return None


def _read_json_lenient(path: Path) -> tuple[dict, str | None]:
    """``({}, None)`` when absent, ``({}, "<file>: <why>")`` when present but
    unusable.

    Reading a profile NEVER raises, because a profile is read from places
    that must not care about it: apply_env() runs during startup, and
    get_footprint_defaults() runs on the pad-authoring path. Raising there
    meant one stray comma in a profile took down the whole MCP server, and
    a JSON file that parsed to a list (not an object) reached a caller's
    ``.get()`` as an AttributeError. Strictness belongs in
    ``profile-validate``, which is what the errors collected here feed.
    """
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, f"{path.name}: {exc}"
    if not isinstance(data, dict):
        return {}, f"{path.name}: expected a JSON object, got {type(data).__name__}"
    return data, None


@dataclass(frozen=True)
class ActiveProfile:
    """The resolved profile's content, parsed. Every field is empty/None when
    the corresponding file is absent -- a profile may define just one piece."""

    directory: Path
    meta: dict = field(default_factory=dict)
    footprint_defaults: dict = field(default_factory=dict)
    footprint_policy: dict = field(default_factory=dict)
    document_settings: dict = field(default_factory=dict)
    naming_path: Path | None = None
    company_goals_path: Path | None = None
    rules_dir: Path | None = None
    fab_profiles_dir: Path | None = None
    templates_dir: Path | None = None
    #: Files that are present but unusable. Loading degrades to defaults
    #: rather than raising; these are what profile-validate reports and
    #: profile_show surfaces, so a broken profile is loud where it should
    #: be and harmless everywhere else.
    errors: tuple[str, ...] = ()

    def summary(self) -> dict:
        """The shape profile_show (MCP) and profile-show (CLI) both report.

        One definition rather than two: they were built separately and had
        already drifted apart by a field.
        """
        return {
            "active": True,
            "directory": str(self.directory),
            "meta": self.meta,
            "footprint_defaults": self.footprint_defaults,
            "footprint_policy": self.footprint_policy,
            "document_settings": self.document_settings,
            "has_naming": self.naming_path is not None,
            "has_company_goals": self.company_goals_path is not None,
            "rulesets": self.rulesets(),
            "fab_profiles": self.fab_profile_names(),
            "templates": self.template_names(),
            "errors": list(self.errors),
        }

    def rulesets(self) -> list[str]:
        if not self.rules_dir or not self.rules_dir.is_dir():
            return []
        return sorted(p.stem for p in self.rules_dir.glob("*.json"))

    def fab_profile_names(self) -> list[str]:
        if not self.fab_profiles_dir or not self.fab_profiles_dir.is_dir():
            return []
        return sorted(p.stem for p in self.fab_profiles_dir.glob("*.json"))

    def template_names(self) -> list[str]:
        if not self.templates_dir or not self.templates_dir.is_dir():
            return []
        return sorted(p.name for p in self.templates_dir.iterdir() if p.is_dir())


def load_profile(directory: Path) -> ActiveProfile:
    """Parse a profile directory. Every file is optional, and a present but
    unusable one degrades to its default while being recorded in
    ``errors`` -- see _read_json_lenient for why this never raises."""
    errors: list[str] = []

    def read(name: str) -> dict:
        data, error = _read_json_lenient(directory / name)
        if error:
            errors.append(error)
        return data

    meta = read("profile.json")
    footprint_defaults = read("footprint_defaults.json")
    footprint_policy = read("footprint_policy.json")
    document_settings = read("document_settings.json")

    naming_path = directory / "naming.json"
    company_goals_path = directory / "company_goals.json"
    rules_dir = directory / "rules"
    fab_profiles_dir = directory / "fab_profiles"
    templates_dir = directory / "templates"

    return ActiveProfile(
        directory=directory,
        meta=meta,
        footprint_defaults=footprint_defaults,
        footprint_policy=footprint_policy,
        document_settings=document_settings,
        naming_path=naming_path if naming_path.exists() else None,
        company_goals_path=company_goals_path if company_goals_path.exists() else None,
        rules_dir=rules_dir if rules_dir.is_dir() else None,
        fab_profiles_dir=fab_profiles_dir if fab_profiles_dir.is_dir() else None,
        templates_dir=templates_dir if templates_dir.is_dir() else None,
        errors=tuple(errors),
    )


def get_active_profile(start: Path | None = None) -> ActiveProfile | None:
    directory = resolve_profile_dir(start)
    return load_profile(directory) if directory else None


def apply_env(start: Path | None = None) -> ActiveProfile | None:
    """Resolve the active profile and, if found, point the existing
    EDA_AGENT_IPC_DIR-reading modules (engineering.naming, engineering.ipc7351)
    at it -- UNLESS the caller already set one of those env vars themselves,
    which always wins. No-op (returns None) when no profile is active.

    Call once, early (server.py's main(), before dispatch), same as
    config.py's EDA_AGENT_WORKSPACE is expected to already be set before
    eda_agent.config is first imported.
    """
    profile = get_active_profile(start)
    if profile is None:
        return None
    if not any(os.environ.get(v) for v in _IPC_DIR_ENV_VARS):
        if profile.naming_path or profile.company_goals_path:
            os.environ[_IPC_DIR_ENV_VARS[0]] = str(profile.directory)
    return profile


# ---------------------------------------------------------------- file contents
#
# What a profile's values are allowed to BE lives here rather than at each
# point of use, so that the tool consuming a setting and `profile-validate`
# checking it can never disagree about what is valid. Consumers degrade to
# the built-in default on a bad value (a typo must not break authoring or
# startup); profile-validate is where the same check is reported loudly.

#: Altium stores a rounded-rect pad's corner radius as a percentage, and
#: 25 is its own typical value.
PAD_CORNER_RADIUS_DEFAULT_PCT = 25

#: The TUnit names sch_set_units accepts (see tools/generic.py).
SCHEMATIC_UNITS = frozenset({"mil", "inch", "dxp", "auto_imperial",
                             "mm", "cm", "m", "auto_metric"})


def _as_percentage(value) -> int | None:
    """A real 0-100 number as an int, or None if it is anything else.

    ``bool`` is excluded deliberately: it is a subclass of int, so a
    ``true`` in the JSON would otherwise arrive as a corner radius of 1.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= 100:
        return None
    return int(round(value))


def _unknown_keys(data: dict, known: frozenset[str], filename: str) -> list[str]:
    """A misspelled key is the quietest way for a profile to do nothing at
    all, so it is named rather than ignored. Keys starting with "_" are
    skipped: the scaffold writes "_comment" into these files, and a file
    format with no room for a note invites one in the wrong place.
    """
    unknown = sorted(k for k in data if not k.startswith("_") and k not in known)
    return [f"{filename}: unknown setting {k!r} (known: {sorted(known)})"
            for k in unknown]


_FOOTPRINT_DEFAULT_KEYS = frozenset({"pad_corner_radius_pct"})
_DOCUMENT_SETTING_KEYS = frozenset({
    "schematic_units", "pcb_units",
    "schematic_snap_grid_mil", "schematic_visible_grid_mil"})


def check_footprint_defaults(data: dict) -> list[str]:
    problems = _unknown_keys(data, _FOOTPRINT_DEFAULT_KEYS, "footprint_defaults.json")
    value = data.get("pad_corner_radius_pct")
    if value is not None and _as_percentage(value) is None:
        problems.append(
            f"footprint_defaults.json: pad_corner_radius_pct must be a number "
            f"from 0 to 100, got {value!r}")
    return problems


def check_document_settings(data: dict) -> list[str]:
    problems = _unknown_keys(data, _DOCUMENT_SETTING_KEYS, "document_settings.json")
    unit = data.get("schematic_units")
    if unit is not None and unit not in SCHEMATIC_UNITS:
        problems.append(
            f"document_settings.json: schematic_units {unit!r} is not one of "
            f"{sorted(SCHEMATIC_UNITS)}")
    for key in ("schematic_snap_grid_mil", "schematic_visible_grid_mil"):
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(
                f"document_settings.json: {key} must be a whole number of mils, "
                f"got {value!r}")
    return problems


def get_pad_corner_radius_pct(start: Path | None = None) -> int:
    """The active profile's pad corner radius %, or Altium's own default.

    The one accessor for this value: tools/library.py reaches it from three
    call sites, and when each read the raw dict instead, a non-numeric
    setting was silently forwarded to Altium by one of them and raised
    TypeError in another.
    """
    value = get_footprint_defaults(start).get("pad_corner_radius_pct")
    resolved = _as_percentage(value) if value is not None else None
    return PAD_CORNER_RADIUS_DEFAULT_PCT if resolved is None else resolved


def get_footprint_defaults(start: Path | None = None) -> dict:
    """Convenience read used by tools/library.py; {} when no profile is active.

    Reads ONLY footprint_defaults.json rather than going through
    load_profile(): this sits on the pad-authoring path, and loading the
    whole profile to reach one file read three others for nothing.
    """
    directory = resolve_profile_dir(start)
    if directory is None:
        return {}
    data, _error = _read_json_lenient(directory / "footprint_defaults.json")
    return data
