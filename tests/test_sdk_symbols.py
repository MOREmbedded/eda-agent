# SPDX-License-Identifier: Apache-2.0
"""The scripting engine's registered names, read off ScriptingSystem.dll.

The assembly declares what Altium HAS. This table says what DelphiScript
can REACH, and the two disagree often enough to matter: a declared
property whose bare name is not registered halts the script debugger on
the line that reads it, with nothing able to catch it.

The load-bearing test here is the last one. It checks the table against
the only ground truth available -- property names our own shipped Pascal
dereferences off real Altium objects, in code that has been running for
months -- and fails if the table would have told us to delete any of
them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from eda_agent.sdk.symbols import (ABSENT, GETTER, MIN_RELIABLE, PLAIN,
                                   UNKNOWN, SymbolsUnavailable, SymbolTable,
                                   extract, load_symbols,
                                   scripting_dll_beside)

REPO = Path(__file__).resolve().parents[1]
PAS = REPO / "scripts" / "altium"


def blob_of(*names: str) -> bytes:
    """A binary shaped like the real one: UTF-16 runs, unprintable gaps."""
    return b"\x01\x00".join(n.encode("utf-16-le") for n in names)


@pytest.fixture
def table() -> SymbolTable:
    return SymbolTable(extract_bytes(blob_of(
        "DisplayMode", "GetState_DisplayMode", "SetState_DisplayMode",
        "AliasAsText", "GetState_AliasAsText",
        "GetState_DescriptionString",
        "ReadOnlyThing", "SetState_WriteOnlyThing")))


def extract_bytes(data: bytes) -> dict:
    """extract() takes a path, so give it one the test owns."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".dll", delete=False) as fh:
        fh.write(data)
        tmp = fh.name
    return extract(tmp)


# --------------------------------------------------------------------------
# What the table says, and why
# --------------------------------------------------------------------------

def test_a_registered_name_is_reachable_as_a_property(table):
    assert table.reach("DisplayMode") == PLAIN
    assert table.emittable("DisplayMode")


def test_a_getter_only_name_is_not_reachable_as_a_property(table):
    """The exact shape of the fault that halted the debugger twice.

    GetState_DescriptionString is registered; DescriptionString is not.
    Altium's error for this is "Undeclared identifier", which is literal.
    """
    assert table.reach("DescriptionString") == GETTER
    assert not table.emittable("DescriptionString")


def test_a_setter_only_name_is_reported_through_its_setter(table):
    assert table.reach("WriteOnlyThing") == GETTER


def test_an_unregistered_name_is_absent(table):
    assert table.reach("NoSuchMemberAnywhere") == ABSENT
    assert not table.emittable("NoSuchMemberAnywhere")


def test_a_short_name_is_unknown_rather_than_absent(table):
    """X and Y are read off real objects at hundreds of sites.

    They do not survive extraction as standalone runs, so calling them
    absent would delete working code. Abstaining is the honest answer,
    and emittable() treats it as permission rather than refusal.
    """
    assert table.reach("X1") == UNKNOWN
    assert table.emittable("X1")
    assert len("X1") < MIN_RELIABLE


def test_a_name_inside_a_longer_run_does_not_count_as_registered():
    """The bug that made the first scan report DescriptionString present.

    A substring search finds it inside ParameterDescriptionStringList; a
    maximal-run extraction does not, and only the second matches what
    the engine will resolve.
    """
    t = SymbolTable(extract_bytes(blob_of("ParameterDescriptionStringList")))
    assert t.reach("DescriptionString") == ABSENT


def test_getters_and_setters_are_split_out(table):
    assert "DisplayMode" in table.getters
    assert "DisplayMode" in table.setters
    assert "AliasAsText" in table.getters
    assert "AliasAsText" not in table.setters


# --------------------------------------------------------------------------
# Finding and producing it
# --------------------------------------------------------------------------

def test_the_engine_dll_is_looked_for_beside_the_reflected_assembly():
    found = scripting_dll_beside(r"F:\x\System\Altium.SDK.Interfaces.dll")
    assert found.name == "ScriptingSystem.dll"
    assert found.parent.name == "System"


def test_a_missing_table_says_how_to_produce_it():
    with pytest.raises(SymbolsUnavailable) as raised:
        load_symbols(REPO / "reference" / "does-not-exist.json")
    message = str(raised.value)
    assert "eda-agent sdk-symbols --refresh" in message
    assert "gitignore" in message.lower()


# --------------------------------------------------------------------------
# The table against the only ground truth there is
# --------------------------------------------------------------------------

def test_the_table_never_contradicts_the_shipped_pascal():
    """Every member our Pascal reads off an object must be emittable.

    This is the test that fails if the extraction regresses. A name the
    table calls unreachable, which production code dereferences and has
    dereferenced for months, is the table being wrong -- and acting on
    it would delete working property chains.

    Skipped without the extracted table, which only exists on a machine
    with Altium installed.
    """
    try:
        table = load_symbols()
    except SymbolsUnavailable:
        pytest.skip("no reference/altium_scripting_symbols.json here")

    from eda_agent.sdk.coverage import CHAINS, handled_heads

    source = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(PAS.glob("*.pas")) if p.name != "Altium_MCP.pas")

    contradicted = []
    for chain in CHAINS:
        for name in sorted(handled_heads(chain)):
            if table.emittable(name):
                continue
            # Only a real dereference counts. Plenty of chain entries are
            # our own synthesized names (ConnectionX, BoundsTop) that no
            # Altium object ever had.
            if re.search(r"\w+\.%s\b" % re.escape(name), source):
                contradicted.append((chain, name))

    assert not contradicted, (
        "the symbol table calls these unreachable, but the Pascal reads "
        f"them off real objects: {contradicted}")
