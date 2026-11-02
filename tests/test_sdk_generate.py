# SPDX-License-Identifier: Apache-2.0
"""Generating Pascal property accessors from the SDK declarations.

The generator turns "the assembly declares this" into a chain entry. It
cannot turn it into a capability: Altium's DelphiScript binds members
late, so a declared-but-unexposed member compiles and faults at runtime
where Try/Except cannot catch it. That is why the output goes into a
probe first and only what answers is folded into GetSchProperty.

These tests are about the Pascal being correct Pascal, and about the
generator refusing to emit what it cannot express.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eda_agent.sdk.generate import (BEGIN_MARKER, BLOCK_ALLOW_GETTER,
                                    END_MARKER, Skipped,
                                    _reject, member_spelling, plan,
                                    render_block, render_probe,
                                    render_write, replace_block)
from eda_agent.sdk.reflect import Property, SdkModel, SdkUnavailable, load_sdk
from eda_agent.sdk.symbols import (SymbolsUnavailable, SymbolTable,
                                   load_symbols)

REPO = Path(__file__).resolve().parents[1]
GENERIC = REPO / "scripts" / "altium" / "Generic.pas"


FAKE = {
    "schema": 1,
    "assembly": "fake",
    "interfaces": {
        "SCH.ISch_Thing": {
            "name": "ISch_Thing",
            "namespace": "SCH",
            "inherits": [],
            "properties": {
                "Caption": {"name": "Caption", "get": "String",
                            "set": "String"},
                "Count": {"name": "Count", "get": "Int32"},
                "Visible": {"name": "Visible", "get": "Boolean",
                            "set": "Boolean"},
                "Ratio": {"name": "Ratio", "get": "Double", "set": "Double"},
                "Trigger": {"name": "Trigger", "set": "Int32"},
                "Blob": {"name": "Blob", "get": "Object", "set": "Object"},
                "ItemAt": {"name": "ItemAt", "get": "String",
                           "get_index": ["Int32"]},
            },
            "methods": [],
        }
    },
}


@pytest.fixture
def model() -> SdkModel:
    return SdkModel(FAKE)


# --------------------------------------------------------------------------
# What it refuses to emit
# --------------------------------------------------------------------------

def test_an_indexed_property_is_skipped(model):
    prop = model.effective_properties("ISch_Thing")["ItemAt"]
    assert "indexed" in _reject(prop, "read")


def test_a_type_that_does_not_cross_the_string_bridge_is_skipped(model):
    prop = model.effective_properties("ISch_Thing")["Blob"]
    reason = _reject(prop, "read")
    assert "Object" in reason and "String bridge" in reason


def test_a_write_only_property_is_skipped_from_the_read_chain(model):
    prop = model.effective_properties("ISch_Thing")["Trigger"]
    assert _reject(prop, "read") == "write only"
    assert _reject(prop, "write") is None


def test_a_read_only_property_is_skipped_from_the_write_chain(model):
    prop = model.effective_properties("ISch_Thing")["Count"]
    assert _reject(prop, "write") == "read only"
    assert _reject(prop, "read") is None


def test_a_name_the_engine_cannot_resolve_is_refused(model):
    """The gate that stops a halted debugger reaching anyone.

    ISch_Component declares DescriptionString and Altium registers only
    GetState_DescriptionString, so `Obj.DescriptionString` faults where
    Try/Except cannot catch it. Measured live, twice, before this gate
    existed.
    """
    table = SymbolTable({"names": ["Caption", "GetState_Count"],
                         "min_reliable": 3})
    prop = model.effective_properties("ISch_Thing")["Count"]
    reason = _reject(prop, "read", table)
    assert reason and "GetState_Count" in reason

    take, skip = plan(model, "ISch_Thing", "read", table=table)
    assert "Count" not in [n for n, _p in take]
    assert "Count" in {s.name for s in skip}


def test_a_member_measured_unreliable_is_refused_with_its_measurement(model):
    """Resolving is not working, and the generator has to know both.

    CompilationMasked resolves: sdk-symbols calls it plain, and a live
    probe read it. It was still removed by hand, because a part's
    compile mask reads back unset after it is written. Without this the
    generator resurrects it on every regeneration, and the block test
    starts contradicting the test that recorded the removal.
    """
    from eda_agent.sdk.generate import UNRELIABLE

    assert "CompilationMasked" in UNRELIABLE
    prop = Property(name="CompilationMasked", get="Boolean", set="Boolean")
    reason = _reject(prop, "read")
    assert reason == UNRELIABLE["CompilationMasked"]
    assert "reads back unset" in reason


def test_the_unreliable_list_agrees_with_the_shipped_pascal():
    """Each exclusion must actually be absent, or it is just a comment."""
    from eda_agent.sdk.generate import UNRELIABLE

    text = GENERIC.read_text(encoding="utf-8")
    start = text.index(BEGIN_MARKER)
    end = text.index(END_MARKER, start)
    block = text[start:end]
    for name in UNRELIABLE:
        assert f"PropName = '{name}'" not in block, name


def test_a_getter_only_member_is_read_through_its_getter(model):
    """The spelling the engine does register for those 213 names.

    Registering is not resolving, so this stays behind allow_getter
    until a live read settles it.
    """
    table = SymbolTable({"names": ["Caption", "GetState_Count"],
                         "min_reliable": 3})
    out = render_probe(model, "ISch_Thing", "ISch_Thing",
                       function_name="ProbeThing", table=table,
                       allow_getter=True)
    assert "Then Result := IntToStr(Obj.GetState_Count)" in out.pascal
    # The caller's vocabulary does not change: it still asks for the
    # property the SDK declares, not for the accessor underneath it.
    assert "If PropName = 'Count'" in out.pascal
    assert "'GetState_Count'" not in out.pascal


def test_the_getter_form_is_off_unless_asked(model):
    table = SymbolTable({"names": ["Caption", "GetState_Count"],
                         "min_reliable": 3})
    out = render_probe(model, "ISch_Thing", "ISch_Thing",
                       function_name="ProbeThing", table=table)
    assert "GetState_" not in out.pascal
    assert "Count" not in out.included


def test_a_plain_member_is_never_rewritten_to_its_getter(model):
    """Obj.Caption resolves, so nothing may redirect it through a getter."""
    table = SymbolTable({"names": ["Caption", "GetState_Caption"],
                         "min_reliable": 3})
    assert member_spelling("Caption", table, allow_getter=True) == "Caption"


def test_without_a_table_the_plan_is_candidates_not_capabilities(model):
    take, _ = plan(model, "ISch_Thing", "read")
    assert "Count" in [n for n, _p in take]


def test_skipping_is_reported_not_silent(model):
    take, skip = plan(model, "ISch_Thing", "read")
    assert {s.name for s in skip} == {"Trigger", "Blob", "ItemAt"}
    assert all(isinstance(s, Skipped) and s.reason for s in skip)


def test_names_a_chain_already_handles_are_not_offered_again(model):
    take, _ = plan(model, "ISch_Thing", "read", already={"caption", "count"})
    assert [n for n, _p in take] == ["Ratio", "Visible"]


# --------------------------------------------------------------------------
# The Pascal it emits
# --------------------------------------------------------------------------

@pytest.fixture
def probe(model):
    return render_probe(model, "ISch_Thing", "ISch_Thing",
                        function_name="ProbeThing")


def test_each_declared_type_gets_the_conversion_the_chains_use(probe):
    body = probe.pascal
    assert "Then Result := Obj.Caption" in body
    assert "Then Result := IntToStr(Obj.Count)" in body
    assert "Then Result := BoolToJsonStr(Obj.Visible)" in body
    assert "Then Result := FloatToJsonStr(Obj.Ratio)" in body


def test_the_chain_has_exactly_one_terminating_semicolon(probe):
    # A ';' on an entry ends the If, and the Else after it then has
    # nothing to attach to. Only the final Else may carry one.
    entries = [l for l in probe.pascal.splitlines()
               if l.strip().startswith(("If PropName", "Else If PropName"))]
    assert entries
    assert not [l for l in entries if l.rstrip().endswith(";")]


def test_an_unknown_name_reports_not_found_rather_than_empty(probe):
    assert "Else Exit;" in probe.pascal
    assert "Found := False;" in probe.pascal


def test_found_is_earned_after_the_chain_not_assumed_before_it(probe):
    """A read that faults part way must not leave Found True behind it.

    Altium resolves the member against the live object, so an unexposed
    one faults on the line that reads it. If Found were set True up
    front, the caller would record that fault as a successful read of an
    empty value, which is the one wrong answer this whole exercise
    exists to avoid.
    """
    body = probe.pascal.splitlines()
    entries = [i for i, l in enumerate(body)
               if l.strip().startswith(("If PropName", "Else If PropName"))]
    set_false = [i for i, l in enumerate(body)
                 if l.strip() == "Found := False;"]
    set_true = [i for i, l in enumerate(body) if l.strip() == "Found := True;"]
    assert set_false and set_true
    assert set_false[0] < entries[0], "Found must be False before the chain"
    assert set_true[0] > entries[-1], "Found must be True only after it"


def test_the_write_chain_assigns_with_the_parsing_the_chains_use(model):
    out = render_write(model, "ISch_Thing", "ISch_Thing",
                       function_name="WriteThing")
    body = out.pascal
    assert "Then Obj.Caption := Value" in body
    assert "Then Obj.Visible := StrToBool(Value)" in body
    assert "Then Obj.Ratio := StrToFloatDef(Value, 0)" in body
    assert "Then Obj.Trigger := StrToIntDef(Value, 0)" in body
    entries = [l for l in body.splitlines()
               if l.strip().startswith(("If PropName", "Else If PropName"))]
    assert not [l for l in entries if l.rstrip().endswith(";")]


def test_brackets_balance_in_the_generated_pascal(probe):
    # The rule that would have caught the shipped `; expected`, applied
    # to generated code before it can ship.
    depth = 0
    for ch in probe.pascal:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
            assert depth >= 0
    assert depth == 0


def test_an_interface_with_nothing_to_add_still_emits_valid_pascal(model):
    out = render_probe(model, "ISch_Thing", "ISch_Thing",
                       function_name="ProbeThing",
                       already={"caption", "count", "visible", "ratio"})
    assert out.included == []
    assert "Found := False;" in out.pascal
    assert "Else If" not in out.pascal


# --------------------------------------------------------------------------
# The block that lives in Generic.pas
# --------------------------------------------------------------------------

def test_replace_block_swaps_only_between_the_markers():
    src = (f"before\n{BEGIN_MARKER}\nold body\n{END_MARKER}\nafter\n")
    out = replace_block(src, f"{BEGIN_MARKER}\nnew body\n{END_MARKER}\n")
    assert out == f"before\n{BEGIN_MARKER}\nnew body\n{END_MARKER}\nafter\n"
    assert "old body" not in out


def test_generic_pas_carries_both_markers():
    text = GENERIC.read_text(encoding="utf-8")
    assert text.count(BEGIN_MARKER) == 1
    assert text.count(END_MARKER) == 1
    assert text.index(BEGIN_MARKER) < text.index(END_MARKER)


def test_the_block_in_generic_pas_matches_the_generator():
    """Generated code that has drifted from its generator is worse than
    hand-written code, because the next regeneration silently reverts
    whatever was edited into it.

    Skipped without the reflected dump, which only exists on a machine
    with Altium installed.
    """
    try:
        model = load_sdk()
        table = load_symbols()
    except (SdkUnavailable, SymbolsUnavailable):
        pytest.skip("no reflected SDK or symbol table on this machine")

    text = GENERIC.read_text(encoding="utf-8")
    start = text.index(BEGIN_MARKER)
    end = text.index(END_MARKER, start) + len(END_MARKER) + 1
    assert text[start:end] == render_block(model, table), (
        "the generated block in Generic.pas is not what the generator "
        "produces; re-run it rather than editing between the markers")


# ---- the base chain ----------------------------------------------------
# Ten lines that moved schematic coverage from 45.3% to 75.1%, because
# the same ten names were missing from 31 of the 32 types. They are
# declared on ISch_GraphicalObject, which is what GetSchProperty already
# takes, so one routine against the base answers them for every object.

def test_the_block_carries_a_routine_for_the_base_type(model):
    block = render_block(model, None)
    assert "Function GetSchGraphicalGenerated(Obj : ISch_GraphicalObject;" \
        in block
    assert "Function GetSchComponentGenerated(Obj : ISch_Component;" in block


def test_the_base_routine_takes_the_type_getschproperty_already_has():
    """No cast and no ObjectId test, which is the whole point of it.

    GetSchProperty's parameter is an ISch_GraphicalObject. A routine
    typed the same way can be handed that parameter directly, so the
    fallback needs no local of its own and no branch to reach it.
    """
    text = GENERIC.read_text(encoding="utf-8")
    assert "Function GetSchProperty(Obj : ISch_GraphicalObject;" in text
    assert "Result := GetSchGraphicalGenerated(Obj, PropName, Have);" in text


def test_the_base_routine_is_defined_before_the_chain_that_calls_it():
    """DelphiScript has no forward declarations.

    A call into a routine defined later in the concatenation is an
    access violation at run time, not a compile error, so the order is
    load bearing rather than cosmetic.
    """
    text = GENERIC.read_text(encoding="utf-8")
    assert (text.index("Function GetSchGraphicalGenerated(")
            < text.index("Function GetSchProperty("))


def test_the_component_chain_is_tried_before_the_base_one():
    """A component declares more than the base, so its chain wins.

    The base chain is the fallback, reached when the component one did
    not set Have. Reverse the two and a component would answer the ten
    shared names off the base and never be offered the rest.
    """
    text = GENERIC.read_text(encoding="utf-8")
    comp = text.index("Result := GetSchComponentGenerated(Comp, PropName, Have);")
    base = text.index("Result := GetSchGraphicalGenerated(Obj, PropName, Have);")
    assert comp < base


def test_the_two_routines_do_not_disagree_about_a_shared_name():
    """ISch_Component inherits the base's ten, so both carry them.

    The duplication is deliberate: either routine has to be correct on
    its own. What must not happen is the two spelling the same read
    differently, because then which one answers would change the value.

    Needs the real declarations: the fake model in this file has no
    inheritance, so there would be nothing shared to compare.
    """
    import re

    try:
        model = load_sdk()
        table = load_symbols()
    except (SdkUnavailable, SymbolsUnavailable):
        pytest.skip("no reflected SDK or symbol table on this machine")

    block = render_block(model, table)
    bodies = {}
    for name in ("GetSchGraphicalGenerated", "GetSchComponentGenerated"):
        start = block.index(f"Function {name}(")
        end = block.index("End;", start)
        bodies[name] = dict(re.findall(
            r"PropName = '([^']+)'\s+Then Result := (.+)", block[start:end]))
    shared = set(bodies["GetSchGraphicalGenerated"]) & set(
        bodies["GetSchComponentGenerated"])
    assert shared, "the component should inherit the base's properties"
    for name in shared:
        left = bodies["GetSchGraphicalGenerated"][name].strip()
        right = bodies["GetSchComponentGenerated"][name].strip()
        assert left == right, f"{name}: {left!r} vs {right!r}"
