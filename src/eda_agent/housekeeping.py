# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Keep the workspace from growing without bound.

Nothing trimmed the workspace before this: activity.log and
bridge_trace.log were append-only from the first install, every
checkpoint ever taken was kept, and so was every screenshot. Six months
of use came to 850 MB.

Runs once when a server starts. Several servers can start together on one
workspace, so every step tolerates another process doing the same thing
at the same moment: a rename that loses the race, or a file already gone,
is skipped rather than reported.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .atomicfile import replace_with_retry

logger = logging.getLogger("eda_agent.housekeeping")

#: A log past this size is renamed to ``<name>.1`` (replacing the previous
#: one) and a fresh file starts with the last LOG_KEEP_TAIL_BYTES of it.
#: The tail matters: sessions read the end of bridge_trace.log to see
#: whether another session is using the bridge, and a server starting
#: while one is would otherwise hand them an empty file. Both writers
#: open the log per line, so a line appended in the instant between the
#: two renames can be lost; nothing else is. The dashboard's tailer
#: replays from the start when the file shrinks.
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_KEEP_TAIL_BYTES = 256 * 1024
LOG_NAMES = ("activity.log", "bridge_trace.log")

#: A checkpoint is deleted only when it is past the newest
#: CHECKPOINT_KEEP AND older than CHECKPOINT_MAX_AGE_DAYS.
CHECKPOINT_KEEP = 20
CHECKPOINT_MAX_AGE_DAYS = 30

#: app_screenshot's default files in the workspace root. Screenshots
#: saved to a path the caller chose are never touched.
SCREENSHOT_GLOB = "altium_*.png"
SCREENSHOT_MAX_AGE_DAYS = 30


def rotate_log(path: Path, max_bytes: int = LOG_MAX_BYTES,
               keep_tail: int = LOG_KEEP_TAIL_BYTES) -> bool:
    """Move ``path`` to ``path.1`` when it is over ``max_bytes``, leaving
    its last ``keep_tail`` bytes (whole lines) in a fresh ``path``."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        size = path.stat().st_size
        if size <= max_bytes:
            return False
        with open(path, "rb") as f:
            f.seek(max(0, size - keep_tail))
            tail = f.read()
        cut = tail.find(b"\n")
        tail = tail[cut + 1:] if cut >= 0 else b""
        tmp.write_bytes(tail)
        if path.stat().st_size <= max_bytes:
            # another server rotated it while the tail was read
            tmp.unlink()
            return False
        replace_with_retry(path, path.with_name(path.name + ".1"))
        replace_with_retry(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def prune_screenshots(workspace: Path,
                      max_age_days: float = SCREENSHOT_MAX_AGE_DAYS) -> int:
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for png in workspace.glob(SCREENSHOT_GLOB):
        try:
            if png.is_file() and png.stat().st_mtime < cutoff:
                png.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def run(workspace: Path) -> dict:
    """Trim the logs, checkpoints and screenshots. Never raises."""
    report = {"rotated": [], "checkpoints_removed": 0, "screenshots_removed": 0}
    workspace = Path(workspace)
    for name in LOG_NAMES:
        if rotate_log(workspace / name):
            report["rotated"].append(name)
    try:
        from .checkpoint import CheckpointStore
        report["checkpoints_removed"] = len(
            CheckpointStore(workspace / "checkpoints").prune_aged(
                CHECKPOINT_KEEP, CHECKPOINT_MAX_AGE_DAYS))
    except Exception as e:  # noqa: BLE001 - housekeeping must not stop a server
        logger.debug("checkpoint prune failed: %s", e)
    report["screenshots_removed"] = prune_screenshots(workspace)
    if report["rotated"] or report["checkpoints_removed"] or report["screenshots_removed"]:
        logger.info("workspace housekeeping: %s", report)
    return report
