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
  - design_rules.json       -- {"fab_profile", "class_current_a", ...}: the
                               TARGETS pcb_rules_plan synthesizes the board's
                               Altium design rules from, never the computed
                               rules themselves
  - erc_levels.json         -- {"levels": {violation type: level}}: the ERC
                               matrix (Project Options > Error Reporting)
                               this owner's projects carry. Named variants
                               live in erc_levels/<name>.json and may
                               "extends" one another, which is how a company
                               base policy and a per-workspace narrowing of
                               it stay two files rather than two copies.
  - phase_gates.json        -- {"stages": ["evt", "dvt", "pvt"]}, which NPI
                               stages this owner runs. Absent means none,
                               and the phase tools are not advertised.
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
    #: design_rules.json -- the TARGETS a board's Altium design rules are
    #: synthesized from (fab profile name, per-class current, differential
    #: impedance), not the computed rules. rule_synthesis asserts that every
    #: number it emits traces to a profile field or a verified calculator,
    #: and storing the output here would let a stored number drift away from
    #: the calculator that justified it.
    design_rules: dict = field(default_factory=dict)
    #: erc_levels.json -- the default ERC set, as written. Kept raw rather
    #: than resolved, because resolving follows "extends" across files and
    #: a set naming a base that is not there has to be an error the tool
    #: reports, not an empty dict every caller silently accepts.
    erc_levels: dict = field(default_factory=dict)
    phase_gates: dict = field(default_factory=dict)
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
            "phase_gates": self.phase_gates,
            "has_erc_levels": bool(self.erc_levels),
            "erc_sets": self.erc_sets(),
            "has_naming": self.naming_path is not None,
            "has_company_goals": self.company_goals_path is not None,
            "rulesets": self.rulesets(),
            "fab_profiles": self.fab_profile_names(),
            "templates": self.template_names(),
            "document_templates": [t["name"] for t in self.document_templates()],
            "errors": list(self.errors),
        }

    def rulesets(self) -> list[str]:
        if not self.rules_dir or not self.rules_dir.is_dir():
            return []
        return sorted(p.stem for p in self.rules_dir.glob("*.json"))

    def erc_sets(self) -> list[str]:
        """The named ERC sets in ``erc_levels/``. The profile's own
        ``erc_levels.json`` is not one of them: it is the default, reached
        by naming no set at all."""
        from .design.erc_matrix import set_names
        return set_names(self.directory)

    def fab_profile_names(self) -> list[str]:
        if not self.fab_profiles_dir or not self.fab_profiles_dir.is_dir():
            return []
        return sorted(p.stem for p in self.fab_profiles_dir.glob("*.json"))

    def template_names(self) -> list[str]:
        """Whole-project templates: a directory per starter project.

        ``documents`` is excluded: it is where single-document templates
        live, and listing it here would offer it as a starter project.
        """
        if not self.templates_dir or not self.templates_dir.is_dir():
            return []
        return sorted(p.name for p in self.templates_dir.iterdir()
                      if p.is_dir() and p.name != "documents")

    def document_templates(self) -> list[dict[str, str]]:
        """Single-document templates from ``templates/documents/``.

        Separate from the project templates above because they are used
        differently: a project template seeds a whole folder, a document
        template seeds one new sheet or board. Keeping them in their own
        subdirectory means neither listing has to filter the other out.

        Altium's own sheet templates are ``.SchDot``; a saved ``.SchDoc``
        works just as well as a starting point and is easier to edit, so
        both are listed and the extension is reported rather than
        enforced.
        """
        if not self.templates_dir:
            return []
        documents = self.templates_dir / "documents"
        if not documents.is_dir():
            return []
        # "suffix" is folded for comparison; "extension" keeps the
        # template's own casing, because Altium writes ".SchDoc" and a
        # copy named ".schdoc" looks wrong in every file listing.
        return [{"name": p.stem, "file": p.name, "suffix": p.suffix.lower(),
                 "extension": p.suffix, "path": str(p)}
                for p in sorted(documents.iterdir()) if p.is_file()]


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
    design_rules = read("design_rules.json")
    erc_levels = read("erc_levels.json")
    phase_gates = read("phase_gates.json")

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
        design_rules=design_rules,
        erc_levels=erc_levels,
        phase_gates=phase_gates,
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


