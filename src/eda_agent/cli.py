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
# easyeda-extension
# --------------------------------------------------------------------------

def cmd_easyeda_extension(action: str, out: Optional[str] = None) -> int:
    """Print the EasyEDA extension source folder, or build its .eext."""
    from . import easyeda_extension

    if action == "path":
        folder = easyeda_extension.source_dir()
        if folder is None:
            print("ERROR: no EasyEDA extension source in this install",
                  file=sys.stderr)
            return 1
        print(str(folder))
        return 0
    code, message = easyeda_extension.build(out)
    print(message, file=sys.stdout if code == 0 else sys.stderr)
    return code


# --------------------------------------------------------------------------
# relaunch
# --------------------------------------------------------------------------

def relaunch_command(exe: str, prjscr: str) -> list[str]:
    """The command that asks a running Altium to start the bridge script.

    A second X2.exe launched with -R hands the process to the instance
    already running, the way Altium's own menu entries run a script.
    Passed as a list, so the | needs no shell escaping.
    """
    return [exe, f"-RScriptingSystem:RunScript(ProjectName={prjscr}"
                 f"|ProcName=Dispatcher.pas>StartMCPServer)"]


def cmd_relaunch(prjscr: Optional[str] = None, dry_run: bool = False,
                 force: bool = False) -> int:
    """Start the bridge script in the running Altium, when you ask for it.

    Nothing runs this on its own: starting a script inside someone's
    Altium is their call. Request files already in the workspace are
    left alone, since another session may be waiting on them, and a
    bridge that already answers a ping is left running unless --force.
    """
    if sys.platform != "win32":
        print("ERROR: relaunch runs on Windows, where Altium is", file=sys.stderr)
        return 1
    project = (Path(prjscr).expanduser() if prjscr
               else get_default_scripts_dest() / "Altium_API.PrjScr")
    if not project.is_file():
        print(f"ERROR: no script project at {project}. Run "
              f"`eda-agent install-scripts` first, or pass --prjscr.",
              file=sys.stderr)
        return 1

    from .bridge.process_manager import AltiumProcessManager

    selection = AltiumProcessManager().select_altium_process()
    if selection.process is None:
        print("ERROR: " + (selection.reason or "Altium is not running"),
              file=sys.stderr)
        return 1
    exe = selection.process.exe_path
    if not exe or not Path(exe).is_file():
        print(f"ERROR: could not read the path of Altium's executable "
              f"(pid {selection.process.pid})", file=sys.stderr)
        return 1

    command = relaunch_command(exe, str(project))
    if dry_run:
        print(" ".join(f'"{part}"' if " " in part else part for part in command))
        return 0
    if not force:
        from .diag.doctor import _ping_altium

        if _ping_altium() is not None:
            print("The bridge already answers a ping; nothing was started. "
                  "Pass --force to run StartMCPServer anyway.")
            return 0

    import subprocess

    from .ui import windows

    frames = windows.visible_main_frames(selection.process.pid)
    launcher = subprocess.Popen(command, close_fds=True)
    print(f"Asked Altium (pid {selection.process.pid}) to run "
          f"StartMCPServer from {project}. Check the status window, or run "
          f"`eda-agent doctor`.")
    shown = _reshow_hidden_frames(launcher, frames)
    if shown:
        print(f"Altium's main window was hidden after the hand-off and has "
              f"been shown again ({len(shown)} window"
              f"{'' if len(shown) == 1 else 's'}).")
    return 0


#: How long relaunch waits for the second X2.EXE to hand over and exit,
#: then how long it keeps watching the main window afterwards.
RELAUNCH_HANDOFF_TIMEOUT_S = 30.0
RELAUNCH_WATCH_S = 5.0


def _reshow_hidden_frames(launcher, frames: list) -> list:
    """Show again any main frame the -R hand-off left hidden.

    Altium's main window has been found hidden, not minimized, with no
    taskbar button, after relaunches; only frames that were on screen
    before the hand-off are touched. Returns the handles shown again.
    """
    if not frames:
        return []
    from .ui import windows
    import time

    wait = getattr(launcher, "wait", None)
    if wait is not None:
        try:
            wait(timeout=RELAUNCH_HANDOFF_TIMEOUT_S)
        except Exception:                         # noqa: BLE001
            pass
    shown: list = []
    deadline = time.monotonic() + RELAUNCH_WATCH_S
    while True:
        for hwnd in windows.show_if_hidden(frames):
            if hwnd not in shown:
                shown.append(hwnd)
        if time.monotonic() >= deadline:
            return shown
        time.sleep(0.25)


# --------------------------------------------------------------------------
# install-scripts
# --------------------------------------------------------------------------

_UTF8_BOM = bytes((0xEF, 0xBB, 0xBF))


def _stamp_deployed_unit(path: Path) -> None:
    """Write the unit's content hash into its UnitStamp function, and drop
    any byte-order mark.

    Only the deployed copy is stamped; the repository keeps 'dev'. See
    eda_agent/script_stamps.py for why one version constant is not enough.
    Altium Designer 21 reads a mark as source text and stops on line 1 with
    "Invalid constant", so no deployed unit carries one.
    """
    from .script_stamps import has_stamp, stamp_source

    raw = path.read_bytes()
    bom = raw.startswith(_UTF8_BOM)
    text = raw.decode("utf-8-sig")
    if has_stamp(text):
        path.write_bytes(stamp_source(text).encode("utf-8"))
    elif bom:
        path.write_bytes(raw[len(_UTF8_BOM):])


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
        if child.suffix == ".pas":
            _stamp_deployed_unit(dst / child.name)
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
