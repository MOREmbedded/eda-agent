# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
r"""CLI subcommands for eda-agent.

Exposed via the `eda-agent` console script. The default subcommand
(invoked when no arguments are given) is `serve`, which starts the
MCP server on stdio -- this is what MCP-compatible clients call.

Other subcommands:
  - scripts-path      : print the path to the bundled DelphiScript sources
  - install-scripts   : copy bundled scripts to a user-chosen directory
                        (default: %USERPROFILE%\EDA Agent\scripts\)
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------------------
# Package-resource helpers
# --------------------------------------------------------------------------

def get_package_root() -> Path:
    """Return the filesystem path to the installed `eda_agent` package."""
    import eda_agent
    return Path(eda_agent.__file__).resolve().parent


def get_bundled_scripts_path() -> Path:
    """Return the path to the bundled DelphiScript sources.

    In a regular (wheel) install this is `eda_agent/scripts/` inside
    the installed package -- populated by `force-include` at wheel build
    time (see pyproject.toml).

    In an editable install (`pip install -e .`), the `force-include`
    mapping is not honoured; the loader falls back to `<repo>/scripts/altium/`
    by walking up from the package's source location.
    """
    bundled = get_package_root() / "scripts"
    if bundled.exists() and (bundled / "Altium_API.PrjScr").exists():
        return bundled

    # Editable-install fallback.
    pkg_parent = get_package_root().parent
    repo_root = pkg_parent.parent
    dev_scripts = repo_root / "scripts" / "altium"
    if dev_scripts.exists() and (dev_scripts / "Altium_API.PrjScr").exists():
        return dev_scripts

    return bundled


# --------------------------------------------------------------------------
# User-profile paths
# --------------------------------------------------------------------------

def get_default_scripts_dest() -> Path:
    """Default destination for `install-scripts`: %USERPROFILE%\\EDA Agent\\scripts\\."""
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        return Path(userprofile) / "EDA Agent" / "scripts"
    return Path.home() / "EDA Agent" / "scripts"


# --------------------------------------------------------------------------
# scripts-path
# --------------------------------------------------------------------------

def cmd_scripts_path() -> int:
    """Print the path to the bundled DelphiScript sources."""
    path = get_bundled_scripts_path()
    if not path.exists():
        print(f"ERROR: bundled scripts directory not found: {path}", file=sys.stderr)
        print("Try reinstalling: pip install --force-reinstall eda-agent", file=sys.stderr)
        return 1
    print(str(path))
    return 0


# --------------------------------------------------------------------------
# install-scripts
# --------------------------------------------------------------------------

def cmd_eco_execute(*, confirm: bool, validate_only: bool,
                    allow_confirm_dialogs: bool, wait_first: float,
                    budget: float) -> int:
    """Watch for and auto-press Altium's Engineering Change Order dialog.

    EXPERIMENTAL, UNVERIFIED: no live Altium session was available while
    building this. A thin wrapper around ui.dialog_driver.drive(), the
    existing, already-exercised reactive dialog driver
    (app_drive_dialogs/app_update_from_libraries already use it against
    a live ECO dialog) -- no new UI-automation code, no second bridge,
    no second polling loop.

    Ported at the owner's explicit request, to mirror Altium Booster
    3.2's EcoDialogDriver. eda-agent's own guidance (tools/guidance.py) and
    proj_sync_pcb's docstring say completing an ECO needs a human; this
    command is the explicit, opt-in way to do it anyway -- run
    deliberately from a terminal, separate from the casual MCP tool
    surface. Verify against a real Altium session (Booster's E-01/E-02
    procedures in its manual are the closest existing check) before
    trusting it on a real design.

    This does not trigger an ECO itself. Start it, THEN trigger one
    separately (proj_sync_pcb through your MCP client, or Design >
    Update PCB Document in Altium) while it runs.
    """
    if not confirm:
        print(
            "Refusing: pass --confirm to acknowledge this presses "
            "Execute Changes on the Engineering Change Order dialog "
            "without a human reviewing it first. eda-agent's own "
            "guidance says that step needs a human; this command is the "
            "explicit opt-in override. See docs/ENGINEERING_TOOLS.md.",
            file=sys.stderr,
        )
        return 1

    from .bridge.process_manager import AltiumProcessManager
    pid = AltiumProcessManager().get_altium_pid()
    if not pid:
        print("Altium Designer process not found; start Altium first.",
              file=sys.stderr)
        return 1

    from .ui import dialog_driver
    result = dialog_driver.drive(
        pid, intent="proceed", allow_commit=not validate_only,
        allow_confirm=allow_confirm_dialogs, dry_run=False,
        wait_first=wait_first, budget=budget,
    )

    import json
    print(json.dumps(result, indent=2, default=str))
    if result.get("stopped_for_a_human"):
        print("Stopped for a human: a dialog needed attention this "
              "driver will not answer on its own.", file=sys.stderr)
        return 2
    return 0


def cmd_install_scripts(dest: Optional[str] = None, force: bool = False) -> int:
    """Copy the bundled script project to a user-chosen directory.

    Default destination: %USERPROFILE%\\EDA Agent\\scripts\\
    Override with --dest PATH.

    After the copy, the user opens `Altium_API.PrjScr` in Altium
    (File > Open, or via the Scripts panel) and runs `StartMCPServer`
    from `Dispatcher.pas`.
    """
    src = get_bundled_scripts_path()
    dst = Path(dest).expanduser().resolve() if dest else get_default_scripts_dest()

    if not src.exists():
        print(f"ERROR: bundled scripts directory not found: {src}", file=sys.stderr)
        return 1

    # An auto-accept harness (e.g. Gemini CLI) may allocate a pty -- so
    # sys.stdin.isatty() is True -- yet never feed the prompt, hanging input()
    # forever. Honor an env override so such harnesses can opt out of the
    # prompt without remembering --force on every call.
    if os.environ.get("EDA_AGENT_ASSUME_YES", "").strip().lower() in ("1", "true", "yes"):
        force = True

    if dst.exists() and any(dst.iterdir()) and not force:
        if not sys.stdin.isatty():
            print(
                f"ERROR: target directory already exists and is not empty:\n"
                f"  {dst}\n"
                f"Re-run with --force (or set EDA_AGENT_ASSUME_YES=1) to overwrite.",
                file=sys.stderr,
            )
            return 1
        print(f"Target directory already exists and contains files:")
        print(f"  {dst}")
        print(
            "Overwriting replaces every deployed script -- edits made "
            "directly in that directory (rather than in the repo) are lost. "
            "Back them up first if you have any."
        )
        try:
            reply = input("Overwrite existing files? [y/N] ").strip().lower()
        except EOFError:
            print("\nAborted (no input available -- re-run with --force).", file=sys.stderr)
            return 1
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 1

    dst.mkdir(parents=True, exist_ok=True)

    # Only ship the files Altium needs: .pas sources, DFM form files, and
    # the project file. Without .dfm the DFM-backed StatusForm dashboard
    # fails to compile and StartMCPServer crashes with "unknown identifier"
    # errors referencing the form's controls.
    allowed_suffixes = {".pas", ".dfm", ".PrjScr"}

    copied = 0
    for child in src.iterdir():
        if not child.is_file():
            continue
        if ".tmp." in child.name:
            continue
        if child.suffix not in allowed_suffixes:
            continue
        shutil.copy2(child, dst / child.name)
        copied += 1

    # Publish the workspace path for DelphiScript to pick up, even before
    # the MCP server has ever run. Workspace lives alongside scripts: if
    # scripts are at <dest>\, workspace is at <dest's parent>\workspace\.
    from .config import write_workspace_pointer
    workspace_dir = dst.parent / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    write_workspace_pointer(workspace_dir)

    prjscr = dst / "Altium_API.PrjScr"
    print(f"Copied {copied} files to: {dst}")
    print(f"Workspace:     {workspace_dir}")
    print()
    print("One-time setup in Altium Designer:")
    print("  DXP -> Preferences -> Scripting System -> Global Projects")
    print(f"  Install from file -> select:")
    print(f"  {prjscr}")
    print()
    print("Each Altium session, start the polling loop via:")
    print("  File -> Run Script... -> Altium_API -> Dispatcher.pas -> StartMCPServer -> Run")
    return 0


# --------------------------------------------------------------------------
# profile-* -- per-workspace owner profile (see profile.py, docs/PROFILES.md)
# --------------------------------------------------------------------------

_PROFILE_EXAMPLE_FOOTPRINT_DEFAULTS = """{
  "_comment": "Overrides the hardcoded default for lib_add_footprint_pad / lib_create_standard_footprint.",
  "pad_corner_radius_pct": 25
}
"""

_PROFILE_EXAMPLE_DOCUMENT_SETTINGS = """{
  "_comment": "Applied to the current schematic by profile_apply_document_settings. PCB-side is documentation only for now -- no bridge command exists yet to set a board's units/grid.",
  "schematic_units": "mil",
  "pcb_units": "mm",
  "schematic_snap_grid_mil": 100,
  "schematic_visible_grid_mil": 100
}
"""

_PROFILE_EXAMPLE_FOOTPRINT_POLICY = """{
  "_comment": "Matches design.footprint_policy.py's explicit-policy keys. Omit a key to infer it from the library's own majority convention instead.",
  "courtyard": true,
  "three_d_model": true,
  "pin1_marker": true
}
"""


def _profile_scaffold(directory, name: str) -> None:
    from pathlib import Path as _Path
    directory = _Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "profile.json").write_text(
        '{\n  "name": "' + name + '",\n  "description": ""\n}\n',
        encoding="utf-8")
    (directory / "footprint_defaults.json").write_text(_PROFILE_EXAMPLE_FOOTPRINT_DEFAULTS, encoding="utf-8")
    (directory / "footprint_policy.json").write_text(_PROFILE_EXAMPLE_FOOTPRINT_POLICY, encoding="utf-8")
    (directory / "document_settings.json").write_text(_PROFILE_EXAMPLE_DOCUMENT_SETTINGS, encoding="utf-8")
    (directory / "rules").mkdir(exist_ok=True)
    (directory / "fab_profiles").mkdir(exist_ok=True)
    (directory / "templates").mkdir(exist_ok=True)
    # Project templates are a directory each, under templates/; single
    # documents live in templates/documents/ so neither listing has to
    # filter the other out.
    (directory / "templates" / "documents").mkdir(exist_ok=True)


def cmd_profile_init(name: str, *, here: bool) -> int:
    """Scaffold a new profile named `name`: `--here` creates a project-local
    .eda-agent/profile/ in the current directory (meant to be committed
    to that project's repo); without it, creates a centrally-switchable
    ~/.eda-agent/profiles/<name>/. See docs/PROFILES.md."""
    from . import profile as profile_mod

    if here:
        directory = Path.cwd() / profile_mod.PROJECT_PROFILE_DIRNAME / profile_mod.PROJECT_PROFILE_SUBDIR
    else:
        directory = profile_mod.central_profiles_root() / name
    if directory.exists() and any(directory.iterdir()):
        print(f"ERROR: {directory} already exists and is not empty.", file=sys.stderr)
        return 1
    _profile_scaffold(directory, name)
    print(f"Scaffolded profile at: {directory}")
    if not here:
        print(f"Activate it with: eda-agent profile-use {name}")
    return 0


def cmd_profile_use(name: str) -> int:
    """Set the centrally active profile (~/.eda-agent/active_profile)."""
    from . import profile as profile_mod

    target = profile_mod.central_profiles_root() / name
    if not target.is_dir():
        print(f"ERROR: no profile named {name!r} at {target}. "
              f"Run `eda-agent profile-init {name}` first.", file=sys.stderr)
        return 1
    pointer = profile_mod.active_profile_pointer()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(name, encoding="utf-8")
    print(f"Active profile: {name} ({target})")
    return 0


def cmd_profile_list() -> int:
    """List centrally-registered profiles and which one is active."""
    from . import profile as profile_mod

    root = profile_mod.central_profiles_root()
    names = sorted(d.name for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
    pointer = profile_mod.active_profile_pointer()
    active = pointer.read_text(encoding="utf-8").strip() if pointer.exists() else ""
    if not names:
        print(f"No central profiles under {root}.")
    for n in names:
        marker = " (active)" if n == active else ""
        print(f"{n}{marker}")
    project = profile_mod.find_project_profile(Path.cwd())
    if project:
        print(f"\nA project-local profile overrides this here: {project}")
    return 0


def cmd_profile_show(*, as_json: bool) -> int:
    """Print the profile actually in effect for the current directory."""
    import json as _json
    from . import profile as profile_mod

    p = profile_mod.get_active_profile()
    if p is None:
        if as_json:
            print(_json.dumps({"active": False}))
        else:
            print("No profile active here.")
            # An override that points nowhere wins and then resolves to
            # nothing, which otherwise reads as "I have no profile" when
            # the real answer is "the path you gave me is wrong".
            override = os.environ.get("EDA_AGENT_PROFILE_DIR")
            if override and not Path(override).is_dir():
                print(f"  (EDA_AGENT_PROFILE_DIR is set to {override}, "
                      f"which is not a directory)", file=sys.stderr)
        return 0
    if as_json:
        print(_json.dumps(p.summary(), indent=2))
        return 0
    print(f"Profile: {p.meta.get('name', '(unnamed)')} -- {p.directory}")
    if p.meta.get("description"):
        print(f"  {p.meta['description']}")
    print(f"  footprint_defaults: {p.footprint_defaults or '(none)'}")
    print(f"  footprint_policy:   {p.footprint_policy or '(none)'}")
    print(f"  document_settings:  {p.document_settings or '(none)'}")
    print(f"  rulesets:           {p.rulesets() or '(none)'}")
    print(f"  fab_profiles:       {p.fab_profile_names() or '(none)'}")
    print(f"  templates:          {p.template_names() or '(none)'}")
    # Listed separately from the project templates above: these seed one
    # sheet or board (profile_new_document) rather than a whole folder,
    # and folding them into one line would read as a project template
    # that profile-new-project then refuses.
    documents = [t["file"] for t in p.document_templates()]
    print(f"  document templates: {documents or '(none)'}")
    for problem in p.errors:
        print(f"  UNREADABLE: {problem}", file=sys.stderr)
    return 0


def cmd_profile_validate(*, name: Optional[str], here: bool) -> int:
    """Load every file in a profile and report errors. The owner writes
    these files; this command is how they check their own work before
    relying on it (same discipline engineering/rules.py documents for
    rulesets)."""
    from . import profile as profile_mod
    from .engineering.naming import NamingPolicy
    from .engineering.rules import validate_ruleset
    from .design.fab_profile import load_fab_profile

    if here:
        directory = Path.cwd() / profile_mod.PROJECT_PROFILE_DIRNAME / profile_mod.PROJECT_PROFILE_SUBDIR
    elif name:
        directory = profile_mod.central_profiles_root() / name
    else:
        directory = profile_mod.resolve_profile_dir()
        if directory is None:
            print("No profile active here and no name/--here given.", file=sys.stderr)
            return 1

    if not directory.is_dir():
        print(f"ERROR: {directory} does not exist.", file=sys.stderr)
        return 1

    p = profile_mod.load_profile(directory)
    # Loading degrades rather than raising (so a typo cannot break startup or
    # pad authoring), which makes this command the place those problems have
    # to surface.
    errors: list[str] = list(p.errors)
    # The same checks the consumers apply, so "valid" here means the tools
    # will accept it rather than refuse at the point of use.
    errors += profile_mod.check_footprint_defaults(p.footprint_defaults)
    errors += profile_mod.check_document_settings(p.document_settings)

    if p.naming_path:
        try:
            NamingPolicy(path=p.naming_path)
        except Exception as exc:  # noqa: BLE001 -- report, don't crash the CLI
            errors.append(f"naming.json: {exc}")

    if p.rules_dir:
        import json as _json
        for rs_path in sorted(p.rules_dir.glob("*.json")):
            try:
                validate_ruleset(_json.loads(rs_path.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"rules/{rs_path.name}: {exc}")

    if p.fab_profiles_dir:
        for fp_path in sorted(p.fab_profiles_dir.glob("*.json")):
            result = load_fab_profile(str(fp_path))
            if not result.get("ok"):
                errors.append(f"fab_profiles/{fp_path.name}: {result.get('reason')}")

    if errors:
        print(f"{len(errors)} problem(s) in {directory}:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"Profile at {directory} is valid.")
    return 0


def cmd_profile_new_project(template: str, dest: str, *, force: bool) -> int:
    """Copy a profile's templates/<template>/ tree verbatim to dest. Plain
    file copy -- Altium project files are just files on disk, so no
    DelphiScript is involved."""
    from . import profile as profile_mod

    p = profile_mod.get_active_profile()
    if not p or not p.templates_dir:
        print("No active profile defines templates/.", file=sys.stderr)
        return 1
    src = p.templates_dir / template
    if not src.is_dir():
        available = p.template_names()
        print(f"ERROR: template {template!r} not found. Available: {available or '(none)'}",
              file=sys.stderr)
        return 1
    dst = Path(dest)
    if dst.exists() and any(dst.iterdir()) and not force:
        print(f"ERROR: {dst} already exists and is not empty. Pass --force to overwrite.",
              file=sys.stderr)
        return 1
    shutil.copytree(src, dst, dirs_exist_ok=True)
    print(f"Created project from template {template!r} at: {dst}")
    return 0


# --------------------------------------------------------------------------
# perf -- where the time went, from the logs already being written
# --------------------------------------------------------------------------

def cmd_perf(*, as_json: bool, limit: int = 15) -> int:
    """Report call timing by joining the workspace's two logs.

    activity.log times the handler inside Altium; bridge_trace.log times
    the round trip the caller saw. Every existing view reads the first
    one, so the difference between them -- IPC plus time queued behind
    another command -- has never been visible.
    """
    import json as _json

    from .config import get_config
    from .diag.perf import format_report, report_from_workspace

    workspace = get_config().workspace_dir
    report = report_from_workspace(workspace)
    if as_json:
        print(_json.dumps(report, indent=2))
        return 0

    print(format_report(report, limit=limit))
    if not report["calls"]:
        print(f"  No calls logged yet in {workspace}.", file=sys.stderr)
        print("  Run some tools against Altium, then try again.", file=sys.stderr)
    return 0
