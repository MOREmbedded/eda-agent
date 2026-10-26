# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""proj_export_image checks that the file it reports is a PDF.

A file being there is not yet a PDF: an empty or cut-off print leaves one
too. Issue #94 asked that the output be shown to decode, not only exist.
"""
from __future__ import annotations

import asyncio

from eda_agent.tools import project as project_tools


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


class _Bridge:
    async def send_command_async(self, command, params=None, timeout=None):
        return {"success": True, "output_path": (params or {}).get("output_path")}


def _export(monkeypatch, path):
    monkeypatch.setattr(project_tools, "get_bridge", lambda: _Bridge())
    mcp = _Mcp()
    project_tools.register_project_tools(mcp)
    return asyncio.run(mcp.tools["proj_export_image"](str(path)))


def test_a_real_pdf_passes(monkeypatch, tmp_path):
    out_file = tmp_path / "sheet.pdf"
    out_file.write_bytes(b"%PDF-1.7\n...")
    out = _export(monkeypatch, out_file)
    assert out["success"] is True and out["file_exists"] is True


def test_an_empty_file_is_not_a_pdf(monkeypatch, tmp_path):
    out_file = tmp_path / "sheet.pdf"
    out_file.write_bytes(b"")
    out = _export(monkeypatch, out_file)
    assert out["success"] is False
    assert out["error"] == "EXPORT_FILE_INVALID"


def test_a_missing_file_is_still_reported(monkeypatch, tmp_path):
    out = _export(monkeypatch, tmp_path / "never.pdf")
    assert out["error"] == "EXPORT_FILE_MISSING"
