# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
r"""CLI subcommands for eda-agent.

Exposed via the `eda-agent` console script. The default subcommand
(invoked when no arguments are given) is `serve`, which starts the
MCP server on stdio -- this is what MCP-compatible clients call.

Other subcommands:
  - scripts-path      : print the path to the bundled DelphiScript sources
  - install-scripts   : copy bundled scripts to a user-chosen directory
                        (default: the folder Altium's Global Projects
                        list loads them from, else
                        %USERPROFILE%\EDA Agent\scripts\)
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

#: Where an Altium install keeps its Global Projects list (the Scripting
#: System preference page), under HKCU\Software\Altium\<install>; one
#: value per project. Read on AD 26.11.
_GLOBAL_PROJECTS = r"DesignExplorer\Preferences\ScriptingSystem\Global Projects"


def registered_script_projects() -> list[dict]:
    """Every Altium_API.PrjScr an Altium install loads at startup.

    [{"install": registry key name, "path": project file, "exists"}], in
    registry order; [] off Windows or when no install registers one.
    The folder that matters is the one Altium loads, and it need not be
    the default this package installs to.
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    found: list[dict] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Altium")
    except OSError:
        return []
    with root:
        index = 0
        while True:
            try:
                install = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                key = winreg.OpenKey(root, install + "\\" + _GLOBAL_PROJECTS)
            except OSError:
                continue
            with key:
                item = 0
                while True:
                    try:
                        _name, value, _kind = winreg.EnumValue(key, item)
                    except OSError:
                        break
                    item += 1
                    if (isinstance(value, str)
                            and Path(value.strip()).name.lower()
                            == "altium_api.prjscr"):
                        path = Path(value.strip())
                        found.append({"install": install, "path": str(path),
                                      "exists": path.is_file()})
    return found


def get_registered_scripts_dirs() -> list[Path]:
    """The distinct folders Altium loads Altium_API.PrjScr from.

    Every one of them, in registry order, so `install-scripts` can deploy
    to each Altium version on the machine and they all compile the same
    build.

    OUR project only. A machine commonly registers other script projects
    (an AltiumLL.PrjScr, a vendor's library tools), and taking every
    registered folder would deploy this package's scripts on top of
    somebody else's.
    """
    folders: list[Path] = []
    seen: set[str] = set()
    for entry in registered_script_projects():
        path = Path(entry["path"])
        if path.name.lower() != "altium_api.prjscr":
            continue
        key = os.path.normcase(str(path.parent))
        if key not in seen:
            seen.add(key)
            folders.append(path.parent)
    return folders


def get_fallback_scripts_dest() -> Path:
    """%USERPROFILE%\\EDA Agent\\scripts\\, when Altium names no folder."""
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        return Path(userprofile) / "EDA Agent" / "scripts"
    return Path.home() / "EDA Agent" / "scripts"


def get_default_scripts_dest() -> Path:
    """Where the scripts belong: the folder Altium loads them from when one
    is registered, else %USERPROFILE%\\EDA Agent\\scripts\\.

    With several installs registering different folders the first is
    returned; `install-scripts` itself deploys to all of them.
    """
    registered = get_registered_scripts_dirs()
    return registered[0] if registered else get_fallback_scripts_dest()


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


#: How long --restart waits for the running loop to finish its command and
#: stop, while a handler is visibly running (a progress file), and while
#: nothing is (a loop that is not running never takes the stop file).
RESTART_STOP_WAIT_S = 300.0
RESTART_IDLE_STOP_WAIT_S = 15.0
#: How long --restart waits for the new loop to answer.
RESTART_PING_WAIT_S = 90.0


def _wait_for_loop_to_stop(workspace: Path) -> str:
    """Drop the stop file and wait for the loop to take it.

    The loop deletes it between commands, so a command in flight always
    finishes first. Returns "stopped", or "not running" when nothing took
    it and no handler was running (the file is then removed, so a later
    start does not stop at once).
    """
    import time

    stop = workspace / "stop"
    stop.write_text("", encoding="ascii")
    start = time.monotonic()
    while stop.exists():
        waited = time.monotonic() - start
        busy = any(workspace.glob("progress_*.json"))
        if waited > RESTART_STOP_WAIT_S or (
                not busy and waited > RESTART_IDLE_STOP_WAIT_S):
            try:
                stop.unlink()
            except OSError:
                pass
            return "not running"
        time.sleep(0.2)
    return "stopped"


def _stop_halted_script(command: list) -> None:
    """Ask Altium to stop a script parked in the debugger.

    A handler that hits an undeclared identifier halts the loop but leaves
    the script running in the engine, and the new loop then fails with
    "Another script executing now". Run > Stop (EditScript:Stop, as the
    install's EditScript.rcs names it) frees the engine; with nothing
    running it does nothing. Measured 2026-10-25: after it, StartMCPServer
    came up at once.
    """
    import subprocess

    try:
        subprocess.Popen([command[0], "-REditScript:Stop"],
                         close_fds=True).wait(timeout=30)
    except Exception:                            # noqa: BLE001
        pass


