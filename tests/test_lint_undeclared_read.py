# SPDX-License-Identifier: Apache-2.0
"""The lint rule for undeclared identifiers that a routine READS.

DelphiScript resolves names late. An identifier nothing declares compiles,
then stops the bridge with "Undeclared identifier: X" the moment its line
runs. Two of these shipped:

  ErrorLevel, SuppressedText -- Gen_GetErcViolations built its JSON with
      both, neither declared anywhere, so proj_run_erc and
      proj_get_erc_violations crashed on every call (AD 26.11, 2026-11-10).
  FindPadNetByName -- called three times in PCB.pas after a merge on
      2026-10-10 dropped its definition (pcb.place_via with a net,
      pcb.rebind_copper_to_pad_nets).

The undeclared-local and undeclared-assignment rules saw neither. These
tests pin the new rule on the real scripts: the shipped tree passes, and
putting either break back makes the lint fail.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from pathlib import Path

import pytest

ALTIUM = Path(__file__).resolve().parents[1] / "scripts" / "altium"
LINT_PATH = ALTIUM / "lint.py"


@pytest.fixture(scope="module")
def lint():
    spec = importlib.util.spec_from_file_location("_altium_lint_undecl_read",
                                                  LINT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_altium_lint_undecl_read"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def unit(tmp_path):
    def write(text: str, name: str = "T.pas") -> str:
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        return str(p)
    return write


def _names(findings) -> set:
    return {re.search(r"never declares (\w+)\]", f.snippet).group(1)
            for f in findings}


# --------------------------------------------------------------------------
# The rule on small units
# --------------------------------------------------------------------------

ERC_BREAK = """Function Gen_GetErcViolations(Params : String) : String;
Var
    Level : Integer;
Begin
    Level := 2;
    Result := '{"error_level":' + IntToStr(ErrorLevel) +
        ',"suppressed":' + SuppressedText +
        ',"level":' + IntToStr(Level) + '}';
End;
"""

ERC_FIXED = """Function Gen_GetErcViolations(Params : String) : String;
Var
    Level : Integer;
Begin
    Level := 2;
    Result := '{"error_level":' + IntToStr(Level) + '}';
End;
"""


def test_the_erc_crash_is_flagged(lint, unit):
    found = lint._scan_undeclared_read([unit(ERC_BREAK)], external=set())
    assert _names(found) == {"ErrorLevel", "SuppressedText"}
    assert all(f.rule == "undeclared-read" for f in found)
    assert all(f.severity == "error" for f in found)


def test_the_erc_fix_passes(lint, unit):
    assert lint._scan_undeclared_read([unit(ERC_FIXED)], external=set()) == []


TOLERATED = """Function Reader(Params : String) : String;
Var
    Comp : ISch_Component;
Begin
    Result := Comp.Designator.Text + #13#10 + IntToStr($FF) + Params;
    If Comp.&Type = 1 Then Result := FloatToStr(1e5);
End;
"""


def test_members_hex_char_codes_and_exponents_are_not_names(lint, unit):
    assert lint._scan_undeclared_read([unit(TOLERATED)],
                                      external={"floattostr"}) == []


def test_a_listed_external_name_passes_in_any_case(lint, unit):
    src = """Function Reader(Params : String) : String;
Begin
    Result := IntToStr(ESCHCOMPONENT);
End;
"""
    assert lint._scan_undeclared_read([unit(src)], external=set()) != []
    assert lint._scan_undeclared_read([unit(src)],
                                      external={"eschcomponent"}) == []


def test_a_routine_from_another_unit_is_in_scope(lint, unit):
    a = unit("""Function Helper(X : Integer) : Integer;
Begin
    Result := X;
End;
""", "A.pas")
    b = unit("""Function User(Params : String) : String;
Begin
    Result := IntToStr(Helper(1));
End;
""", "B.pas")
    assert lint._scan_undeclared_read([a, b], external=set()) == []


def test_one_finding_per_name_per_routine(lint, unit):
    src = """Function Reader(Params : String) : String;
Begin
    Result := Missing + Missing;
    Result := Result + Missing;
End;
"""
    assert len(lint._scan_undeclared_read([unit(src)], external=set())) == 1


# --------------------------------------------------------------------------
# The real scripts
# --------------------------------------------------------------------------

def _real_targets(root: Path, lint) -> list:
    return [str(root / f) for f in lint.PAS_FILES]


def test_the_shipped_scripts_pass(lint):
    assert lint._scan_undeclared_read(_real_targets(ALTIUM, lint)) == []


@pytest.fixture
def tree(tmp_path, lint):
    """A copy of the real units, to seed a break into."""
    for f in lint.PAS_FILES:
        shutil.copy(ALTIUM / f, tmp_path / f)
    return tmp_path


def test_putting_the_erc_typo_back_fails_the_lint(lint, tree):
    gen = tree / "Generic.pas"
    text = gen.read_text(encoding="utf-8")
    good = "            ',\"error_level\":' + IntToStr(Level) +\n"
    assert text.count(good) == 1, "Gen_GetErcViolations changed shape"
    gen.write_text(text.replace(
        good,
        "            ',\"error_level\":' + IntToStr(ErrorLevel) +\n"
        "            ',\"suppressed\":' + SuppressedText +\n"), encoding="utf-8")
    found = lint._scan_undeclared_read(_real_targets(tree, lint))
    assert _names(found) == {"ErrorLevel", "SuppressedText"}
    assert all(f.file.endswith("Generic.pas") for f in found)


def test_dropping_find_pad_net_by_name_fails_the_lint(lint, tree):
    pcb = tree / "PCB.pas"
    text = pcb.read_text(encoding="utf-8")
    m = re.search(r"^Function FindPadNetByName\b.*?^End;\n", text,
                  re.MULTILINE | re.DOTALL)
    assert m, "FindPadNetByName is no longer defined in PCB.pas"
    pcb.write_text(text[:m.start()] + text[m.end():], encoding="utf-8")
    found = lint._scan_undeclared_read(_real_targets(tree, lint))
    assert _names(found) == {"FindPadNetByName"}
    # Every calling routine is reported, not just the first.
    assert len(found) == 3


def test_run_lint_counts_the_finding(lint, unit):
    # Wired into run_lint, which build.py aborts on. A small unit, not the
    # whole tree: every other rule runs too and the tree takes minutes.
    src = ERC_FIXED.replace("IntToStr(Level)", "IntToStr(ErrorLeveI)")
    assert lint.run_lint([unit(ERC_FIXED)], verbose=False) == 0
    assert lint.run_lint([unit(src)], verbose=False) == 1


# --------------------------------------------------------------------------
# The external-names list stays honest
# --------------------------------------------------------------------------

def test_the_list_holds_no_name_the_units_declare(lint):
    # A declared name on the list would hide nothing today, but would keep
    # covering the name after its declaration is deleted.
    declared = set()
    for f in lint.PAS_FILES:
        lines = lint._blank_block_comments(
            (ALTIUM / f).read_text(encoding="utf-8").split("\n"))
        declared |= lint._file_level_names(lines)
    assert lint._load_external_names() & declared == set()


def test_every_listed_name_is_still_read(lint):
    # A stale entry is a hole: a later typo that happens to match it passes.
    raw = lint._scan_undeclared_read(_real_targets(ALTIUM, lint),
                                     external=set())
    used = {n.lower() for n in _names(raw)}
    assert lint._load_external_names() - used == set()


def test_the_known_breaks_are_not_listed(lint):
    listed = lint._load_external_names()
    for name in ("errorlevel", "errorlevei", "suppressedtext",
                 "findpadnetbyname"):
        assert name not in listed
