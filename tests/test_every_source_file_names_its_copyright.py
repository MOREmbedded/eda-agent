# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Every source file carries the licence and George Saliba's copyright.

A contributor's own copyright line stays beside it (Apache-2.0 section 4(c)
keeps existing notices), but no file may lack the project's. Two
contributed tests had been merged without it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_OWNER = "Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>"
_SOURCE = (".py", ".pas", ".js", ".mjs", ".ts", ".ps1")


def _tracked():
    out = subprocess.run(["git", "ls-files"], cwd=_ROOT, capture_output=True,
                         text=True, check=True).stdout.splitlines()
    return [p for p in out if p.endswith(_SOURCE)
            and not p.startswith("reference/")]


def test_every_tracked_source_file_has_the_owner_and_licence():
    files = _tracked()
    assert files, "git ls-files returned nothing"
    missing = []
    for rel in files:
        path = _ROOT / rel
        if not path.exists():
            continue
        head = path.read_text(encoding="utf-8", errors="replace")[:800]
        if _OWNER not in head or "SPDX-License-Identifier: Apache-2.0" not in head:
            missing.append(rel)
    assert not missing, (
        f"{len(missing)} source files lack the licence or the project's "
        f"copyright line ({_OWNER!r}); add it above any contributor's "
        f"own line: {missing[:20]}")