def _blocked_by_dialog(pid: int, consequence: str) -> bool:
    """Say so and return True when Altium sits behind a blocking dialog.

    Only a window that BLOCKS the loop counts: a hover preview
    (FormPreviewPopup) is listed as a dialog and blocks nothing. A plain
    relaunch used to skip this: with "Unsaved Changes" up, the ping failed,
    so relaunch started the script, and X2.EXE -R went to a NEW Altium
    instead of the busy one.
    """
    from .ui import dialog_report, windows

    if not windows.available():
        return False
    report = dialog_report.report(pid)
    if report.get("blocked"):
        print(f"ERROR: Altium has a dialog open, so {consequence}: "
              f"{report.get('summary')}. Close it first.", file=sys.stderr)
        return True
    return False


def _restart(command: list, frames_pid: int) -> int:
    """Stop the running loop and start a new one, with other sessions'
    calls waiting through it instead of failing.

    maintenance.json tells every eda-agent server that this is a restart
    on purpose: a call that is not taken keeps waiting rather than
    reporting a dead loop, and the loop keeps the requests on disk across
    the stop and the start instead of purging them. It is removed when the
    new loop answers, or when this gives up.
    """
    import json
    import os
    import subprocess
    import time

    from .bridge.altium_bridge import MAINTENANCE_FILE
    from .config import get_config
    from .diag.doctor import _ping_altium
    from .ui import windows

    if _blocked_by_dialog(frames_pid, "the loop cannot take the stop file"):
        return 1

    workspace = Path(get_config().workspace_dir)
    # THE LOOP THAT IS RUNNING, not the one the config names: after a demo
    # take the pointer named the normal workspace while the loop still
    # served the demo one, and the stop file went where nobody read it.
    # Capped: with the loop dead the bridge's own wait ran 267 s before
    # the restart even began.
    import concurrent.futures
    pool = concurrent.futures.ThreadPoolExecutor(1)
    try:
        ping = pool.submit(_ping_altium).result(timeout=5.0)
    except concurrent.futures.TimeoutError:
        ping = None
    finally:
        pool.shutdown(wait=False)
    running = Path(str((ping or {}).get("workspace_dir") or "")) if ping else None
    if running and str(running) not in ("", ".") and running.is_dir() \
            and running.resolve() != workspace.resolve():
        print(f"The running loop serves {running}, not {workspace}; "
              f"stopping that one.")
        workspace = running
    marker = workspace / MAINTENANCE_FILE
    marker.write_text(json.dumps({"reason": "relaunch --restart",
                                  "started": time.time(), "pid": os.getpid()}),
                      encoding="utf-8")
    try:
        state = _wait_for_loop_to_stop(workspace)
        print(f"The loop {'stopped' if state == 'stopped' else 'was not running'}; "
              f"other sessions' calls are waiting for the new one.")
        if state != "stopped" and ping is None:
            _stop_halted_script(command)
            print(f"Nothing answered a ping through {workspace}, so any script "
                  f"left halted in Altium was stopped first. If Altium still "
                  f"says \"Another script executing now\", a loop is serving "
                  f"another workspace: put an empty file named stop in that "
                  f"workspace and run this again.")
        frames = windows.visible_main_frames(frames_pid)
        launcher = subprocess.Popen(command, close_fds=True)
        _reshow_hidden_frames(launcher, frames)
        deadline = time.monotonic() + RESTART_PING_WAIT_S
        while time.monotonic() < deadline:
            ping = _ping_altium()
            if ping is not None:
                waiting = len(list(workspace.glob("request_*.json")))
                print(f"The bridge answers again (version "
                      f"{ping.get('script_version', '?')}); {waiting} waiting "
                      f"call{'' if waiting == 1 else 's'} now being served.")
                return 0
            time.sleep(1.0)
        print("ERROR: the new loop did not answer within "
              f"{RESTART_PING_WAIT_S:.0f}s. Check Altium's status window, or "
              "run `eda-agent doctor`.", file=sys.stderr)
        return 1
    finally:
        try:
            marker.unlink()
        except OSError:
            pass


