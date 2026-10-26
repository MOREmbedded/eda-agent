# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Every source file states its licence, and derivative files keep the
notice they inherited.

Two separate obligations, and this fork had been conflating them.

The LICENCE line belongs on every source file, no exceptions: the project
is Apache-2.0 and a file with no notice leaves a reader guessing.

The upstream COPYRIGHT line is what Apache-2.0 section 4(c) requires, and
it says to RETAIN the notices already in the Source form of the work a
derivative is built from. So it is required on every file that also
exists upstream, and on no other. Adding it to a file written here would
not satisfy 4(c); it would misstate who wrote the file, and this fork's
own rule is that no personal name goes into a source file that did not
earn it.

Measured on 2026-10-26, merging 63 upstream commits: 107 tracked source
files lacked the upstream copyright line and every one of them was
written in this fork, so none of them owed it. 43 lacked a licence line
and all 43 now have one.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_OWNER = "Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>"
_LICENCE = "SPDX-License-Identifier: Apache-2.0"
_SOURCE = (".py", ".pas", ".js", ".mjs", ".ts", ".ps1")


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=_ROOT, capture_output=True,
                         text=True, check=True).stdout.splitlines()
    return [p for p in out if p.endswith(_SOURCE)
            and not p.startswith("reference/")]


def _upstream_files() -> set[str]:
    """Paths upstream also has, or an empty set when it is not fetched.

    A checkout without the remote should not fail this file; it just
    cannot check the retention half.
    """
    done = subprocess.run(["git", "ls-tree", "-r", "--name-only",
                           "upstream/main"], cwd=_ROOT, capture_output=True,
                          text=True)
    if done.returncode != 0:
        return set()
    return set(done.stdout.splitlines())


def _head(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8", errors="replace")[:800]


def test_every_tracked_source_file_states_its_licence():
    files = _tracked()
    assert files, "git ls-files returned nothing"
    missing = [rel for rel in files
               if (_ROOT / rel).exists() and _LICENCE not in _head(rel)]
    assert not missing, (
        f"{len(missing)} source files carry no licence line "
        f"({_LICENCE!r}); every file in an Apache-2.0 project needs one: "
        f"{missing[:20]}")


def test_a_file_shared_with_upstream_keeps_its_copyright_notice():
    """Apache-2.0 4(c): retain the notices a derivative inherited."""
    upstream = _upstream_files()
    if not upstream:
        import pytest
        pytest.skip("upstream/main is not fetched in this checkout")

    shared = [rel for rel in _tracked()
              if rel in upstream and (_ROOT / rel).exists()]
    assert shared, "no files in common with upstream; the comparison broke"
    stripped = [rel for rel in shared if _OWNER not in _head(rel)]
    assert not stripped, (
        f"{len(stripped)} files this fork shares with upstream have lost "
        f"the notice they inherited ({_OWNER!r}). Apache-2.0 section 4(c) "
        f"says to retain it: {stripped[:20]}")
