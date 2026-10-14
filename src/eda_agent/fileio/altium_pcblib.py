# SPDX-License-Identifier: Apache-2.0
"""Headless .PcbLib reader (library hygiene, no Altium running).

Sibling of altium_schlib.py, and scoped by the same rule: a ``.PcbLib`` is
an OLE compound document whose per-footprint ``Data`` stream is Altium's
BINARY primitive format, and that layout is NOT reverse-engineered here
(no guessing a binary layout without ground truth). What this reads is the
ASCII around it, measured from a real library written by Altium:

    FileHeader                     "PCB 6.0 Binary Library File"
    Library/ComponentParamsTOC     Name=..|Pad Count=..|Height=..|Description=..
    <Footprint>/Parameters         |PATTERN=..|HEIGHT=..|DESCRIPTION=..|...
    <Footprint>/Data               <len><pascal-len><name> then binary

That covers the header level: which footprints exist, how many pads each
has, its height and description. Enough for the library-hygiene checks
worth running in CI (an undescribed footprint, a pad count that disagrees
with its symbol's pin count), which is the same bar altium_schlib.py set.

Pad GEOMETRY is deliberately absent: it lives in the binary region, and
reading it live through the bridge (library.get_footprint_pads) is both
exact and already implemented. This reader is for when Altium is not
running at all.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

#: Top-level storages that are not footprints. Every other storage holding
#: a ``Data`` stream is one, which is how altium_schlib.py finds components
#: too; listing the exclusions is more robust than trying to pattern-match
#: footprint names, which are arbitrary.
_NON_FOOTPRINT_STORAGES = {"Library", "FileVersionInfo", "FileHeader"}


def _length_prefixed_text(data: bytes) -> str:
    """Altium's ``<uint32 length><payload>`` ASCII block, trailing NUL stripped."""
    if len(data) < 4:
        return ""
    length = struct.unpack("<I", data[:4])[0]
    return data[4:4 + length].rstrip(b"\x00").decode("latin-1", "replace")


def _fields(text: str, separator: str = "|") -> dict[str, str]:
    out: dict[str, str] = {}
    for part in text.split(separator):
        if "=" in part:
            key, value = part.split("=", 1)
            key = key.strip()
            if key:
                out[key] = value.strip()
    return out


def _read_stream(ole, path: list[str]) -> bytes:
    try:
        return ole.openstream(path).read()
    except OSError:
        return b""


def _toc_by_name(ole) -> dict[str, dict[str, str]]:
    """Library/ComponentParamsTOC, one ``Name=..|Pad Count=..|..`` line per
    footprint, keyed by name.

    Measured against a single-footprint library, so the per-line split is
    the part taken on trust; it is treated as enrichment rather than as the
    list of footprints, which comes from the storages themselves.
    """
    text = _length_prefixed_text(_read_stream(ole, ["Library", "ComponentParamsTOC", "Data"]))
    out: dict[str, dict[str, str]] = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line.strip():
            continue
        row = _fields(line)
        name = row.get("Name", "")
        if name:
            out[name] = row
    return out


def _as_int(value: str) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def read_pcblib_footprints(path: str | Path) -> list[dict[str, Any]]:
    """Return each footprint's header: ``{name, pattern, description,
    pad_count, height}``.

    ``pad_count`` is None when the library's table of contents does not
    carry one, rather than 0, because "no pads" and "not stated" are
    different findings to a hygiene check.

    Raises ValueError if the file is not an OLE compound document.
    """
    import olefile

    path = str(path)
    if not olefile.isOleFile(path):
        raise ValueError(f"not an OLE compound file (not a .PcbLib?): {path}")
    ole = olefile.OleFileIO(path)
    try:
        storages = sorted(
            entry[0] for entry in ole.listdir()
            if len(entry) == 2 and entry[1] == "Data"
            and entry[0] not in _NON_FOOTPRINT_STORAGES
        )
        toc = _toc_by_name(ole)
        out: list[dict[str, Any]] = []
        for name in storages:
            params = _fields(_length_prefixed_text(
                _read_stream(ole, [name, "Parameters"])))
            row = toc.get(name, {})
            out.append({
                "name": name,
                # PATTERN is the footprint's own name for itself; it has
                # matched the storage name in every sample seen, but it is
                # reported rather than assumed.
                "pattern": params.get("PATTERN", "") or name,
                "description": params.get("DESCRIPTION", "") or row.get("Description", ""),
                "pad_count": _as_int(row.get("Pad Count", "")),
                "height": params.get("HEIGHT", "") or row.get("Height", ""),
            })
        return out
    finally:
        ole.close()


def read_pcblib_header(path: str | Path) -> str:
    """The library's format string, e.g. ``PCB 6.0 Binary Library File``.

    Useful as a cheap "is this really a PcbLib" check before a caller
    commits to a sweep over a directory of mixed files.
    """
    import olefile

    path = str(path)
    if not olefile.isOleFile(path):
        raise ValueError(f"not an OLE compound file (not a .PcbLib?): {path}")
    ole = olefile.OleFileIO(path)
    try:
        raw = _read_stream(ole, ["FileHeader"])
    finally:
        ole.close()
    # <uint32 total><uint8 pascal-length><text>: the inner Pascal string is
    # what carries the readable name.
    if len(raw) < 5:
        return ""
    inner_length = raw[4]
    return raw[5:5 + inner_length].decode("latin-1", "replace")
