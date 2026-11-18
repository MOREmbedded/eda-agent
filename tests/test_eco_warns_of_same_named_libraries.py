# SPDX-License-Identifier: Apache-2.0
"""A footprint library named without a path is flagged when two libraries
carry that name.

Live on Altium 26.11 the demo project's Footprints.PcbLib shared its name
with a library of another open project. Its parts named the library
without a path, the ECO resolved the other one and reported "Footprint Not
Found", and nothing in proj_sync_pcb or proj_component_models said why.
"""
from __future__ import annotations

import asyncio

import pytest

from eda_agent.tools import project
from tests.test_sync_reports_what_it_left_undone import _tools

_MINE = r"C:\work\demo\Footprints.PcbLib"
_THEIRS = r"C:\work\other\Footprints.PcbLib"
_UNIQUE = r"C:\work\demo\Only.PcbLib"


def _parts(library="Footprints.PcbLib", found=_THEIRS):
    return {"components": 2, "parts": [
        {"designator": "U1", "models": [
            {"name": "QFN16", "type": "PCBLIB", "current": True, "integrated": False,
             "datafiles": [{"library": library, "entity": "QFN16",
                            "kind": "PCBLIB", "found_at": found}]}]},
        {"designator": "R1", "models": [
            {"name": "R0402", "type": "PCBLIB", "current": True, "integrated": False,
             "datafiles": [{"library": "Only.PcbLib", "entity": "R0402",
                            "kind": "PCBLIB", "found_at": _UNIQUE}]}]}]}


class _Bridge:
    def __init__(self, models):
        self.models = models

    async def send_command_async(self, command, params=None, timeout=None, **kw):
        if command == "project.component_models":
            return self.models
        if command == "application.get_open_documents":
            return [{"file_path": _MINE}, {"file_path": r"C:\work\demo\Board.PcbDoc"}]
        if command == "project.get_open_projects":
            return {"projects": [{"project_path": r"C:\work\demo\Demo.PrjPcb"},
                                 {"project_path": r"C:\work\other\Other.PrjPcb"}]}
        if command == "project.get_documents":
            if "other" in params["project_path"]:
                return [{"file_path": _THEIRS}]
            return [{"file_path": _MINE}, {"file_path": _UNIQUE}]
        if command == "library.get_installed_libraries":
            return {"libraries": []}
        if command == "project.update_pcb":
            return {"success": True}
        if command in ("pcb.get_differential_pairs", "project.get_bom",
                       "pcb.get_components"):
            return {}
        raise AssertionError(command)


@pytest.fixture(autouse=True)
def _no_real_dialogs(monkeypatch):
    monkeypatch.setattr(project, "_list_open_dialogs", lambda: [])


def test_component_models_names_both_libraries(monkeypatch):
    out = asyncio.run(_tools(_Bridge(_parts()), monkeypatch)["proj_component_models"]())
    (warning,) = out["warnings"]
    assert "Footprints.PcbLib" in warning and "U1" in warning
    assert _MINE in warning and _THEIRS in warning
    assert "R1" not in warning


def test_sync_names_both_libraries(monkeypatch):
    out = asyncio.run(_tools(_Bridge(_parts()), monkeypatch)["proj_sync_pcb"]())
    assert any(_MINE in w and _THEIRS in w for w in out["warnings"])


def test_an_empty_location_is_judged_by_the_library_altium_took(monkeypatch):
    out = asyncio.run(_tools(_Bridge(_parts(library="")), monkeypatch)[
        "proj_component_models"]())
    assert any(_MINE in w and _THEIRS in w for w in out["warnings"])


def test_a_full_path_is_not_ambiguous(monkeypatch):
    out = asyncio.run(_tools(_Bridge(_parts(library=_MINE, found=_MINE)), monkeypatch)[
        "proj_component_models"]())
    assert "warnings" not in out
