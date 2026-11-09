# SPDX-License-Identifier: Apache-2.0
"""Design modes: named presets for what kind of design run this is.

The autonomy harness (``session.py``, ``state_machine.py``) walks one
13-stage spec-to-board pipeline. Most real jobs are not that job. Redrawing
the schematic of a vendor's reference design is five of those stages, with
rules of its own (every part traced to a reference page, nothing
"improved"); laying out a board whose schematic is finished is the other
eight. A mode says which stages a run walks, what extra rules bind it, what
each stage should do differently, and which inputs the run cannot start
without.

A mode does NOT change the tool surface. Every mode uses the tools that are
already registered; it only narrows the pipeline and adds guidance. That
keeps a mode cheap to add (a JSON file) and means picking one never needs a
server restart.

Where modes come from (a later source replaces an earlier one by name):

  1. ``BUILT_IN`` below.
  2. The active profile's ``modes/<name>.json`` -- a company's own modes,
     or its variant of a built-in (``"extends": "reference_redraw"``).

Which mode a run uses (first match wins):

  1. The ``mode`` argument to ``design_session_start``.
  2. ``EDA_AGENT_MODE`` (also set by the ``--mode`` CLI flag).
  3. ``default_mode`` in the active profile's ``preferences.json``, so each
     company or client starts its runs in its own mode.
  4. ``~/.eda-agent/active_mode``, written by ``eda-agent mode-use``.
  5. ``full_design``: the whole pipeline, exactly as before modes existed.

A session records the resolved mode (its whole definition, not just the
name) in its ``session_start`` event, so editing a mode file later never
changes a run that is already under way, and a resumed run walks the same
stages it started with.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .session import STAGES

DEFAULT_MODE = "full_design"
MODE_ENV = "EDA_AGENT_MODE"
MODES_SUBDIR = "modes"

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_KEYS = frozenset({"name", "title", "description", "extends", "stages",
                   "inputs", "rules", "stage_notes", "profile_hints"})
_INPUT_KEYS = frozenset({"name", "description", "required", "default"})
#: Profile pieces a mode may point at. They are hints the run reads and
#: reports, never applied silently: applying a ruleset or ERC set is still
#: the owner's call through the tools that do it.
PROFILE_HINT_KEYS = frozenset({"erc_set", "ruleset", "fab_profile",
                               "document_template", "project_template"})


@dataclass(frozen=True)
class ModeInput:
    name: str
    description: str
    required: bool = True
    default: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "required": self.required, "default": self.default}


@dataclass(frozen=True)
class Mode:
    name: str
    title: str
    description: str
    stages: tuple[str, ...] = STAGES
    inputs: tuple[ModeInput, ...] = ()
    #: Hard rules added to the discipline for this kind of run.
    rules: tuple[str, ...] = ()
    #: Stage -> what this mode wants done differently in it.
    stage_notes: dict = field(default_factory=dict)
    profile_hints: dict = field(default_factory=dict)
    #: "built-in", or the file it was read from.
    source: str = "built-in"

    def as_dict(self) -> dict:
        return {"name": self.name, "title": self.title,
                "description": self.description,
                "stages": list(self.stages),
                "skipped_stages": [s for s in STAGES if s not in self.stages],
                "inputs": [i.as_dict() for i in self.inputs],
                "rules": list(self.rules),
                "stage_notes": dict(self.stage_notes),
                "profile_hints": dict(self.profile_hints),
                "source": self.source}

    def missing_inputs(self, given: dict) -> list[str]:
        return [i.name for i in self.inputs
                if i.required and not str(given.get(i.name) or i.default).strip()]

    def resolve_inputs(self, given: dict) -> dict:
        """Given values with defaults filled in; unknown names are kept,
        because a client may well pass something useful we did not ask for."""
        out = {i.name: i.default for i in self.inputs if i.default}
        out.update({k: str(v) for k, v in (given or {}).items()
                    if str(v).strip()})
        return out


def _stage_range(first: str, last: str) -> tuple[str, ...]:
    return STAGES[STAGES.index(first):STAGES.index(last) + 1]


_REFERENCE_INPUTS = (
    ModeInput("vendor", "Who publishes the reference design, e.g. NXP, ST, "
                        "TI, Microchip, Espressif"),
    ModeInput("board", "The dev kit or reference design and its revision, "
                       "e.g. 'MIMXRT1170-EVKB rev B'"),
    ModeInput("source", "Where its schematic is: a URL or a local path to the "
                        "vendor's PDF or design files"),
)

_PUBLIC_ONLY = ("Use only what the vendor publishes for this board (schematic "
                "PDF, design files, user guide, the parts' datasheets). The "
                "NDA rule still holds: no other client's design is a source.")

BUILT_IN: tuple[Mode, ...] = (
    Mode(
        name="full_design",
        title="Full design, spec to board",
        description="The whole 13-stage pipeline from a written requirement "
                    "to a fab package. What a run did before modes existed.",
    ),
    Mode(
        name="schematic_only",
        title="Schematic from a requirement",
        description="Requirement to an ERC-clean schematic; stops before the "
                    "board.",
        stages=_stage_range("requirement", "schematic_emit"),
        stage_notes={
            "schematic_emit": "This is the last stage of the run: leave the "
                              "project compiled and ERC clean, with no PCB "
                              "document created.",
        },
    ),
    Mode(
        name="layout_only",
        title="Layout from a finished schematic",
        description="The schematic exists and is the source of truth; take it "
                    "to a routed, checked board and outputs.",
        stages=_stage_range("sch_to_pcb", "outputs"),
        rules=(
            "The schematic is finished and is not yours to change. When the "
            "layout needs a schematic change (a pin swap, a part change), "
            "log it as blocked with the change as the question and wait.",
        ),
        stage_notes={
            "sch_to_pcb": "Compile the existing project first and read its "
                          "ERC; a schematic that does not compile clean is "
                          "a question for the user, not something to fix.",
        },
    ),
    Mode(
        name="reference_redraw",
        title="Redraw a vendor reference design",
        description="Redraw the schematic of a vendor's dev kit or reference "
                    "design in your own libraries and house style, faithful "
                    "to the original. Schematic only.",
        stages=("requirement", "plan", "plan_verification",
                "library_readiness", "schematic_emit"),
        inputs=_REFERENCE_INPUTS + (
            ModeInput("scope", "Which part of the reference to redraw, e.g. "
                               "'MCU, power and debug only'. Blank means all "
                               "of it.", required=False),
        ),
        rules=(
            _PUBLIC_ONLY,
            "Faithful copy: every part, value and connection comes from the "
            "reference and cites its sheet and page. Do not improve the "
            "circuit. Anything that looks wrong goes on a list for the user, "
            "drawn as the reference has it.",
            "Keep the reference's net names, and its designators unless the "
            "profile's naming policy says otherwise; record each rename.",
            "A part the reference marks DNP or not fitted is drawn and kept "
            "not fitted, never dropped.",
            "Substituting a part (your library has a different MPN, or the "
            "reference part is obsolete) needs the user's yes: log it as "
            "blocked with the reference part, the substitute and why.",
        ),
        stage_notes={
            "requirement": "The requirement is the reference itself: record "
                           "vendor, board, revision, the document and its "
                           "URL or path, and the scope. Read the sheet list "
                           "and say which sheets are in scope.",
            "plan": "Transcribe sheet by sheet: one circuit block per "
                    "reference sheet or functional block, in the reference's "
                    "order. Each part carries its reference designator, MPN, "
                    "value and the page it is on.",
            "plan_verification": "Check the plan against the reference: part "
                                 "count and net count per block, every power "
                                 "rail and its source. List each difference; "
                                 "zero is the target.",
            "library_readiness": "Map each reference MPN to a part in your "
                                 "libraries. An exact MPN match is fine; "
                                 "anything else is a substitution and needs "
                                 "the user.",
            "schematic_emit": "Keep the reference's sheet split so a reviewer "
                              "can lay the two side by side. This is the last "
                              "stage: no PCB.",
        },
    ),
    Mode(
        name="devkit_derivative",
        title="New board derived from a dev kit",
        description="Start from a vendor dev kit or reference design, keep "
                    "the proven core (processor, power, clocks, boot, debug), "
                    "drop what you do not need, add your own blocks, and take "
                    "it to a finished board.",
        inputs=_REFERENCE_INPUTS + (
            ModeInput("changes", "What to keep, drop and add compared with "
                                 "the dev kit, e.g. 'keep MCU, PMIC, DDR, "
                                 "Ethernet; drop camera and LCD; add 2x CAN'",
                      required=False),
        ),
        rules=(
            _PUBLIC_ONLY,
            "The kept core is copied from the reference as drawn, each part "
            "citing its reference page; it is the part of the board that is "
            "already proven.",
            "Every deviation from the reference in a kept block (a value, a "
            "part, a removed component) is listed with its reason in the "
            "design report.",
            "Pins freed by a dropped block are terminated the way the "
            "processor's datasheet says for unused pins, not left floating "
            "by accident.",
        ),
        stage_notes={
            "requirement": "Record the reference (vendor, board, revision, "
                           "source) and the keep / drop / add list. Anything "
                           "the list leaves open is an open question.",
            "architecture": "Start from the dev kit's block diagram. Decide "
                            "keep, drop or add per block, and recheck the "
                            "power budget with the new set of loads.",
            "plan": "Kept blocks: transcribe from the reference with page "
                    "citations. New blocks: design them as in a full design.",
            "plan_verification": "Compare each kept block with the reference "
                                 "and list the differences; check that no "
                                 "net of a dropped block is left dangling.",
            "placement": "The dev kit's layout of the kept core (processor, "
                         "memory, PMIC) is a good floorplan to start from; "
                         "follow it unless the outline says otherwise.",
        },
    ),
)


def _home() -> Path:
    userprofile = os.environ.get("USERPROFILE")
    return Path(userprofile) if userprofile else Path.home()


def active_mode_pointer() -> Path:
    """One-line file naming the centrally selected mode, written by mode-use."""
    return _home() / ".eda-agent" / "active_mode"


def _profile_modes_dir(profile_dir: Optional[Path]) -> Optional[Path]:
    if profile_dir is None:
        from ..profile import resolve_profile_dir
        profile_dir = resolve_profile_dir()
    if profile_dir is None:
        return None
    d = Path(profile_dir) / MODES_SUBDIR
    return d if d.is_dir() else None


def check_mode_dict(data: Any, filename: str) -> list[str]:
    """Problems with one mode file's content, as profile-validate reports them."""
    if not isinstance(data, dict):
        return [f"{filename}: expected a JSON object, got {type(data).__name__}"]
    problems = [f"{filename}: unknown setting {k!r} (known: {sorted(_KEYS)})"
                for k in sorted(data) if not k.startswith("_") and k not in _KEYS]
    stages = data.get("stages")
    if stages is not None:
        if not isinstance(stages, list) or not stages:
            problems.append(f"{filename}: stages must be a non-empty list")
        else:
            bad = [s for s in stages if s not in STAGES]
            if bad:
                problems.append(f"{filename}: unknown stage(s) {bad}; the "
                                f"stages are {list(STAGES)}")
    notes = data.get("stage_notes")
    if notes is not None:
        if not isinstance(notes, dict):
            problems.append(f"{filename}: stage_notes must be an object")
        else:
            bad = [s for s in notes if s not in STAGES]
            if bad:
                problems.append(f"{filename}: stage_notes names unknown "
                                f"stage(s) {bad}")
    rules = data.get("rules")
    if rules is not None and (not isinstance(rules, list)
                              or not all(isinstance(r, str) for r in rules)):
        problems.append(f"{filename}: rules must be a list of strings")
    inputs = data.get("inputs")
    if inputs is not None:
        if not isinstance(inputs, list):
            problems.append(f"{filename}: inputs must be a list")
        else:
            for i, entry in enumerate(inputs):
                if not isinstance(entry, dict) or not _NAME.match(str(entry.get("name", ""))):
                    problems.append(f"{filename}: inputs[{i}] needs a lowercase "
                                    f"name (letters, digits, underscores)")
                    continue
                problems += [f"{filename}: inputs[{i}] unknown setting {k!r}"
                             for k in sorted(entry)
                             if not k.startswith("_") and k not in _INPUT_KEYS]
    hints = data.get("profile_hints")
    if hints is not None:
        if not isinstance(hints, dict):
            problems.append(f"{filename}: profile_hints must be an object")
        else:
            problems += [f"{filename}: profile_hints has unknown key {k!r} "
                         f"(known: {sorted(PROFILE_HINT_KEYS)})"
                         for k in sorted(hints) if k not in PROFILE_HINT_KEYS]
    return problems


