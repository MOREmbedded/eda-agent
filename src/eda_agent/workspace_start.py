# SPDX-License-Identifier: Apache-2.0
"""`eda-agent start`: bring one design workspace up in one command.

A design workspace (a folder of Altium projects for one client or one
product line) keeps its own start settings in
``<workspace>/.eda-agent/workspace.json``, next to the project-local
profile directory ``.eda-agent/profile/`` that profile.py already looks
for. Everything ``start`` writes for a workspace also stays under that
folder (``.eda-agent/logs/``), so two workspaces never share settings or
logs, and nothing about a client lands in this package.

    {
      "profile": "acme",                     # central profile to activate
      "dashboard_port": 8777,                # dashboard for this workspace
      "altium365_workspaces": [              # what the altium365_ tools may use
        "https://acme.365.altium.com/"
      ]
    }

The file holds no secrets and refuses keys that look like one: the Nexar
app id and secret stay in the user's environment (``setx``), which
altium365.read_environment already reads from the registry.

Steps, each skippable: activate the profile, fill the Altium 365 settings
in from the user's saved environment, restart the dashboard on the
workspace's port, run doctor, make sure the Nexar login is valid (the
browser opens only when it is not), and say whether the Altium bridge is
up and what to do when it is not.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

DIRNAME = ".eda-agent"
FILENAME = "workspace.json"
LOGS = "logs"
KEYS = frozenset({"profile", "dashboard_port", "altium365_workspaces"})
_SECRET_WORDS = ("secret", "token", "password", "key")
BRIDGE_CHECK = "DelphiScript polling loop responsive"


class StartError(Exception):
    """A problem with the workspace settings, worded for the user."""


@dataclass
class WorkspaceSettings:
    root: Path
    profile: str = ""
    dashboard_port: int = 0
    altium365_workspaces: list[str] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return self.root / DIRNAME / FILENAME

    @property
    def logs(self) -> Path:
        return self.root / DIRNAME / LOGS

    def as_json(self) -> dict:
        out: dict = {}
        if self.profile:
            out["profile"] = self.profile
        if self.dashboard_port:
            out["dashboard_port"] = self.dashboard_port
        if self.altium365_workspaces:
            out["altium365_workspaces"] = list(self.altium365_workspaces)
        return out


def find_workspace(start: Path) -> Optional[Path]:
    """The nearest folder at or above ``start`` holding .eda-agent/workspace.json."""
    current = start.resolve()
    while True:
        if (current / DIRNAME / FILENAME).is_file():
            return current
        if current.parent == current:
            return None
        current = current.parent


def parse_settings(root: Path, data: dict) -> WorkspaceSettings:
    if not isinstance(data, dict):
        raise StartError(f"{root / DIRNAME / FILENAME} must hold a JSON object")
    secretish = [k for k in data if any(w in k.lower() for w in _SECRET_WORDS)]
    if secretish:
        raise StartError(
            f"{', '.join(sorted(secretish))} looks like a secret. Keep secrets in "
            "your user environment (setx), not in workspace.json.")
    unknown = sorted(set(data) - KEYS)
    if unknown:
        raise StartError(f"unknown key(s) in workspace.json: {', '.join(unknown)}; "
                         f"known: {', '.join(sorted(KEYS))}")
    profile = data.get("profile", "") or ""
    if not isinstance(profile, str):
        raise StartError("profile must be a profile name")
    port = data.get("dashboard_port", 0) or 0
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise StartError("dashboard_port must be a port number")
    urls = data.get("altium365_workspaces", []) or []
    if isinstance(urls, str):
        urls = [u for u in urls.split(",")]
    if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
        raise StartError("altium365_workspaces must be a list of workspace URLs")
    urls = [u.strip() for u in urls if u.strip()]
    for u in urls:
        if not u.startswith("https://"):
            raise StartError(f"{u} is not an https:// workspace URL")
    return WorkspaceSettings(root=root, profile=profile.strip(),
                             dashboard_port=port, altium365_workspaces=urls)


def load_settings(root: Path) -> WorkspaceSettings:
    path = root / DIRNAME / FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise StartError(f"cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise StartError(f"{path} is not valid JSON: {exc}") from exc
    return parse_settings(root, data)


def write_settings(settings: WorkspaceSettings) -> Path:
    settings.path.parent.mkdir(parents=True, exist_ok=True)
    settings.path.write_text(json.dumps(settings.as_json(), indent=2) + "\n",
                             encoding="utf-8")
    return settings.path


# --- dashboard ---------------------------------------------------------------

def _listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _listeners(port: int) -> list[int]:
    import psutil
    pids = set()
    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, OSError):
        return []
    for c in conns:
        if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
            pids.add(c.pid)
    return sorted(pids)


def _is_ours(pid: int) -> bool:
    """Only a Python or eda-agent process is ours to stop."""
    import psutil
    try:
        name = psutil.Process(pid).name().lower()
    except (psutil.Error, OSError):
        return False
    return name.startswith(("python", "eda-agent"))


def stop_port(port: int, say: Callable[[str], None]) -> bool:
    """Stop the eda-agent process listening on ``port``. False if something
    else holds it, which is left running."""
    import psutil
    ok = True
    for pid in _listeners(port):
        if not _is_ours(pid):
            say(f"   port {port} is held by pid {pid}, which is not eda-agent; left it alone")
            ok = False
            continue
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            proc.wait(timeout=5)
        except psutil.TimeoutExpired:
            psutil.Process(pid).kill()
        except psutil.Error:
            pass
        say(f"   stopped the old dashboard (pid {pid})")
    return ok


def start_dashboard(settings: WorkspaceSettings, say: Callable[[str], None],
                    wait_s: float = 15.0) -> bool:
    port = settings.dashboard_port
    if not stop_port(port, say):
        return False
    deadline = time.monotonic() + 5
    while _listening(port) and time.monotonic() < deadline:
        time.sleep(0.2)
    settings.logs.mkdir(parents=True, exist_ok=True)
    log_path = settings.logs / f"dashboard-{port}.log"
    # Detached, no console window, and outliving this command.
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
             | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "eda_agent.server", "dashboard", "--port", str(port)],
            cwd=str(settings.root), stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, close_fds=True, creationflags=flags,
            start_new_session=sys.platform != "win32")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if _listening(port):
            say(f"   dashboard on http://127.0.0.1:{port}/ (pid {proc.pid}, log {log_path})")
            return True
        if proc.poll() is not None:
            break
        time.sleep(0.3)
    say(f"   the dashboard did not come up; see {log_path}")
    return False


# --- the steps -----------------------------------------------------------------

def _fill_altium365_env(settings: WorkspaceSettings, say: Callable[[str], None]) -> None:
    from .altium365 import saved_user_environment
    filled = []
    for name, value in saved_user_environment().items():
        if not os.environ.get(name, "").strip():
            os.environ[name] = value
            filled.append(name)
    if filled:
        say(f"   from your user environment: {', '.join(sorted(filled))}")
    if settings.altium365_workspaces:
        os.environ["EDA_AGENT_ALTIUM365_WORKSPACES"] = ",".join(settings.altium365_workspaces)
        say(f"   altium365 workspaces: {', '.join(settings.altium365_workspaces)}")


def _login(relogin: bool, say: Callable[[str], None]) -> bool:
    import webbrowser
    from .altium365 import (Altium365Error, Altium365Session, browser_login,
                            login_workspaces, read_environment)
    config = read_environment()
    if config.mode == "workspace":
        say("   workspace token route (EDA_AGENT_ALTIUM365_DOMAIN): no sign-in needed")
        return True
    if config.mode != "nexar":
        say("   EDA_AGENT_NEXAR_APP_ID is not set; skipped. Set it once with setx.")
        return False
    session = Altium365Session(config)
    try:
        if relogin:
            raise Altium365Error("asked to sign in again")
        session._current_token()
        say("   the saved Nexar login is still valid")
    except Altium365Error:
        try:
            browser_login(config, webbrowser.open)
            say("   signed in to Nexar")
        except Altium365Error as exc:
            say(f"   not signed in: {exc}")
            return False
        session = Altium365Session(config)
    for line in login_workspaces(session):
        say(f"   {line}")
    return True


def run_start(settings: WorkspaceSettings, *, dashboard: bool = True,
              doctor: bool = True, login: bool = True, relogin: bool = False,
              say: Callable[[str], None] = print) -> int:
    from . import profile as profile_mod

    say(f"Workspace {settings.root}")
    failed = False

    if settings.profile:
        say(f"\n== Profile {settings.profile}")
        try:
            target = profile_mod.use_central_profile(settings.profile)
            say(f"   active: {target}")
        except ValueError as exc:
            say(f"   {exc}")
            failed = True

    say("\n== Altium 365 settings")
    _fill_altium365_env(settings, say)

    if dashboard and settings.dashboard_port:
        say(f"\n== Dashboard on port {settings.dashboard_port}")
        failed |= not start_dashboard(settings, say)

    bridge_up: Optional[bool] = None
    if doctor:
        from .diag.checks import Status, format_report
        from .diag.doctor import run_doctor_checks
        say("\n== Doctor")
        checks = run_doctor_checks()
        say(format_report(checks, title="eda-agent doctor"))
        bridge_up = any(c.name == BRIDGE_CHECK and c.status == Status.PASS
                        for c in checks)

    if login:
        say("\n== Altium 365 login")
        failed |= not _login(relogin, say)

    say("\n== Altium bridge")
    if bridge_up is True:
        say("   up")
    elif bridge_up is False:
        say("   NOT up: in Altium run StartMCPServer (File > Run Script), "
            "or run `eda-agent relaunch`")
    else:
        say("   not checked (doctor skipped)")
    return 1 if failed else 0


# --- shortcut --------------------------------------------------------------------

def _shortcut_icon() -> str:
    """The app icon file for the shortcut, or "" to keep the console's."""
    icon = Path(__file__).resolve().parent / "web" / "dashboard_static" / "eda-agent.ico"
    return str(icon) if icon.exists() else ""


