# SPDX-License-Identifier: Apache-2.0
"""The id in the Altium status window names one call, not the session.

Every row of the in-Altium log showed ``Copy(RequestId, 1, 8)``, and
the comment above it promised the user could grep bridge_trace.log for
"the exact call". Request ids have been
``<session tag>-<sequence>-<random>`` since sessions gained tags, and
the tag is one value for the life of a server process, so those 8
characters named the SESSION: every row of a session carried the same
id, and grepping bridge_trace.log for it returned every call the
session had made.

Read from the source rather than run, because DelphiScript runs only
inside Altium. Comments are stripped first, so the comment that
explains the defect cannot be what satisfies a check for the fix.
"""
from __future__ import annotations

from pathlib import Path

from eda_agent.diag.perf import request_key
from tests.pascal_source import load

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "altium"
STATUS_FORM = _SCRIPTS / "StatusForm.pas"
BUNDLE = _SCRIPTS / "Altium_MCP.pas"

CURRENT = "a3c05b5f-00000001-435402a91e3b"
PRE_TAG = "5d765674e207407ea8657a6d742a01b7"


def _append_log_line(path: Path) -> str:
    return load(path)["AppendLogLine"]


def test_a_row_does_not_shorten_every_id_to_its_session_tag():
    """Tag plus sequence is 17 characters and names one call."""
    assert "Copy(RequestId, 1, 17)" in _append_log_line(STATUS_FORM)


def test_the_branch_turns_on_the_dash_that_follows_the_tag():
    """Position 9 holds that dash, which is where Main.pas's request
    scanner looks when it takes sessions in turn."""
    assert "Copy(RequestId, 9, 1) = '-'" in _append_log_line(STATUS_FORM)


def test_an_id_from_before_session_tags_is_still_shown_as_it_was():
    """Those carried no dash, and their leading 8 did name one call."""
    assert "Copy(RequestId, 1, 8)" in _append_log_line(STATUS_FORM)


def test_the_widths_are_the_ones_the_python_side_joins_on():
    """Pascal counts characters where request_key splits on dashes. If
    the id shape changes, the two drift apart without a word and the
    window starts naming something that is not a call."""
    assert len(request_key(CURRENT)) == 17
    assert len(request_key(PRE_TAG)) == 8


def test_the_bundle_altium_actually_loads_carries_the_fix():
    """Altium runs Altium_MCP.pas, not StatusForm.pas, so a change that
    was never rebuilt into the bundle reaches nothing."""
    assert "Copy(RequestId, 1, 17)" in _append_log_line(BUNDLE)
