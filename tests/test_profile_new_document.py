# SPDX-License-Identifier: Apache-2.0
"""profile_new_document: seeding a new sheet or board from a template.

Altium has no API that applies a document template -- its own New
Document makes a blank sheet, and so does the bridge's create_document.
Copying the template file and opening that is what applying one amounts
to on disk, so these tests cover the copy rules and the open handoff.
"""
from __future__ import annotations

import asyncio

import pytest

from eda_agent.tools import profile as profile_tools
from eda_agent.tools.registry import ToolRegistry


class _Bridge:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        if self.fail:
            raise OSError("Altium is not answering")
        return {"success": True}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("EDA_AGENT_PROFILE_DIR", raising=False)


def _profile_with_templates(tmp_path, monkeypatch, files=("Standard.SchDoc",)):
    directory = tmp_path / "profile"
    documents = directory / "templates" / "documents"
    documents.mkdir(parents=True)
    for name in files:
        (documents / name).write_text(f"template body for {name}", encoding="utf-8")
    monkeypatch.setenv("EDA_AGENT_PROFILE_DIR", str(directory))
    return directory


def _tool(monkeypatch, bridge):
    import eda_agent.bridge as bridge_pkg
    monkeypatch.setattr(bridge_pkg, "get_bridge", lambda: bridge, raising=False)
    registry = ToolRegistry()
    profile_tools.register_profile_tools(registry, altium=True)
    return registry.get("profile_new_document").fn


def test_a_template_is_copied_and_opened(tmp_path, monkeypatch):
    _profile_with_templates(tmp_path, monkeypatch)
    bridge = _Bridge()
    target = tmp_path / "board" / "Sheet1.SchDoc"

    result = asyncio.run(_tool(monkeypatch, bridge)("Standard", str(target)))
    assert result["ok"] and result["opened"]
    assert target.read_text(encoding="utf-8").startswith("template body")
    assert bridge.calls[0][0] == "application.open_document"
    assert bridge.calls[0][1]["file_path"] == str(target)


def test_the_parent_directory_is_created(tmp_path, monkeypatch):
    _profile_with_templates(tmp_path, monkeypatch)
    target = tmp_path / "a" / "b" / "c" / "New.SchDoc"
    assert asyncio.run(_tool(monkeypatch, _Bridge())("Standard", str(target)))["ok"]
    assert target.exists()


def test_the_templates_own_extension_wins(tmp_path, monkeypatch):
    """Asking for a .SchDoc template at a .PcbDoc path would write a file
    Altium cannot open."""
    _profile_with_templates(tmp_path, monkeypatch)
    result = asyncio.run(_tool(monkeypatch, _Bridge())(
        "Standard", str(tmp_path / "Thing.PcbDoc")))
    assert result["path"].endswith(".SchDoc")


def test_a_template_can_be_named_by_file_or_by_stem(tmp_path, monkeypatch):
    _profile_with_templates(tmp_path, monkeypatch)
    for name in ("Standard", "Standard.SchDoc"):
        result = asyncio.run(_tool(monkeypatch, _Bridge())(
            name, str(tmp_path / f"out_{name}.SchDoc")))
        assert result["ok"], name


def test_an_unknown_template_lists_what_there_is(tmp_path, monkeypatch):
    _profile_with_templates(tmp_path, monkeypatch, files=("A4.SchDoc", "Four.PcbDoc"))
    result = asyncio.run(_tool(monkeypatch, _Bridge())(
        "Nope", str(tmp_path / "x.SchDoc")))
    assert result["ok"] is False
    assert "A4" in result["reason"] and "Four" in result["reason"]


def test_an_existing_file_is_not_overwritten_by_default(tmp_path, monkeypatch):
    _profile_with_templates(tmp_path, monkeypatch)
    target = tmp_path / "Existing.SchDoc"
    target.write_text("work in progress", encoding="utf-8")

    result = asyncio.run(_tool(monkeypatch, _Bridge())("Standard", str(target)))
    assert result["ok"] is False and "overwrite=True" in result["reason"]
    assert target.read_text(encoding="utf-8") == "work in progress"

    ok = asyncio.run(_tool(monkeypatch, _Bridge())(
        "Standard", str(target), overwrite=True))
    assert ok["ok"] and target.read_text(encoding="utf-8").startswith("template body")


def test_open_can_be_skipped(tmp_path, monkeypatch):
    _profile_with_templates(tmp_path, monkeypatch)
    bridge = _Bridge()
    result = asyncio.run(_tool(monkeypatch, bridge)(
        "Standard", str(tmp_path / "Quiet.SchDoc"), open_in_altium=False))
    assert result["ok"] and result["opened"] is False
    assert bridge.calls == []


def test_a_failed_open_still_reports_the_file_that_was_written(tmp_path, monkeypatch):
    """The copy succeeded; losing that fact because Altium was busy
    would leave a file nobody knows about."""
    _profile_with_templates(tmp_path, monkeypatch)
    target = tmp_path / "Orphan.SchDoc"
    result = asyncio.run(_tool(monkeypatch, _Bridge(fail=True))("Standard", str(target)))
    assert result["ok"] is True and result["opened"] is False
    assert "not answering" in result["open_error"]
    assert target.exists()


def test_no_active_profile_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_PROFILE_DIR", str(tmp_path / "nothing"))
    result = asyncio.run(_tool(monkeypatch, _Bridge())("x", str(tmp_path / "y.SchDoc")))
    assert result["ok"] is False and "no profile" in result["reason"]


def test_a_profile_without_document_templates_says_so(tmp_path, monkeypatch):
    directory = tmp_path / "profile"
    directory.mkdir()
    monkeypatch.setenv("EDA_AGENT_PROFILE_DIR", str(directory))
    result = asyncio.run(_tool(monkeypatch, _Bridge())("x", str(tmp_path / "y.SchDoc")))
    assert result["ok"] is False and "templates/documents" in result["reason"]


def test_document_templates_are_listed_separately_from_project_templates(
        tmp_path, monkeypatch):
    from eda_agent import profile as profile_mod

    directory = _profile_with_templates(tmp_path, monkeypatch,
                                        files=("A4.SchDoc", "Four.PcbDoc"))
    (directory / "templates" / "StarterProject").mkdir()

    profile = profile_mod.load_profile(directory)
    assert profile.template_names() == ["StarterProject"]
    assert [t["name"] for t in profile.document_templates()] == ["A4", "Four"]
    assert profile.summary()["document_templates"] == ["A4", "Four"]
