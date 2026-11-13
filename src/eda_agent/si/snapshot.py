# SPDX-License-Identifier: Apache-2.0
"""Board snapshots: the board and its stackup, read once, shared by jobs.

Every analysis of a board (line models, net channels, PDN, thermal, the
3D field model) needs the same two things from Altium: the copper
(a LayoutBoard) and the stackup. A snapshot keeps both in one folder
named by their content hash, so jobs that cite the same snapshot are
provably about the same board, a re-read of an unchanged board lands in
the same folder, and the board need not be read again for the next job.

A snapshot also records the board file's size and modification time on
disk. When the file has been saved since, :func:`status` reports the
snapshot as stale, and so does every result made from it.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from ..layout.model import LayoutBoard

INDEX = "snapshot.json"


def snapshots_dir() -> Path:
    from ..simulation.jobs import jobs_dir
    return jobs_dir() / "_snapshots"


def _file_stamp(path: str) -> Optional[dict[str, Any]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {"size": st.st_size, "mtime": st.st_mtime}


def save(board: LayoutBoard, stackup: Optional[dict], source_file: str = "") -> dict[str, Any]:
    """Keep ``board`` and ``stackup``; returns the snapshot's record."""
    board_text = json.dumps(board.to_dict(), sort_keys=True, separators=(",", ":"))
    stack_text = json.dumps(stackup or {}, sort_keys=True, indent=1, default=str)
    digest = hashlib.sha256((board_text + "\n" + stack_text).encode("utf-8")).hexdigest()
    sid = digest[:16]
    folder = snapshots_dir() / sid
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / "board.json").exists():
        (folder / "board.json").write_text(board_text, encoding="utf-8")
    if not (folder / "stackup.json").exists():
        (folder / "stackup.json").write_text(stack_text, encoding="utf-8")
    record = {
        "id": sid, "sha256": digest, "source_file": source_file or board.source,
        "read_at": time.time(), "file_stamp": _file_stamp(source_file or board.source),
        "has_stackup": bool(stackup and stackup.get("layers")),
        "counts": {"pads": len(board.pads), "tracks": len(board.tracks), "arcs": len(board.arcs),
                   "vias": len(board.vias), "regions": len(board.regions),
                   "diff_pairs": len(board.diff_pairs)},
        "board": str(folder / "board.json"), "stackup": str(folder / "stackup.json"),
    }
    (folder / INDEX).write_text(json.dumps(record, indent=1), encoding="utf-8")
    return record


def load(sid: str) -> dict[str, Any]:
    path = snapshots_dir() / sid / INDEX
    if not path.is_file():
        known = ", ".join(sorted(p.name for p in snapshots_dir().glob("*") if p.is_dir())) or "none"
        raise KeyError(f"no snapshot {sid!r}; known: {known}")
    return json.loads(path.read_text(encoding="utf-8"))


def status(record: dict[str, Any]) -> dict[str, Any]:
    """Is the board file still as it was when the snapshot was read?"""
    then, now = record.get("file_stamp"), _file_stamp(record.get("source_file", ""))
    if then is None or now is None:
        return {"stale": None, "why": "the board file is not reachable from here"}
    if then != now:
        return {"stale": True, "why": "the board file was saved after this snapshot; "
                                      "take a new one for current results"}
    return {"stale": False, "why": "the board file is unchanged since the snapshot"}


def listing() -> list[dict[str, Any]]:
    out = []
    root = snapshots_dir()
    if not root.is_dir():
        return out
    for p in sorted(root.glob(f"*/{INDEX}"), key=lambda q: q.stat().st_mtime, reverse=True):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({**rec, **status(rec)})
    return out
