# SPDX-License-Identifier: Apache-2.0
"""Catch the set-membership syntax rejected by the live Altium compiler."""
import importlib.util
import sys
from pathlib import Path


def test_set_membership_regression(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts/altium/lint.py"
    spec = importlib.util.spec_from_file_location("_altium_lint_set_membership", path)
    lint = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = lint
    spec.loader.exec_module(lint)
    source = tmp_path / "membership.pas"
    source.write_text("""Procedure Check;
Begin
    If eError_SinglePinNet In Errors Then Result := True;
    If InSet(eError_SinglePinNet, Errors) Then Result := True;
    // If A in B then this comment must not trigger.
    If MessageText = 'in the set' Then Result := False;
End;
""")
    findings = [f for f in lint.lint_file(str(source))
                if f.rule == "unsupported-pascal-in-operator"]
    assert len(findings) == 1
    assert findings[0].line == 3
