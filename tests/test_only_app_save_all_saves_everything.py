# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Only app_save_all writes every open document.

application.save_all writes every document open in Altium, whatever
project it is in. Tools that sent it as a step of their own (a checkpoint
of one project, a designator pass over one library, the doctor's
"no-op" canary) rewrote sheets and boards in other projects, which a
field report caught as hash changes on files nobody touched (issue #94).
A tool saves what it changed: project.save for a project,
application.save_document for one file.
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "eda_agent"


def test_no_tool_sends_save_all_as_a_side_step():
    senders = []
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"[\"']application\.save_all[\"']", text):
            line = text.count("\n", 0, m.start()) + 1
            senders.append(f"{path.relative_to(_SRC).as_posix()}:{line}")
    owner = [s for s in senders if s.startswith("tools/application.py:")]
    others = [s for s in senders if s not in owner]
    assert not others, f"save_all sent outside app_save_all: {others}"
    assert len(owner) == 1, (
        f"application.py sends save_all {len(owner)} times; only "
        f"app_save_all should: {owner}")


def test_the_one_sender_is_app_save_all():
    text = (_SRC / "tools" / "application.py").read_text(encoding="utf-8")
    at = text.index('"application.save_all"')
    start = text.rfind("async def ", 0, at)
    assert text[start:].startswith("async def app_save_all("), (
        "the save_all sender is not app_save_all")