def create_shortcut(root: Path, say: Callable[[str], None] = print) -> int:
    """A desktop shortcut that opens a console running `start` for ``root``."""
    from .noconsole import no_window
    if sys.platform != "win32":
        say("Desktop shortcuts are made on Windows only.")
        return 1
    exe = Path(sys.executable).with_name("eda-agent.exe")
    command = (f'"{exe}" start --workspace "{root}"' if exe.exists()
               else f'"{sys.executable}" -m eda_agent.server start --workspace "{root}"')
    script = (
        "$s = New-Object -ComObject WScript.Shell;"
        "$d = [Environment]::GetFolderPath('Desktop');"
        "$l = $s.CreateShortcut((Join-Path $d ('eda-agent ' + $env:_EDA_WS_NAME + '.lnk')));"
        "$l.TargetPath = \"$env:ComSpec\";"
        "$l.Arguments = '/k ' + $env:_EDA_WS_CMD;"
        "$l.WorkingDirectory = $env:_EDA_WS_ROOT;"
        "$l.Description = 'Start eda-agent for ' + $env:_EDA_WS_NAME;"
        "if ($env:_EDA_WS_ICON) { $l.IconLocation = $env:_EDA_WS_ICON + ',0' };"
        "$l.Save(); $l.FullName")
    env = {**os.environ, "_EDA_WS_NAME": root.name, "_EDA_WS_CMD": command,
           "_EDA_WS_ROOT": str(root), "_EDA_WS_ICON": _shortcut_icon()}
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          env=env, capture_output=True, text=True, check=False,
                          **no_window())
    if done.returncode != 0:
        say(f"Could not create the shortcut: {done.stderr.strip()}")
        return 1
    say(f"Shortcut: {done.stdout.strip()}")
    return 0


