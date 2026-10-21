# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Leftover [DocumentN] sections are taken out of a .PrjPcb, and nothing else.

Altium rewrites [Document1]..[DocumentN] for a project's new document
count and leaves the sections past N in the file, so a removed document
comes back on reopen (issue #94, reproduced on AD 26.11). The fix edits
the file, so it is held to three things: only sections past the count go,
only when the rest match the project, and every other byte stays.
"""
from __future__ import annotations

from pathlib import Path

from eda_agent.prjpcb import finish_removal, lists_document, prune_stale_documents

_BOM = b"\xef\xbb\xbf"


def _project(tmp_path: Path, docs: list[str]) -> Path:
    for d in set(docs):
        (tmp_path / d).write_bytes(b"x")
    parts = ["[Design]\r\nVersion=1.0\r\n\r\n",
             f"[Document1]\r\nDocumentPath={docs[0]}\r\nAnnotationEnabled=1\r\n\r\n",
             "[SmartPDF]\r\nPageOptions=Fit\r\n\r\n"]
    for i, d in enumerate(docs[1:], start=2):
        parts.append(f"[Document{i}]\r\nDocumentPath={d}\r\nAnnotationEnabled=1\r\n\r\n")
    path = tmp_path / "P.PrjPcb"
    path.write_bytes(_BOM + "".join(parts).encode("utf-8"))
    return path


def test_only_the_sections_past_the_count_go(tmp_path):
    path = _project(tmp_path, ["Main.SchDoc", "中文.SchDoc", "Gone.SchDoc", "中文.SchDoc"])
    before = path.read_bytes()
    members = [str(tmp_path / "Main.SchDoc"), str(tmp_path / "中文.SchDoc"),
               "ActiveBOM.VirtualBOM"]
    out = prune_stale_documents(str(path), members, str(tmp_path / "bak"))
    assert out["removed_sections"] == ["Document3", "Document4"]
    after = path.read_bytes()
    assert after.startswith(_BOM) and b"\r\n" in after
    # Everything that stays is byte for byte what was there.
    cut = before.index(b"[Document3]")
    assert after == before[:cut]
    assert "中文.SchDoc".encode("utf-8") in after
    assert Path(out["backup"]).read_bytes() == before


def test_a_file_that_does_not_match_the_project_is_left_alone(tmp_path):
    path = _project(tmp_path, ["Main.SchDoc", "Other.SchDoc", "Gone.SchDoc"])
    before = path.read_bytes()
    members = [str(tmp_path / "Main.SchDoc"), str(tmp_path / "Gone.SchDoc")]
    out = prune_stale_documents(str(path), members, str(tmp_path / "bak"))
    assert out["pruned"] == 0 and "left as it is" in out["reason"]
    assert path.read_bytes() == before
    assert not (tmp_path / "bak").exists()


def test_nothing_stale_writes_nothing(tmp_path):
    path = _project(tmp_path, ["Main.SchDoc", "B.SchDoc"])
    before = path.read_bytes()
    members = [str(tmp_path / "Main.SchDoc"), str(tmp_path / "B.SchDoc")]
    assert prune_stale_documents(str(path), members, str(tmp_path / "bak"))["pruned"] == 0
    assert path.read_bytes() == before


def test_a_finished_removal_reports_the_file_as_it_now_is(tmp_path):
    path = _project(tmp_path, ["Main.SchDoc", "Main.SchDoc", "Gone.SchDoc"])
    reply = {"success": False, "removed": True, "in_project_file": True,
             "project_file_entry": "DocumentPath=Gone.SchDoc",
             "document_path": str(tmp_path / "Gone.SchDoc"),
             "project_path": str(path), "members": [str(tmp_path / "Main.SchDoc")]}
    out = finish_removal(reply, str(tmp_path / "bak"))
    assert out["success"] is True and out["in_project_file"] is False
    assert not lists_document(str(path), str(tmp_path / "Gone.SchDoc"))
    assert lists_document(str(path), str(tmp_path / "Main.SchDoc"))


def test_shifted_copies_of_another_member_go_too(tmp_path):
    """Measured: three removals from the middle left three copies of the
    last member past the count. The removed names were gone, so a check on
    them alone saw nothing to do."""
    path = _project(tmp_path, ["Main.SchDoc", "H.SchDoc", "H.SchDoc", "H.SchDoc"])
    reply = {"success": True, "removed": True, "in_project_file": False,
             "document_path": str(tmp_path / "G.SchDoc"), "project_path": str(path),
             "members": [str(tmp_path / "Main.SchDoc"), str(tmp_path / "H.SchDoc")]}
    out = finish_removal(reply, str(tmp_path / "bak"))
    assert out["stale_sections"]["removed_sections"] == ["Document3", "Document4"]
    assert out["success"] is True and "members" not in out


def test_each_edit_keeps_its_own_backup(tmp_path):
    """Measured: two removals in one second wrote one backup name, and the
    second copy replaced the first, the only one from before any edit."""
    path = _project(tmp_path, ["Main.SchDoc", "B.SchDoc", "Gone.SchDoc"])
    first = path.read_bytes()
    one = prune_stale_documents(str(path), [str(tmp_path / "Main.SchDoc"),
                                            str(tmp_path / "B.SchDoc")], str(tmp_path / "bak"))
    two = prune_stale_documents(str(path), [str(tmp_path / "Main.SchDoc")],
                                str(tmp_path / "bak"))
    assert one["backup"] != two["backup"]
    assert Path(one["backup"]).read_bytes() == first


def test_a_reply_without_members_is_passed_through(tmp_path):
    reply = {"success": True, "removed": True, "in_project_file": False}
    assert finish_removal(dict(reply), str(tmp_path)) == reply