def _build(name: str, data: dict, base: Optional[Mode], source: str) -> Mode:
    """A Mode from a file's dict, on top of the mode it extends (if any).

    Lists and objects replace the base's rather than merging, with two
    exceptions that are what extending is for: ``rules`` are added to the
    base's, and ``stage_notes`` are merged per stage.
    """
    inputs = base.inputs if base else ()
    if isinstance(data.get("inputs"), list):
        inputs = tuple(ModeInput(str(e["name"]), str(e.get("description", "")),
                                 bool(e.get("required", True)),
                                 str(e.get("default", "") or ""))
                       for e in data["inputs"]
                       if isinstance(e, dict) and _NAME.match(str(e.get("name", ""))))
    stages = base.stages if base else STAGES
    if isinstance(data.get("stages"), list) and data["stages"]:
        wanted = set(data["stages"])
        # Pipeline order, whatever order the file lists them in: the
        # stages depend on one another front to back.
        stages = tuple(s for s in STAGES if s in wanted)
    rules = (base.rules if base else ()) + tuple(
        r for r in data.get("rules") or () if isinstance(r, str))
    notes = dict(base.stage_notes) if base else {}
    if isinstance(data.get("stage_notes"), dict):
        notes.update({k: str(v) for k, v in data["stage_notes"].items() if k in STAGES})
    hints = dict(base.profile_hints) if base else {}
    if isinstance(data.get("profile_hints"), dict):
        hints.update({k: v for k, v in data["profile_hints"].items()
                      if k in PROFILE_HINT_KEYS})
    return Mode(
        name=name,
        title=str(data.get("title") or (base.title if base else name)),
        description=str(data.get("description") or (base.description if base else "")),
        stages=stages, inputs=inputs, rules=rules, stage_notes=notes,
        profile_hints=hints, source=source)


