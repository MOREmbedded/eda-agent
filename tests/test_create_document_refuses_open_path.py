# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""app_create_document must not create over a document that is open.

Creating a blank PcbLib at the path of an open one raised "object accessed
after being destroyed" in ADVPCB and halted the bridge script (2026-10-20).
App_CreateDocument now looks the path up first and refuses.
"""

from __future__ import annotations

from pathlib import Path

APPLICATION = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "Application.pas"


def _body() -> str:
    src = APPLICATION.read_text(encoding="utf-8")
    start = src.index("Function App_CreateDocument(")
    return src[start:src.index("\nFunction ", start + 1)]


def test_the_open_check_runs_before_the_new_document_is_made():
    body = _body()
    check = body.index("Client.GetDocumentByPath(FilePath)")
    create = body.index("Client.OpenNewDocument(")
    assert check < create
    refusal = body.index("'ALREADY_OPEN'")
    assert check < refusal < create
