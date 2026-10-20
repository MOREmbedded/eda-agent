# SPDX-License-Identifier: Apache-2.0
"""Copies of a single library file, taken before it is edited.

``app_checkpoint`` snapshots the focused *project*, which misses a library
opened on its own or kept outside the project folder. This keeps one
library's history instead: each backup is a byte copy under
``<workspace>/library_backups/<name>-<path hash>/``, named by the UTC time
it was taken, with a JSON sidecar holding the source path, size and
SHA-256. A backup identical to the newest one is not stored twice.

Restoring first backs up the file it is about to replace, so a restore can
itself be undone. Altium keeps an open library in memory and writes that
back on save, so the library has to be closed in Altium before a restore
and reopened (``lib_reload_library``) after it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .atomicfile import replace_with_retry

LIBRARY_SUFFIXES = (".schlib", ".pcblib", ".pcb3dlib", ".intlib", ".dblib",
                    ".libpkg")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_dir(workspace_dir: Path, library: Path) -> Path:
    key = hashlib.sha256(str(library.resolve()).lower().encode("utf-8")).hexdigest()[:10]
    return Path(workspace_dir) / "library_backups" / f"{library.stem}-{key}"


def _entries(folder: Path) -> list[dict[str, Any]]:
    out = []
    for meta in sorted(folder.glob("*.json")):
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        copy = meta.with_suffix(info.get("suffix", ""))
        if copy.is_file():
            info["backup_path"] = str(copy)
            out.append(info)
    return out


def backup_library(library_path: str | Path, workspace_dir: str | Path,
                   label: str = "") -> dict[str, Any]:
    """Copy the library into its backup folder; ``{backup_path, created,
    sha256, bytes, deduplicated}``. Raises ValueError for a missing file or
    one that is not a library."""
    lib = Path(library_path)
    if lib.suffix.lower() not in LIBRARY_SUFFIXES:
        raise ValueError(f"not a library file: {lib}")
    if not lib.is_file():
        raise ValueError(f"no such file: {lib}")
    folder = backup_dir(Path(workspace_dir), lib)
    folder.mkdir(parents=True, exist_ok=True)
    digest = _sha256(lib)
    existing = _entries(folder)
    if existing and existing[-1].get("sha256") == digest:
        return {**existing[-1], "deduplicated": True}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    copy = folder / f"{stamp}{lib.suffix}"
    fd, tmp = tempfile.mkstemp(dir=str(folder), suffix=".tmp")
    try:
        with open(fd, "wb") as dst, lib.open("rb") as src:
            shutil.copyfileobj(src, dst)
        replace_with_retry(tmp, copy)
    finally:
        Path(tmp).unlink(missing_ok=True)
    info = {"source": str(lib.resolve()), "created": stamp, "label": label,
            "sha256": digest, "bytes": copy.stat().st_size, "suffix": lib.suffix}
    copy.with_suffix(".json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return {**info, "backup_path": str(copy), "deduplicated": False}


def list_backups(workspace_dir: str | Path,
                 library_path: Optional[str | Path] = None) -> list[dict[str, Any]]:
    """Backups oldest first; of one library when ``library_path`` is given."""
    root = Path(workspace_dir) / "library_backups"
    if library_path:
        folders = [backup_dir(Path(workspace_dir), Path(library_path))]
    else:
        folders = sorted(p for p in root.glob("*") if p.is_dir()) if root.is_dir() else []
    out: list[dict[str, Any]] = []
    for folder in folders:
        if folder.is_dir():
            out.extend(_entries(folder))
    return out


def restore_backup(backup_path: str | Path, workspace_dir: str | Path) -> dict[str, Any]:
    """Put a backup back over its source library, backing the current file
    up first. ``{restored_to, from_backup, previous}``; ``previous`` is the
    backup of what was replaced (None when the source no longer existed).
    Raises ValueError for a path that is not a backup taken here, or one
    whose bytes no longer match its recorded hash."""
    copy = Path(backup_path)
    meta = copy.with_suffix(".json")
    root = (Path(workspace_dir) / "library_backups").resolve()
    if root not in copy.resolve().parents or not meta.is_file() or not copy.is_file():
        raise ValueError(f"not a library backup taken here: {copy}")
    info = json.loads(meta.read_text(encoding="utf-8"))
    if _sha256(copy) != info.get("sha256"):
        raise ValueError(f"backup {copy} does not match its recorded hash; not restored")
    target = Path(info["source"])
    previous = None
    if target.is_file():
        previous = backup_library(target, workspace_dir, label=f"before restoring {copy.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with open(fd, "wb") as dst, copy.open("rb") as src:
            shutil.copyfileobj(src, dst)
        replace_with_retry(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return {"restored_to": str(target), "from_backup": str(copy), "previous": previous}


__all__ = ["backup_dir", "backup_library", "list_backups", "restore_backup"]
