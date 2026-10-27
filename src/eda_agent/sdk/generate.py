# SPDX-License-Identifier: Apache-2.0
"""Emit Pascal property accessors from what the SDK assembly declares.

``coverage.py`` measures the gap between the hand-written chains and
Altium.SDK.Interfaces.dll. This closes it: given an interface, it writes
the If-chain entries for every property the chain does not already
mention, in the same shape and with the same conversions the existing
chains use.

WHAT THIS CANNOT KNOW. The assembly declares the COM interface. Altium's
DelphiScript engine binds members late, at runtime, so a generated line
compiles whether or not the running object actually exposes that member,
and a member it does not expose faults where ``Try/Except`` cannot catch
it, taking the polling loop with it (see docs/RELEASE_VERIFICATION.md).

So generated lines are NOT written straight into GetSchProperty. They go
into a probe first, which reads them one at a time against a real object
and records which ones answer. What the probe proves can then be folded
into the chain. The declaration is the candidate list; the probe is the
evidence.

Recovery when a probe does fault: ``eda-agent relaunch`` restarts the
loop in the running Altium, without closing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .coverage import handled_heads, interface_for, supported_sch_types
from .reflect import Property, SdkModel
from .symbols import ABSENT, GETTER, SymbolTable

#: Reading a property into the chain's String result. The chains report
#: every value as text, so each declared type needs the conversion the
#: existing lines already use: IntToStr for counts and enums,
#: BoolToJsonStr for flags (it writes true/false, not -1/0, so the JSON
#: a caller receives is a real boolean), FloatToJsonStr for reals.
READ_CAST: dict[str, str] = {
    "String": "{var}.{name}",
    "Boolean": "BoolToJsonStr({var}.{name})",
    "Int32": "IntToStr({var}.{name})",
    "Int64": "IntToStr({var}.{name})",
    "Byte": "IntToStr({var}.{name})",
    "Double": "FloatToJsonStr({var}.{name})",
    "Single": "FloatToJsonStr({var}.{name})",
}

#: Writing one back. The defaulting forms (StrToIntDef, StrToFloatDef)
#: are deliberate: a caller sending a malformed number gets the default
#: written rather than an exception that reads as "the property does not
#: exist", which is the one failure this layer must not fake.
WRITE_CAST: dict[str, str] = {
    "String": "{var}.{name} := Value;",
    "Boolean": "{var}.{name} := StrToBool(Value);",
    "Int32": "{var}.{name} := StrToIntDef(Value, 0);",
    "Int64": "{var}.{name} := StrToIntDef(Value, 0);",
    "Byte": "{var}.{name} := StrToIntDef(Value, 0);",
    "Double": "{var}.{name} := StrToFloatDef(Value, 0);",
    "Single": "{var}.{name} := StrToFloatDef(Value, 0);",
}


#: Column the chains line their Then up at, so a generated block reads
#: like the hand-written ones around it rather than announcing itself.
_THEN_COLUMN = 52


def _aligned(left: str, right: str) -> str:
    """One chain line with Then at a fixed column, or one space past."""
    pad = max(1, _THEN_COLUMN - len(left))
    return left + " " * pad + right


@dataclass(frozen=True)
class Skipped:
    """A declared property the generator will not emit, and why."""

    name: str
    reason: str


@dataclass
class Generated:
    """One rendered chain: the Pascal, what went in, and what did not."""

    interface: str
    kind: str
    pascal: str
    included: list[str] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.interface} {self.kind}: {len(self.included)} emitted, "
                f"{len(self.skipped)} skipped")


def _reject(prop: Property, kind: str,
            table: SymbolTable | None = None) -> str | None:
    """Why this property cannot be a plain chain entry, or None.

    Order matters only for the message: an indexed write-only Object is
    reported as indexed, which is the first thing to fix about it.

    ``table`` is the scripting engine's registered symbol table. The
    assembly declaring a property does not mean DelphiScript can reach
    it, and reaching for one it cannot halts the debugger on that line
    with nothing able to catch it. Checking here means an unreachable
    name never reaches the Pascal in the first place.
    """
    if prop.indexed:
        return "indexed, needs an index the chain has no way to pass"
    declared = prop.get if kind == "read" else prop.set
    if kind == "read" and not prop.readable:
        return "write only"
    if kind == "write" and not prop.writable:
        return "read only"
    casts = READ_CAST if kind == "read" else WRITE_CAST
    if declared not in casts:
        return f"declared {declared}, which does not cross the String bridge"
    if table is not None:
        reach = table.reach(prop.name)
        if reach == GETTER:
            return (f"registered only as GetState_{prop.name}, so property "
                    f"syntax does not resolve")
        if reach == ABSENT:
            return "not registered with the scripting engine"
    return None


def plan(model: SdkModel, interface: str, kind: str, *,
         already: Iterable[str] = (),
         table: SymbolTable | None = None
         ) -> tuple[list[tuple[str, Property]], list[Skipped]]:
    """Split an interface's properties into emittable and skipped.

    ``already`` is the set of names a chain handles, matched the same way
    coverage matches them, so re-running the generator after folding in a
    batch does not offer the same properties twice.
    """
    known = {n.lower() for n in already}
    props = model.effective_properties(interface)
    take: list[tuple[str, Property]] = []
    skip: list[Skipped] = []
    for name in sorted(props):
        if name.lower() in known:
            continue
        reason = _reject(props[name], kind, table)
        if reason:
            skip.append(Skipped(name, reason))
        else:
            take.append((name, props[name]))
    return take, skip


def render_probe(model: SdkModel, interface: str, var_type: str, *,
                 function_name: str, already: Iterable[str] = (),
                 table: SymbolTable | None = None
                 ) -> Generated:
    """A routine that reads one named property off a typed object.

    This is the probe body, and deliberately also the shape the entries
    take once they move into GetSchProperty, so folding a proven batch in
    is a copy rather than a rewrite.

    Found is earned, not assumed: False on entry, True only after the
    chain has returned. A read that faults part way never reaches that
    line, so the caller can tell a clean read from a dead one without
    trusting the value that came back.
    """
    take, skip = plan(model, interface, "read", already=already,
                      table=table)
    var = "Obj"
    lines = [
        "{..............................................................................}",
        f"{{ {function_name} - read one declared property off a {interface}.".ljust(79) + "}",
        "{ GENERATED by eda-agent sdk-generate from Altium.SDK.Interfaces.dll.         }",
        "{ Every line below is a property the assembly declares and the chain does     }",
        "{ not. Declaring it is not exposing it: DelphiScript resolves the member      }",
        "{ against the live object, so an unexposed one faults on the line that reads  }",
        "{ it. Found stays False unless the chain returns, which is what lets the      }",
        "{ caller record the fault instead of trusting the result.                     }",
        "{..............................................................................}",
        "",
        f"Function {function_name}({var} : {var_type}; PropName : String;",
        "    Var Found : Boolean) : String;",
        "Begin",
        "    Result := '';",
        "    Found := False;",
    ]
    for n, (name, prop) in enumerate(take):
        cast = READ_CAST[prop.get].format(var=var, name=name)
        head = "    If" if n == 0 else "    Else If"
        lines.append(_aligned(f"{head} PropName = '{name}'",
                              f"Then Result := {cast}"))
    if take:
        lines.append("    Else Exit;")
        lines.append("    { Reached only when a branch read without faulting. That is the }")
        lines.append("    { whole measurement: the line proves the member resolved.       }")
        lines.append("    Found := True;")
    lines.append("End;")

    return Generated(interface=interface, kind="read",
                     pascal="\n".join(lines) + "\n",
                     included=[name for name, _ in take], skipped=skip)


def render_write(model: SdkModel, interface: str, var_type: str, *,
                 function_name: str, already: Iterable[str] = (),
                 table: SymbolTable | None = None
                 ) -> Generated:
    """The write counterpart. Returns 1 on a write, 0 when unrecognised."""
    take, skip = plan(model, interface, "write", already=already,
                      table=table)
    var = "Obj"
    lines = [
        f"Function {function_name}({var} : {var_type}; PropName : String;",
        "    Value : String) : Integer;",
        "Begin",
        "    Result := 1;",
    ]
    for n, (name, prop) in enumerate(take):
        # NO trailing semicolon on a chain entry. A ';' before 'Else'
        # ends the If statement, and the Else that follows is then a
        # syntax error with nothing to attach to.
        stmt = WRITE_CAST[prop.set].format(var=var, name=name).rstrip(";")
        head = "    If" if n == 0 else "    Else If"
        lines.append(_aligned(f"{head} PropName = '{name}'",
                              f"Then {stmt}"))
    if take:
        lines.append("    Else Result := 0;")
    else:
        lines.append("    Result := 0;")
    lines.append("End;")
    return Generated(interface=interface, kind="write",
                     pascal="\n".join(lines) + "\n",
                     included=[name for name, _ in take], skipped=skip)


#: The generated block lives inside Generic.pas rather than its own unit,
#: so nothing in the build, the lint file list, the deploy set or the
#: unit-stamp reporting has to learn about a file that exists only to be
#: rewritten. The markers are what makes that safe: a test re-runs the
#: generator and asserts the block between them still matches.
BEGIN_MARKER = ("{ BEGIN GENERATED - SDK property probe. Produced by "
                "`eda-agent sdk-generate`. }")
END_MARKER = "{ END GENERATED - SDK property probe. }"


def render_block(model: SdkModel,
                 table: SymbolTable | None = None) -> str:
    """The whole generated region, markers included.

    One probe per object type. Only ISch_Component today: the point of
    probing is to learn the exposure rate on one type before committing
    to generating the other thirty-one.

    Pass ``table`` and the block carries only what the engine will
    resolve. Without it the block is a list of candidates, and running
    it is how you find out which ones halt the debugger.
    """
    body = render_probe(
        model, "ISch_Component", "ISch_Component",
        function_name="ProbeSchComponentProperty",
        already=handled_heads("GetSchProperty"), table=table).pascal
    return f"{BEGIN_MARKER}\n{body}{END_MARKER}\n"


def replace_block(source: str, block: str) -> str:
    """Swap the generated region in a Pascal source for a fresh one."""
    start = source.index(BEGIN_MARKER)
    end = source.index(END_MARKER, start) + len(END_MARKER) + 1
    return source[:start] + block + source[end:]


def candidates_for(model: SdkModel, object_type: str, kind: str = "read",
                   table: SymbolTable | None = None
                   ) -> tuple[str, list[tuple[str, Property]], list[Skipped]]:
    """Resolve a schematic object type name and plan its missing properties.

    ``object_type`` is the name the resolver accepts ("component",
    "pin"), not the Altium constant, so the caller uses the same spelling
    the tools take.
    """
    types = supported_sch_types()
    constant = types.get(object_type.lower())
    if constant is None:
        raise KeyError(f"unknown schematic object type {object_type!r}; "
                       f"known: {', '.join(sorted(types))}")
    interface = interface_for(constant, "SCH")
    chain = "GetSchProperty" if kind == "read" else "SetSchProperty"
    take, skip = plan(model, interface, kind,
                      already=handled_heads(chain), table=table)
    return interface, take, skip
