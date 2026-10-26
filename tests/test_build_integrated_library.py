# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Libraries are packaged and compiled through Altium, and the IntLib is
looked for rather than assumed.

Measured on AD 26.11: a LibPkg created with proj_create, given a SchLib
and a PcbLib and compiled, writes <stem>.IntLib to "Project Outputs for
<stem>" next to the package.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from eda_agent.tools import library as library_tools


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


class _Bridge:
    """Writes the IntLib on compile, as Altium does, unless told not to."""

    def __init__(self, writes=True):
        self.sent = []
        self.writes = writes

    async def send_command_async(self, command, params=None, timeout=None):
        self.sent.append(command)
        if command == "project.create":
            Path(params["project_path"]).write_text("[Design]\n")
        if command == "project.compile" and self.writes:
            pkg = Path(params["project_path"])
            out = pkg.parent / f"Project Outputs for {pkg.stem}"
            out.mkdir(exist_ok=True)
            (out / f"{pkg.stem}.IntLib").write_bytes(b"x" * 64)
        return {"success": True}


def _call(monkeypatch, bridge, **kw):
    monkeypatch.setattr(library_tools, "get_bridge", lambda: bridge)
    mcp = _Mcp()
    library_tools.register_library_tools(mcp)
    return asyncio.run(mcp.tools["lib_build_integrated_library"](**kw))


def _sources(tmp_path):
    out = []
    for name in ("Parts.SchLib", "Parts.PcbLib"):
        p = tmp_path / name
        p.write_text("lib")
        out.append(str(p))
    return out


def test_a_new_package_is_created_filled_and_compiled(monkeypatch, tmp_path):
    bridge = _Bridge()
    pkg = tmp_path / "pkg" / "Parts.LibPkg"
    out = _call(monkeypatch, bridge, package_path=str(pkg),
                source_libraries=_sources(tmp_path), install=True)
    assert out["built"] and out["created"]
    assert out["intlib"].endswith("Parts.IntLib") and out["intlib_bytes"] == 64
    assert bridge.sent == ["project.create", "project.add_document",
                           "project.add_document", "project.compile",
                           "library.install_library"]


def test_a_compile_that_writes_nothing_is_not_built(monkeypatch, tmp_path):
    out = _call(monkeypatch, _Bridge(writes=False),
                package_path=str(tmp_path / "Parts.LibPkg"),
                source_libraries=_sources(tmp_path))
    assert out["built"] is False and out["success"] is False
    assert "no new IntLib" in out["reason"]


def test_an_old_intlib_left_from_before_does_not_count(monkeypatch, tmp_path):
    pkg = tmp_path / "Parts.LibPkg"
    pkg.write_text("[Design]\n")
    old = tmp_path / "Project Outputs for Parts"
    old.mkdir()
    (old / "Parts.IntLib").write_bytes(b"old")
    out = _call(monkeypatch, _Bridge(writes=False), package_path=str(pkg),
                source_libraries=_sources(tmp_path))
    assert out["built"] is False, "a stale IntLib was taken for a new build"


def test_bad_inputs_send_nothing(monkeypatch, tmp_path):
    bridge = _Bridge()
    assert _call(monkeypatch, bridge, package_path=str(tmp_path / "x.PrjPcb"),
                 source_libraries=_sources(tmp_path))["error"] == "BAD_PARAM"
    assert _call(monkeypatch, bridge, package_path=str(tmp_path / "x.LibPkg"),
                 source_libraries=[str(tmp_path / "nope.SchLib")])["error"] \
        == "MISSING_SOURCES"
    assert bridge.sent == []
