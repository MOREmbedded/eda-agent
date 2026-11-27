# SPDX-License-Identifier: Apache-2.0
"""The lint rule for a local declared twice in one Var block.

A merge left ``BodyJson : String`` on two lines of PCB_GetComponents'
Var block. DelphiScript refused the whole bridge with "Identifier
redeclared: 'BodyJson'", so StartMCPServer would not run at all.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LINT_PATH = ROOT / "scripts" / "altium" / "lint.py"


@pytest.fixture(scope="module")
def lint():
    spec = importlib.util.spec_from_file_location("_altium_lint_twice", LINT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_altium_lint_twice"] = mod
    spec.loader.exec_module(mod)
    return mod


TWICE = """Function GetComponents(Params, RequestId : String) : String;
Var
    CommentStr, BodyJson : String;
    Count : Integer;
    BodyJson : String;
Begin
    Result := BodyJson;
End;
"""

NESTED = """Function Outer(Params : String) : String;

    Function Inner(Var MaxW : Integer) : Boolean;
    Var
        First : Boolean;
    Begin
        Result := First;
    End;

Var
    First : Boolean;
    MaxW : Integer;
Begin
    Result := '';
End;
"""


def _rules(lint, src):
    return [f for f in lint._scan_local_declared_twice("x.pas", src.splitlines())]


def test_flags_the_second_declaration(lint):
    found = _rules(lint, TWICE)
    assert [(f.rule, f.line) for f in found] == [("local-declared-twice", 5)]


def test_nested_routine_locals_are_separate_blocks(lint):
    assert _rules(lint, NESTED) == []


def test_no_source_file_declares_a_local_twice(lint):
    hits = []
    for pas in sorted((ROOT / "scripts" / "altium").glob("*.pas")):
        lines = pas.read_text(encoding="utf-8", errors="replace").splitlines()
        hits += [f"{pas.name}:{f.line}" for f in
                 lint._scan_local_declared_twice(pas.name, lines)]
    assert hits == []
