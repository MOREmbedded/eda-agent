# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The lint rule for a local that hides an enum constant.

DelphiScript names are case-insensitive. Gen_MirrorSchComponent declared
``EArc : ISch_EllipticalArc`` and compared ``Kind = eArc``; inside that
routine eArc was the unassigned local, 0, so no arc ever matched and every
arc of a mirrored symbol stayed put (measured 2026-10-11).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

LINT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "lint.py"


@pytest.fixture(scope="module")
def lint():
    spec = importlib.util.spec_from_file_location("_altium_lint_shadow", LINT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_altium_lint_shadow"] = mod
    spec.loader.exec_module(mod)
    return mod


SHADOWED = """Function Mirror(Params : String) : String;
Var
    Kind : Integer;
    EArc : ISch_EllipticalArc;
Begin
    Result := '{"kind":' + IntToStr(Kind) + '}';
    If Kind = eArc Then
        EArc := Nil;
End;
"""

FINE = """Function Mirror(Params : String) : String;
Var
    Kind : Integer;
    ElArc : ISch_EllipticalArc;
    eCount : Integer;
Begin
    Result := '{"kind":' + IntToStr(Kind) + '}';
    eCount := 1;
    If Kind = eArc Then
        ElArc := Nil;
End;
"""


def test_a_local_hiding_the_constant_is_flagged(lint):
    found = lint._scan_local_shadows_enum("t.pas", SHADOWED.split("\n"))
    assert [f.rule for f in found] == ["local-shadows-enum"]
    assert "Kind = eArc" in found[0].snippet


def test_distinct_names_and_a_local_used_as_spelled_pass(lint):
    assert lint._scan_local_shadows_enum("t.pas", FINE.split("\n")) == []


def test_the_scripts_are_clean(lint):
    altium = LINT_PATH.parent
    found = []
    for pas in altium.glob("*.pas"):
        if pas.name == "Altium_MCP.pas":
            continue
        lines = pas.read_text(encoding="utf-8").split("\n")
        found += lint._scan_local_shadows_enum(pas.name, lines)
    assert not found, [f.format() for f in found]
