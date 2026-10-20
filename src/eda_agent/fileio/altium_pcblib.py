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

The one binary payload read here is the embedded 3D models, because their
container is not Altium's primitive format but plain zlib around a STEP file,
indexed by an ASCII table (measured on two vendor libraries written by
Altium, one model and eleven):

    Library/Models/Header          <uint32 count>
    Library/Models/Data            count x <uint32 length><NAME=..|ID=..|.. NUL>
    Library/Models/<i>             zlib(STEP text) for the i-th Data record

Pad GEOMETRY is deliberately absent: it lives in the binary region, and
reading it live through the bridge (library.get_footprint_pads) is both
exact and already implemented. This reader is for when Altium is not
running at all.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from ..units import MM_PER_MIL

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


def _model_records(data: bytes) -> list[dict[str, str]]:
    """Library/Models/Data: back-to-back ``<uint32 length><fields NUL>``
    records, one per embedded model, in stream order."""
    out: list[dict[str, str]] = []
    pos = 0
    while pos + 4 <= len(data):
        length = struct.unpack("<I", data[pos:pos + 4])[0]
        if length == 0 or pos + 4 + length > len(data):
            break
        text = data[pos + 4:pos + 4 + length].rstrip(b"\x00").decode("latin-1", "replace")
        out.append(_fields(text))
        pos += 4 + length
    return out


def _open_pcblib(path: str | Path):
    import olefile

    path = str(path)
    if not olefile.isOleFile(path):
        raise ValueError(f"not an OLE compound file (not a .PcbLib?): {path}")
    return olefile.OleFileIO(path)


def _models(ole) -> list[dict[str, Any]]:
    records = _model_records(_read_stream(ole, ["Library", "Models", "Data"]))
    out: list[dict[str, Any]] = []
    for i, rec in enumerate(records):
        out.append({
            "index": i,
            "name": rec.get("NAME", ""),
            "id": rec.get("ID", ""),
            "embedded": rec.get("EMBED", "").upper() == "TRUE",
            "rotation_deg": [float(rec.get(k, "0") or 0) for k in ("ROTX", "ROTY", "ROTZ")],
            # DZ is in Altium's internal unit, 1/10000 mil.
            "dz_mm": round(float(rec.get("DZ", "0") or 0) / 10000.0 * MM_PER_MIL, 4),
            "checksum": rec.get("CHECKSUM", ""),
            "stream": ole.exists(["Library", "Models", str(i)]),
        })
    return out


def read_pcblib_models(path: str | Path) -> list[dict[str, Any]]:
    """The library's embedded 3D models: ``{index, name, id, embedded,
    rotation_deg, dz_mm, checksum, stream}``. ``stream`` is False when the
    table names a model whose data is missing. Raises ValueError if the
    file is not an OLE compound document."""
    ole = _open_pcblib(path)
    try:
        return _models(ole)
    finally:
        ole.close()


def _safe_filename(name: str, index: int) -> str:
    stem = "".join(c if c.isalnum() or c in "-_. " else "_" for c in name).strip(" .")
    if not stem:
        stem = f"model_{index}"
    if not stem.lower().endswith((".step", ".stp")):
        stem += ".step"
    return stem


def extract_pcblib_models(path: str | Path, out_dir: str | Path,
                          names: list[str] | None = None,
                          overwrite: bool = False) -> dict[str, Any]:
    """Write the embedded STEP models to ``out_dir``; ``names`` limits it to
    those model names (case-insensitive). A model whose stream is missing or
    is not zlib is reported in ``failed``, an existing file left alone in
    ``skipped`` unless ``overwrite``."""
    import zlib

    want = {n.lower() for n in names or []}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    used: set[str] = set()
    ole = _open_pcblib(path)
    try:
        for m in _models(ole):
            if want and m["name"].lower() not in want:
                continue
            filename = _safe_filename(m["name"], m["index"])
            if filename.lower() in used:
                stem, dot, ext = filename.rpartition(".")
                filename = f"{stem}_{m['index']}.{ext}"
            used.add(filename.lower())
            target = out / filename
            if not m["stream"]:
                failed.append({"name": m["name"], "reason": "model data stream missing"})
                continue
            try:
                step = zlib.decompress(_read_stream(ole, ["Library", "Models", str(m["index"])]))
            except zlib.error as exc:
                failed.append({"name": m["name"], "reason": f"not zlib data: {exc}"})
                continue
            if target.exists() and not overwrite:
                skipped.append({"name": m["name"], "path": str(target),
                                "reason": "file exists (overwrite=False)"})
                continue
            target.write_bytes(step)
            written.append({"name": m["name"], "path": str(target), "bytes": len(step),
                            "is_step": step.startswith(b"ISO-10303-21")})
    finally:
        ole.close()
    missing = sorted(want - {m["name"].lower() for m in written + skipped + failed})
    return {"written": written, "skipped": skipped, "failed": failed,
            "not_found": missing}
