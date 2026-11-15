# SPDX-License-Identifier: Apache-2.0
"""Hand the app icon to the Altium status window.

Altium's script form loader drops ``Icon.Data`` from a .dfm, and an icon
loaded by another process dies with that process, so the status window
loads the icon itself (ApplyAppIcon in StatusForm.pas) from a file in the
IPC workspace. This module puts that file there.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .atomicfile import replace_with_retry

logger = logging.getLogger(__name__)

#: The name StatusForm.pas looks for (APP_ICON_FILE) in the workspace.
ICON_NAME = "eda-agent.ico"

#: The icon that ships with the package (scripts/build_icons.py builds it).
PACKAGED_ICON = Path(__file__).resolve().parent / "web" / "dashboard_static" / ICON_NAME


def publish_icon(workspace_dir: Path) -> bool:
    """Copy the app icon into ``workspace_dir`` unless an identical copy is
    already there. Returns True when the workspace holds the current icon.

    Never raises: the icon is cosmetic, the workspace is not.
    """
    target = Path(workspace_dir) / ICON_NAME
    try:
        data = PACKAGED_ICON.read_bytes()
        if target.is_file() and target.read_bytes() == data:
            return True
        tmp = target.with_name(ICON_NAME + ".tmp")
        shutil.copyfile(PACKAGED_ICON, tmp)
        replace_with_retry(tmp, target)
        return True
    except OSError as exc:
        logger.debug("could not publish the app icon to %s: %s", target, exc)
        return False
