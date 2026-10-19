# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The lint catches a comparison next to Or/And/Xor without brackets.

Pascal binds Or, And and Xor tighter than = and <>, so one unbracketed
line in a property allow-list read as nonsense and nothing flagged it.
Prose inside a multi-line comment must not be flagged.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

LINT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "altium" / "lint.py"


@pytest.fixture(scope="module")
def lint():
    spec = importlib.util.spec_from_file_location("_altium_lint_cmp", LINT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_altium_lint_cmp"] = mod
    spec.loader.exec_module(mod)
    return mod


def _rules(lint, text):
    return [f.line for f in lint._scan_unbracketed_comparison("x.pas", text)]


@pytest.mark.parametrize("line", [
    "    Result := (A = 1) Or\n        PropName = 'Opacity' Or\n        (B = 2);",
    "    If Kind = 3 And Done Then Exit;",
    "    If Ok Or Count > 0 Then Exit;",
    "    If (A = 1) Or B.Name <> '' Then Exit;",
])
def test_an_unbracketed_comparison_is_flagged(lint, line):
    assert _rules(lint, line)


@pytest.mark.parametrize("line", [
    "    Result := (PropName = 'A') Or (PropName = 'B');",
    "    If (Kind = 3) And Done Then Exit;",
    "    If Ok Or Not (Count > 0) Then Exit;",
    "    S := 'A = 1 Or B = 2';",
    "    { width or height = a zero area } X := 1;",
    "    { first line\n      prose where Action=Update Or B = 2 reads like code }\n    X := 1;",
    "    // A = 1 Or B = 2",
])
def test_bracketed_code_strings_and_comments_pass(lint, line):
    assert _rules(lint, line) == []
