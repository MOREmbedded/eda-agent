# SPDX-License-Identifier: Apache-2.0
"""Everything this fork ships that points somewhere names MOREmbedded.

The fork once ran for weeks with upstream's URLs baked in: its update
checker asked upstream whether the user was current, its SARIF output
cited upstream's tracker, its HTTP clients introduced themselves as
upstream, and its FUNDING.yml put upstream's handles on this
repository's Sponsor button. An upstream merge brings those back
without a conflict whenever upstream edits a nearby line, so each one
is pinned here.

What deliberately stays upstream's is NOT checked: the Apache-2.0 SPDX
and copyright headers (a licence condition), the CHANGELOG's merge
records, and the one credit line in the README's About section.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from eda_agent import updates

ROOT = Path(__file__).resolve().parents[1]
FORK = "https://github.com/MOREmbedded/eda-agent"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_update_checker_asks_this_fork():
    assert updates.REPO_URL == FORK
    assert "/MOREmbedded/eda-agent/" in updates.PYPROJECT_URL
    assert "/repos/MOREmbedded/eda-agent/" in updates.COMMIT_URL


def test_pyproject_urls_and_author_are_this_fork():
    text = _read("pyproject.toml")
    for key in ("Homepage", "Repository"):
        assert f'{key} = "{FORK}"' in text
    assert f'Issues = "{FORK}/issues"' in text
    assert '{ name = "MOREmbedded", email = "bartosz.ziolek@morembedded.com" }' in text
    assert "salitronic" not in text


def test_sarif_points_at_this_fork():
    from eda_agent.fileio.review import to_sarif

    sarif = to_sarif({"file": "x.SchDoc", "findings": [
        {"check": "probe", "severity": "warning", "message": "m"}]})
    driver = sarif["runs"][0]["tool"]["driver"]
    assert driver["informationUri"] == FORK
    assert driver["rules"]
    assert {rule["helpUri"] for rule in driver["rules"]} == {FORK}


@pytest.mark.parametrize("rel", [
    "src/eda_agent/design/datasheet_fetch.py",
    "src/eda_agent/libimport/easyeda/fetch.py",
    "src/eda_agent/libimport/providers/public_libraries.py",
    "src/eda_agent/libimport/providers/_http.py",
])
def test_user_agent_names_this_fork(rel):
    text = _read(rel)
    agents = re.findall(r'^_(?:USER_AGENT|UA) = "([^"]*)"', text, re.M)
    assert agents, f"{rel}: no User-Agent constant found"
    for agent in agents:
        assert FORK in agent, f"{rel}: {agent!r}"


def test_no_source_file_links_to_upstream():
    """A copyright line names the author's address; a URL is a pointer."""
    hits = []
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "github.com/salitronic" in text or "salitronic/eda-agent" in text:
            hits.append(str(path.relative_to(ROOT)))
    assert hits == []


def test_funding_file_carries_no_handles():
    """GitHub's template, every platform left blank: upstream had
    `github: salitronic` and `buy_me_a_coffee: george.saliba` here."""
    text = _read(".github/FUNDING.yml")
    assert "salitronic" not in text and "george.saliba" not in text
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and not key.lstrip().startswith("#"):
            assert not value.split("#", 1)[0].strip(), line


def test_issue_template_readme_and_ci_doc_name_this_fork():
    assert f"{FORK}/security/policy" in _read(".github/ISSUE_TEMPLATE/config.yml")
    assert f"git clone {FORK}" in _read("README.md")
    assert f"pip install git+{FORK}" in _read("docs/HARDWARE_CI.md")


@pytest.mark.parametrize("rel", ["SECURITY.md", "CODE_OF_CONDUCT.md"])
def test_report_address_is_this_fork(rel):
    assert "bartosz.ziolek@morembedded.com" in _read(rel)
