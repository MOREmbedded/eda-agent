# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Every upper-case constant the scripts use is declared in them.

An identifier DelphiScript does not know stops the script when the line
runs, Try/Except does not catch it, and lint.py cannot see it because it
checks one line at a time. Two Workspace constants arrived used but never
declared, so the first managed placement would have stopped the bridge.
"""

from __future__ import annotations

import re
from pathlib import Path

PASCAL = Path(__file__).resolve().parents[1] / "scripts" / "altium"
_CONSTANT = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_DECLARED = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*(?::\s*\w+\s*)?=", re.MULTILINE)


def _code(text: str) -> str:
    """The source with comments and string literals blanked out."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            end = i + 1
            while end < n:
                if text[end] == "'":
                    if end + 1 < n and text[end + 1] == "'":
                        end += 2
                        continue
                    break
                end += 1
            out.append("''")
            i = end + 1
        elif ch == "{":
            end = text.find("}", i)
            i = n if end == -1 else end + 1
            out.append(" ")
        elif text.startswith("(*", i):
            end = text.find("*)", i)
            i = n if end == -1 else end + 2
            out.append(" ")
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end == -1 else end
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _build_files() -> list[Path]:
    build = (PASCAL / "build.py").read_text(encoding="utf-8")
    listed = re.search(r"FILES = \[(.*?)\]", build, re.DOTALL).group(1)
    return [PASCAL / name for name in re.findall(r"'([^']+\.pas)'", listed)]


def test_every_upper_case_constant_used_is_declared():
    sources = {p.name: _code(p.read_text(encoding="utf-8-sig",
                                         errors="replace"))
               for p in _build_files()}
    declared = set()
    for code in sources.values():
        declared.update(_DECLARED.findall(code))
    assert "SCRIPT_VERSION" in declared, "the declaration scan found nothing"

    undeclared = {}
    for name, code in sources.items():
        for used in set(_CONSTANT.findall(code)) - declared:
            undeclared.setdefault(used, name)
    assert not undeclared, (
        f"used but never declared, each stops the script when reached: "
        f"{sorted(undeclared.items())}")