def load_modes(profile_dir: Optional[Path] = None) -> tuple[dict[str, Mode], list[str]]:
    """Every mode by name, and the problems met reading the profile's ones.

    Like every profile read, this never raises: a broken mode file is
    skipped and named in the problems, which ``profile-validate`` and
    ``design_list_modes`` report.
    """
    modes = {m.name: m for m in BUILT_IN}
    problems: list[str] = []
    directory = _profile_modes_dir(profile_dir)
    if directory is None:
        return modes, problems

    raw: dict[str, tuple[dict, str]] = {}
    for path in sorted(directory.glob("*.json")):
        label = f"{MODES_SUBDIR}/{path.name}"
        if not _NAME.match(path.stem):
            problems.append(f"{label}: a mode's file name must be lowercase "
                            f"letters, digits and underscores")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            problems.append(f"{label}: {exc}")
            continue
        found = check_mode_dict(data, label)
        if not isinstance(data, dict):
            problems += found
            continue
        problems += found
        raw[path.stem] = (data, str(path))

    def resolve(name: str, chain: tuple[str, ...]) -> Optional[Mode]:
        data, source = raw[name]
        parent = data.get("extends")
        base = None
        if parent:
            if parent in chain or parent == name:
                problems.append(f"{MODES_SUBDIR}/{name}.json: extends forms a "
                                f"loop ({' -> '.join(chain + (name, parent))})")
                return None
            if parent in raw:
                base = resolve(parent, chain + (name,))
            else:
                base = next((m for m in BUILT_IN if m.name == parent), None)
            if base is None:
                problems.append(f"{MODES_SUBDIR}/{name}.json: extends "
                                f"{parent!r}, which is not a mode")
                return None
        # No extends: the file is the whole mode, even when it takes a
        # built-in's name (that replaces the built-in outright).
        return _build(name, data, base, source)

    for name in raw:
        mode = resolve(name, ())
        if mode is not None:
            modes[name] = mode
    # One loop reported once per member is noise.
    problems = list(dict.fromkeys(problems))
    return modes, problems