def check_erc_sets(directory: Path) -> list[str]:
    """Every ERC set in a profile, each resolved the way the tools resolve it.

    One function rather than a per-file check, because a set is only valid
    in the company of the sets it ``extends``: a file that is perfect on
    its own but names a base that is not there sets nothing, and the point
    of ``profile-validate`` is to say so before somebody applies it to a
    project and wonders why half their policy is missing.
    """
    from .design.erc_matrix import DEFAULT_FILENAME, read_set, set_names

    root = Path(directory)
    names = [""] if (root / DEFAULT_FILENAME).is_file() else []
    names += set_names(root)
    problems: list[str] = []
    for name in names:
        got = read_set(root, name)
        reason = str(got.get("reason"))
        # A base set is read once on its own and again through every set
        # that extends it, so the same broken line arrives once per
        # dependant. Reported once, in the order first met.
        if not got.get("ok") and reason not in problems:
            problems.append(reason)
    return problems


_PHASE_GATE_KEYS = frozenset({"stages"})

#: Overrules a profile's phase_gates.json, the way EDA_AGENT_BACKEND
#: overrules the backend. Empty or unset means "the profile decides";
#: "none" is how a shell turns the gate off over a profile that turned
#: it on, which an empty value cannot say.
PHASE_STAGES_ENV = "EDA_AGENT_PHASES"


def check_phase_gates(data: dict) -> list[str]:
    from .design.phase_gates import parse_stages

    problems = _unknown_keys(data, _PHASE_GATE_KEYS, "phase_gates.json")
    if "stages" in data:
        try:
            parse_stages(data["stages"])
        except ValueError as exc:
            problems.append(f"phase_gates.json: {exc}")
    return problems


def get_phase_stages(start: Path | None = None) -> tuple[str, ...]:
    """The NPI stages in force: the environment, else the profile, else none.

    Reads ONLY phase_gates.json rather than going through load_profile():
    this runs while the tool surface is being built, and loading the whole
    profile to reach one file would read four others for nothing.

    A value that is not a list of stages degrades to the default rather
    than raising, as every profile setting does -- a typo must not leave
    the server without a tool surface -- and profile-validate is where the
    same value is reported loudly.
    """
    from .design.phase_gates import DEFAULT_STAGES, parse_stages

    raw: object = os.environ.get(PHASE_STAGES_ENV, "")
    if not str(raw).strip():
        directory = resolve_profile_dir(start)
        if directory is None:
            return DEFAULT_STAGES
        data, _error = _read_json_lenient(directory / "phase_gates.json")
        if "stages" not in data:
            return DEFAULT_STAGES
        raw = data["stages"]
    try:
        return parse_stages(raw)
    except ValueError:
        return DEFAULT_STAGES


def describe_phase_stages(start: Path | None = None) -> dict:
    """What get_phase_stages resolved, where it came from, and what it ignored.

    get_phase_stages swallows a bad value so a typo cannot leave the server
    without tools. Something has to say it out loud, or the typo is
    invisible until someone wonders where their stages went; this is what
    `eda-agent phases-show` prints.

    Returns ``{"stages", "source", "raw", "problem", "path"}``. ``source``
    is "environment", "profile" or "default"; ``problem`` is empty when the
    value parsed.
    """
    from .design.phase_gates import DEFAULT_STAGES, parse_stages

    raw: object = os.environ.get(PHASE_STAGES_ENV, "")
    source = "environment"
    path: Path | None = None
    if not str(raw).strip():
        directory = resolve_profile_dir(start)
        path = directory / "phase_gates.json" if directory else None
        data = {} if directory is None else _read_json_lenient(path)[0]
        if "stages" not in data:
            return {"stages": list(DEFAULT_STAGES), "source": "default",
                    "raw": None, "problem": "", "path": str(path) if path else ""}
        raw, source = data["stages"], "profile"
    try:
        stages = parse_stages(raw)
    except ValueError as exc:
        return {"stages": list(DEFAULT_STAGES), "source": source, "raw": raw,
                "problem": str(exc), "path": str(path) if path else ""}
    return {"stages": list(stages), "source": source, "raw": raw,
            "problem": "", "path": str(path) if path else ""}


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


# ------------------------------------------------------------ where, and which
#
# The CLI and the dashboard both answer "which profile applies here, and
# why" and both switch the central one. Each answer lives here once, so a
# button on the dashboard can never do something `profile-use` would not.


def central_profile_names() -> list[str]:
    """Every profile under ~/.eda-agent/profiles/, sorted."""
    root = central_profiles_root()
    return sorted(d.name for d in root.iterdir() if d.is_dir()) if root.is_dir() else []


def central_active_name() -> str:
    """The name ``active_profile`` holds, or "" when it holds none."""
    pointer = active_profile_pointer()
    try:
        return pointer.read_text(encoding="utf-8").strip() if pointer.exists() else ""
    except (OSError, UnicodeDecodeError):
        return ""


