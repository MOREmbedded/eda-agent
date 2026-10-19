# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""An installed wheel carries the EasyEDA extension source and can build it.

The wheel used to have no extensions directory, so an installed server
reported the extension as absent and had nothing to offer for import.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from eda_agent import easyeda_extension

ROOT = Path(__file__).resolve().parents[1]


def test_every_source_file_is_tracked_and_put_in_the_wheel():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    tracked = set(subprocess.run(
        ["git", "ls-files", "extensions/easyeda"], cwd=ROOT,
        capture_output=True, text=True).stdout.split())
    for name in easyeda_extension.SOURCE_FILES:
        assert f"extensions/easyeda/{name}" in tracked, name
        assert re.search(
            rf'"extensions/easyeda/{re.escape(name)}" = '
            rf'"eda_agent/easyeda_extension/{re.escape(name)}"', pyproject), name


def _packaged_only(tmp_path, monkeypatch):
    packaged = tmp_path / "site" / "easyeda_extension"
    packaged.mkdir(parents=True)
    for name in easyeda_extension.SOURCE_FILES:
        (packaged / name).write_text(name, encoding="utf-8")
    monkeypatch.setattr(easyeda_extension, "_CHECKOUT", tmp_path / "none")
    monkeypatch.setattr(easyeda_extension, "_PACKAGED", packaged)
    monkeypatch.setenv("EDA_AGENT_WORKSPACE", str(tmp_path / "ws" / "workspace"))
    return packaged


def test_a_wheel_finds_its_own_copy(tmp_path, monkeypatch):
    packaged = _packaged_only(tmp_path, monkeypatch)
    assert easyeda_extension.source_dir() == packaged
    assert easyeda_extension.is_packaged()


def test_the_checkout_wins_when_both_exist(tmp_path, monkeypatch):
    _packaged_only(tmp_path, monkeypatch)
    checkout = tmp_path / "repo" / "extensions" / "easyeda"
    checkout.mkdir(parents=True)
    (checkout / "main.js").write_text("", encoding="utf-8")
    monkeypatch.setattr(easyeda_extension, "_CHECKOUT", checkout)
    assert easyeda_extension.source_dir() == checkout
    assert not easyeda_extension.is_packaged()


def test_a_wheel_builds_in_a_folder_of_the_users(tmp_path, monkeypatch):
    _packaged_only(tmp_path, monkeypatch)
    monkeypatch.setattr(easyeda_extension.shutil, "which", lambda _: None)
    code, message = easyeda_extension.build()
    target = tmp_path / "ws" / "easyeda-extension"
    assert code == 1 and "Node.js" in message and str(target) in message
    assert sorted(p.name for p in target.iterdir()) == sorted(
        easyeda_extension.SOURCE_FILES)


def test_readers_use_the_build_folder_for_a_wheel(tmp_path, monkeypatch):
    _packaged_only(tmp_path, monkeypatch)
    monkeypatch.delenv("EDA_AGENT_EASYEDA_VERIFIED", raising=False)
    from eda_agent.bridge import easyeda_verified

    assert easyeda_verified.verified_path() == (
        tmp_path / "ws" / "easyeda-extension" / "verified.json")
