# SPDX-License-Identifier: Apache-2.0
"""The README's contents list against the README's own sections.

It is a hand-written list in a collapsed <details>, which is exactly
the kind of thing that stops matching the document and nobody notices:
two sections had been added without it, and its order had drifted from
the document's in five places, so following it jumped back and forth.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

README = Path(__file__).resolve().parents[1] / "README.md"


def _slug(title: str) -> str:
    """GitHub's heading anchor, near enough for the headings used here."""
    text = re.sub(r"`", "", title).lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text.strip())


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def contents(readme: str) -> str:
    start = readme.index("<summary><b>Full contents</b></summary>")
    return readme[start:readme.index("</details>", start)]


def headings(readme: str, level: int) -> list[str]:
    mark = "#" * level
    return [m.group(1) for m in
            re.finditer(rf"^{mark} (?!#)(.+?)\s*$", readme, re.M)]


def test_every_section_is_in_the_contents_list(readme, contents):
    listed = set(re.findall(r"\]\(#([^)]+)\)", contents))
    missing = [t for t in headings(readme, 2) if _slug(t) not in listed]
    assert not missing, f"sections with no contents entry: {missing}"


def test_the_contents_list_is_in_document_order(readme, contents):
    """A list that jumps around is worse than no list."""
    in_doc = [_slug(t) for t in headings(readme, 2)]
    listed = [a for a in re.findall(r"\]\(#([^)]+)\)", contents) if a in in_doc]
    assert listed == in_doc


def test_no_contents_entry_points_nowhere(readme, contents):
    anchors = {_slug(t) for lvl in (1, 2, 3, 4) for t in headings(readme, lvl)}
    dead = sorted({a for a in re.findall(r"\]\(#([^)]+)\)", contents)
                   if a not in anchors})
    assert not dead, f"contents entries with no heading: {dead}"


def test_no_link_anywhere_in_the_readme_points_nowhere(readme):
    """Every #anchor in the body, not only the ones in the list."""
    anchors = {_slug(t) for lvl in (1, 2, 3, 4, 5, 6)
               for t in headings(readme, lvl)}
    dead = sorted({a for a in re.findall(r"\]\(#([^)]+)\)", readme)
                   if a not in anchors})
    assert not dead, f"dead internal links: {dead}"


def test_every_relative_link_resolves_to_a_file(readme):
    """A doc link that 404s is the one a reader follows first."""
    root = README.parent
    broken = []
    for target in re.findall(r"\]\((?!https?:|#|mailto:)([^)]+)\)", readme):
        path = root / target.split("#", 1)[0]
        if not path.exists():
            broken.append(target)
    assert not broken, f"links to files that do not exist: {broken}"


def test_every_doc_is_reachable_from_the_readme():
    """A document nobody links to is a document nobody reads.

    Five were in docs/ and named nowhere in the README, including the
    runbook and the offline-CI guide. The README is the only index this
    project has, so a new doc belongs in it.
    """
    root = README.parent
    readme = README.read_text(encoding="utf-8")
    linked = set(re.findall(r"\]\((docs/[^)#]+)\)", readme))
    present = {f"docs/{p.name}" for p in (root / "docs").glob("*.md")}
    missing = sorted(present - linked)
    assert not missing, (
        f"docs the README never links to: {missing}. Add a row to the "
        "table under 'Tool reference', or link it where it belongs.")
