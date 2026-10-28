# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""EDA Agent MCP Server - Main entry point + CLI subcommands."""

import argparse
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional
from mcp.server.fastmcp import FastMCP

from .tools import (
    BACKENDS, DEFAULT_BACKEND, DEFAULT_TOOLSET, TOOLSETS, register_backend,
)
from .config import get_config

logger = logging.getLogger("eda_agent")


class _ThreadAwareStdout:
    """sys.stdout wrapper that keeps the MAIN thread's writes on the
    real stdout (which the MCP server uses for JSON-RPC) but redirects
    every BACKGROUND-thread write to stderr.

    Lenient MCP clients (Claude Code) tolerate stray stdout bytes from
    the dashboard thread; strict clients (Codex) see a single non-JSON
    byte and close the transport. This wrapper makes the dashboard
    coexist with strict-stdio clients without disabling it -- any
    background-thread print() / write() lands on stderr where it's
    safe, while the main thread's MCP I/O stays untouched.
    """

    __slots__ = ("_real_stdout", "_real_stderr")

    def __init__(self, real_stdout, real_stderr):
        self._real_stdout = real_stdout
        self._real_stderr = real_stderr

    def _target(self):
        if threading.current_thread() is threading.main_thread():
            return self._real_stdout
        return self._real_stderr

    # Required stream methods. Delegating __getattr__ alone isn't safe
    # because some callers do isinstance checks / direct attribute peeks
    # before writing.
    def write(self, s):
        return self._target().write(s)

    def writelines(self, lines):
        return self._target().writelines(lines)

    def flush(self):
        return self._target().flush()

    def isatty(self):
        return self._target().isatty()

    def fileno(self):
        return self._target().fileno()

    @property
    def buffer(self):
        return self._target().buffer

    @property
    def encoding(self):
        return self._target().encoding

    @property
    def errors(self):
        return self._target().errors

    def __getattr__(self, name):
        return getattr(self._target(), name)


def _install_stdio_guard() -> None:
    """Replace sys.stdout with the thread-aware wrapper.

    Idempotent: a second call is a no-op. Captures the original stdout
    so the MCP main thread can keep writing JSON-RPC to it; everything
    on background threads falls through to stderr automatically.
    """
    if isinstance(sys.stdout, _ThreadAwareStdout):
        return
    sys.stdout = _ThreadAwareStdout(sys.stdout, sys.stderr)