# --- CLI glue ----------------------------------------------------------------------

def cmd_start(args) -> int:
    start = Path(args.workspace) if args.workspace else Path.cwd()
    if args.init:
        root = start.resolve()
        existing = find_workspace(root)
        settings = (load_settings(existing) if existing == root
                    else WorkspaceSettings(root=root))
        if args.profile:
            settings.profile = args.profile
        if args.port:
            settings.dashboard_port = args.port
        if args.altium365_workspace:
            settings.altium365_workspaces = list(args.altium365_workspace)
        try:
            settings = parse_settings(root, settings.as_json())
        except StartError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Wrote {write_settings(settings)}")
        print(json.dumps(settings.as_json(), indent=2))
        if args.shortcut:
            return create_shortcut(root)
        return 0
    root = find_workspace(start)
    if root is None:
        print(f"No {DIRNAME}/{FILENAME} at or above {start.resolve()}. Make one with\n"
              "  eda-agent start --init --profile NAME --port 8777 "
              "--altium365-workspace https://<name>.365.altium.com/", file=sys.stderr)
        return 2
    try:
        settings = load_settings(root)
    except StartError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.shortcut:
        return create_shortcut(root)
    if args.profile:
        settings.profile = args.profile
    if args.port:
        settings.dashboard_port = args.port
    return run_start(settings, dashboard=not args.no_dashboard, doctor=not args.no_doctor,
                     login=not args.no_login, relogin=args.relogin,
                     say=lambda m: print(m, flush=True))
