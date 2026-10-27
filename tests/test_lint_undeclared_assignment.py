# SPDX-License-Identifier: Apache-2.0
"""The lint rules for undeclared assignment targets and unbalanced brackets.

Both classes shipped on 2026-10-27 and Altium reported them one at a time,
each costing a close / reopen / run cycle:

  "Variable <OpFailed> not defined"  -- ten of these, from a merge that
      brought in the uses of ten locals without their declarations.
  "; expected"                       -- five of these, from a refactor onto
      TStringList.Add() that was half-applied to five JSON arrays, each
      keeping `XxxJson := XxxJson +` and gaining the Add call's `)`.

The suite never compiles Pascal, so neither class was visible to it. These
rules are what makes `build.py` fail instead of Altium.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ALTIUM = Path(__file__).resolve().parents[1] / "scripts" / "altium"
LINT_PATH = ALTIUM / "lint.py"


@pytest.fixture(scope="module")
def lint():
    spec = importlib.util.spec_from_file_location("_altium_lint_undecl",
                                                  LINT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_altium_lint_undecl"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def unit(tmp_path):
    """Write one .pas and return the path, for the cross-file scanner."""
    def write(text: str, name: str = "T.pas") -> str:
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        return str(p)
    return write


# --------------------------------------------------------------------------
# Undeclared assignment targets
# --------------------------------------------------------------------------

MISSING = """Function Gen_BatchDelete(Params : String) : String;
Var
    OpCount : Integer;
Begin
    OpCount := 0;
    OpFailed := 0;
    Inc(OpFailed);
    Result := IntToStr(OpCount);
End;
"""

DECLARED = """Function Gen_BatchDelete(Params : String) : String;
Var
    OpCount : Integer;
    OpFailed : Integer;
Begin
    OpCount := 0;
    OpFailed := 0;
    Inc(OpFailed);
    Result := IntToStr(OpCount);
End;
"""


def test_an_undeclared_target_is_flagged(lint, unit):
    found = lint._scan_undeclared_assignment([unit(MISSING)])
    assert [f.rule for f in found] == ["undeclared-assignment"]
    assert "OpFailed" in found[0].snippet
    assert found[0].severity == "error"


def test_one_finding_per_name_not_per_use(lint, unit):
    # OpFailed is assigned AND incremented; reporting twice would make a
    # single omission look like two bugs.
    assert len(lint._scan_undeclared_assignment([unit(MISSING)])) == 1


def test_a_declared_target_passes(lint, unit):
    assert lint._scan_undeclared_assignment([unit(DECLARED)]) == []


INLINE_VAR = """Function SplitNextTab(Var S : String) : String;
Var P : Integer;
Begin
    P := Pos(#9, S);
    Result := S;
End;
"""


def test_a_declaration_on_the_var_line_itself_counts(lint, unit):
    # `Var P : Integer;` puts the name on the keyword's own line.
    assert lint._scan_undeclared_assignment([unit(INLINE_VAR)]) == []


WRAPPED_VAR = """Function Lib_SetLabelFormats(Params : String) : String;
Var
    OpTargets, OpFontIds, OpColors, OpIsHidden,
        OpOrientation, OpXs, OpYs : TStringList;
Begin
    OpTargets := TStringList.Create;
    OpIsHidden := TStringList.Create;
    OpYs := TStringList.Create;
    Result := '';
End;
"""


def test_a_declaration_that_wraps_across_lines_counts(lint, unit):
    # The first line carries no ':', so a per-line reader drops it and
    # calls four declared variables undeclared.
    assert lint._scan_undeclared_assignment([unit(WRAPPED_VAR)]) == []


PROPERTY_WRITE = """Function Touch(Params : String) : String;
Var
    Comp : ISch_Component;
Begin
    Comp.Designator.Text := 'R1';
    Result := '';
End;
"""


def test_a_property_write_is_not_a_local(lint, unit):
    assert lint._scan_undeclared_assignment([unit(PROPERTY_WRITE)]) == []


GLOBAL_OWNER = """Function First(Params : String) : String;
Begin
    Result := '';
End;

Var
    PumpRunning : Boolean;

Function Second(Params : String) : String;
Begin
    Result := '';
End;
"""

GLOBAL_USER = """Function Third(Params : String) : String;
Begin
    PumpRunning := True;
    Result := '';
End;
"""


def test_a_global_from_another_unit_is_in_scope(lint, unit):
    # build.py concatenates the units into one namespace, so a global
    # declared in Main.pas is visible from Generic.pas. Judging this per
    # file reports every cross-unit global as undeclared.
    targets = [unit(GLOBAL_OWNER, "A.pas"), unit(GLOBAL_USER, "B.pas")]
    assert lint._scan_undeclared_assignment(targets) == []


# --------------------------------------------------------------------------
# Bracket balance
# --------------------------------------------------------------------------

STRAY_CLOSE = """Function Gen_GetPcbGeometry(Params : String) : String;
Var
    TracksJson : String;
Begin
    TracksJson := TracksJson +
        '{"x1":' + FloatToJsonStr(X1) +
        ',"net":"' + EscapeJsonString(NetName) + '"}');
    Result := TracksJson;
