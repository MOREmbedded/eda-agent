# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Signing out of Altium 365 removes the saved Nexar login.

This fork's own Altium 365 client had a365-login and a365-logout. When it
gave way to upstream's equivalent altium365 module, which has a login but
no logout, the logout was the one piece kept: without it the only way to
drop a stored login was to find and delete the file by hand.
"""

from __future__ import annotations

import sys


def test_logout_removes_the_saved_login(monkeypatch, tmp_path, capsys):
    saved = tmp_path / "nexar_login.bin"
    saved.write_bytes(b"protected")
    from eda_agent import altium365, server

    real = altium365.read_environment
    monkeypatch.setattr(
        altium365, "read_environment",
        lambda env=None: real({}).__class__(saved_login=saved))
    monkeypatch.setattr(sys, "argv", ["eda-agent", "altium365-logout"])

    assert server.main() == 0
    assert not saved.exists()
    assert "Removed the saved Nexar login" in capsys.readouterr().out


def test_logout_with_nothing_saved_says_so(monkeypatch, tmp_path, capsys):
    saved = tmp_path / "nexar_login.bin"
    from eda_agent import altium365, server

    real = altium365.read_environment
    monkeypatch.setattr(
        altium365, "read_environment",
        lambda env=None: real({}).__class__(saved_login=saved))
    monkeypatch.setattr(sys, "argv", ["eda-agent", "altium365-logout"])

    assert server.main() == 0
    assert "No saved Nexar login" in capsys.readouterr().out