def resolution(start: Path | None = None) -> dict:
    """Which step of the resolution order decided, and what the others hold.

    ``source`` is "environment", "project", "central" or "none". The other
    steps are reported too, because the question a person asks after
    switching profiles is "why is it still the old one", and the answer is
    always one of the steps above the one they changed.
    """
    override = os.environ.get(_ENV_OVERRIDE, "")
    project = find_project_profile(start or Path.cwd())
    central_name = central_active_name()
    central_dir = central_profiles_root() / central_name if central_name else None
    directory = resolve_profile_dir(start)
    if override:
        source = "environment" if directory is not None else "none"
    elif project is not None:
        source = "project"
    elif directory is not None:
        source = "central"
    else:
        source = "none"
    return {
        "source": source,
        "directory": str(directory) if directory else "",
        "environment": {"variable": _ENV_OVERRIDE, "value": override,
                        "usable": bool(override) and Path(override).is_dir()},
        "project": str(project) if project else "",
        "central": {"name": central_name,
                    "directory": str(central_dir) if central_dir else "",
                    "exists": bool(central_dir) and central_dir.is_dir(),
                    "root": str(central_profiles_root())},
        "start": str((start or Path.cwd()).resolve()),
    }


def use_central_profile(name: str) -> Path:
    """Make ``name`` the centrally active profile; its directory.

    Raises ValueError for a name that is not a profile under the central
    root. A name with a path separator in it is refused rather than
    resolved: the pointer file holds a name, not a path.
    """
    if not name or name in (".", "..") or any(c in name for c in "/\\:"):
        raise ValueError(f"{name!r} is not a profile name")
    target = central_profiles_root() / name
    if not target.is_dir():
        raise ValueError(f"no profile named {name!r} at {target}. "
                         f"Run `eda-agent profile-init {name}` first.")
    pointer = active_profile_pointer()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(name, encoding="utf-8")
    return target


def follow_env(previous: Path | None, current: ActiveProfile | None) -> bool:
    """After a switch inside a running server, move the naming/IPC-goals
    pointer apply_env set at startup onto the new profile. True if moved.

    Only a value apply_env itself wrote (the previous profile's directory)
    is touched: one the person set in their own environment always wins,
    exactly as it does at startup.
    """
    var = _IPC_DIR_ENV_VARS[0]
    value = os.environ.get(var, "")
    if any(os.environ.get(v) for v in _IPC_DIR_ENV_VARS[1:]):
        return False
    if value and (previous is None or Path(value) != Path(previous)):
        return False
    wanted = ""
    if current is not None and (current.naming_path or current.company_goals_path):
        wanted = str(current.directory)
    if wanted == value:
        return False
    if wanted:
        os.environ[var] = wanted
    else:
        os.environ.pop(var, None)
    return True


def validate_profile(directory: Path) -> list[str]:
    """Every problem in a profile, as `profile-validate` reports them.

    Loading degrades rather than raising (so a typo cannot break startup or
    pad authoring), which makes this the place those problems surface. The
    same checks the consumers apply, so "valid" here means the tools will
    accept it rather than refuse at the point of use.
    """
    from .design.fab_profile import load_fab_profile
    from .engineering.naming import NamingPolicy
    from .engineering.rules import validate_ruleset

    p = load_profile(Path(directory))
    errors: list[str] = list(p.errors)
    errors += check_footprint_defaults(p.footprint_defaults)
    errors += check_document_settings(p.document_settings)
    errors += check_phase_gates(p.phase_gates)
    errors += check_erc_sets(p.directory)

    if p.naming_path:
        try:
            NamingPolicy(path=p.naming_path)
        except Exception as exc:  # noqa: BLE001 -- report, never raise
            errors.append(f"naming.json: {exc}")

    if p.rules_dir:
        for rs_path in sorted(p.rules_dir.glob("*.json")):
            try:
                validate_ruleset(json.loads(rs_path.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001
                # "ruleset invalid" alone names no line to fix; the
                # per-rule reasons travel in the error's details.
                details = getattr(exc, "details", None) or {}
                reasons = details.get("errors") if isinstance(details, dict) else None
                suffix = f" ({'; '.join(map(str, reasons))})" if reasons else ""
                errors.append(f"rules/{rs_path.name}: {exc}{suffix}")

    if p.fab_profiles_dir:
        for fp_path in sorted(p.fab_profiles_dir.glob("*.json")):
            result = load_fab_profile(str(fp_path))
            if not result.get("ok"):
                errors.append(f"fab_profiles/{fp_path.name}: {result.get('reason')}")
    return errors
