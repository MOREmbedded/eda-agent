# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Every DRC call checks the board, and its report is found where AD25 puts it.

PCB:DesignRuleCheck acts on the CURRENT board. The first run opens its
report document, which takes the focus, so a second run in the same
session checked nothing and handed back the old report: a caller
re-running DRC to confirm a fix read a stale answer. And AD25 writes the
report into the project's output folder, so drc_confirmed was false on
every run there even when the check ran.
"""
from __future__ import annotations

from pathlib import Path

from tests.pascal_source import load

_PCB = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "PCB.pas"


def _body():
    return load(_PCB)["PCB_RunDRC"]


def test_the_board_is_shown_before_the_check_runs():
    body = _body()
    assert body.index("Client.ShowDocument(BoardDoc)") < body.index(
        "RunProcess('PCB:DesignRuleCheck')")


def test_the_project_output_report_is_watched_too():
    body = _body()
    assert "DrcOutputsReportPath(Board)" in body
    before = body.index("OutputsBefore := DrcReportAge(OutputsPath)")
    after = body.index("OutputsAfter := DrcReportAge(OutputsPath)")
    run = body.index("RunProcess('PCB:DesignRuleCheck')")
    assert before < run < after
    # The reply names the file that changed.
    assert "ReportPath := OutputsPath" in body


def test_the_output_report_name_follows_altium():
    helper = load(_PCB)["DrcOutputsReportPath"]
    assert "'Design Rule Check - '" in helper
    assert "Project.DM_GetOutputPath" in helper
