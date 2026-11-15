# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""EDA Agent - MCP server bridging Altium Designer to MCP clients."""

__version__ = "0.8.2"


def _let_go_of_the_launcher() -> None:
    """Take the launcher off ``sys.path`` before anything reads metadata.

    pip's eda-agent.exe is a zip with a ``__main__.py`` that Python runs,
    so the launcher is ``sys.path[0]``. The first importlib.metadata
    lookup opens every zip on the path and caches it open for the life
    of the process, and Windows then refuses to rename, replace or
    delete eda-agent.exe (WinError 32). An update run from the launcher,
    or with a server started from it, could not set it aside, and pip
    failed halfway through and left eda-agent uninstalled. The zip has
    nothing on it but the ``__main__`` that is already running.
    """
    import os
    import sys

    first = sys.path[0] if sys.path else ""
    if not (first.lower().endswith(".exe") and os.path.isfile(first)):
        return
    del sys.path[0]
    if "importlib.metadata" in sys.modules:
        # Already looked through: drop the open zip it cached.
        try:
            sys.modules["importlib.metadata"].FastPath.__new__.cache_clear()
        except AttributeError:
            pass


_let_go_of_the_launcher()
