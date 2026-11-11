# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The status window's update icon, and the sponsor note it no longer has.

The update icon is driven by a flag file the server writes after each
update check; the form only tests that the file exists. These tests pin
both ends to the same file name and check the server writes and removes
it with the news.

Upstream added a sponsor note, a heart button and a README "Support the
project" section beside the icon. This fork removed them (see
CHANGELOG.md); the last test keeps them from coming back on a later
upstream merge.
"""
from pathlib import Path

from eda_agent import updates

ROOT = Path(__file__).resolve().parents[1]
PAS = (ROOT / "scripts" / "altium" / "StatusForm.pas").read_text(encoding="utf-8")
DFM = (ROOT / "scripts" / "altium" / "StatusForm.dfm").read_text(encoding="utf-8")


def test_form_and_server_agree_on_the_flag_name():
    assert f"UPDATE_FLAG_FILE     = '{updates.UPDATE_FLAG}';" in PAS


def test_flag_written_while_an_update_is_out(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "_cache_path", lambda: tmp_path / "update_check.json")
    monkeypatch.setattr(updates, "notice",
                        lambda: {"update_available": True, "next_step": "3 behind"})
    updates._sync_flag()
    assert (tmp_path / updates.UPDATE_FLAG).read_text(encoding="utf-8") == "3 behind"


def test_flag_removed_once_current(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "_cache_path", lambda: tmp_path / "update_check.json")
    (tmp_path / updates.UPDATE_FLAG).write_text("old", encoding="utf-8")
    monkeypatch.setattr(updates, "notice", lambda: None)
    updates._sync_flag()
    assert not (tmp_path / updates.UPDATE_FLAG).exists()


def test_flag_failure_never_raises(tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("git gone")
    monkeypatch.setattr(updates, "_cache_path", lambda: tmp_path / "update_check.json")
    monkeypatch.setattr(updates, "notice", boom)
    updates._sync_flag()


def test_update_icon_hidden_until_the_flag_appears():
    block = DFM.split("object btn_Update: TPanel", 1)[1].split("    end", 1)[0]
    assert "Visible = False" in block
    assert "UpdateUpdateBadge(0);" in PAS


def test_every_update_handler_is_defined():
    for name in ("btn_UpdateClick", "btn_UpdateEnter", "btn_UpdateLeave"):
        assert f"= {name}" in DFM
        assert f"Procedure {name}(Sender : TObject);" in PAS


# Where the sponsor note, its links and its handles lived. The status
# form has no legitimate use of the word at all, so it is matched in any
# case there; the README keeps "not ... sponsored by Altium", so only the
# capitalised word and the two links are refused in it.
# The author's address is not a needle: it is in the copyright headers,
# which stay.
_SPONSOR_NEEDLES = ("sponsors/salitronic", "buymeacoffee")


def sponsor_leftovers(pas: str, dfm: str, readme: str, funding: str) -> list[str]:
    found = []
    for name, text in (("StatusForm.pas", pas), ("StatusForm.dfm", dfm)):
        if "sponsor" in text.lower():
            found.append(f"{name}: sponsor")
    if "Sponsor" in readme:
        found.append("README.md: Sponsor")
    for name, text in (("StatusForm.pas", pas), ("StatusForm.dfm", dfm),
                       ("README.md", readme), ("FUNDING.yml", funding)):
        for needle in _SPONSOR_NEEDLES:
            if needle in text.lower():
                found.append(f"{name}: {needle}")
    # GitHub's FUNDING.yml template names "GitHub Sponsors" in a comment,
    # so that file is judged by its values: every platform left blank.
    for line in funding.splitlines():
        key, sep, value = line.partition(":")
        value = value.split("#", 1)[0].strip()
        if sep and not key.lstrip().startswith("#") and value:
            found.append(f"FUNDING.yml: {key.strip()} is set")
    return found


def test_no_sponsor_note_or_handles_come_back():
    # The README is now a front page; the documentation pages and the
    # website built from them (mkdocs.yml, website/) are where the same
    # note could come back, so they are read as part of it.
    paths = [ROOT / "README.md", ROOT / "mkdocs.yml",
             *sorted((ROOT / "docs").rglob("*.md")),
             *sorted(p for p in (ROOT / "website").rglob("*") if p.is_file())]
    readme = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                        for p in paths)
    funding = (ROOT / ".github" / "FUNDING.yml").read_text(encoding="utf-8")
    assert sponsor_leftovers(PAS, DFM, readme, funding) == []


def test_the_sponsor_guard_sees_the_note_put_back():
    """Each place the note lived, restored as upstream had it, is caught."""
    assert sponsor_leftovers(
        PAS + "\n    SPONSOR_URL          = 'https://github.com/sponsors/salitronic';",
        DFM, "", "") != []
    assert sponsor_leftovers("", DFM + "\n  object pnl_Sponsor: TPanel", "", "") != []
    assert sponsor_leftovers("", "", "## Support the project\n[Sponsor on GitHub]", "") != []
    assert sponsor_leftovers(
        "", "", "", "github: salitronic\nbuy_me_a_coffee: george.saliba\n") != []
    # The disclaimer the README keeps is not a sponsor note.
    assert sponsor_leftovers("", "", "not affiliated with, endorsed by, or sponsored by Altium", "") == []
