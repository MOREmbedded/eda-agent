# SPDX-License-Identifier: Apache-2.0
"""What the scripting engine will actually resolve, read off its own table.

`reflect.py` reads Altium.SDK.Interfaces.dll and reports what Altium
DECLARES. That is not the same question as what a DelphiScript line can
reach, and the gap is not academic: a declared-but-unregistered member
halts the script debugger on the line that touches it, and no
Try/Except catches it. Measured, not assumed - the handler in
Generic.pas wraps the call and the Except block does not run.

ScriptingSystem.dll carries the engine's registered symbol table as
UTF-16 runs, laid out per member as a triple:

    DisplayMode / GetState_DisplayMode / SetState_DisplayMode

A member reachable as `Obj.Name` has the bare name registered. One that
only has `GetState_Name` is reachable by that spelling and NOT by the
property syntax, which is exactly what "Undeclared identifier" means
when the assembly plainly declares the property.

### What this is allowed to claim

Names are recovered as maximal runs of printable characters, so a short
name can be merged with whatever sits beside it in the binary. X, Y, X1,
X2, Y1 and Y2 are read off real objects at hundreds of sites in our own
Pascal and still do not appear as standalone runs. The table therefore
ABSTAINS below MIN_RELIABLE characters rather than calling those
unreachable, because a confident wrong answer here deletes working code.

Validated against every piece of live evidence available: 283 names
across the four shipped property chains yield zero cases where the
table calls a name unreachable that the Pascal dereferences, and a live
probe of 26 approved names answered 26.

The output is derived from a non-redistributable assembly, so it stays
in reference/, which .gitignore excludes.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

#: Shorter than this, a name cannot be told apart from its neighbours in
#: the binary, so the table declines to answer instead of guessing.
MIN_RELIABLE = 3

SYMBOLS_ENV = "EDA_AGENT_SDK_SYMBOLS"
SCRIPTING_DLL = "ScriptingSystem.dll"

PLAIN = "plain"
GETTER = "getter"
ABSENT = "absent"
UNKNOWN = "unknown"

_GET = "GetState_"
_SET = "SetState_"

#: A UTF-16LE run of printable ASCII. Maximal by construction, which is
#: what stops a name matching inside a longer string: the reason an
#: earlier substring search reported DescriptionString as present when
#: the engine has no such identifier.
_RUN = re.compile(rb"(?:[\x20-\x7e]\x00){%d,}" % MIN_RELIABLE)


class SymbolsUnavailable(RuntimeError):
    """The engine's symbol table has not been extracted on this machine.

    Carries the instruction to produce it, because every caller would
    otherwise have to repeat it.
    """

    def __init__(self, path: Path):
        super().__init__(
            f"no scripting symbol table at {path}.\n"
            f"Produce it on a machine with Altium installed:\n"
            f"    eda-agent sdk-symbols --refresh\n"
            f"It reads {SCRIPTING_DLL} from the install that "
            f"reference/altium_sdk.json was reflected from. The output "
            f"stays local: it is derived from a non-redistributable "
            f"assembly and .gitignore excludes reference/."
        )
        self.path = path


def symbols_json_path(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve where the extracted symbol table should be read from."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(SYMBOLS_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    # parents[3] is the repo root: src/eda_agent/sdk/symbols.py
    return Path(__file__).resolve().parents[3] / "reference" / \
        "altium_scripting_symbols.json"


def scripting_dll_beside(sdk_source: str) -> Path:
    """The engine DLL next to the assembly the SDK dump was read from.

    Both live in the install's System folder, so the path already
    recorded in the SDK dump saves discovering the install twice.
    """
    return Path(sdk_source).parent / SCRIPTING_DLL


def extract(dll: str | os.PathLike) -> dict:
    """Read the registered names out of the engine DLL."""
    path = Path(dll)
    blob = path.read_bytes()
    names = sorted({m.group().decode("utf-16-le")
                    for m in _RUN.finditer(blob)})
    return {
        "schema": 1,
        "source": str(path),
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
        "min_reliable": MIN_RELIABLE,
        "names": names,
    }


class SymbolTable:
    """Membership over the engine's registered names."""

    def __init__(self, doc: dict):
        self.source: str = doc.get("source", "")
        self.extracted_at: str = doc.get("extracted_at", "")
        self.min_reliable: int = int(doc.get("min_reliable") or MIN_RELIABLE)
        self.names: frozenset[str] = frozenset(doc.get("names") or ())
        self.getters: frozenset[str] = frozenset(
            n[len(_GET):] for n in self.names if n.startswith(_GET))
        self.setters: frozenset[str] = frozenset(
            n[len(_SET):] for n in self.names if n.startswith(_SET))

    def __len__(self) -> int:
        return len(self.names)

    def reach(self, name: str) -> str:
        """How, if at all, DelphiScript can get at this member.

        UNKNOWN is a real answer, not a failure: see MIN_RELIABLE.
        """
        if len(name) < self.min_reliable:
            return UNKNOWN
        if name in self.names:
            return PLAIN
        if name in self.getters or name in self.setters:
            return GETTER
        return ABSENT

    def emittable(self, name: str) -> bool:
        """May the generator write `Obj.{name}` without risking a halt?

        UNKNOWN counts as emittable. The six names it covers are
        coordinates the shipped chains have read for months, so refusing
        them would delete working code to avoid a hypothetical.
        """
        return self.reach(name) in (PLAIN, UNKNOWN)


def load_symbols(path: str | os.PathLike | None = None) -> SymbolTable:
    """Read the extracted table, or say how to produce it."""
    resolved = symbols_json_path(path)
    if not resolved.is_file():
        raise SymbolsUnavailable(resolved)
    return SymbolTable(json.loads(resolved.read_text(encoding="utf-8-sig")))


def refresh(sdk_source: str, path: str | os.PathLike | None = None) -> Path:
    """Extract the table from the install and cache it. Returns the path."""
    dll = scripting_dll_beside(sdk_source)
    if not dll.is_file():
        raise SymbolsUnavailable(dll)
    target = symbols_json_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    doc = extract(dll)
    target.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return target
