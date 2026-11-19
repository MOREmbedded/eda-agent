# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The ``eda-agent`` command.

Importing eda_agent.server registers every tool, which takes seconds: a
pydantic model and a JSON schema for each of several hundred tools. The
MCP server needs that; ``eda-agent update`` or ``eda-agent std-list`` do
not. This entry point marks the commands that never touch the tools, so
server.py skips the registration for them, and hands everything else to
server.main unchanged.

A command missing from LIGHT_COMMANDS is never wrong, only slower: it
registers the tools as it always did. The first argument that is not an
option names the command, so a top-level option with a value
(``--backend kicad update``) makes the guess fail safe, to the full path.
"""

from __future__ import annotations

import sys

#: Set before eda_agent.server is imported; server.py reads it once.
DEFER_TOOLS = False

#: Commands that run without the tool registry.
LIGHT_COMMANDS = frozenset({
    "update", "scripts-path", "install-scripts", "compute-status",
    "relaunch", "stop-dashboard", "perf", "features", "install-deps",
    "profile-init", "profile-list", "profile-show", "profile-use",
    "profile-validate", "mode-list", "mode-show", "mode-use", "mode-init",
    "phases-show", "phases-enable", "phases-disable",
    "router-show", "router-use",
    "std-list", "std-rules", "std-floors",
    "fab-list", "fab-rules", "fab-compare", "fab-refresh",
    "stackup-vendors", "stackup-show", "stackup-profile", "laminates",
})


def command_of(argv: list[str]) -> str | None:
    """The subcommand argv names, or None for the server or a bare option."""
    for arg in argv:
        if not arg.startswith("-"):
            return arg
    return None


def defers_tools(argv: list[str]) -> bool:
    if any(a in ("-h", "--help") for a in argv) and command_of(argv) is None:
        return True
    return command_of(argv) in LIGHT_COMMANDS


def main() -> int:
    global DEFER_TOOLS
    DEFER_TOOLS = defers_tools(sys.argv[1:])
    from .server import main as server_main
    return server_main()


if __name__ == "__main__":
    sys.exit(main())
