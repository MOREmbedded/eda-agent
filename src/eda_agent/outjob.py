# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Clone an OutJob into another project, keeping its owner's export style.

Altium's scripting can create an empty OutJob and run one, but cannot add
an output to it, so a project without an OutJob could not produce
Gerbers, drill files or a pick-and-place from this server. Every user
exports differently, so nothing here invents outputs: an existing OutJob,
the user's own or one Altium ships, is copied and pointed at the target
project. Approved by George 2026-10-23 as the third exception to the rule
that design files are reached only through the bridge. It writes a NEW
file and never edits an existing one.

What changes in the copy, and nothing else:

* each output's document (``OutputDocumentPathN``) is pointed at the
  target project's document of the same kind, when there is exactly one;
  anything ambiguous is left and reported, never guessed;
* each container writes to ``output_dir`` and is not release managed,
  since a release-managed container writes nothing outside a release
  (measured 2026-10-17);
* the vault identity in ``[OutputJobFile]`` is cleared, so the copy is
  not taken for the released item it came from.

Bytes outside those lines are kept as they were: encoding, byte-order
mark and line endings included.
"""
from __future__ import annotations

import os
import re
from pathlib import PureWindowsPath
from typing import Any, Iterable

_HEADER = re.compile(r"^\[(?P<name>[^\]\r\n]+)\]\s*$")
_KEY = re.compile(r"^(?P<key>[A-Za-z_]+?)(?P<n>\d+)=(?P<value>.*)$")
_DOC_PATH = re.compile(r"^OutputDocumentPath(?P<n>\d+)=(?P<value>.*)$")
_VAULT_KEYS = ("VaultGUID", "ItemGUID", "ItemHRID", "RevisionGUID",
               "RevisionId", "VaultHRID", "AutoItemHRID", "NextRevId",
               "FolderGUID", "LifeCycleDefinitionGUID",
               "RevisionNamingSchemeGUID")
_CONTAINER_SECTIONS = ("publishsettings", "generatedfilessettings")
#: Keys that hold where a container writes. A value naming a file keeps
#: its file name; one naming a folder becomes output_dir.
_WHERE_KEYS = ("OutputFilePath", "RelativeOutputPath")


def _split(data: bytes) -> tuple[bytes, list[str], str]:
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    body = data[len(bom):]
    try:
        text, encoding = body.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text, encoding = body.decode("cp1252"), "cp1252"
    return bom, text.splitlines(keepends=True), encoding


def _eol(line: str) -> tuple[str, str]:
    stripped = line.rstrip("\r\n")
    return stripped, line[len(stripped):]


def _is_folder(value: str) -> bool:
    return not value or value.endswith(("\\", "/")) or not PureWindowsPath(value).suffix


def clone(data: bytes, project_path: str, documents: Iterable[str],
          output_dir: str) -> tuple[bytes, dict[str, Any]]:
    """The OutJob ``data`` pointed at another project.

    Args:
        data: the source .OutJob's bytes.
        project_path: the target .PrjPcb.
        documents: the target project's document paths.
        output_dir: absolute folder every container writes into.

    Returns:
        The new bytes and a report: ``retargeted`` (output number, old,
        new), ``unmapped`` (output number, old, why) and ``containers``.
    """
    bom, lines, encoding = _split(data)
    project_dir = os.path.dirname(project_path)
    by_ext: dict[str, list[str]] = {}
    for doc in documents:
        by_ext.setdefault(PureWindowsPath(doc).suffix.lower(), []).append(doc)

    retargeted, unmapped, containers = [], [], set()
    section = ""
    out = []
    for line in lines:
        text, eol = _eol(line)
        head = _HEADER.match(text)
        if head:
            section = head.group("name").lower()
            out.append(line)
            continue

        if section == "outputjobfile":
            key = text.split("=", 1)[0]
            if key in _VAULT_KEYS and "=" in text:
                out.append(f"{key}={eol}")
                continue

        doc = _DOC_PATH.match(text)
        if doc and section.startswith("outputgroup"):
            old = doc.group("value")
            if old:
                matches = by_ext.get(PureWindowsPath(old).suffix.lower(), [])
                if len(matches) == 1:
                    new = os.path.relpath(matches[0], project_dir)
                    retargeted.append({"output": int(doc.group("n")),
                                       "was": old, "now": new})
                    out.append(f"OutputDocumentPath{doc.group('n')}={new}{eol}")
                    continue
                unmapped.append({"output": int(doc.group("n")), "was": old,
                                 "why": ("the project has no document of that "
                                         "kind" if not matches else
                                         f"the project has {len(matches)} of "
                                         "that kind; pick one in Altium")})
            out.append(line)
            continue

        keyed = _KEY.match(text)
        if keyed and section in _CONTAINER_SECTIONS:
            key, n, value = keyed.group("key"), keyed.group("n"), keyed.group("value")
            containers.add(int(n))
            if key == "ReleaseManaged":
                out.append(f"ReleaseManaged{n}=0{eol}")
                continue
            if key == "OutputBasePath":
                out.append(f"OutputBasePath{n}={output_dir}{eol}")
                continue
            if key in _WHERE_KEYS:
                if _is_folder(value):
                    new = output_dir.rstrip("\\/") + "\\"
                else:
                    new = str(PureWindowsPath(output_dir) / PureWindowsPath(value).name)
                out.append(f"{key}{n}={new}{eol}")
                continue
        out.append(line)

    report = {"retargeted": retargeted, "unmapped": unmapped,
              "containers": sorted(containers)}
    return bom + "".join(out).encode(encoding), report


def outputs(data: bytes) -> list[dict[str, Any]]:
    """Every output in an OutJob: number, type, name, enabled."""
    _bom, lines, _enc = _split(data)
    found: dict[int, dict[str, Any]] = {}
    for line in lines:
        text, _eol_ = _eol(line)
        keyed = _KEY.match(text)
        if not keyed:
            continue
        key, n = keyed.group("key"), int(keyed.group("n"))
        if key in ("OutputType", "OutputName", "OutputEnabled") \
                and not text.startswith(f"{key}{n}_"):
            found.setdefault(n, {"output": n})[key] = keyed.group("value")
    return [{"output": n, "type": o.get("OutputType", ""),
             "name": o.get("OutputName", ""),
             "enabled": o.get("OutputEnabled") == "1"}
            for n, o in sorted(found.items())]
