# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""What the bridge's window asks to open through the link file.

The window's Dashboard button writes a URL to ``open_dashboard.url``; the
process that takes the file opens it. Only a web address is opened, and
anything else in the file is ignored. Upstream's ``help:<setting>`` links
opened a help page drawn from pictures of upstream's window, which this
fork's window does not match, so they are not followed here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def link_target(text: str, workspace_dir: Path) -> Optional[str]:
    """The address to open for what the link file held: a web address as
    it is, anything else None."""
    text = (text or "").strip()
    if text.startswith(("http://", "https://")):
        return text
    return None
