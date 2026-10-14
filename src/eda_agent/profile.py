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


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


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
    """Parse a profile directory. Every file is optional; a missing or
    unreadable optional file is skipped, not an error -- only a malformed
    *present* JSON file raises (surfaced by `profile-validate`)."""
    meta = {}
    meta_path = directory / "profile.json"
    if meta_path.exists():
        meta = _read_json(meta_path)

    footprint_defaults = {}
    fd_path = directory / "footprint_defaults.json"
    if fd_path.exists():
        footprint_defaults = _read_json(fd_path)

    footprint_policy = {}
    fp_path = directory / "footprint_policy.json"
    if fp_path.exists():
        footprint_policy = _read_json(fp_path)

    document_settings = {}
    ds_path = directory / "document_settings.json"
    if ds_path.exists():
        document_settings = _read_json(ds_path)

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


def get_footprint_defaults(start: Path | None = None) -> dict:
    """Convenience read used by tools/library.py; {} when no profile is active."""
    profile = get_active_profile(start)
    return profile.footprint_defaults if profile else {}