def profile_default_mode(profile_dir: Optional[Path] = None) -> str:
    """``default_mode`` from the profile's preferences.json, or ""."""
    from .. import preferences
    stored = (preferences.read_profile_file(Path(profile_dir)) if profile_dir is not None
              else preferences._profile_layer())
    value = stored.get(preferences.DEFAULT_MODE_KEY)
    return value.strip() if isinstance(value, str) else ""


def selected_mode_name(profile_dir: Optional[Path] = None) -> tuple[str, str]:
    """(name, where it came from) for a run that names no mode itself."""
    env = os.environ.get(MODE_ENV, "").strip()
    if env:
        return env, "environment"
    from_profile = profile_default_mode(profile_dir)
    if from_profile:
        return from_profile, "profile"
    pointer = active_mode_pointer()
    try:
        name = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        name = ""
    if name:
        return name, "mode-use"
    return DEFAULT_MODE, "default"


def get_mode(name: str = "", profile_dir: Optional[Path] = None) -> Mode:
    """The named mode, or the selected one when ``name`` is blank.

    Raises ValueError naming the modes there are when it does not exist:
    a run must not quietly fall back to the full pipeline when the user
    asked for a redraw.
    """
    modes, _problems = load_modes(profile_dir)
    wanted = (name or "").strip() or selected_mode_name(profile_dir)[0]
    mode = modes.get(wanted)
    if mode is None:
        raise ValueError(f"no design mode named {wanted!r}; the modes are "
                         f"{sorted(modes)}")
    return mode


