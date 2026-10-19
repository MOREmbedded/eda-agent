# SPDX-License-Identifier: Apache-2.0
"""Every DelphiScript routine closes every block it opens, and ends on End;.

The upstream 0.7.1 merge produced four Pascal breaks of one shape: a
conflict where both sides added something, and the ``End`` closing the
first block lived in the text the resolution cut away. Two of them
reached a commit (``Gen_SetSchComponentParameters`` and
``RebuildPCBConnectivity`` at 8f77d1b). FPC syntax checks and
``scripts/altium/lint.py`` passed all four; Altium was the first thing
to notice, by refusing to compile the script project.

So this reads each routine on its own and asks two questions:

* **balance**: ``Begin``/``Try``/``Case``/``Record`` openers minus
  ``End`` closers is zero;
* **ending**: the routine's last line of code ends on ``End;``.

Both are absolute, with no allowlist and no comparison against git.
That only works because comments AND string literals are blanked first:
the handlers build JSON and messages full of words like "End" and
"Begin", and counting those is where an earlier version of this check
drowned in false positives. A routine followed by a unit-level
``Var``/``Const``/``Type`` section is cut where that section starts, so
global declarations are not read as the routine's last line.

The self-tests at the bottom break a real routine the way the merge did
and assert the check notices. A structural check that has never been
seen failing is not known to work.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.pascal_source import strip_comments

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts" / "altium"

#: The concatenated bundle is generated from the others; checking it
#: would report every defect twice.
EXCLUDED = {"Altium_MCP.pas"}

_OPENERS = re.compile(r"\b(?:Begin|Try|Case|Record)\b", re.IGNORECASE)
_CLOSERS = re.compile(r"\bEnd\b", re.IGNORECASE)
_HEADER = re.compile(r"(?im)^(?=(?:Function|Procedure)\s+\w+)")
_NAME = re.compile(r"(?i)(?:Function|Procedure)\s+(\w+)")
_FIRST_BEGIN = re.compile(r"(?im)^\s*Begin\b")
_UNIT_SECTION = re.compile(r"(?im)^(?:Var|Const|Type)\b")
_ENDS_ON_END = re.compile(r"(?i)\bEnd\s*;\s*$")
_STRING = re.compile(r"'(?:[^'\n]|'')*'")


def _code(text: str) -> str:
    """Source with comments removed and every string literal emptied."""
    return _STRING.sub("''", strip_comments(text))


def routines(text: str) -> list[tuple[str, str]]:
    """``(name, body)`` per top-level routine, in file order.

    A list rather than a dict, so two routines with one name (an
    overload, or a merge that pasted one twice) are both checked.
    """
    found = []
    for part in _HEADER.split(_code(text)):
        name = _NAME.match(part)
        if not name:
            continue
        begin = _FIRST_BEGIN.search(part)
        if begin:
            section = _UNIT_SECTION.search(part, begin.end())
            if section:
                part = part[:section.start()]
        found.append((name.group(1), part))
    return found


def defects(text: str) -> list[str]:
    """One line per routine that is unbalanced or does not end on End;."""
    out = []
    for name, body in routines(text):
        balance = len(_OPENERS.findall(body)) - len(_CLOSERS.findall(body))
        if balance:
            out.append(f"{name}: {balance:+d} blocks left open"
                       if balance > 0 else
                       f"{name}: {-balance} more End than openers")
        lines = [line.strip() for line in body.split("\n") if line.strip()]
        if not lines or not _ENDS_ON_END.search(lines[-1]):
            last = lines[-1] if lines else ""
            out.append(f"{name}: does not end on End; (last line: {last[:60]!r})")
    return out


def _sources() -> list[Path]:
    return sorted(p for p in SCRIPTS_DIR.glob("*.pas")
                  if p.name not in EXCLUDED and ".tmp." not in p.name)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def test_sources_are_found():
    # Vacuity floor: a glob or splitter that stopped matching would turn
    # every check below into a pass over nothing.
    sources = _sources()
    assert len(sources) >= 10, [p.name for p in sources]
    total = sum(len(routines(_read(p))) for p in sources)
    assert total >= 800, f"only {total} routines parsed"


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_every_routine_is_closed(path):
    found = defects(_read(path))
    assert not found, (
        f"{path.name}: routines whose block structure is broken. Altium "
        f"will refuse to compile the script project, and the linter does "
        f"not see this:\n  " + "\n  ".join(found))


# --- the check itself --------------------------------------------------

_SAMPLE = """\
Function Sample(A : Integer) : String;
Var
    I : Integer;
