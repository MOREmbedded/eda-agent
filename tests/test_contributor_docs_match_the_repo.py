# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""SECURITY.md and the GitHub templates describe the repository as it is.

They drifted: the policy named version 0.2.x while the package was at
0.6.1 and listed an installer/ folder that no longer existed, and the PR
template asked for conventional commits, which CONTRIBUTING.md forbids.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "SECURITY.md", ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md",
        *sorted((ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.md"))]


def _version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_the_supported_version_is_the_current_one():
    major_minor = ".".join(_version().split(".")[:2])
    policy = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    supported = re.findall(r"\|\s*([\d.]+)\.x\s*\|\s*:white_check_mark:", policy)
    assert supported == [major_minor], (
        f"SECURITY.md supports {supported}; the package is {_version()}")
    assert f"current version {major_minor}.x" in policy


def test_every_folder_named_exists():
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for folder in re.findall(r"`([\w.-]+(?:/[\w.-]+)*/)`", text):
            assert (ROOT / folder).is_dir(), (
                f"{doc.name} names {folder}, which is not in the repository")


def test_no_template_asks_for_a_commit_style_contributing_forbids():
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8").lower()
        assert "conventional-commit" not in text, doc.name
        assert "conventional commit" not in text, doc.name