End;
"""

BALANCED = """Function Gen_GetPcbGeometry(Params : String) : String;
Var
    TracksParts : TStringList;
Begin
    TracksParts.Add(
        '{"x1":' + FloatToJsonStr(X1) +
        ',"net":"' + EscapeJsonString(NetName) + '"}');
    Result := '[' + TracksParts.Text + ']';
End;
"""

UNCLOSED = """Function Opener(Params : String) : String;
Begin
    Result := Trim(Copy(Params, 1, 5);
End;

Function Next(Params : String) : String;
Begin
    Result := '';
End;
"""


def test_a_stray_close_is_flagged(lint):
    found = lint._scan_bracket_balance("t.pas", STRAY_CLOSE)
    assert [f.rule for f in found] == ["unbalanced-bracket"]
    assert "never opened" in found[0].snippet


def test_the_add_form_is_balanced(lint):
    assert lint._scan_bracket_balance("t.pas", BALANCED) == []


def test_an_unclosed_open_is_reported_where_it_opened(lint):
    found = lint._scan_bracket_balance("t.pas", UNCLOSED)
    assert len(found) == 1
    # The line to look at is the opener, not the next routine's header.
    assert found[0].line == 3
    assert "Opener ends" in found[0].snippet


BRACES_IN_STRINGS = """Function Respond(Params : String) : String;
Begin
    Result := '{"matched":' + IntToStr(N) + '}';
    Result := Result + '[' + Body + ']';
End;
"""


def test_braces_and_brackets_inside_string_literals_are_ignored(lint):
    assert lint._scan_bracket_balance("t.pas", BRACES_IN_STRINGS) == []


# --------------------------------------------------------------------------
# The helper both rules stand on
# --------------------------------------------------------------------------

def test_a_json_literal_does_not_open_a_comment(lint):
    """`'{"matched":'` is a string, not the start of a `{ ... }` comment.

    _blank_block_comments ignored strings AND nested braces, so the first
    JSON literal in a unit opened a comment that the next real comment's
    `}` could not close. In Generic.pas that hid 124 of 174 routines from
    every rule built on this helper.
    """
    src = [
        "Function A(P : String) : String;",
        "Begin",
        "    Result := '{\"matched\":' + IntToStr(N) +",
        "        { a real comment, which must still be blanked }",
        "        ',\"n\":1}';",
        "End;",
        "",
        "Function B(P : String) : String;",
        "Begin",
        "    Result := '';",
        "End;",
    ]
    out = lint._blank_block_comments(src)
    assert out[7].startswith("Function B"), out[7]
    assert "real comment" not in out[3]


def test_every_routine_in_the_scripts_is_visible_to_the_rules(lint):
    """No unit may hide routines from the line-based rules.

    This is the guard on the bug above: a silent drop in coverage here
    disables several rules at once and nothing else would notice.
    """
    import re

    head = re.compile(r"^(?:Function|Procedure)\s+(\w+)", re.IGNORECASE)
    for name in lint.PAS_FILES:
        raw = (ALTIUM / name).read_text(encoding="utf-8").split("\n")
        blanked = lint._blank_block_comments(raw)
        before = {head.match(l).group(1) for l in raw if head.match(l)}
        after = {head.match(l).group(1) for l in blanked if head.match(l)}
        assert before == after, f"{name} hides {sorted(before - after)}"


def test_the_scripts_are_clean(lint):
    targets = [str(ALTIUM / f) for f in lint.PAS_FILES]
    found = lint._scan_undeclared_assignment(targets)
    for path in targets:
        text = Path(path).read_text(encoding="utf-8")
        found += lint._scan_bracket_balance(Path(path).name, text)
    assert not found, [f.format() for f in found]