Begin
    Result := 'Begin, End and Case in a string are not code';
    { an End in a comment is not code either }
    Try
        Case A Of
            1 : Result := 'one';
        End;
        If A > 1 Then
        Begin
            Result := 'many';
        End;
    Except
        Result := '';
    End;
End;

Var
    UnitLevelState : Boolean;
"""


def test_a_well_formed_routine_passes():
    assert defects(_SAMPLE) == []


def test_a_block_whose_end_was_cut_is_caught():
    # The merge shape: one block's End gone, the routine's last line
    # still a plausible End;.
    broken = _SAMPLE.replace(
        "            Result := 'many';\n        End;\n",
        "            Result := 'many';\n", 1)
    assert broken != _SAMPLE
    found = defects(broken)
    assert found == ["Sample: +1 blocks left open"], found


def test_a_routine_missing_its_final_end_is_caught():
    # The Except's End; is now the last line, so the ending check is
    # satisfied; the balance is what catches this one.
    broken = _SAMPLE.replace("    End;\nEnd;\n", "    End;\n", 1)
    assert broken != _SAMPLE
    found = defects(broken)
    assert found == ["Sample: +1 blocks left open"], found


def test_a_routine_that_ends_on_something_else_is_caught():
    broken = _SAMPLE.replace("    End;\nEnd;\n", "    End;\nEnd;\nFoo;\n", 1)
    found = defects(broken)
    assert found == ["Sample: does not end on End; (last line: 'Foo;')"], found


def test_the_merge_breaks_that_reached_a_commit_are_caught():
    # 8f77d1b is the upstream 0.7.1 merge before f0e0d5b and 7b3ad16
    # restored the two End lines it lost. Skipped where that commit is
    # not available (a shallow clone, an sdist).
    import subprocess

    commit = "8f77d1b"
    found = []
    for name in ("Generic.pas", "PCBGeneric.pas"):
        try:
            shown = subprocess.run(
                ["git", "show", f"{commit}:scripts/altium/{name}"],
                cwd=REPO_ROOT, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pytest.skip("git is not available")
        if shown.returncode != 0:
            pytest.skip(f"{commit} is not in this clone")
        found += defects(shown.stdout.decode("utf-8", errors="replace"))
    assert found == ["Gen_SetSchComponentParameters: +1 blocks left open",
                     "RebuildPCBConnectivity: +1 blocks left open"], found


def test_a_stray_end_is_caught():
    broken = _SAMPLE.replace("End;\n\nVar", "End;\nEnd;\n\nVar", 1)
    found = defects(broken)
    assert found == ["Sample: 1 more End than openers"], found


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_removing_any_end_from_a_real_file_is_caught(path):
    # Not just the toy above: take a real module and drop each of its
    # first twenty "End;" lines in turn. Every one must be reported.
    text = _read(path)
    lines = text.split("\n")
    ends = [i for i, line in enumerate(lines)
            if line.strip().lower() == "end;"][:20]
    assert ends, f"{path.name} has no End; lines to remove"
    for i in ends:
        mutated = "\n".join(lines[:i] + lines[i + 1:])
        assert defects(mutated), (
            f"{path.name}: removing the End; on line {i + 1} went unnoticed")
