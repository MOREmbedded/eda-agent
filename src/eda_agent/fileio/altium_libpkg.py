# SPDX-License-Identifier: Apache-2.0
"""Write an Altium integrated library package (.LibPkg).

A LibPkg is the project that compiles a set of .SchLib and .PcbLib files
into one .IntLib. Unlike the libraries it groups, it is a plain INI text
file, so writing one does not touch Altium's binary formats. The layout is
the one Altium itself writes, measured from 44 packages Altium saved on
this machine: a ``[Design]`` section, a ``[Preferences]`` section, then
one ``[DocumentN]`` per library, numbered from 1, each ``DocumentPath``
relative to the package (backslash-separated) when the library is on the
same drive and absolute otherwise. UTF-8 with a byte-order mark and CRLF
line ends, as Altium saves it.

Only the keys a fresh package needs are written. Altium fills in the rest
(output groups, search paths) with its defaults the first time it saves.
"""

from __future__ import annotations

import ntpath
import os
from pathlib import Path

LIBRARY_SUFFIXES = (".schlib", ".pcblib", ".pcb3dlib", ".dblib", ".svndblib")

_DESIGN = [
    ("Version", "1.0"),
    ("HierarchyMode", "0"),
    ("ChannelRoomNamingStyle", "0"),
    ("ReleasesFolder", ""),
    ("ChannelDesignatorFormatString", "$Component_$RoomName"),
    ("ChannelRoomLevelSeperator", "_"),
    ("OpenOutputs", "1"),
    ("ArchiveProject", "0"),
    ("TimestampOutput", "0"),
    ("SeparateFolders", "0"),
    ("TemplateLocationPath", ""),
    ("DefaultConfiguration", "Default - All Constraints"),
    ("UserID", "0xFFFFFFFF"),
    ("OutputPath", ""),
    ("LogFolderPath", ""),
    ("ManagedProjectGUID", ""),
]

_DOCUMENT = [
    ("AnnotationEnabled", "1"),
    ("AnnotateStartValue", "1"),
    ("AnnotationIndexControlEnabled", "0"),
    ("AnnotateSuffix", ""),
    ("AnnotateScope", "All"),
    ("AnnotateOrder", "-1"),
    ("DoLibraryUpdate", "1"),
    ("DoDatabaseUpdate", "1"),
    ("ClassGenCCAutoEnabled", "1"),
    ("ClassGenCCAutoRoomEnabled", "0"),
    ("ClassGenNCAutoScope", "None"),
    ("DItemRevisionGUID", ""),
    ("GenerateClassCluster", "0"),
    ("DocumentUniqueId", ""),
]


def document_path(libpkg: Path, doc: Path) -> str:
    """``doc`` as the LibPkg stores it: relative to the package's folder
    when on the same drive, else absolute; Windows separators."""
    base = str(libpkg.resolve().parent)
    target = str(doc.resolve())
    if ntpath.splitdrive(base)[0].lower() == ntpath.splitdrive(target)[0].lower():
        try:
            return os.path.relpath(target, base).replace("/", "\\")
        except ValueError:
            pass
    return target.replace("/", "\\")


def render_libpkg(libpkg: Path, documents: list[Path]) -> str:
    """The package text for ``documents``, CRLF line ends, no BOM."""
    lines = ["[Design]"] + [f"{k}={v}" for k, v in _DESIGN]
    lines += ["", "[Preferences]", "PrefsVaultGUID=", "PrefsRevisionGUID="]
    for i, doc in enumerate(documents, start=1):
        lines += ["", f"[Document{i}]", f"DocumentPath={document_path(libpkg, doc)}"]
        lines += [f"{k}={v}" for k, v in _DOCUMENT]
    return "\r\n".join(lines) + "\r\n"


def write_libpkg(libpkg_path: str | Path, documents: list[str | Path],
                 overwrite: bool = False) -> dict:
    """Write ``libpkg_path`` grouping ``documents``.

    Raises ValueError for a path that is not a .LibPkg, an existing file
    without ``overwrite``, no documents, a document that does not exist or
    is not a library, or the same library twice."""
    pkg = Path(libpkg_path)
    if pkg.suffix.lower() != ".libpkg":
        raise ValueError(f"not a .LibPkg path: {pkg}")
    if pkg.exists() and not overwrite:
        raise ValueError(f"{pkg} exists; pass overwrite=True to replace it")
    if not documents:
        raise ValueError("no documents to group")
    docs: list[Path] = []
    seen: set[str] = set()
    for d in documents:
        p = Path(d)
        if p.suffix.lower() not in LIBRARY_SUFFIXES:
            raise ValueError(f"not a library file: {p}")
        if not p.is_file():
            raise ValueError(f"no such file: {p}")
        key = str(p.resolve()).lower()
        if key in seen:
            raise ValueError(f"listed twice: {p}")
        seen.add(key)
        docs.append(p)
    pkg.parent.mkdir(parents=True, exist_ok=True)
    text = render_libpkg(pkg, docs)
    pkg.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    return {"libpkg_path": str(pkg),
            "documents": [document_path(pkg, d) for d in docs]}


__all__ = ["document_path", "render_libpkg", "write_libpkg"]
