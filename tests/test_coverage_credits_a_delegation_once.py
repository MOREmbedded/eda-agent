# SPDX-License-Identifier: Apache-2.0
"""A delegated chain is reachable for one object type, not for all of them.

GetSchProperty hands component reads to a generated routine behind
`If Obj.ObjectId = eSchComponent`. Counting those names for every
schematic type moved the coverage report from 29.7% to 46.5% on a
change that added thirty component properties, and credited a pin with
DescriptionString, which a pin cannot answer.

The report is the number this project steers by, so an overstatement in
it is worse than no report.

The chains delegate in three shapes, and all three are measured here: a
branch guarded on the object type, a family of extra properties whose
owner table names the type each one belongs to, and a call no branch
guards at all. Reading only the chain's own body missed 116 reachable
properties; reading them all into one flat set, which is what upstream's
sdk-reach did, is the overstatement these tests exist to prevent.
"""

from __future__ import annotations

import pytest

from eda_agent.sdk import coverage as cov

PASCAL = """
Function ReadThingGenerated(Obj : ISch_Thing; PropName : String;
    Var Found : Boolean) : String;
Begin
    Result := '';
    If PropName = 'OnlyOnThing' Then Result := Obj.OnlyOnThing
    Else If PropName = 'AlsoOnThing' Then Result := Obj.AlsoOnThing
    Else Exit;
    Found := True;
End;

Function GetSchProperty(Obj : ISch_GraphicalObject; PropName : String) : String;
Begin
    Result := '';
    If PropName = 'Shared' Then Result := Obj.Shared
    Else If Obj.ObjectId = eSchThing Then
    Begin
        Result := ReadThingGenerated(Obj, PropName, Have);
    End;
End;
"""


@pytest.fixture
def pascal(monkeypatch):
    monkeypatch.setattr(cov, "_read", lambda _rel: PASCAL)


def test_the_chain_reports_only_what_it_carries_itself(pascal):
    """Planning depends on this.

    If handled_names counted the delegated names, the generator would
    treat them as already handled and emit nothing, leaving the
    delegation calling a chain that no longer carried them.
    """
    assert cov.handled_names("GetSchProperty") == {"Shared"}


def test_delegated_names_are_keyed_by_the_type_that_guards_them(pascal):
    assert cov.delegated_names("GetSchProperty") == {
        "eSchThing": {"OnlyOnThing", "AlsoOnThing"}}


def test_a_type_the_guard_excludes_is_not_credited(pascal):
    delegated = cov.delegated_heads("GetSchProperty")
    assert delegated.get("eSchPin") is None
    assert "OnlyOnThing" not in cov.handled_heads("GetSchProperty")


OWNER_PASCAL = """
Function ThingExtraOwner(PropName : String) : Integer;
Begin
    Result := 0;
    If (PropName = 'OnlyOnPin') Or (PropName = 'AlsoOnPin') Then Result := 1
    Else If PropName = 'OnlyOnPort' Then Result := 2;
End;

Function ThingExtraFits(Obj : ISch_GraphicalObject; PropName : String) : Boolean;
Var
    Owner : Integer;
Begin
    Owner := ThingExtraOwner(PropName);
    Result := ((Owner = 1) And (Obj.ObjectId = ePin))
        Or ((Owner = 2) And (Obj.ObjectId = ePort));
End;

Function ThingGetExtra(Obj : ISch_GraphicalObject; PropName : String) : String;
Begin
    Result := '';
    If Not ThingExtraFits(Obj, PropName) Then Exit;
    If PropName = 'OnlyOnPin' Then Result := Obj.OnlyOnPin
    Else If PropName = 'AlsoOnPin' Then Result := Obj.AlsoOnPin
    Else If PropName = 'OnlyOnPort' Then Result := Obj.OnlyOnPort;
End;

Function IsThingProp(PropName : String) : Boolean;
Begin
    Result := (PropName = 'Gated') Or (PropName = 'AlsoGated');
End;

Function ReadThingProp(Obj : ISch_GraphicalObject; PropName : String) : String;
Begin
    If PropName = 'Gated' Then Result := Obj.Gated
    Else If PropName = 'AlsoGated' Then Result := Obj.AlsoGated
    Else If PropName = 'Loose' Then Result := Obj.Loose;
End;

Function GetSchProperty(Obj : ISch_GraphicalObject; PropName : String) : String;
Begin
    Result := '';
    If ThingExtraOwner(PropName) > 0 Then Result := ThingGetExtra(Obj, PropName)
    Else If PropName = 'Shared' Then Result := Obj.Shared
    Else If IsThingProp(PropName) Then
    Begin
        If Obj.ObjectId = ePin Then Result := ReadThingProp(Obj, PropName);
    End
    Else If PropName = 'Loose' Then Result := ReadThingProp(Obj, 'Loose');
End;
"""


@pytest.fixture
def owner_pascal(monkeypatch):
    monkeypatch.setattr(cov, "_read", lambda _rel: OWNER_PASCAL)