def cmd_update_apply(check_only: bool = False, assume_yes: bool = False,
                     relaunch: bool = True) -> int:
    """Show what a newer eda-agent brings, and install it once agreed.

    The APPLY half of updating: fast-forwards the clone, reinstalls when
    the dependencies changed, copies the scripts and restarts the loop.
    ``cmd_update`` is the REPORT half, which says what is stale across the
    remote, the package, the deployed scripts and the compiled copy. The
    `update` verb runs the report; `--yes` runs this.
    """
    from . import updates

    info = updates.check(force=True)
    if info.get("update_available") is None:
        print(info.get("how") or info.get("error") or "Could not check for an update.")
        return 1
    if not info.get("update_available"):
        print("eda-agent is up to date.")
        return 0
    print(f"{info['behind']} new commit(s) on {updates.REMOTE}/{updates.BRANCH}:")
    for title in info.get("new_commits", []):
        print(f"  - {title}")
    if check_only:
        return 0
    if not assume_yes:
        if not sys.stdin.isatty():
            print("Re-run with --yes to update.", file=sys.stderr)
            return 1
        try:
            reply = input("Update now? [y/N] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            print("Not updated.")
            return 1
    out = updates.apply(relaunch=relaunch)
    if not out.get("updated"):
        print(f"Not updated: {out.get('reason') or out.get('error') or out}",
              file=sys.stderr)
        return 1
    print(f"Updated {out['from']} -> {out['to']}.")
    for step in out.get("steps", []):
        print(f"  {step['step']}: {'ok' if step['ok'] else 'FAILED'}")
        if not step["ok"] and step.get("output"):
            print("    " + step["output"].replace("\n", "\n    "))
    print(out["next_step"])
    return 1 if out.get("failed_steps") else 0


def cmd_relaunch(prjscr: Optional[str] = None, dry_run: bool = False,
                 force: bool = False, restart: bool = False) -> int:
    """Start the bridge script in the running Altium, when you ask for it.

    Nothing runs this on its own: starting a script inside someone's
    Altium is their call. Request files already in the workspace are
    left alone, since another session may be waiting on them, and a
    bridge that already answers a ping is left running unless --force.

    ``restart`` stops a running loop first, after its current command,
    and other sessions' calls wait through the restart (see _restart),
    which is how a deploy reaches a loop several sessions are using.
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
    if restart:
        return _restart(command, selection.process.pid)
    if _blocked_by_dialog(selection.process.pid,
                          "a script started now would go to a second Altium"):
        return 1
    if not force:
        from .diag.doctor import _ping_altium

        if _ping_altium() is not None:
            print("The bridge already answers a ping; nothing was started. "
                  "Pass --force to run StartMCPServer anyway.")
            return 0
        _stop_halted_script(command)

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
    """Copy the bundled script project where Altium loads it from.

    Default destination: every folder an Altium install registers the
    script project in (Global Projects), so each version on the machine
    compiles the same build; with none registered,
    %USERPROFILE%\\EDA Agent\\scripts\\. Override with --dest PATH.

    After the copy, the user opens `Altium_API.PrjScr` in Altium
    (File > Open, or via the Scripts panel) and runs `StartMCPServer`
    from `Dispatcher.pas`.
    """
    if dest:
        targets = [Path(dest).expanduser().resolve()]
    else:
        targets = get_registered_scripts_dirs() or [get_fallback_scripts_dest()]
    status = 0
    for target in targets:
        status = max(status, _install_scripts_into(target, force))
    if len(targets) > 1:
        print(f"Installed into {len(targets)} folders that Altium installs "
              f"register: {', '.join(map(str, targets))}")
    return status


def _install_scripts_into(dst: Path, force: bool) -> int:
    src = get_bundled_scripts_path()

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
    # the MCP server has ever run. It is the SERVER's workspace, wherever
    # the scripts went: the script reads the pointer once at startup, and
    # deriving it from the destination (<dest>\..\workspace) pointed
    # Altium at a folder no request is written to whenever --dest or a
    # registered folder was not the default.
    from .config import get_config, write_workspace_pointer
    workspace_dir = Path(get_config().workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    write_workspace_pointer(workspace_dir)

    prjscr = dst / "Altium_API.PrjScr"
    print(f"Copied {copied} files to: {dst}")
    print(f"Workspace:     {workspace_dir}")
    print()
    registered = {os.path.normcase(str(f)) for f in get_registered_scripts_dirs()}
    if os.path.normcase(str(dst)) in registered:
        print("Altium already loads the script project from this folder.")
    else:
        print("One-time setup in Altium Designer:")
        print("  DXP -> Preferences -> Scripting System -> Global Projects")
        print(f"  Install from file -> select:")
        print(f"  {prjscr}")
        if registered:
            print("  (Altium currently loads it from elsewhere: "
                  f"{', '.join(map(str, get_registered_scripts_dirs()))})")
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

def cmd_sdk_coverage(*, as_json: bool = False, detail: int = 0,
                     sdk_json: str | None = None) -> int:
    """Measure the generic property layer against Altium's own SDK.

    Altium.SDK.Interfaces.dll declares every interface and property the
    scripting engine exposes. The four Pascal If-chains are written by
    hand, so what they reach was a guess until there was something to
    compare them against. This prints the comparison.

    It needs the reflected dump, which only exists on a machine with
    Altium installed; without one it says how to produce it and exits
    non-zero rather than reporting a coverage figure it cannot stand
    behind.
    """
    import json as _json

    from .sdk.coverage import build_report, format_report, report_as_dict
    from .sdk.reflect import SdkUnavailable, load_sdk

    try:
        model = load_sdk(sdk_json)
    except SdkUnavailable as missing:
        print(str(missing), file=sys.stderr)
        return 1

    report = build_report(model)
    if as_json:
        print(_json.dumps(report_as_dict(report), indent=2))
        return 0

    print(format_report(report, detail=detail))
    return 0


def cmd_sdk_symbols(*, refresh: bool = False, as_json: bool = False,
                    name: str = "", sdk_json: str | None = None) -> int:
    """Report what the scripting engine will actually resolve.

    `sdk-coverage` counts what the chains reach and `sdk-probe` asks the
    running Altium. This answers the same question offline, by reading
    the engine's own registered symbol table out of ScriptingSystem.dll,
    which matters because the live answer costs a halted debugger every
    time it is "no".
    """
    import json as _json

    from .sdk.reflect import SdkUnavailable, load_sdk
    from .sdk.symbols import (ABSENT, GETTER, PLAIN, UNKNOWN,
                              SymbolsUnavailable, load_symbols, refresh as
                              refresh_symbols)

    if refresh:
        try:
            target = refresh_symbols(load_sdk(sdk_json).source)
        except (SdkUnavailable, SymbolsUnavailable) as missing:
            print(str(missing), file=sys.stderr)
            return 1
        print(f"extracted to {target}")

    try:
        table = load_symbols()
    except SymbolsUnavailable as missing:
        print(str(missing), file=sys.stderr)
        return 1

    if name:
        reach = table.reach(name)
        explain = {
            PLAIN: f"Obj.{name} resolves",
            GETTER: f"only Obj.GetState_{name} is registered; property "
                    f"syntax faults",
            ABSENT: "the engine registers neither spelling",
            UNKNOWN: f"shorter than {table.min_reliable} characters, so the "
                     f"extraction cannot tell it from its neighbours",
        }[reach]
        if as_json:
            print(_json.dumps({"name": name, "reach": reach,
                               "explain": explain}, indent=2))
        else:
            print(f"{name}: {reach} -- {explain}")
        return 0

    summary = {
        "source": table.source,
        "extracted_at": table.extracted_at,
        "names": len(table.names),
        "getters": len(table.getters),
        "setters": len(table.setters),
        "min_reliable": table.min_reliable,
    }
    if as_json:
        print(_json.dumps(summary, indent=2))
        return 0
    print(f"Scripting symbol table from {table.source}")
    print(f"  extracted   {table.extracted_at}")
    print(f"  names       {len(table.names)}")
    print(f"  GetState_   {len(table.getters)}")
    print(f"  SetState_   {len(table.setters)}")
    print()
    print(f"Names shorter than {table.min_reliable} characters are reported "
          f"as unknown, not absent: the extraction reads maximal printable "
          f"runs, and a short name merges with whatever sits beside it.")
    return 0


def cmd_sdk_probe(*, designator: str = "", properties: str = "",
                  doc_path: str = "", as_json: bool = False,
                  sdk_json: str | None = None) -> int:
    """Ask the running Altium which DECLARED properties it actually exposes.

    `sdk-coverage` counts what the chains reach. This measures something
    the assembly cannot answer: whether a property the SDK declares is
    reachable from the scripting engine at all. DelphiScript binds late,
    so the generated probe compiles either way, and a member the engine
    does not expose faults where Try/Except cannot catch it.

    THAT FAULT KILLS THE POLLING LOOP, and this is written around that.
    Every attempt is logged inside Altium before it is made, so when the
    call does not come back, the last 'attempt=' line in activity.log
    names the property responsible. Re-run without it, after
    `eda-agent relaunch`.
    """
    import asyncio
    import json as _json

    from .bridge import get_bridge
    from .config import get_config
    from .sdk.generate import candidates_for
    from .sdk.reflect import SdkUnavailable, load_sdk
    from .sdk.symbols import SymbolsUnavailable, load_symbols

    names = [n.strip() for n in properties.split(",") if n.strip()]
    if not names:
        try:
            model = load_sdk(sdk_json)
        except SdkUnavailable as missing:
            print(str(missing), file=sys.stderr)
            return 1
        try:
            table = load_symbols()
        except SymbolsUnavailable:
            # Without the table this is the old behaviour: a list of
            # candidates, one of which may halt the debugger.
            table = None
        _, take, _ = candidates_for(model, "component", "read", table)
        names = [name for name, _prop in take]

    # A probe list built from today's table against yesterday's script
    # is how the debugger gets halted: the running block may still read
    # a name with a spelling the engine does not resolve.
    from .diag.doctor import _ping_altium, bundled_script_version
    ping = _ping_altium()
    if ping is None:
        print("Altium did not answer a ping, so the polling loop is not "
              "running. Start it with Dispatcher.pas > StartMCPServer.",
              file=sys.stderr)
        return 1
    running, bundled = ping.get("script_version", ""), bundled_script_version()
    if running and bundled and running != bundled:
        print(f"the running script is {running} and this tree generated "
              f"{bundled}. Probing across that gap can touch a property the "
              f"running block reads with the old spelling, which halts the "
              f"debugger. Reload the script project in Altium and run "
              f"StartMCPServer again.", file=sys.stderr)
        return 1

    params = {"properties": ",".join(names)}
    if designator:
        params["designator"] = designator
    if doc_path:
        params["doc_path"] = doc_path

    async def _run():
        return await get_bridge().send_command_async(
            "generic.probe_sdk_properties", params, timeout=120.0)

    try:
        reply = asyncio.run(_run())
    except Exception as failed:  # noqa: BLE001 - the point is to explain it
        print(f"probe did not return: {failed}", file=sys.stderr)
        print(_last_attempt_hint(get_config().workspace_dir), file=sys.stderr)
        return 1

    if not isinstance(reply, dict) or "results" not in reply:
        print(f"unexpected reply: {reply!r}", file=sys.stderr)
        return 1

    if as_json:
        print(_json.dumps(reply, indent=2))
        return 0

    rows = reply["results"]
    exposed = [r for r in rows if r.get("exposed")]
    print(f"Probed {len(rows)} declared properties on "
          f"{reply.get('interface', '?')} "
          f"(component {reply.get('designator') or '?'})")
    print(f"  {len(exposed)} of {len(rows)} answered "
          f"({100.0 * len(exposed) / len(rows):.0f}%)" if rows else "  nothing probed")
    print()
    for row in rows:
        mark = "ok  " if row.get("exposed") else "MISS"
        value = row.get("value", "")
        shown = value if len(value) <= 48 else value[:45] + "..."
        print(f"  [{mark}] {row['name']:<32} {shown}")
    return 0


def _last_attempt_hint(workspace) -> str:
    """What the activity log says the probe was doing when it stopped."""
    log = workspace / "activity.log"
    if not log.is_file():
        return "no activity.log to say which property it died on"
    attempted = None
    completed = None
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if ",_sdk_probe,attempt=" in line:
            attempted = line.split("attempt=", 1)[1].split(",", 1)[0]
        elif ",_sdk_probe,done=" in line:
            completed = line.split("done=", 1)[1].split(",", 1)[0]
    if attempted and attempted != completed:
        return (f"the loop stopped on {attempted!r}: it is declared but not "
                f"exposed. Run `eda-agent relaunch`, then probe again with "
                f"--properties listing the rest.")
    return "the log shows no unfinished probe, so the loop stopped elsewhere"


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


# --------------------------------------------------------------------------
# Calculators
# --------------------------------------------------------------------------
#
# One table drives three things: the argparse subcommands, the CLI
# dispatch, and the test that checks the two agree. Ten calculators
# wired up by hand would be ten chances for a flag name in the parser
# to drift from the parameter it fills, and argparse reports that as a
# TypeError at the moment the user runs it.
#
# Flat hyphenated verbs (calc-spacing, not "calc spacing") because that
# is this CLI's existing shape; nesting would make these the only
# nested commands in it.

#: verb -> (function name in engineering.pcb_calc, one-line help,
#:          [(flag, type, default, help), ...])
#: A default of None marks a REQUIRED positional-style option.
CALC_COMMANDS: dict = {
    "calc-spacing": (
        "ipc2221_spacing",
        "Minimum conductor spacing for a voltage (IPC-2221 Table 6-1)",
        [("voltage", float, None, "DC or AC peak BETWEEN CONDUCTORS, volts"),
         ("environment", str, "B2",
          "B1 internal, B2 external uncoated, B3 above 3050 m, "
          "B4 polymer coated, A5 conformal coated, A6 bare leads, "
          "A7 coated leads")],
    ),
    "calc-fusing": (
        "fusing_current",
        "Current that MELTS a track in a given time (Onderdonk)",
        [("width_mils", float, None, "Track width, mils"),
         ("copper_oz", float, 1.0, "Copper weight, oz/ft^2"),
         ("time_s", float, 1.0, "Duration, seconds (trust under ~1 s)"),
         ("ambient_c", float, 25.0, "Ambient temperature, degC")],
    ),
    "calc-via": (
        "via_parasitics",
        "Via inductance, capacitance and barrel resistance",
        [("drill_mils", float, None, "Finished hole diameter, mils"),
         ("length_mils", float, None, "Barrel length (board thickness), mils"),
         ("pad_mils", float, 0.0, "Pad diameter, mils (needed for C)"),
         ("antipad_mils", float, 0.0,
          "Antipad diameter, mils; must exceed the pad"),
         ("dielectric_constant", float, 4.3, "Relative permittivity"),
         ("plating_mils", float, 1.0, "Barrel plating thickness, mils")],
    ),
    "calc-spiral": (
        "spiral_inductor",
        "Planar spiral inductance (Mohan's modified Wheeler)",
        [("turns", float, None, "Number of turns"),
         ("outer_diameter_mils", float, None, "Outer diameter, mils"),
         ("inner_diameter_mils", float, None, "Inner diameter, mils"),
         ("shape", str, "square", "square, hexagonal or octagonal")],
    ),
    "calc-reactance": (
        "reactance",
        "Capacitive and inductive reactance at a frequency",
        [("frequency_hz", float, None, "Frequency, Hz"),
         ("capacitance_f", float, 0.0, "Capacitance, farads"),
         ("inductance_h", float, 0.0, "Inductance, henries")],
    ),
    "calc-resonance": (
        "resonant_frequency",
        "LC resonant frequency and characteristic impedance",
        [("inductance_h", float, None, "Inductance, henries"),
         ("capacitance_f", float, None, "Capacitance, farads")],
    ),
    "calc-wavelength": (
        "wavelength_in_dielectric",
        "Wavelength in a dielectric, with quarter- and twentieth-wave",
        [("frequency_hz", float, None, "Frequency, Hz"),
         ("dielectric_constant", float, 4.3, "Relative permittivity")],
    ),
    "calc-pdn": (
        "pdn_target_impedance",
        "Target power-distribution-network impedance",
        [("voltage_v", float, None, "Rail voltage, volts"),
         ("ripple_percent", float, None, "Allowed ripple, percent of rail"),
         ("transient_current_a", float, None, "Transient step, amps")],
    ),
    "calc-padstack": (
        "padstack_geometry",
        "Pad and antipad diameters, and the gap between neighbours",
        [("drill_mils", float, None, "Finished hole diameter, mils"),
         ("annular_ring_mils", float, None, "Annular ring, mils"),
         ("pitch_mils", float, 0.0, "Neighbour pitch, mils"),
         ("clearance_mils", float, 0.0, "Plane clearance, mils")],
    ),
    "calc-wire": (
        "awg_properties",
        "AWG wire diameter, area and resistance",
        [("awg", float, None, "Gauge; -1 is 00, -2 is 000, -3 is 0000"),
         ("length_m", float, 1.0, "Length, metres"),
         ("temperature_c", float, 20.0, "Conductor temperature, degC")],
    ),
}


def cmd_calc(verb: str, values: dict, *, as_json: bool = False) -> int:
    """Run one calculator and print it.

    Exit code 1 on a refusal, so a shell script can tell "this geometry
    is impossible" from "here is your answer" without parsing the text.
    """
    import json as _json

    from . import engineering as _eng  # noqa: F401  (package import)
    from .engineering import pcb_calc

    entry = CALC_COMMANDS.get(verb)
    if entry is None:
        print(f"unknown calculator {verb!r}", file=sys.stderr)
        return 2
    fn_name, _help, params = entry
    kwargs = {name: values.get(name) for name, _t, _d, _h in params}
    result = getattr(pcb_calc, fn_name)(**kwargs)

    if as_json:
        print(_json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1

    if not result.get("ok"):
        print(f"Cannot compute: {result.get('reason', 'no reason given')}",
              file=sys.stderr)
        return 1

    # Ordered so the answer comes first and the caveats last, which is
    # the order they matter in and the opposite of dict insertion for
    # several of these.
    skip = {"ok", "status", "source", "note", "warning", "out_of_range",
            "disputed"}
    print(f"{verb}  [{result['status']}]")
    for key, value in result.items():
        if key in skip:
            continue
        print(f"  {key:28} {value}")
    if result.get("source"):
        print(f"  source: {result['source']}")
    # The caveats go to stderr so a shell can keep the figures alone on
    # stdout, which means they are UNBUFFERED while the numbers above
    # are not: without this flush they arrive first and read as a
    # warning about the previous command.
    sys.stdout.flush()
    for key in ("note", "warning", "out_of_range"):
        if result.get(key):
            print(f"  {key.upper().replace('_', ' ')}: {result[key]}",
                  file=sys.stderr)
    if result.get("disputed"):
        d = result["disputed"]
        print(f"  DISPUTED: sources disagree; took {d['taken']} mm, the "
              f"other reproduction says {d['other_source_says']} mm",
              file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# Fab capabilities, stackups and laminates
# --------------------------------------------------------------------------

def cmd_stackup_vendors(*, as_json: bool = False) -> int:
    """List the bundled fab vendors with their provenance."""
    import json as _json

    from .design.stackup_library import STALE_AFTER_DAYS, list_vendors

    vendors = list_vendors()
    if as_json:
        print(_json.dumps(vendors, indent=2))
        return 0
    if not vendors:
        print("No vendor data is bundled.", file=sys.stderr)
        return 1
    for vendor in vendors:
        age = ("unknown age" if vendor["age_days"] is None
               else f"{vendor['age_days']}d old")
        flag = "  STALE" if vendor["stale"] else ""
        print(f"{vendor['vendor']:10} {vendor['display_name']}{flag}")
        print(f"  {vendor['process']}")
        print(f"  source     {vendor['source']}")
        print(f"  read       {vendor['retrieved']} ({age}, stale after "
              f"{STALE_AFTER_DAYS}d)")
        print(f"  stackups   {vendor['stackups'] or '(none bundled)'}")
        if vendor["missing"]:
            print(f"  not published: {', '.join(vendor['missing'])}")
    # stderr is unbuffered and stdout is not, so without this the
    # caveat lands above the list it is a caveat about.
    sys.stdout.flush()
    print("\nA starting point, not an authority: a fab changes its "
          "capabilities\nwithout changing this data. Confirm before "
          "ordering.", file=sys.stderr)
    return 0


def cmd_stackup_show(vendor: str, *, as_json: bool = False) -> int:
    """Print one vendor's capabilities and stackup templates."""
    import json as _json

    from .design.stackup_library import load_vendor

    loaded = load_vendor(vendor)
    if as_json:
        print(_json.dumps(loaded, indent=2))
        return 0 if loaded.get("ok") else 1
    if not loaded.get("ok"):
        print(loaded["reason"], file=sys.stderr)
        return 1

    print(f"{loaded['display_name']} -- {loaded['process']}")
    print(f"  source {loaded['source']}")
    print(f"  read   {loaded['retrieved']}")
    print(f"  copper layer counts: {loaded['copper_layer_counts']}")
    print(f"  board thickness mm:  {loaded['board_thickness_mm']}")
    print("\n  minimums (mm / mils):")
    for field, value_mm in loaded["capabilities_mm"].items():
        mils = loaded["capabilities_mils"].get(
            field.replace("_mm", "_mils"))
        if value_mm is None:
            print(f"    {field[4:-3]:22} NOT PUBLISHED")
        else:
            print(f"    {field[4:-3]:22} {value_mm:<8} {mils}")
    for stackup in loaded["stackups"]:
        print(f"\n  stackup {stackup['name']} "
              f"({stackup.get('layer_count', '?')} layer, "
              f"{stackup.get('finished_thickness_mm', '?')} mm)")
        for ply in stackup["layers"]:
            extra = ""
            if ply.get("er") is not None:
                extra = f"  er {ply['er']}"
            if ply.get("copper_oz") is not None:
                extra = f"  {ply['copper_oz']} oz"
            print(f"    {ply['kind']:8} {ply['thickness_mm']:<9} mm  "
                  f"{ply['name']}{extra}")
    for field, why in (loaded["not_published"] or {}).items():
        print(f"\n  {field} is NOT PUBLISHED: {why}", file=sys.stderr)
    sys.stdout.flush()
    for warning in loaded["warnings"]:
        print(f"  ! {warning}", file=sys.stderr)
    return 0


def cmd_stackup_profile(vendor: str, *, fill: Optional[list] = None,
                        as_json: bool = True) -> int:
    """Emit a FabProfile JSON for a bundled vendor, or say what is missing."""
    import json as _json

    from .design.stackup_library import vendor_fab_profile

    values = {}
    for item in fill or []:
        if "=" not in item:
            print(f"--fill wants name=value, got {item!r}", file=sys.stderr)
            return 2
        name, _, raw = item.partition("=")
        try:
            values[name.strip()] = float(raw)
        except ValueError:
            print(f"--fill value for {name.strip()} is not a number: {raw!r}",
                  file=sys.stderr)
            return 2

    result = vendor_fab_profile(vendor, fill=values or None)
    if not result.get("ok"):
        print(result["reason"], file=sys.stderr)
        for field, why in (result.get("why") or {}).items():
            print(f"  {field}: {why}", file=sys.stderr)
        if result.get("missing"):
            example = " ".join(f"--fill {f}=<mils>"
                               for f in result["missing"])
            print(f"\nOnce the fab tells you:\n  eda-agent stackup-profile "
                  f"{vendor} {example}", file=sys.stderr)
        return 1
    print(_json.dumps(result["profile"], indent=2))
    sys.stdout.flush()
    for warning in result.get("warnings", []):
        print(f"  ! {warning}", file=sys.stderr)
    return 0


def cmd_laminates(*, as_json: bool = False) -> int:
    """Print the bundled laminate electrical properties."""
    import json as _json

    from .design.stackup_library import list_laminates

    data = list_laminates()
    if as_json:
        print(_json.dumps(data, indent=2))
        return 0 if data.get("ok") else 1
    if not data.get("ok"):
        print(data["reason"], file=sys.stderr)
        return 1
    print(f"{'laminate':24} {'er(proc)':>9} {'er(design)':>11} "
          f"{'tan d':>8} {'Tg':>6}")
    for lam in data["laminates"]:
        design = "-" if lam["er_design"] is None else lam["er_design"]
        loss = "-" if lam["loss_tangent"] is None else lam["loss_tangent"]
        tg = "-" if lam["tg_c"] is None else lam["tg_c"]
        print(f"{lam['name']:24} {lam['er_process']:>9} {design:>11} "
              f"{loss:>8} {tg:>6}")
    for conv in data["conventions"]:
        print(f"{conv['name']:24} {conv['er_low']:>9} to {conv['er_high']}")
    sys.stdout.flush()
    print("\nTwo dielectric constants per laminate on purpose: the PROCESS "
          "value is\nmeasured one way and the DESIGN value another, they "
          "differ by about 5%,\nand the design value is the one to size an "
          "impedance with.", file=sys.stderr)
    print(data["note"], file=sys.stderr)
    return 0


def cmd_calc_cross_check(calculator: str, values: list,
                         *, as_json: bool = False) -> int:
    """Compare one calculator against a locally built pcb-toolkit."""
    import json as _json

    from .engineering.pcb_toolkit import cross_check

    kwargs: dict = {}
    for item in values or []:
        if "=" not in item:
            print(f"--arg wants name=value, got {item!r}", file=sys.stderr)
            return 2
        name, _, raw = item.partition("=")
        name = name.strip()
        try:
            kwargs[name] = float(raw)
        except ValueError:
            kwargs[name] = raw

    result = cross_check(calculator, kwargs)
    if as_json:
        print(_json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    if not result.get("ok"):
        print(result["reason"], file=sys.stderr)
        return 1

    print(f"{calculator}  via {result['binary']}")
    print(f"  {' '.join(result['command'][1:])}")
    if result["agreements"]:
        print()
        print(f"  agree ({len(result['agreements'])}):")
        for row in result["agreements"]:
            print(f"    {row['field']:28} {row['ours']}")
    if result["disagreements"]:
        print()
        print(f"  DISAGREE ({len(result['disagreements'])}):")
        for row in result["disagreements"]:
            print(f"    {row['field']:28} ours {row['ours']}  "
                  f"theirs {row['theirs']}  delta {row['delta']}")
    if result["unpaired_ours"] or result["unpaired_theirs"]:
        print()
        print(f"  not compared: ours {result['unpaired_ours']}, "
              f"theirs {result['unpaired_theirs']}")
    sys.stdout.flush()
    if result.get("caveat"):
        print(f"  CAVEAT: {result['caveat']}", file=sys.stderr)
    print(f"  {result['note']}", file=sys.stderr)
    return 1 if result["disagreements"] else 0


def cmd_update(*, pull: bool = False, fetch: bool = False,
               check_only: bool = False, force_scripts: bool = False,
               source: str = "", as_json: bool = False) -> int:
    """Bring this installation up to date, and say what is left to do.

    Exit 0 when everything is current, 1 when a step needs a human, 2
    when a step this command owns failed. A script can branch on that
    without parsing the text.
    """
    import json as _json

    from .diag.update import format_report, run_update

    # --from takes either a remote name or a folder. A path is what it
    # usually is, so anything that exists on disk is treated as one and
    # the rest is passed to git as a remote name.
    from pathlib import Path as _Path

    source = (source or "").strip()
    is_dir = bool(source) and _Path(source).expanduser().is_dir()
    steps, code = run_update(pull=pull, fetch=fetch, check_only=check_only,
                             force_scripts=force_scripts,
                             compare_remote="" if is_dir else source,
                             source_dir=source if is_dir else "")
    if as_json:
        print(_json.dumps({
            "ok": code != 2,
            "exit_code": code,
            "steps": [{"name": s.name, "status": s.status,
                       "message": s.message, "action": s.action,
                       "details": s.details} for s in steps],
        }, indent=2))
        return code

    print(format_report(steps))
    if check_only:
        sys.stdout.flush()
        print("\n(--check: nothing was changed)", file=sys.stderr)
    return code
