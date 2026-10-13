"""Keep the Altium network MCP endpoint and dashboard available."""

from __future__ import annotations

import logging
import msvcrt
import os
import asyncio
from pathlib import Path
import subprocess
import sys
import time

import psutil
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from eda_agent.bridge.altium_bridge import get_bridge
from eda_agent.config import get_config


SCRIPTS = ROOT / "scripts"
PYTHON = Path(sys.executable).with_name("python.exe")
MCP_HOST = "192.168.0.33"
MCP_PORT = 14700
DASHBOARD_PORT = 8766
POLL_SECONDS = 30
MCP_FAILURE_LIMIT = 3


def listener(port: int) -> int | None:
    for conn in psutil.net_connections(kind="tcp"):
        if conn.status == psutil.CONN_LISTEN and conn.laddr.port == port:
            return conn.pid
    return None


def active_altium_command(workspace: Path) -> bool:
    # DelphiScript removes its progress marker when the command returns.
    # Do not interrupt a long-running command just because its ping timed out.
    return any(workspace.glob("progress_*.json"))


def pending_script_launcher() -> bool:
    """A URI launch still waiting in X2 must not be launched again."""
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if (proc.info["name"] or "").lower() != "x2.exe":
                continue
            if any("dxpprocess://ScriptingSystem:RunScript" in arg
                   for arg in (proc.info["cmdline"] or [])):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


async def _check_mcp() -> None:
    url = f"http://{MCP_HOST}:{MCP_PORT}/mcp"
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            if not tools.tools:
                raise RuntimeError("MCP returned no tools")


def mcp_healthy() -> bool:
    try:
        asyncio.run(asyncio.wait_for(_check_mcp(), timeout=10))
        return True
    except Exception as exc:
        logging.warning("MCP protocol check failed: %s", exc)
        return False


def our_mcp_process(pid: int) -> bool:
    try:
        return any("serve-altium-http.py" in arg for arg in psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def start(args: list[str], log_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["EDA_AGENT_DASHBOARD_ALLOWED_HOSTS"] = "192.168.0.33,192.168.2.43"
    with log_path.open("ab") as output:
        proc = subprocess.Popen(
            [str(PYTHON), *args],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    logging.info("started %s pid=%s", args[0], proc.pid)


def main() -> None:
    workspace = get_config().workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=workspace / "altium-mcp-supervisor.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mcp.client").setLevel(logging.WARNING)

    # A second copy, including one launched at sign-in, exits immediately.
    with (workspace / "altium-mcp-supervisor.lock").open("a+b") as lock:
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return

        bridge = get_bridge()
        failed_pings = 0
        failed_mcp_checks = 0
        last_script_launch = 0.0
        last_mcp_restart = 0.0
        while True:
            try:
                mcp_pid = listener(MCP_PORT)
                if not mcp_pid:
                    start(
                        [str(SCRIPTS / "serve-altium-http.py")],
                        workspace / "altium-http.log",
                    )
                    failed_mcp_checks = 0
                elif mcp_healthy():
                    failed_mcp_checks = 0
                else:
                    failed_mcp_checks += 1
                    if (
                        failed_mcp_checks >= MCP_FAILURE_LIMIT
                        and not active_altium_command(workspace)
                        and our_mcp_process(mcp_pid)
                        and time.monotonic() - last_mcp_restart > 120
                    ):
                        psutil.Process(mcp_pid).terminate()
                        try:
                            psutil.Process(mcp_pid).wait(timeout=5)
                        except psutil.TimeoutExpired:
                            psutil.Process(mcp_pid).kill()
                        start(
                            [str(SCRIPTS / "serve-altium-http.py")],
                            workspace / "altium-http.log",
                        )
                        logging.warning("restarted unresponsive MCP pid=%s", mcp_pid)
                        failed_mcp_checks = 0
                        last_mcp_restart = time.monotonic()
                if not listener(DASHBOARD_PORT):
                    start(
                        ["-m", "eda_agent", "dashboard", "--host", "0.0.0.0", "--port", "8766"],
                        workspace / "altium-dashboard.log",
                    )

                if bridge.is_altium_running() and not active_altium_command(workspace):
                    if bridge.ping():
                        failed_pings = 0
                    else:
                        failed_pings += 1
                        logging.warning("Altium script ping failed (%s)", failed_pings)
                        if (failed_pings >= 2
                                and time.monotonic() - last_script_launch > 120
                                and not pending_script_launcher()):
                            subprocess.run(
                                [
                                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                    "-File", str(SCRIPTS / "start-altium-bridge.ps1"),
                                ],
                                cwd=ROOT,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW,
                                timeout=15,
                                check=False,
                            )
                            last_script_launch = time.monotonic()
                            logging.info("requested Altium script restart")
                elif not bridge.is_altium_running():
                    failed_pings = 0
            except Exception:
                logging.exception("supervisor check failed")
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
