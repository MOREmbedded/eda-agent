# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The pad paste flag is only touched on Altium Designer 25 or later.

IsTopPasteEnabled does not exist on AD23: the line that names it stops the
whole script with "Undeclared identifier", Try/Except does not catch it,
and the bridge never answers a ping. Every use must sit behind the
version gate, read and write alike. It reads and writes on 25.8.1.18
(issue #105), so 25 is inside the gate; 24 is not measured.
"""

from __future__ import annotations

import re
from pathlib import Path

LIBRARY = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "Library.pas"


def _routine(text: str, name: str) -> str:
    m = re.search(rf"\nFunction {name}\b.*?\nEnd;", text, re.S)
    assert m, f"{name} is missing from Library.pas"
    return m.group(0)


def test_the_gate_reads_the_major_version():
    body = _routine(LIBRARY.read_text(encoding="utf-8"), "PadPasteFlagSupported")
    assert "Client.GetProductVersion" in body
    assert "Major >= 25" in body


def test_every_use_of_the_flag_follows_the_gate():
    lines = LIBRARY.read_text(encoding="utf-8").splitlines()
    uses = [i for i, line in enumerate(lines)
            if "IsTopPasteEnabled" in line and not line.lstrip().startswith("{")]
    assert uses, "the flag is no longer used; drop this guard with it"
    for i in uses:
        window = "\n".join(lines[max(0, i - 40):i])
        assert "PadPasteFlagSupported" in window, (
            f"Library.pas line {i + 1} touches IsTopPasteEnabled with no "
            f"PadPasteFlagSupported check above it, so AD23 stops on it")


def test_the_read_returns_null_before_the_call():
    body = _routine(LIBRARY.read_text(encoding="utf-8"), "PadPasteTopJson")
    gate = body.index("If Not PadPasteFlagSupported(0) Then Exit;")
    assert gate < body.index("GetState_IsTopPasteEnabled")
