# SPDX-License-Identifier: Apache-2.0
"""The README as a front page, and the documents it leads to.

The README used to be the whole manual, 1464 lines with a hand-written
contents list that kept drifting from the sections. It is now a short
front page: a pitch, a quick start, and a documentation map that links
every page in docs/. The pages themselves are also published as the
product website (mkdocs.yml), so the guards here cover both readers:

* a link in the README or in any doc must land on a file and, when it
  names a section, on a heading that exists;
* every doc must be reachable from the README map AND from the website
  navigation, because a page nobody links to is a page nobody reads;
* the README has to stay short, or it turns back into the manual.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DOCS = ROOT / "docs"
MKDOCS = ROOT / "mkdocs.yml"

#: The README is a front page. 1464 lines was the manual; this leaves
#: room to grow without letting it become one again.
MAX_README_LINES = 250

_LINK = re.compile(r"\]\((?!https?:|mailto:)([^)\s]+)\)")
_FENCE = re.compile(r"^(```|~~~)")


def _slug(title: str) -> str:
    """GitHub's heading anchor, near enough for the headings used here."""
    text = re.sub(r"`", "", title).lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text.strip())


def _strip_code(text: str) -> str:
    """Drop fenced blocks, so an example link in code is not checked."""
    out, fence = [], False
    for line in text.splitlines():
        if _FENCE.match(line.lstrip()):
            fence = not fence
            continue
        if not fence:
            out.append(line)
    return "\n".join(out)


def _anchors(path: Path) -> set[str]:
    text = _strip_code(path.read_text(encoding="utf-8"))
    return {_slug(m.group(1))
            for m in re.finditer(r"^#{1,6} (.+?)\s*$", text, re.M)}


def _documents() -> list[Path]:
    return [README, *sorted(DOCS.rglob("*.md"))]


def _site_nav_pages() -> set[str]:
    text = MKDOCS.read_text(encoding="utf-8")
    nav = text[text.index("\nnav:"):]
    return set(re.findall(r"(?<=\s)([\w.-][\w./-]*\.md)\s*$", nav, re.M))


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


def test_the_readme_stays_a_front_page(readme):
    lines = len(readme.splitlines())
    assert lines <= MAX_README_LINES, (
        f"README.md is {lines} lines. It is the front page, not the "
        f"manual: move the detail into a page under docs/ and link it "
        f"from the documentation map.")


def test_the_readme_starts_with_a_quick_start(readme):
    """Install, connect, load into Altium, check: before anything else."""
    sections = re.findall(r"^## (.+?)\s*$", readme, re.M)
    assert "Quick start" in sections[:2], sections
    quick = readme[readme.index("## Quick start"):]
    quick = quick[:quick.index("\n## ", 1)]
    for step in ("pip install", "claude mcp add", "install-scripts",
                 "StartMCPServer", "eda-agent doctor"):
        assert step in quick, f"the quick start lost its {step!r} step"


@pytest.mark.parametrize("doc", _documents(), ids=lambda p: str(p.relative_to(ROOT)))
def test_every_relative_link_resolves(doc):
    """A link that 404s is the one a reader follows first.

    Covers the file and, for ``page.md#section``, the section, in the
    README and in every page under docs/.
    """
    text = _strip_code(doc.read_text(encoding="utf-8"))
    broken = []
    for target in _LINK.findall(text):
        path, _, anchor = target.partition("#")
        dest = (doc.parent / path).resolve() if path else doc
        if not dest.exists():
            broken.append(target)
            continue
        if anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
            broken.append(target)
    assert not broken, f"{doc.relative_to(ROOT)}: links that land nowhere: {broken}"


def test_every_doc_is_reachable_from_the_readme(readme):
    """A document nobody links to is a document nobody reads.

    Five were once in docs/ and named nowhere, including the runbook and
    the offline-CI guide. The README's documentation map is the index on
    GitHub, so a new page belongs in it.
    """
    linked = set(re.findall(r"\]\((docs/[^)#]+)\)", readme))
    present = {f"docs/{p.name}" for p in DOCS.glob("*.md")} - {"docs/index.md"}
    missing = sorted(present - linked)
    assert not missing, (
        f"docs the README never links to: {missing}. Add a row to the "
        "documentation map in README.md.")


def test_every_doc_is_in_the_website_navigation():
    """The same rule for the website: a page outside nav is hidden there."""
    nav = _site_nav_pages()
    present = {str(p.relative_to(DOCS)).replace("\\", "/")
               for p in DOCS.rglob("*.md")}
    missing = sorted(present - nav)
    assert not missing, (
        f"pages missing from the nav in mkdocs.yml: {missing}")
    stale = sorted(nav - present)
    assert not stale, f"mkdocs.yml nav names pages that do not exist: {stale}"


def test_the_readme_points_at_the_website(readme):
    site = re.search(r"^site_url:\s*(\S+)", MKDOCS.read_text(encoding="utf-8"), re.M)
    assert site, "mkdocs.yml has no site_url"
    assert site.group(1) in readme, (
        "the README no longer links to the published website")


def test_the_guards_see_a_broken_link(tmp_path):
    """Guard the guard: a seeded dead link and dead anchor are caught."""
    page = tmp_path / "page.md"
    page.write_text("# Title\n\n## Real section\n", encoding="utf-8")
    assert "real-section" in _anchors(page)
    assert "missing-section" not in _anchors(page)
    text = "[a](nowhere.md) [b](page.md#missing-section) [c](https://x.y)"
    assert _LINK.findall(text) == ["nowhere.md", "page.md#missing-section"]