def test_an_extra_family_goes_to_the_types_its_owner_table_names(owner_pascal):
    """The Pascal's own table is the only place that mapping exists.

    Without it the whole family would have to be credited to every type,
    which is how upstream's sdk-reach counted it.
    """
    delegated = cov.delegated_names("GetSchProperty")
    assert delegated["ePin"] >= {"OnlyOnPin", "AlsoOnPin"}
    assert delegated["ePort"] == {"OnlyOnPort"}
    assert "OnlyOnPort" not in delegated["ePin"]


def test_a_gate_in_the_condition_is_not_itself_a_source_of_names(owner_pascal):
    """IsThingProp decides whether the branch is taken; it answers nothing.

    Its names reach the caller through the routine the branch calls, and
    only for the type that call is guarded on, so counting the gate as
    well would credit every type with them.
    """
    delegated = cov.delegated_names("GetSchProperty")
    assert {"Gated", "AlsoGated"} <= delegated["ePin"]
    assert cov.ANY_TYPE not in delegated


def test_a_call_passing_a_literal_name_does_not_drag_the_rest_in(owner_pascal):
    """`ReadThingProp(Obj, 'Loose')` reaches Loose and nothing else.

    The branch testing for Loose already carries it, and the routine's
    other names are reachable only where the gated branch calls it.
    """
    assert "Loose" in cov.handled_names("GetSchProperty")
    assert "Loose" not in cov.delegated_names("GetSchProperty").get("ePort", set())


def test_the_real_chain_credits_each_delegation_to_its_own_type():
    """Against the shipped Pascal, not a fixture.

    Five type guards, because the chain answers a pin, a port, a
    parameter and a No-ERC marker through routines of their own as well
    as a component. A guard added later fails this, which is the point:
    each one has to be credited to its own type rather than folded into
    the first. The sixth key is the base chain, which no guard gates.
    """
    delegated = cov.delegated_heads("GetSchProperty")
    assert set(delegated) == {"eSchComponent", "ePin", "ePort",
                              "eParameter", "eNoERC", cov.ANY_TYPE}
    assert "DescriptionString" in delegated["eSchComponent"]
    # Read live through generic.query_objects on 2026-10-27, so this is
    # a measurement rather than a claim about what should work.
    assert "IdentifierString" in delegated["eSchComponent"]
    # A pin does not reach those through the COMPONENT chain. It reaches
    # them through the base one, which is a different thing and has to
    # stay a different thing: fold the two together and a pin would be
    # credited with the properties only a component declares.
    assert not {"DescriptionString", "IdentifierString"} & delegated["ePin"]
    # Symbol_Inner is owner 1 in SchExtraOwner, which SchExtraFits reads
    # as ePin alone; HarnessColor is owner 4, which is ePort alone.
    assert "Symbol_Inner" in delegated["ePin"]
    assert "Symbol_Inner" not in delegated["ePort"]
    assert "HarnessColor" in delegated["ePort"]


def test_the_base_chain_is_credited_to_every_schematic_type():
    """GetSchGraphicalGenerated sits in a branch that names no type.

    Its ten properties are declared on ISch_GraphicalObject, which is
    what GetSchProperty's own parameter already is, so no cast and no
    ObjectId test stand between any schematic object and those reads.
    Crediting them to one constant would undercount 31 types; crediting
    them to a type that cannot reach them would be the overcount this
    file exists to prevent. ANY_TYPE is how coverage says "all of them".
    """
    delegated = cov.delegated_heads("GetSchProperty")
    shared = delegated[cov.ANY_TYPE]
    assert "Dimmed" in shared
    assert "LiveHighlightValue" in shared
    # CompilationMasked is declared on the same interface and is NOT
    # here: it resolves and still does not behave, so generate.py
    # refuses it. See UNRELIABLE there.
    assert "CompilationMasked" not in shared
    # And it really is a separate credit, not the component one renamed.
    assert shared != delegated["eSchComponent"]


def test_the_board_chain_splits_its_extra_family_the_same_way():
    """ExtraPCBPropertyFits is the board's version of the same table."""
    delegated = cov.delegated_heads("GetPCBProperty")
    assert "BarCodeKind" in delegated["eTextObject"]
    assert "BarCodeKind" not in delegated.get("ePadObject", set())
    assert "IsCounterHole" in delegated["eViaObject"]
    # PCBGetMask and its siblings sit in branches that name no type, so
    # they are reachable for whatever object the chain is handed.
    assert cov.ANY_TYPE in delegated


def test_the_board_base_chain_is_credited_to_every_board_type():
    """GetPCBPrimitiveGenerated, the board half of the same move.

    ANY_TYPE was already in this dict before the base chain existed,
    from PCBGetMask and its siblings, so the assertion that matters is
    that the new names joined it rather than landing under a constant.
    """
    delegated = cov.delegated_heads("GetPCBProperty")
    shared = delegated[cov.ANY_TYPE]
    assert "DescriptorString" in shared
    assert "Moveable" in shared
    assert "PowerPlaneClearance" in shared
    # Not credited to a single object type, which is the undercount.
    for constant in ("eTrackObject", "ePadObject", "eViaObject"):
        assert "DescriptorString" not in delegated.get(constant, set())
