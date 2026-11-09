# SPDX-License-Identifier: Apache-2.0
"""The Workspace page writes the ERC matrix of the project it showed.

profile_apply_all refuses to write the ERC matrix without project_path,
so the page has to pass one: the project its last Compare read, shown
beside the board in front, and only when that is a project file rather
than Altium's "Free Documents".
"""
from __future__ import annotations

from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "src" / "eda_agent" / "web"
        / "dashboard_static" / "workbench.html").read_text(encoding="utf-8")


def _ws_do() -> str:
    start = HTML.index("async function wsDo(")
    return HTML[start:HTML.index("\n}\n", start)]


def test_preview_and_apply_pass_the_project_compare_read():
    body = _ws_do()
    assert "args.project_path = S.wsProject" in body


def test_only_a_project_file_is_remembered():
    body = _ws_do()
    assert "S.wsProject = e && e.ok !== false && /\\.prj[a-z]*$/i.test(e.project" in body


def test_the_page_shows_which_project_that_is():
    assert 'T("Project"))}</dt><dd class="m" title="${esc(S.wsProject' in HTML


def test_an_apply_that_wrote_nothing_does_not_say_written():
    """Live, 2026-11-08: with the ERC section refused and no other section
    in the profile, Apply wrote nothing and the page still said "Written
    to Altium."."""
    body = _ws_do()
    assert "!r.data.applied.length && (r.data.failed || []).length > 0" in body
    assert 'if (wroteNothing) what_ = T("{0} failed.", name);' in body
    assert "r.data.ok !== false && !wroteNothing" in body