def session_payload(mode: Mode, inputs: dict) -> dict:
    """What a session_start event records about its mode."""
    return {"mode": mode.name, "mode_def": mode.as_dict(), "inputs": inputs}


def prompt_text(mode: Mode, values: dict) -> str:
    """The canned request a client sends to start a run in this mode."""
    lines = [f"Run a design in the '{mode.name}' mode ({mode.title}).",
             mode.description, ""]
    given = {i.name: (values.get(i.name) or i.default or "").strip() for i in mode.inputs}
    if mode.inputs:
        lines.append("Inputs:")
        for i in mode.inputs:
            lines.append(f"- {i.name}: {given[i.name] or '(not given: ask me)'}")
        lines.append("")
    stage_list = ", ".join(mode.stages)
    lines.append(f"Stages in this run: {stage_list}.")
    if mode.rules:
        lines.append("")
        lines.append("Rules for this mode:")
        lines += [f"- {r}" for r in mode.rules]
    inputs_json = json.dumps({k: v for k, v in given.items() if v})
    lines += ["",
              f"Start with design_session_start(requirement=<one paragraph "
              f"saying what this run makes>, mode=\"{mode.name}\", "
              f"inputs={inputs_json}), then loop design_next_action, doing "
              f"what it says and logging each result with design_session_log, "
              f"until it reports complete or blocked."]
    return "\n".join(lines)