def setup_logging() -> None:
    """Configure logging for the MCP server."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    )
    root = logging.getLogger("eda_agent")
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # A capped file in the workspace beside stderr, and secrets redacted
    # from both. Best-effort: a workspace that cannot be written never
    # stops the server.
    try:
        from .config import get_config
        from .server_log import install

        install(root, get_config().workspace_dir)
    except Exception:  # noqa: BLE001
        pass

    # A rotating, capped, secret-redacting file under %USERPROFILE%\EDA
    # Agent\logs\ (or EDA_AGENT_LOG_DIR), alongside the stderr handler
    # above. Best-effort: a disk or permissions problem here must not
    # stop the server, which already logs to stderr regardless.
    try:
        from .activity_log import install as install_activity_log
        install_activity_log(root)
    except OSError as exc:
        root.warning("could not set up the rotating log file: %s", exc)


# Which EDA tool this server drives. Resolved once, at import, from the
# environment so it is settled before the dashboard (which reads the
# registered tool set) or any subcommand touches ``mcp``. MCP clients select
# it in their server config's ``env``; the ``--backend`` CLI flag re-execs
# with this set for terminal use. Altium is the default so existing installs
# are unaffected.
ACTIVE_BACKEND = os.environ.get("EDA_AGENT_BACKEND", DEFAULT_BACKEND)

# Some MCP clients cap tool count or stall serializing hundreds of schemas
# at startup. EDA_AGENT_TOOLSET=minimal advertises only tool_catalog and
# tool_invoke while keeping every other tool reachable through them. The
# default stays "full" so existing installs are unaffected.
ACTIVE_TOOLSET = os.environ.get("EDA_AGENT_TOOLSET", DEFAULT_TOOLSET)

# WHY THERE ARE SERVER INSTRUCTIONS AT ALL.
#
# tool_catalog and tool_guide only help a caller who thinks to call them,
# and the recorded failures are precisely the ones where nobody did: a
# capability was reported ABSENT while the tool existed under another
# namespace, four times over. A tool description cannot fix that, because
# it is read only once the right tool has already been found. Server
# instructions are the one piece of text a client sees before choosing
# anything, so the pointer belongs here.
#
# Deliberately short. This is prepended to a client's context on every
# session, so it earns its place by covering the mistakes that actually
# happened and nothing else.
#: The schematic-engine paragraph, per backend, because the tools it
#: names are not the same on each. The Altium surface carries the whole
#: family; EasyEDA carries the plan-building half and emits through its
#: own tool; KiCad carries neither, so it gets no paragraph rather than
#: a paragraph naming tools that are not there. A preamble that names a
#: missing tool is read first and trusted most, which is the worst place
#: for a dead end.
#: Altium draws from a hand template (decided 2026-10-20: the automatic
#: layout is being retired; a planned template came out clean first time
#: where the engine did not after many rounds).
_ENGINE_ALTIUM = (
    "A SCHEMATIC IS DRAWN FROM A HAND TEMPLATE. Plan the sheet the way\n"
    "an engineer draws it, every coordinate on the 100 mil grid: the IC\n"
    "in the middle, supply rails as straight wires with their capacitors\n"
    "hanging straight down to ground, a feedback divider as a vertical\n"
    "stack whose midpoint runs straight across to its pin, net labels\n"
    "only where a signal leaves the block. Read each symbol's pins with\n"
    "lib_get_pin_list, write the template (parts, wires through pin\n"
    "anchors like U1.EN, power ports, labels and the expected nets),\n"
    "check it with sch_preview_template and look at its PNG, then draw\n"
    "it with sch_draw_template. That places the parts, works out every\n"
    "pin end, checks the wiring against the placed pins and refuses a\n"
    "short or a misplaced part before the first wire.\n"
    "\n"
    "To CHANGE a block, redraw it from a template on a fresh sheet\n"
    "rather than moving parts one at a time. The sch_ and obj_ tools\n"
    "are for small repairs.\n"
    "\n"
)

_ENGINE_EASYEDA = (
    "A SCHEMATIC IS DRAWN FROM A HAND TEMPLATE. Build the DesignPlan,\n"
    "then plan the sheet the way an engineer draws it, every coordinate\n"
    "on the 100 mil grid: the IC in the middle, rails as straight wires\n"
    "with their capacitors hanging straight down to ground, a divider\n"
    "whose midpoint runs straight across to its pin, net labels only\n"
    "where a signal leaves the block. easyeda_emit_template returns the\n"
    "calls that place the parts there; run them, read the pins back with\n"
    "easyeda_get_schematic_pins, and easyeda_emit_template_connections\n"
    "checks the template's wiring against those pins and the plan's\n"
    "nets and returns the calls that draw it.\n"
    "\n"
)

_ENGINE_PARAGRAPHS = {
    "altium": _ENGINE_ALTIUM,
    "both": _ENGINE_ALTIUM,
    "easyeda": _ENGINE_EASYEDA,
    "kicad": "",
}


# Altium only: it is the backend with tools that drive the interface. The
# week of 2026-09-19 produced three separate sessions that fell back to
# them after an API tool seemed to fail or be missing. Placement ended in
# the Place Part dialog and a crashed Altium, a text-frame delete in an
# interactive process waiting for a mouse drag, and a Design Item ID change
# in a plan to edit 1,499 symbols through the Properties panel. The API
# route existed for two of the three; the third was a real gap, now fixed.
# "Usually" is the accurate word, and a genuine gap is worth reporting.
# The paragraph then told sessions to ask the user before driving the
# interface, and they did: they asked George to click things they could
# click themselves. His rule (2026-10-23): the API route when there is
# one, otherwise the interface, without asking.
_UI_AUTOMATION_ALTIUM = (
    "API FIRST, THEN THE INTERFACE, WITHOUT ASKING. The app_ tools that\n"
    "press buttons, pick menus and type into Altium's dialogs\n"
    "(app_click_menu, app_run_ui_command, app_press_dialog_button,\n"
    "app_drive_dialogs, app_set_dialog_control, app_canvas and the rest)\n"
    "come second to an API tool. When an API tool seems to fail or be\n"
    "missing, the cause has usually been a wrong argument or a tool that\n"
    "was not found, and driving a dialog instead has ended with Altium\n"
    "crashed, so first {reach} with what you are trying to do. When there\n"
    "is no API route, drive the interface yourself and carry on: never\n"
    "ask the user to click, pick a menu or press a button you can press.\n"
    "Read every dialog you raise (app_list_open_dialogs, app_screenshot)\n"
    "and answer it as the task requires; needs_a_human marks one the\n"
    "dialog driver will not answer blindly, not one to hand to the user.\n"
    "Ask the user only for a decision that is theirs, never for a click.\n"
    "Say in your report which step needed the interface: a missing API\n"
    "route is worth reporting, because it gets fixed.\n"
    "\n"
)

_UI_AUTOMATION_PARAGRAPHS = {
    "altium": _UI_AUTOMATION_ALTIUM,
    "both": _UI_AUTOMATION_ALTIUM,
    "easyeda": "",
    "kicad": "",
}


def build_server_instructions(
    toolset: str = DEFAULT_TOOLSET, backend: str = DEFAULT_BACKEND
) -> str:
    """The preamble, worded for the surface the client will actually see.

    Under the minimal toolset only tool_catalog and tool_invoke are
    advertised, so instructing a client to "call tool_guide" names
    something it cannot see. It stays reachable through tool_invoke, and
    saying which applies is the difference between guidance and a dead
    end for the clients that most need the guidance.

    The backend does the same job for the schematic paragraph: the
    template tools differ per backend, so naming Altium's to a KiCad
    client would send it looking for a tool that backend never registers.
    """
    if (toolset or DEFAULT_TOOLSET).strip().lower() == "minimal":
        reach = ('reach tool_guide through tool_invoke, since this server '
                 'is advertising only the two meta-tools,')
    else:
        reach = "call tool_guide"
    engine = _ENGINE_PARAGRAPHS.get(
        (backend or DEFAULT_BACKEND).strip().lower(), _ENGINE_ALTIUM)
    ui = _UI_AUTOMATION_PARAGRAPHS.get(
        (backend or DEFAULT_BACKEND).strip().lower(), _UI_AUTOMATION_ALTIUM)
    ui = ui.replace("{reach}", reach)
    return (
        "Tools are grouped by the DOCUMENT they act on, and mixing them up\n"
        "is the most common error here: lib_ acts on a .PcbLib or .SchLib,\n"
        "pcb_ on an open .PcbDoc, sch_ on an open .SchDoc, and obj_ on\n"
        "either, by the object type it is given (eTrackObject acts on the\n"
        "active board). A board tool aimed at a library does not always\n"
        "fail. It can resolve some other open board and report success for\n"
        "work you never asked for.\n"
        "\n"
        f"Before concluding that this server cannot do something, {reach}\n"
        "with what you are trying to do. It answers three separate things:\n"
        "the tool and what it needs first, the tool you were probably\n"
        "reaching for and why it acts on a different document, and the\n"
        "short list of things that are genuinely impossible with the\n"
        "reason. Use tool_catalog to search the surface by name or\n"
        "category.\n"
        "\n"
        + ui
        + engine +
        "Coordinates are in mils throughout, on every backend.\n"
        "\n"
        "A reply carrying \"update\" means a newer eda-agent is out. Tell\n"
        "the user what is new and ask; call server_update(apply=True) only\n"
        "after they agree.\n"
    )


SERVER_INSTRUCTIONS = build_server_instructions(ACTIVE_TOOLSET, ACTIVE_BACKEND)


def compact_text_blocks(blocks):
    """Re-serialize JSON text blocks without the indentation.

    FastMCP writes a returned dict as JSON with indent=2, which is 40-65%
    more characters than the same JSON compact. A client that reads the
    text copy (rather than structuredContent) sends all of that padding to
    the model on every call. Only text that parses as JSON is rewritten, so
    the data is unchanged; anything else passes through as it was.
    """
    import json
    out = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text[:1] in ("{", "["):
            try:
                compact = json.dumps(json.loads(text), separators=(",", ":"),
                                     ensure_ascii=False)
            except ValueError:
                compact = None
            if compact is not None and len(compact) < len(text):
                block = block.model_copy(update={"text": compact})
        out.append(block)
    return out


class _CompactFastMCP(FastMCP):
    """FastMCP whose tool results carry compact JSON text.

    structuredContent is left exactly as FastMCP builds it: Claude Code
    reads that copy, and only the text copy carried the indentation.
    """

    async def call_tool(self, name, arguments):
        result = await super().call_tool(name, arguments)
        if isinstance(result, tuple):
            return (compact_text_blocks(result[0]), result[1])
        if isinstance(result, list):
            return compact_text_blocks(result)
        return result


# Create global FastMCP instance, named for the backend so a client that
# lists several eda-agent servers can tell them apart.
mcp = _CompactFastMCP(
    f"eda-agent-{ACTIVE_BACKEND.strip().lower() or DEFAULT_BACKEND}",
    instructions=SERVER_INSTRUCTIONS,
)

# Register only the selected backend's tools. Returns the normalised name
# (an unrecognised value falls back to the default).
ACTIVE_BACKEND = register_backend(mcp, ACTIVE_BACKEND, ACTIVE_TOOLSET)


def _probe_port_owner(host: str, port: int) -> Optional[int]:
    """Return the OS pid that owns ``host:port`` if it's already bound, else None.

    Used at startup to detect the orphan-MCP-server situation: a previous
    eda-agent instance failed to exit on stdio EOF and is still holding
    port 8766, so this new instance's Flask thread will silently fail to
    bind. Surfacing the owning pid + a kill hint turns a confusing
    "dashboard not loading new endpoints" experience into one obvious fix.
    """
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        for conn in psutil.net_connections(kind="tcp"):
            la = conn.laddr
            if la and la.port == port and conn.status == "LISTEN":
                return conn.pid
    except (psutil.AccessDenied, OSError):
        return None
    return None


def _dashboard_disabled_via_env() -> bool:
    """Check the env vars that opt out of the dashboard.

    Accepts any of:
      EDA_AGENT_NO_DASHBOARD=1      (original name)
      EDA_AGENT_DISABLE_DASHBOARD=1 (alias requested in GH issue #4)
      EDA_AGENT_HEADLESS=1          (alias)
    Reading all three keeps existing setups working while matching the
    naming pattern users / docs may have settled on.
    """
    import os
    for key in ("EDA_AGENT_NO_DASHBOARD",
                "EDA_AGENT_DISABLE_DASHBOARD",
                "EDA_AGENT_HEADLESS"):
        if os.environ.get(key, "").strip() in ("1", "true", "yes", "on"):
            return True
    return False


def _spawn_dashboard_background(host: str, port: int) -> "Optional[object]":
    """Start the local web dashboard on a background thread.

    Two ways the dashboard runs:
      1. Auto-spawned in-process when MCP starts (this function) -- the
         common case when the user opens Claude with the MCP server.
         Dies when MCP exits.
      2. Manually via `eda-agent dashboard --port 8766` in a terminal --
         a standalone process the user controls.

    Returns the Werkzeug server handle so the caller can ``.shutdown()``
    it on stdio EOF. Werkzeug's request-handler threads aren't daemonic,
    so without the explicit shutdown the process can't exit and the
    next /mcp reconnect ends up with port 8766 still held by the orphan.
    """
    import os
    import threading

    if _dashboard_disabled_via_env():
        logger.info("dashboard disabled via env var (no/disable/headless)")
        return None

    # If port is already bound -- probably a manually-launched standalone
    # `eda-agent dashboard` -- skip with an info log. The MCP server
    # works fine without an in-process dashboard.
    owner_pid = _probe_port_owner(host, port)
    if owner_pid is not None and owner_pid != os.getpid():
        logger.info(
            "dashboard already running on port %s (pid %s) -- not "
            "spawning another. http://%s:%s/", port, owner_pid, host, port,
        )
        return {"already_running": True, "owner_pid": owner_pid}

    server_holder: dict[str, object] = {}

    def _run():
        try:
            from werkzeug.serving import make_server
            from .web.dashboard import create_app
            app = create_app()
            import logging as _log
            _log.getLogger("werkzeug").setLevel(_log.WARNING)
            srv = make_server(host, port, app, threaded=True)
            server_holder["srv"] = srv
            srv.serve_forever()
        except OSError as e:
            logger.warning("dashboard could not bind %s:%s (%s)",
                           host, port, e)
        except Exception as e:
            logger.warning("dashboard background thread crashed: %s", e)

    t = threading.Thread(target=_run, name="dashboard-server", daemon=True)
    t.start()
    logger.info("dashboard scheduled on http://%s:%s/", host, port)
    server_holder["thread"] = t
    return server_holder


def serve_mcp(no_dashboard: bool = False) -> int:
    """Start the MCP server on stdio. This is the default mode -- it's
    what an MCP-compatible client calls when it invokes `eda-agent` with no args.

    Passing ``no_dashboard=True`` (or setting any of the supported env
    vars listed in ``_dashboard_disabled_via_env``) skips the dashboard
    background thread entirely. Strict MCP clients (Codex, MCP CLI, etc)
    do not tolerate ANY noise on stdio, and even with the dashboard
    running silently a stray print from a transitive import can corrupt
    the JSON-RPC stream. Headless mode is the safe default for those
    clients.
    """
    # CRITICAL ORDER: install the thread-aware stdio guard BEFORE any
    # other module gets a chance to print. Strict MCP stdio clients
    # (Codex etc) close the transport on the first non-JSON byte. The
    # guard sends background-thread writes to stderr while keeping the
    # main thread's stdout intact for the MCP JSON-RPC stream.
    _install_stdio_guard()
    sys.stdout.flush()

    setup_logging()
    logger.info("Starting EDA Agent MCP Server")

    config = get_config()
    config.ensure_workspace()
    logger.info("Workspace directory: %s", config.workspace_dir)

    # Trim the logs, old checkpoints and old screenshots. Off the main
    # thread so a large prune never delays the MCP handshake.
    from . import housekeeping
    threading.Thread(target=housekeeping.run, args=(config.workspace_dir,),
                     name="workspace-housekeeping", daemon=True).start()

    # The first 3D model would otherwise wait on a cold CadQuery import.
    from .design.models3d import warm_cadquery
    warm_cadquery()

    # Record this server and warn (stderr only) about others on the same
    # workspace. Never refuses and never stops anything: several sessions
    # at once are allowed. app_ping and app_get_status repeat the warning.
    from . import server_registry, updates
    server_registry.register(config.workspace_dir)
    # Once a day at most, in the background: a newer eda-agent is then
    # reported by app_ping and app_context, never installed unasked.
    updates.start_background_check()
    duplicate = server_registry.duplicate_warning(
        server_registry.others(config.workspace_dir))
    if duplicate:
        logger.warning("%s", duplicate)

    # Auto-launch the web dashboard in-process. Skip for headless / strict
    # MCP clients that can't tolerate dashboard side-effects.
    if no_dashboard or _dashboard_disabled_via_env():
        logger.info("headless mode -- dashboard not started")
        dash = None
    else:
        dash = _spawn_dashboard_background(host="127.0.0.1", port=8766)

    try:
        mcp.run(transport="stdio")
    finally:
        # Shut down the in-process Werkzeug server so the process can
        # actually exit. Werkzeug's request-handler threads aren't
        # daemonic, so without this they keep the process alive past
        # stdio-EOF and the next /mcp reconnect ends up with port 8766
        # still held by the orphan.
        if dash and isinstance(dash, dict):
            srv = dash.get("srv")
            try:
                if srv is not None and hasattr(srv, "shutdown"):
                    srv.shutdown()  # type: ignore[attr-defined]
            except Exception as e:
                logger.debug("dashboard shutdown raised: %s", e)
        # Before _exit, which skips atexit. A record left by a crash is
        # removed by the next server that reads the folder.
        server_registry.unregister(config.workspace_dir)
        import os as _os
        _os._exit(0)
    return 0


def _run_review(args) -> int:
    """Handle ``eda-agent review <file>`` -- offline fallback review.

    This is the opt-in, no-Altium fallback (component-level checks only); it
    is NOT the preferred review path. It is disabled unless the caller passes
    ``--offline`` or sets ``EDA_AGENT_HEADLESS_REVIEW=1``.

    Exit code: 0 clean, 1 if any finding at/above ``--fail-on`` (so CI fails
    the build), 2 if disabled or the file could not be read.
    """
    from .fileio.review import (
        ERROR,
        HEADLESS_DISABLED_MESSAGE,
        headless_review_enabled,
        review_project_file,
        to_sarif,
    )

    if not (getattr(args, "offline", False) or headless_review_enabled()):
        print(f"ERROR: {HEADLESS_DISABLED_MESSAGE}", file=sys.stderr)
        return 2

    try:
        report = review_project_file(args.file)
    except (ValueError, OSError) as e:
        print(f"ERROR: cannot read {args.file}: {e}", file=sys.stderr)
        return 2

    if getattr(args, "sarif", False):
        import json as _json
        from . import __version__ as _ver
        print(_json.dumps(to_sarif(report, tool_version=_ver), indent=2))
    elif getattr(args, "json", False):
        import json as _json
        print(_json.dumps(report, indent=2))
    else:
        s = report["summary"]
        print(f"Reviewed {report['file']}")
        if "sheet_count" in report:  # project review
            print(f"  {report['sheet_count']} sheet(s), "
                  f"{report['component_count']} components")
        elif "document" in report:  # single schematic sheet
            doc = report["document"]
            print(f"  {doc.get('title') or '(untitled)'}  "
                  f"rev {doc.get('revision') or '-'}  --  "
                  f"{report['component_count']} components, "
                  f"{len(report.get('net_names', []))} named nets")
        else:  # library review
            print(f"  {report['component_count']} library components")
        print(f"  {s.get('error', 0)} error(s), {s.get('warning', 0)} "
              f"warning(s), {s.get('info', 0)} info")
        for f in report["findings"]:
            tag = f["designator"] or "-"
            sheet = f" ({f['sheet']})" if f.get("sheet") else ""
            print(f"  [{f['severity'].upper():7}] {tag:6} {f['check']}: "
                  f"{f['message']}{sheet}")
    # Gating: exit 1 if any finding at or above the --fail-on threshold.
    # Default "error" preserves the prior behavior.
    fail_on = getattr(args, "fail_on", "error")
    order = {"info": 0, "warning": 1, "error": 2}
    if fail_on == "never":
        return 0
    threshold = order.get(fail_on, 2)
    s = report["summary"]
    triggered = sum(
        s.get(sev, 0) for sev, rank in order.items() if rank >= threshold
    )
    return 1 if triggered > 0 else 0


def _offline_gate_ok(args) -> bool:
    """Shared opt-in check for the offline (no-Altium) CLI fallbacks."""
    from .fileio.review import (
        HEADLESS_DISABLED_MESSAGE,
        headless_review_enabled,
    )
    if getattr(args, "offline", False) or headless_review_enabled():
        return True
    print(f"ERROR: {HEADLESS_DISABLED_MESSAGE}", file=sys.stderr)
    return False


def _run_bom(args) -> int:
    """Handle ``eda-agent bom <file>`` -- offline consolidated BOM.

    Opt-in, no-Altium fallback. Exit 0 on success, 2 if disabled/unreadable.
    """
    if not _offline_gate_ok(args):
        return 2
    from .fileio.bom import bom_from_file, bom_to_csv

    try:
        lines = bom_from_file(args.file)
    except (ValueError, OSError) as e:
        print(f"ERROR: cannot read {args.file}: {e}", file=sys.stderr)
        return 2
    if not lines:
        from .fileio.bom import EMPTY_BOM_WARNING
        print(f"WARNING: {args.file}: {EMPTY_BOM_WARNING}", file=sys.stderr)

    if getattr(args, "csv", False):
        print(bom_to_csv(lines), end="")
    elif getattr(args, "json", False):
        import json as _json
        print(_json.dumps(lines, indent=2))
    else:
        parts = sum(ln["quantity"] for ln in lines)
        print(f"BOM for {args.file}: {len(lines)} line(s), {parts} parts")
        for ln in lines:
            print(f"  {ln['quantity']:3}x  {', '.join(ln['designators']):24}  "
                  f"{ln['value'] or '-':10}  {ln['mpn'] or '-'}")
    return 0


def _run_netlist(args) -> int:
    """Handle ``eda-agent netlist <file>`` -- offline netlist + connectivity.

    Opt-in, no-Altium fallback. Reconstructs the netlist geometrically and
    runs connectivity ERC. Exit 0 clean, 1 if any connectivity finding at/
    above ``--fail-on``, 2 if disabled/unreadable.
    """
    if not _offline_gate_ok(args):
        return 2
    from .fileio.netlist_solver import solve_schematic_nets
    from .fileio.review import review_connectivity

    try:
        solved = solve_schematic_nets(args.file)
    except (ValueError, OSError, KeyError) as e:
        print(f"ERROR: cannot solve {args.file}: {e}", file=sys.stderr)
        return 2

    findings = review_connectivity(solved)
    if getattr(args, "sarif", False):
        import json as _json
        from . import __version__ as _ver
        from .fileio.review import to_sarif
        report = {"file": str(args.file), "findings": findings}
        print(_json.dumps(to_sarif(report, tool_version=_ver), indent=2))
    elif getattr(args, "json", False):
        import json as _json
        print(_json.dumps({"nets": {n: [f"{m['component']}.{m['pin']}"
                                        for m in v]
                                    for n, v in solved["nets"].items()},
                           "findings": findings}, indent=2))
    else:
        print(f"Netlist for {args.file}: {len(solved['nets'])} nets, "
              f"{len(solved['pin_nets'])} pins")
        for f in findings:
            tag = f["designator"] or "-"
            print(f"  [{f['severity'].upper():7}] {tag:6} {f['check']}: "
                  f"{f['message']}")

    fail_on = getattr(args, "fail_on", "error")
    if fail_on == "never":
        return 0
    order = {"info": 0, "warning": 1, "error": 2}
    threshold = order.get(fail_on, 2)
    triggered = any(order.get(f["severity"], 2) >= threshold for f in findings)
    return 1 if triggered else 0


def main() -> int:
    """CLI entry point.

    Subcommands:
      serve             -- run the MCP server (default when no args given)
      scripts-path      -- print the path to the bundled DelphiScript files
      install-scripts   -- copy bundled scripts to a chosen directory
      easyeda-extension -- locate or build the EasyEDA extension
      relaunch          -- start the bridge script in the running Altium

    IMPORTANT: when invoked with no arguments, this MUST start the MCP
    server on stdio -- MCP-compatible clients rely on that behaviour.
    """
    # Lazy import, but earlier than the dispatch below needs it: the
    # calculator subcommands are GENERATED from cli.CALC_COMMANDS, so
    # the table has to exist before the parser is built. Still cheap --
    # cli pulls in os/shutil/sys/pathlib only, and everything heavier
    # in it is imported inside the function that needs it, so the
    # no-argument MCP stdio path is unaffected.
    from . import cli

    parser = argparse.ArgumentParser(
        prog="eda-agent",
        description=(
            "MCP server bridge for Altium Designer. "
            "Run with no arguments to start the MCP server on stdio."
        ),
    )
    # The bug-report template and CONTRIBUTING both ask reporters for
    # `eda-agent --version`. Without this the flag was an argparse error,
    # so the first thing a bug reporter was told to run did not work.
    from . import __version__ as _pkg_version
    parser.add_argument(
        "--version", action="version",
        version=f"eda-agent {_pkg_version}",
    )
    # Top-level flag so `eda-agent --no-dashboard` works without the
    # `serve` subcommand. Important: most MCP clients invoke the binary
    # with NO arguments, so this needs to attach at the top level.
    parser.add_argument(
        "--no-dashboard", action="store_true",
        help=("Skip the in-process web dashboard. Required by strict "
              "MCP stdio clients (Codex, etc) that can't tolerate the "
              "dashboard thread's side-effects. Equivalent to setting "
              "EDA_AGENT_DISABLE_DASHBOARD=1 / EDA_AGENT_HEADLESS=1."),
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Alias for --no-dashboard.",
    )
    # Top-level so `eda-agent --backend kicad` works with no subcommand, the
    # way MCP clients invoke the binary. The real selection happens at import
    # from EDA_AGENT_BACKEND; when this flag names a different backend we
    # re-exec with that env set so registration runs against the right one.
    parser.add_argument(
        # Choices come from BACKENDS rather than a literal list: adding a
        # backend to the registry and forgetting this line rejects the new
        # name here while EDA_AGENT_BACKEND accepts it, so the flag and the
        # variable disagree about what exists. That is how 'easyeda' was
        # unreachable from the CLI after the backend itself worked.
        "--backend", choices=BACKENDS, default=None,
        help=("Which EDA tool to drive (default: altium). Selects the tool "
              "surface: 'altium' is the full Altium suite, 'kicad' the "
              "KiCad-native tools, 'easyeda' the EasyEDA Pro tools, 'both' "
              "the union of Altium and KiCad. Equivalent to setting "
              "EDA_AGENT_BACKEND."),
    )
    parser.add_argument(
        "--toolset", choices=TOOLSETS, default=None,
        help=("How many tools to advertise (default: full). 'minimal' "
              "exposes only tool_catalog and tool_invoke, keeping every "
              "other tool reachable through them, for MCP clients that "
              "limit tool count or are slow to load hundreds of schemas. "
              "Equivalent to setting EDA_AGENT_TOOLSET."),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # serve -- default when no args given
    serve_p = subparsers.add_parser(
        "serve",
        help="Run the MCP server on stdio (default when no args given)",
    )
    serve_p.add_argument(
        "--no-dashboard", action="store_true",
        help="Skip the in-process web dashboard (see top-level flag).",
    )
    serve_p.add_argument(
        "--headless", action="store_true",
        help="Alias for --no-dashboard.",
    )
    serve_p.add_argument(
        "--backend", choices=BACKENDS, default=None,
        help="Which EDA tool to drive (see top-level flag).",
    )

    # scripts-path
    subparsers.add_parser(
        "scripts-path",
        help="Print the path to the bundled DelphiScript files",
    )

    # install-scripts
    install_p = subparsers.add_parser(
        "install-scripts",
        help="Copy bundled scripts to a directory of your choice",
    )
    install_p.add_argument(
        "--dest",
        help=r"Destination directory (default: the folder Altium's Global Projects list loads it from, else %%USERPROFILE%%\EDA Agent\scripts)",
    )
    install_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing scripts without prompting",
    )

    # easyeda-extension -- where the editor half is, and building it
    ext_p = subparsers.add_parser(
        "easyeda-extension",
        help=("Print where the EasyEDA extension source is (path), or "
              "build the .eext to import into EasyEDA Pro (build; needs "
              "Node.js)."),
    )
    ext_p.add_argument("action", choices=["path", "build"])
    ext_p.add_argument(
        "--out", default=None, metavar="DIR",
        help=("Folder to build in. An installed copy is built beside the "
              "default workspace unless this is given; a checkout builds "
              "in place."),
    )

    # relaunch -- start the bridge script in the running Altium, on request
    relaunch_p = subparsers.add_parser(
        "relaunch",
        help=("Ask the running Altium to run StartMCPServer from the "
              "installed script project. Leaves pending requests alone and "
              "does nothing if the bridge already answers."),
    )
    relaunch_p.add_argument("--prjscr", default=None, metavar="PATH",
                            help="Script project to run (default: the "
                                 "install-scripts destination).")
    relaunch_p.add_argument("--dry-run", action="store_true",
                            help="Print the command instead of running it.")
    relaunch_p.add_argument("--force", action="store_true",
                            help="Run it even when the bridge answers a ping.")
    relaunch_p.add_argument("--restart", action="store_true",
                            help="Stop the running loop after its current "
                                 "command and start it again (after "
                                 "install-scripts). Other sessions' calls "
                                 "wait through it instead of failing.")

    # update -- pull a newer eda-agent into this clone, after asking
    # health -- offline, fast
    subparsers.add_parser(
        "health",
        help="Fast offline preconditions (workspace, pointer file, scripts)",
    )

    subparsers.add_parser("a365-login", help="Authorize Nexar and store the token for Altium 365 read tools")
    subparsers.add_parser("a365-logout", help="Remove the stored Nexar token")

    # doctor -- full preflight, talks to Altium
    doctor_p = subparsers.add_parser(
        "doctor",
        help="Full preflight: workspace + Altium + version + canary IPC calls",
    )
    doctor_p.add_argument(
        "--library",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "Optional .SchLib path to test reachability. Repeat for "
            "multiple libs. The doctor never crawls; it only tests "
            "paths you supply."
        ),
    )
    doctor_p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the text report.",
    )

    # eco-execute -- EXPERIMENTAL, UNVERIFIED: auto-press the ECO dialog
    # The full story lives in the epilog rather than in `help`: a
    # paragraph here is reprinted in the top-level command list and
    # swamps every other entry.
    eco_p = subparsers.add_parser(
        "eco-execute",
        help="EXPERIMENTAL. Auto-press Altium's ECO dialog (needs --confirm).",
        epilog=(
            "EXPERIMENTAL, UNVERIFIED. Watches for Altium's Engineering "
            "Change Order dialog and presses Validate Changes / Execute "
            "Changes / Close on it automatically, via app_drive_dialogs' "
            "existing reactive driver. Off unless --confirm is passed. "
            "This automates the one step eda-agent's own guidance and "
            "proj_sync_pcb's docstring say needs a human; see "
            "docs/ENGINEERING_TOOLS.md before using it on a real design. "
            "Trigger the ECO separately (proj_sync_pcb via your MCP "
            "client, or Design > Update PCB Document in Altium itself) "
            "while this is running -- it only watches and presses, it "
            "does not trigger anything."
        ),
    )
    eco_p.add_argument(
        "--confirm", action="store_true",
        help="Required. Acknowledges this presses Execute Changes without "
             "a human looking at the dialog first.",
    )
    eco_p.add_argument(
        "--validate-only", action="store_true",
        help="Press Validate Changes only; leave Execute Changes/Close "
             "for a human to press after reviewing the preview.",
    )
    eco_p.add_argument(
        "--allow-confirm-dialogs", action="store_true",
        help="Also answer yes/no confirmation dialogs encountered along "
             "the way (see app_drive_dialogs' allow_confirm).",
    )
    eco_p.add_argument(
        "--wait-first", type=float, default=60.0,
        help="Seconds to wait for the ECO dialog to appear (default 60).",
    )
    eco_p.add_argument(
        "--budget", type=float, default=300.0,
        help="Seconds before giving up on a wedged dialog sequence (default 300).",
    )

    # perf -- where the time went, from logs already on disk
    perf_p = subparsers.add_parser(
        "perf",
        help="Report where call time goes, from the workspace's own logs.",
    )
    perf_p.add_argument("--json", action="store_true", dest="json_out",
                        help="Emit machine-readable JSON instead of the table.")
    perf_p.add_argument("--limit", type=int, default=15,
                        help="How many commands to list (default 15).")

    # sdk-coverage -- the generic property layer against Altium's own
    # declarations. Needs reference/altium_sdk.json, which
    # scripts/altium_sdk_reflect.ps1 produces from the local install.
    sdk_p = subparsers.add_parser(
        "sdk-coverage",
        help="Report how much of Altium's SDK the property chains reach.",
    )
    sdk_p.add_argument("--json", action="store_true", dest="json_out",
                       help="Emit machine-readable JSON instead of the table.")
    sdk_p.add_argument("--detail", type=int, default=0, metavar="N",
                       help="List N missing property names per object type.")
    sdk_p.add_argument("--sdk-json", default=None, metavar="PATH",
                       help="Read a reflected SDK from PATH instead of "
                            "reference/altium_sdk.json.")

    # sdk-probe -- what sdk-coverage cannot know. The assembly says a
    # property is declared; only the running engine says whether it is
    # reachable, and finding out costs a fault that stops the loop, so
    # this is a deliberate command and never part of a tool call.
    probe_p = subparsers.add_parser(
        "sdk-probe",
        help="Ask the running Altium which declared properties it exposes.",
    )
    probe_p.add_argument("--designator", default="", metavar="REF",
                         help="Component to read (default: the first on the "
                              "active sheet).")
    probe_p.add_argument("--properties", default="", metavar="A,B,C",
                         help="Probe only these names. Default: every "
                              "candidate the generator plans for.")
    probe_p.add_argument("--doc-path", default="", metavar="PATH",
                         help="Schematic to read instead of the active one.")
    probe_p.add_argument("--json", action="store_true", dest="json_out",
                         help="Emit machine-readable JSON instead of a table.")
    probe_p.add_argument("--sdk-json", default=None, metavar="PATH",
                         help="Read a reflected SDK from PATH instead of "
                              "reference/altium_sdk.json.")

    # sdk-symbols -- the offline half of sdk-probe. ScriptingSystem.dll
    # carries the engine's registered names, and a name it does not carry
    # halts the debugger when a script touches it, so this is what the
    # generator checks before emitting a line.
    sym_p = subparsers.add_parser(
        "sdk-symbols",
        help="Report what the scripting engine will resolve, offline.",
    )
    sym_p.add_argument("--refresh", action="store_true",
                       help="Re-extract the table from the local install.")
    sym_p.add_argument("--name", default="", metavar="NAME",
                       help="Explain how one member can be reached.")
    sym_p.add_argument("--json", action="store_true", dest="json_out",
                       help="Emit machine-readable JSON instead of the table.")
    sym_p.add_argument("--sdk-json", default=None, metavar="PATH",
                       help="Read a reflected SDK from PATH instead of "
                            "reference/altium_sdk.json.")

    # profile-* -- per-workspace owner profile (naming/rules/fab/footprint
    # conventions, document units & grid, project templates). See profile.py
    # and docs/PROFILES.md. Flat hyphenated verbs, same style as
    # scripts-path/install-scripts and a365-login/a365-logout -- this
    # codebase has no nested-subcommand precedent to follow instead.
    profile_init_p = subparsers.add_parser(
        "profile-init",
        help="Scaffold a new owner profile (see docs/PROFILES.md).",
    )
    profile_init_p.add_argument("name", help="Profile name.")
    profile_init_p.add_argument(
        "--here", action="store_true",
        help="Scaffold a project-local .eda-agent/profile/ in the current "
             "directory instead of a centrally-switchable one. Meant to be "
             "committed to that project's own repo.",
    )

    subparsers.add_parser("profile-list", help="List central profiles and which one is active.")

    profile_show_p = subparsers.add_parser(
        "profile-show",
        help="Show the profile actually in effect for the current directory.",
    )
    profile_show_p.add_argument("--json", action="store_true", dest="json_out",
                                help="Emit machine-readable JSON.")

    profile_use_p = subparsers.add_parser(
        "profile-use",
        help="Set the centrally active profile.",
    )
    profile_use_p.add_argument("name")

    profile_validate_p = subparsers.add_parser(
        "profile-validate",
        help="Load every file in a profile and report errors.",
    )
    profile_validate_p.add_argument("name", nargs="?", default=None,
                                    help="Central profile name (default: whatever resolves here).")
    profile_validate_p.add_argument("--here", action="store_true",
                                    help="Validate the project-local .eda-agent/profile/ in cwd.")

    profile_new_project_p = subparsers.add_parser(
        "profile-new-project",
        help="Copy a profile's templates/<name>/ tree to a new project directory.",
    )
    profile_new_project_p.add_argument("template")
    profile_new_project_p.add_argument("dest")
    profile_new_project_p.add_argument(
        "--force", action="store_true",
        help="Copy into a non-empty destination. Files the template also "
             "has are overwritten; anything else already there is left "
             "alone (this merges, it does not replace the directory).")

    # dashboard -- local web UI for the MCP bridge
    update_p = subparsers.add_parser(
        "update",
        help=("Update from your remote or a local folder, and say "
              "what is left to do"),
        epilog=(
            "Checks four things that go stale independently: your git remote, the\n"
            "installed package, the deployed DelphiScript files, and the copy Altium\n"
            "has COMPILED. Redeploys the scripts when they differ.\n"
            "\n"
            "Only the remote you PUSH to is checked. Other remotes are opt-in, because\n"
            "a checkout can have your fork, the project it came from and a colleague's\n"
            "copy configured at once:\n"
            "\n"
            "    eda-agent update --from upstream\n"
            "\n"
            "--from <folder> updates from a local copy, with no git and no network in\n"
            "it: another checkout, a copy on a share, a release directory. It reads\n"
            "that folder's version and SCRIPT_VERSION and deploys its Altium scripts.\n"
            "\n"
            "    eda-agent update --from D:\\builds\\eda-agent-0.7.1\n"
            "\n"
            "It will NOT pull (that touches your tree; pass --pull, and it is refused\n"
            "on a dirty tree), it will NOT pip install for you, and it CANNOT reload\n"
            "Altium's script project because there is no API for it, so that step is\n"
            "reported for you to do.\n"
            "\n"
            "Exits 0 when current, 1 when something needs you, 2 on an error.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    update_p.add_argument(
        "--pull", action="store_true",
        help="Also run `git pull --ff-only`. Refused on a dirty tree.")
    update_p.add_argument(
        "--fetch", action="store_true",
        help="Refresh remote refs first, so 'behind by N' is current "
             "rather than from your last fetch.")
    update_p.add_argument(
        "--check", dest="check_only", action="store_true",
        help="Report only; change nothing.")
    update_p.add_argument(
        "--force-scripts", action="store_true",
        help="Redeploy the Altium scripts even when the versions match.")
    update_p.add_argument(
        "--from", dest="source", metavar="FOLDER|REMOTE", default="",
        help="Update from a local eda-agent folder (a checkout, a copy "
             "on a share, a release directory), or compare against "
             "another git remote by name. A path that exists is "
             "treated as a folder.")
    update_p.add_argument(
        "--yes", action="store_true",
        help=("Apply the update instead of only reporting: fast-forward "
              "this clone, reinstall if the dependencies changed, copy the "
              "scripts and restart the Altium loop."))
    update_p.add_argument(
        "--no-relaunch", action="store_true",
        help="With --yes, leave the Altium loop running the old scripts.")
    update_p.add_argument(
        "--json", dest="json_out", action="store_true",
        help="Print the steps as JSON.")

    # --- calculators -------------------------------------------------
    # Generated from cli.CALC_COMMANDS so a flag cannot drift from the
    # parameter it fills. Ten hand-written blocks would be ten chances
    # for that, and argparse reports the mismatch as a TypeError at the
    # moment the user runs the command.
    for _verb, (_fn, _help, _params) in cli.CALC_COMMANDS.items():
        _p = subparsers.add_parser(
            _verb, help=_help,
            epilog=("Every calculator reports a `status`: exact (a "
                    "definition), published (a named standard or "
                    "textbook closed form) or estimate (a rule of "
                    "thumb). Use --json for the full reply."))
        for _name, _type, _default, _phelp in _params:
            if _default is None:
                _p.add_argument(f"--{_name.replace('_', '-')}",
                                dest=_name, type=_type, required=True,
                                help=_phelp)
            else:
                _p.add_argument(f"--{_name.replace('_', '-')}",
                                dest=_name, type=_type, default=_default,
                                help=f"{_phelp} (default: {_default})")
        _p.add_argument("--json", dest="json_out", action="store_true",
                        help="Print the full reply as JSON.")

    cross_p = subparsers.add_parser(
        "calc-cross-check",
        help="Compare a calculator against a locally built pcb-toolkit",
        epilog=("pcb-toolkit is OPTIONAL and is not published as a "
                "binary, so it must be built from source with Rust. "
                "Nothing in eda-agent needs it; it is a second opinion. "
                "Exits 1 when the two disagree."),
    )
    cross_p.add_argument("calculator",
                         help="Function name in engineering.pcb_calc, "
                              "e.g. ipc2221_spacing")
    cross_p.add_argument("--arg", action="append", metavar="NAME=VALUE",
                         help="Argument to pass to both. Repeatable.")
    cross_p.add_argument("--json", dest="json_out", action="store_true",
                         help="Print the full comparison as JSON.")

    # --- fab capabilities, stackups, laminates -----------------------
    subparsers.add_parser(
        "stackup-vendors",
        help="List the bundled fab vendors, with source and age",
    ).add_argument("--json", dest="json_out", action="store_true",
                   help="Print as JSON.")
    stackup_show_p = subparsers.add_parser(
        "stackup-show",
        help="Show one vendor's capabilities and stackup templates",
    )
    stackup_show_p.add_argument("vendor", help="e.g. jlcpcb, pcbway")
    stackup_show_p.add_argument("--json", dest="json_out",
                                action="store_true", help="Print as JSON.")
    stackup_profile_p = subparsers.add_parser(
        "stackup-profile",
        help="Emit a fab profile JSON for a bundled vendor",
        epilog=("Refuses when the vendor does not publish every minimum a "
                "fab profile needs, and names them. Nothing here will "
                "guess a design rule. Supply the missing ones with "
                "--fill min_hole_to_hole_mils=17.7 once the fab has told "
                "you; they are recorded as yours, not theirs."),
    )
    stackup_profile_p.add_argument("vendor", help="e.g. jlcpcb, pcbway")
    stackup_profile_p.add_argument(
        "--fill", action="append", metavar="NAME=MILS",
        help="Supply a minimum the vendor does not publish. Repeatable.")
    subparsers.add_parser(
        "laminates",
        help="Bundled laminate properties, with BOTH dielectric constants",
    ).add_argument("--json", dest="json_out", action="store_true",
                   help="Print as JSON.")

    dash_p = subparsers.add_parser(
        "dashboard",
        help="Launch the local web dashboard (http://127.0.0.1:8766).",
        epilog=(
            "Shows live MCP activity, performance and health. The "
            "in-Altium status form has an 'Open Dashboard' button that "
            "auto-launches this server's URL via a workspace sentinel."
        ),
    )
    dash_p.add_argument("--host", default="127.0.0.1")
    dash_p.add_argument("--port", type=int, default=8766)
    dash_p.add_argument("--debug", action="store_true")

    # altium365-login -- sign in to Nexar once for the Altium 365 tools
    subparsers.add_parser(
        "altium365-login",
        help=("Sign in to Nexar in the browser for the altium365_ tools "
              "(needs EDA_AGENT_NEXAR_APP_ID). The workspace route needs no "
              "sign-in: set EDA_AGENT_ALTIUM365_DOMAIN and EDA_AGENT_ALTIUM365_TOKEN."),
    )

    # sdk-reach -- how much of Altium's SDK the property tools reach
    sdk_p = subparsers.add_parser(
        "sdk-reach",
        help=("Count the properties Altium's SDK declares on each object type "
              "and the share obj_query/obj_modify reach. Reads the SDK index in "
              "reference/altium_sdk (or EDA_AGENT_SDK_INDEX)."),
    )
    sdk_p.add_argument("--detail", type=int, default=0, metavar="N",
                       help="List up to N properties not reached, per type.")
    sdk_p.add_argument("--json", action="store_true",
                       help="Every type with every name not reached, as JSON.")

    # stop-dashboard -- terminate the dashboard process by PID file
    stop_dash_p = subparsers.add_parser(
        "stop-dashboard",
        help="Stop the dashboard process (reads workspace/dashboard.pid).",
        epilog=("Use this when the dashboard was launched detached by the "
                "Altium script and you want to kill it without rebooting."),
    )

    # vote -- pairwise-preference vote UI in the browser
    vote_p = subparsers.add_parser(
        "vote",
        help="Launch the pairwise layout-preference vote UI in a browser.",
        epilog=(
            "Generates two layouts of the same plan; you click the "
            "better one. Builds training data for the quality model."
        ),
    )
    vote_p.add_argument("--plan", required=True, type=Path,
                        help="Path to the DesignPlan JSON to vote on.")
    vote_p.add_argument("--symbols", type=Path, default=None,
                        help="Symbol fixtures JSON for offline mode. "
                             "Omit to use the live Altium bridge.")
    vote_p.add_argument("--host", default="127.0.0.1")
    vote_p.add_argument("--port", type=int, default=8765)
    vote_p.add_argument("--debug", action="store_true")

    # review -- OFFLINE FALLBACK, opt-in schematic review (no Altium).
    # Not the preferred path: the live-Altium tools see connectivity this
    # reader can't. Disabled unless --offline / EDA_AGENT_HEADLESS_REVIEW=1.
    review_p = subparsers.add_parser(
        "review",
        help="Offline fallback design review of a .SchDoc/.PrjPcb (needs --offline).",
        epilog=(
            "Parses the file directly (no running Altium, no license) for "
            "the component-level subset only: missing MPN / datasheet, "
            "placeholder values, designator collisions. NOT the preferred "
            "path -- prefer the live tools when Altium is available. Opt-in: "
            "requires --offline (or EDA_AGENT_HEADLESS_REVIEW=1)."
        ),
    )
    review_p.add_argument("file", type=Path,
                          help="Path to a .SchDoc sheet or a .PrjPcb project "
                               "(reviews all its sheets).")
    review_p.add_argument("--offline", action="store_true",
                          help="Opt in to the no-Altium file-reader review. "
                               "Required (this review is off by default); the "
                               "live-Altium tools are preferred when a session "
                               "is available.")
    review_p.add_argument("--json", action="store_true",
                          help="Emit the full report as JSON.")
    review_p.add_argument("--sarif", action="store_true",
                          help="Emit SARIF 2.1.0 (for GitHub code scanning / "
                               "PR annotations).")
    review_p.add_argument("--fail-on", dest="fail_on", default="error",
                          choices=["error", "warning", "info", "never"],
                          help="Exit non-zero when a finding at or above this "
                               "severity exists (default: error).")

    # bom -- offline consolidated BOM (opt-in, no Altium).
    bom_p = subparsers.add_parser(
        "bom",
        help=("Offline consolidated BOM from a .SchDoc/.PrjPcb (no running "
              "Altium). Opt-in: requires --offline (or "
              "EDA_AGENT_HEADLESS_REVIEW=1). Prefer live proj_get_bom."))
    bom_p.add_argument("file", type=Path, help="A .SchDoc or .PrjPcb.")
    bom_p.add_argument("--offline", action="store_true",
                       help="Opt in to the no-Altium reader (required).")
    bom_p.add_argument("--csv", action="store_true", help="Emit CSV.")
    bom_p.add_argument("--json", action="store_true", help="Emit JSON.")

    # netlist -- offline geometric netlist + connectivity ERC (opt-in).
    net_p = subparsers.add_parser(
        "netlist",
        help=("Offline netlist reconstruction + connectivity ERC "
              "(single_pin_net, net_short) from a .SchDoc (no running "
              "Altium). Opt-in: requires --offline. Prefer live "
              "proj_get_nets/proj_run_erc."))
    net_p.add_argument("file", type=Path, help="A .SchDoc sheet.")
    net_p.add_argument("--offline", action="store_true",
                       help="Opt in to the no-Altium solver (required).")
    net_p.add_argument("--json", action="store_true", help="Emit JSON.")
    net_p.add_argument("--sarif", action="store_true",
                       help="Emit SARIF 2.1.0 (GitHub code scanning / PR "
                            "annotations).")
    net_p.add_argument("--fail-on", dest="fail_on", default="error",
                       choices=["error", "warning", "info", "never"],
                       help="Exit non-zero on a finding at/above this "
                            "severity (default: error).")

    # calc -- the offline engineering calculators, without an MCP client.
    calc_p = subparsers.add_parser(
        "calc",
        help=("Run an engineering calculator: `eda-agent calc fusing-current "
              "seconds=1 area_sq_mils=103`. `eda-agent calc` lists them; "
              "`eda-agent calc NAME --help` shows a calculator's inputs."),
    )
    calc_p.add_argument("name", nargs="?", help="Calculator name.")
    calc_p.add_argument("inputs", nargs="*", metavar="KEY=VALUE",
                        help="Inputs, named as the calculator's arguments.")
    calc_p.add_argument("--json", action="store_true", help="Emit JSON.")

    args = parser.parse_args()

    # Resolve the active profile (see profile.py) before anything else runs,
    # so naming.py / ipc7351.py's own EDA_AGENT_IPC_DIR reads transparently
    # pick up a profile's naming.json / company_goals.json. Safe at any point
    # before the first tool call: registration (just above, at import) only
    # decides WHICH tools exist, not what they read at call time.
    from . import profile as _profile
    _profile.apply_env()

    # Backend was already registered at import from EDA_AGENT_BACKEND. If
    # --backend asks for a different one, re-exec once with the env set so the
    # reload registers the right tool surface. MCP clients pass the backend via
    # env, not this flag, so their startup never re-execs. A sentinel prevents
    # an exec loop if the child somehow disagrees.
    # --toolset needs the same treatment for the same reason: the surface
    # is chosen at import, so changing it after parsing is too late.
    requested = getattr(args, "backend", None)
    requested_toolset = getattr(args, "toolset", None)
    needs_backend = bool(requested) and requested != ACTIVE_BACKEND
    needs_toolset = (bool(requested_toolset)
                     and requested_toolset != ACTIVE_TOOLSET)
    if ((needs_backend or needs_toolset)
            and not os.environ.get("_EDA_AGENT_BACKEND_REEXEC")):
        if needs_backend:
            os.environ["EDA_AGENT_BACKEND"] = requested
        if needs_toolset:
            os.environ["EDA_AGENT_TOOLSET"] = requested_toolset
        os.environ["_EDA_AGENT_BACKEND_REEXEC"] = "1"
        os.execv(sys.executable,
                 [sys.executable, "-m", "eda_agent.server", *sys.argv[1:]])

    if args.command is None or args.command == "serve":
        # Honour the flag whether it was given at the top level
        # (`eda-agent --no-dashboard`) or on the serve subcommand
        # (`eda-agent serve --no-dashboard`). Either form should work.
        no_dash = bool(
            getattr(args, "no_dashboard", False)
            or getattr(args, "headless", False)
        )
        return serve_mcp(no_dashboard=no_dash)

    if args.command == "scripts-path":
        return cli.cmd_scripts_path()
    if args.command == "install-scripts":
        return cli.cmd_install_scripts(dest=args.dest, force=args.force)
    if args.command in ("a365-login", "a365-logout"):
        from .engineering.a365 import TokenStore, interactive_login
        from .tools.engineering import a365_config
        try:
            if args.command == "a365-login":
                cfg = a365_config()
                store = TokenStore(cfg.token_store)
                store.save(interactive_login(
                    cfg, os.environ.get("EDA_AGENT_NEXAR_CLIENT_SECRET")))
                print("Altium 365 token stored for the current Windows user")
            else:
                store = TokenStore(Path.home() / "EDA Agent" / "a365_token.bin")
                store.clear()
                print("Altium 365 token removed")
            return 0
        except Exception as exc:
            print(f"Altium 365 authorization failed: {exc}", file=sys.stderr)
            return 1
    if args.command == "easyeda-extension":
        return cli.cmd_easyeda_extension(args.action, out=args.out)
    if args.command == "relaunch":
        return cli.cmd_relaunch(prjscr=args.prjscr, dry_run=args.dry_run,
                                force=args.force, restart=args.restart)
    if args.command == "dashboard":
        from .web.dashboard import main as dashboard_main
        return dashboard_main([
            "--host", args.host,
            "--port", str(args.port),
            *(["--debug"] if args.debug else []),
        ])
    if args.command == "stop-dashboard":
        # Read the PID written by the dashboard process and SIGTERM it.
        # No PID file = no dashboard running -> success (idempotent stop).
        import os as _os, signal as _sig
        pid_path = get_config().workspace_dir / "dashboard.pid"
        if not pid_path.exists():
            print("dashboard.pid not found -- dashboard not running "
                  "(or workspace mismatch).")
            return 0
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as e:
            print(f"could not read dashboard.pid: {e}")
            return 1
        try:
            if sys.platform == "win32":
                # On Windows, SIGTERM isn't honoured for native procs;
                # use TerminateProcess via taskkill for reliability.
                import subprocess as _sp
                from .noconsole import no_window
                _sp.run(["taskkill", "/PID", str(pid), "/F"],
                        capture_output=True, check=False, **no_window())
            else:
                _os.kill(pid, _sig.SIGTERM)
            print(f"stopped dashboard pid {pid}")
            try: pid_path.unlink()
            except OSError: pass
            return 0
        except Exception as e:
            print(f"could not stop dashboard pid {pid}: {e}")
            return 1
    if args.command == "altium365-login":
        import webbrowser
        from .altium365 import Altium365Error, browser_login, read_environment
        try:
            print(browser_login(read_environment(), webbrowser.open))
            return 0
        except Altium365Error as exc:
            print(f"Not signed in: {exc}")
            return 1
    if args.command == "sdk-reach":
        import json
        from .diag import sdk_reach
        path = sdk_reach.index_path()
        if not path.is_file():
            print(f"No SDK index at {path}. It is made from an Altium install "
                  "(reference/altium_sdk/dump_sdk.ps1); set EDA_AGENT_SDK_INDEX "
                  "to point at one.")
            return 1
        rows = sdk_reach.measure(sdk_reach.SdkIndex(path))
        if args.json:
            print(json.dumps(sdk_reach.as_dict(rows), indent=2))
        else:
            print(sdk_reach.report(rows, args.detail))
        return 0
    if args.command == "review":
        return _run_review(args)
    if args.command == "bom":
        return _run_bom(args)
    if args.command == "netlist":
        return _run_netlist(args)
    if args.command == "calc":
        return cli.cmd_calc(args.name, args.inputs, as_json=args.json)
    if args.command in ("health", "doctor"):
        from .diag.checks import format_report, overall_exit_code
        if args.command == "health":
            from .diag.health import run_health_checks
            checks = run_health_checks()
            title = "eda-agent health"
        else:
            from .diag.doctor import run_doctor_checks
            checks = run_doctor_checks(library_paths=args.library)
            title = "eda-agent doctor"

        emit_json = bool(getattr(args, "json", False))
        if emit_json:
            import json as _json
            payload = {
                "title": title,
                "checks": [
                    {
                        "name": c.name,
                        "status": c.status.value,
                        "message": c.message,
                        "fix": c.fix,
                        "severity": c.severity.value,
                    }
                    for c in checks
                ],
            }
            print(_json.dumps(payload, indent=2))
        else:
            print(format_report(checks, title=title))
        return overall_exit_code(checks)

    if args.command == "eco-execute":
        return cli.cmd_eco_execute(
            confirm=args.confirm,
            validate_only=args.validate_only,
            allow_confirm_dialogs=args.allow_confirm_dialogs,
            wait_first=args.wait_first,
            budget=args.budget,
        )

    if args.command == "perf":
        return cli.cmd_perf(as_json=args.json_out, limit=args.limit)
    if args.command == "sdk-coverage":
        return cli.cmd_sdk_coverage(as_json=args.json_out,
                                    detail=args.detail,
                                    sdk_json=args.sdk_json)
    if args.command == "sdk-probe":
        return cli.cmd_sdk_probe(designator=args.designator,
                                 properties=args.properties,
                                 doc_path=args.doc_path,
                                 as_json=args.json_out,
                                 sdk_json=args.sdk_json)
    if args.command == "sdk-symbols":
        return cli.cmd_sdk_symbols(refresh=args.refresh, name=args.name,
                                   as_json=args.json_out,
                                   sdk_json=args.sdk_json)
    if args.command == "profile-init":
        return cli.cmd_profile_init(args.name, here=args.here)
    if args.command == "profile-list":
        return cli.cmd_profile_list()
    if args.command == "profile-show":
        return cli.cmd_profile_show(as_json=args.json_out)
    if args.command == "profile-use":
        return cli.cmd_profile_use(args.name)
    if args.command == "profile-validate":
        return cli.cmd_profile_validate(name=args.name, here=args.here)
    if args.command == "profile-new-project":
        return cli.cmd_profile_new_project(args.template, args.dest, force=args.force)

    if args.command == "update":
        # --yes is the APPLY half (fast-forward, reinstall, copy scripts,
        # restart the loop). Without it the verb REPORTS what is stale
        # across remote, package, deployed scripts and the compiled copy.
        if getattr(args, "yes", False):
            return cli.cmd_update_apply(
                check_only=args.check_only, assume_yes=True,
                relaunch=not args.no_relaunch)
        return cli.cmd_update(pull=args.pull, fetch=args.fetch,
                              check_only=args.check_only,
                              force_scripts=args.force_scripts,
                              source=args.source, as_json=args.json_out)
    if args.command == "calc-cross-check":
        return cli.cmd_calc_cross_check(args.calculator, args.arg,
                                        as_json=args.json_out)
    if args.command == "stackup-vendors":
        return cli.cmd_stackup_vendors(as_json=args.json_out)
    if args.command == "stackup-show":
        return cli.cmd_stackup_show(args.vendor, as_json=args.json_out)
    if args.command == "stackup-profile":
        return cli.cmd_stackup_profile(args.vendor, fill=args.fill)
    if args.command == "laminates":
        return cli.cmd_laminates(as_json=args.json_out)

    if args.command in cli.CALC_COMMANDS:
        _, _, params = cli.CALC_COMMANDS[args.command]
        return cli.cmd_calc(
            args.command,
            {name: getattr(args, name) for name, _t, _d, _h in params},
            as_json=args.json_out)

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
