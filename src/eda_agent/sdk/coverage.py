# SPDX-License-Identifier: Apache-2.0
"""Measure the Pascal property chains against what the SDK declares.

The generic property layer is four hand-written If-chains:
GetSchProperty and SetSchProperty in Generic.pas, GetPCBProperty and
SetPCBProperty in PCBGeneric.pas. Every property a caller can read or
write is there because somebody typed a line for it. That is why
coverage was a guess for so long: the only way to state it was to read
the SDK documentation page by page and compare by eye.

Altium.SDK.Interfaces.dll settles it. This module parses the chains,
maps each supported object type onto the interface the SDK declares for
it, and reports the difference. The result is a number nobody has to
trust: every missing property it names is one the assembly declares and
the chain does not mention.

Matching is by HEAD SEGMENT. The chains accept paths the SDK has no
single member for -- Designator.Text reads the component's designator
label and then that label's text, Location.X reads one field of a
TLocation. Both are reached through the SDK property named by the
first segment, so Designator.Text counts as covering Designator. This
OVERSTATES coverage slightly, never understates it: a chain that
handles only Designator.Text still counts Designator as reached, which
is the honest reading since the caller can get at it.

A chain also DELEGATES. Most of what it answers for a pin, a port, a
parameter or a dimension is written one routine down, behind a branch
that tests the object type, or behind a table that maps each property
name to the type that owns it. Reading only the chain's own body missed
116 reachable properties. Those routines are followed here, and each
name is credited to the types the Pascal guards it on, never to all of
them: a flat union would credit a pin with properties only a component
can answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .reflect import Interface, Property, SdkModel, load_sdk

REPO = Path(__file__).resolve().parents[3]

#: Which file holds which chain, and what the chain is for.
CHAINS: dict[str, tuple[str, str]] = {
    "GetSchProperty": ("scripts/altium/Generic.pas", "schematic read"),
    "SetSchProperty": ("scripts/altium/Generic.pas", "schematic write"),
    "GetPCBProperty": ("scripts/altium/PCBGeneric.pas", "board read"),
    "SetPCBProperty": ("scripts/altium/PCBGeneric.pas", "board write"),
}

#: Object types whose SDK interface is not the mechanical
#: e<Name> -> I<Domain>_<Name> rewrite.
SCH_INTERFACE_OVERRIDES: dict[str, str] = {
    "eSchComponent": "ISch_Component",
    "ePowerObject": "ISch_PowerObject",
    "eNoERC": "ISch_NoERC",
    "eTextFrame": "ISch_TextFrame",
    "eCrossSheetConnector": "ISch_CrossSheetConnector",
    # A designator is a label with a role, and the SDK says so.
    "eDesignator": "ISch_Designator",
}

#: Schematic object types Altium's SDK assembly declares no interface
#: for, with what a script reaches them through instead. They are listed
#: apart and not measured: a stand-in interface would count properties
#: the object may not have. The test against a real reflection checks
#: each is still absent, so an entry goes once Altium declares one.
SCH_NOT_IN_SDK: dict[str, str] = {
    # Built like a wire, through ISch_Polygon's vertex members. AD26's
    # assembly has no ISch_SignalHarness: its Harness interfaces are the
    # harness connector, its entries and type label, and the objects of
    # a Wire Harness document.
    "eSignalHarness": "reached through ISch_GraphicalObject and ISch_Polygon",
}

PCB_INTERFACE_OVERRIDES: dict[str, str] = {
    "eComponentObject": "IPCB_Component",
    "eTrackObject": "IPCB_Track",
    "eArcObject": "IPCB_Arc",
    "ePadObject": "IPCB_Pad",
    "eViaObject": "IPCB_Via",
    "eTextObject": "IPCB_Text",
    "eFillObject": "IPCB_Fill",
    "eRegionObject": "IPCB_Region",
    "ePolyObject": "IPCB_Polygon",
    "eDimensionObject": "IPCB_Dimension",
    "eBoardObject": "IPCB_Board",
    "eNetObject": "IPCB_Net",
    "eComponentBodyObject": "IPCB_ComponentBody",
    "eRuleObject": "IPCB_Rule",
    "eConnectionObject": "IPCB_Connection",
    "eSplitPlaneObject": "IPCB_SplitPlane",
}


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8", errors="replace")


def chain_body(source: str, function_name: str) -> str:
    """The text of one Function, from its header to its closing End;.

    Nested Begin/End blocks make a naive first-End search wrong, so this
    takes the LAST End; before the next top-level Function instead.
    """
    start = source.index(f"Function {function_name}(")
    rest = source[start:]
    nxt = re.search(r"\n(?=Function |Procedure )", rest[1:])
    block = rest[: nxt.start() + 1] if nxt else rest
    return block


#: Every top-level routine header in a Pascal unit.
_HEADER = re.compile(r"^(?:Function|Procedure) (\w+)\(", re.M)

#: A call to another routine. A chain delegates rather than carrying a
#: second copy of an accessor, so part of what it answers is written one
#: level down.
_CALL = re.compile(r"\b([A-Za-z_]\w*)\(")

#: Where the branch a call sits in begins. These chains are one long
#: If / Else If ladder, so the nearest If above a call opens the
#: condition that has to hold for the call to be reached at all.
_BRANCH = re.compile(r"(?:\bElse\s+)?\bIf\b")

#: The object-type test a branch is guarded on. Generic.pas tests
#: Obj.ObjectId in place; PCBGeneric.pas reads it into a local, Oid.
_GUARD = re.compile(r"(?:ObjectId|Oid)\s*=\s*(e\w+)")

#: A call that maps a property name to an owner code. Both domains gate
#: a family of extra properties on one of these.
_OWNER_CALL = re.compile(r"\b(\w*Owner)\(PropName\)")

#: The key delegated_names uses for a call no branch guards on a type,
#: whose names are reachable for whatever object the chain is given.
ANY_TYPE = "*"


def routines(source: str) -> dict[str, str]:
    """Each top-level routine of a Pascal unit, by name."""
    marks = list(_HEADER.finditer(source))
    return {m.group(1): source[m.start():(marks[i + 1].start()
                                          if i + 1 < len(marks) else len(source))]
            for i, m in enumerate(marks)}


def _call_args(body: str, opening: int) -> str:
    """The text between one call's parentheses, nesting respected."""
    depth = 0
    for i in range(opening, len(body)):
        if body[i] == "(":
            depth += 1
        elif body[i] == ")":
            depth -= 1
            if depth == 0:
                return body[opening + 1:i]
    return ""


def _owner_codes(body: str) -> dict[str, int]:
    """Each property name an owner routine classifies, to its code.

    The body is a ladder of `(PropName = 'A') Or (PropName = 'B') Then
    Result := 1`, so the names before each Result belong to that code.
    """
    out: dict[str, int] = {}
    chunks = re.split(r"Then\s+Result := (\d+)", body)
    for text, code in zip(chunks[0::2], chunks[1::2]):
        for name in re.findall(r"PropName = '([^']+)'", text):
            out[name] = int(code)
    return out


def _owner_types(code: dict[str, str], owner: str) -> dict[int, set[str]]:
    """Each owner code, to the object types that carry its properties.

    The companion routine reads `(Owner = 1) And ((Oid = ePadObject) Or
    (Oid = eViaObject))`, one alternative per code. It is the only place
    the Pascal says which type an extra property belongs to, and without
    it the whole family would have to be credited to every type.
    """
    for name, body in code.items():
        if name == owner or "(Owner = " not in body:
            continue
        if not re.search(r"\b" + re.escape(owner) + r"\(PropName\)", body):
            continue
        parts = re.split(r"\(Owner = (\d+)\)", body)
        return {int(c): set(_GUARD.findall(segment))
                for c, segment in zip(parts[1::2], parts[2::2])}
    return {}


def handled_names(function_name: str) -> set[str]:
    """Every property name a chain carries in its own body.

    Not what it ANSWERS: a chain may delegate to another routine, and
    those names are reported separately by delegated_names, because most
    of them are reachable only for the object type the delegation is
    guarded on.
    """
    path, _ = CHAINS[function_name]
    body = chain_body(_read(path), function_name)
    return set(re.findall(r"PropName = '([^']+)'", body))


def delegated_names(function_name: str) -> dict:
    """Names a chain answers through another routine, by object type.

    Three shapes, all of them in the shipped Pascal:

    - a branch guarded on the type, `Else If Obj.ObjectId = eSchComponent
      Then ... GetSchComponentGenerated(...)`, whose names belong to that
      constant and no other;
    - a family of extra properties gated on an owner routine, where the
      Pascal's own owner table says which type each name belongs to;
    - a call no branch guards on a type, reachable for whatever object
      the chain is given, keyed under ANY_TYPE.

    A call that IS the branch condition is skipped: it gates the branch
    rather than answering it, and the routine the branch then calls
    carries the same names. So is one passed a literal name in place of
    PropName, because the branch testing for that name already has it.

    Returning one flat set instead would credit every schematic type with
    properties only a component can answer.
    """
    path, _ = CHAINS[function_name]
    code = routines(_read(path))
    body = code.get(function_name, "")
    out: dict = {}
    for match in _CALL.finditer(body):
        callee = match.group(1)
        if callee == function_name or callee not in code:
            continue
        names = set(re.findall(r"PropName = '([^']+)'", code[callee]))
        if not names:
            continue
        if not re.search(r"\bPropName\b", _call_args(body, match.end() - 1)):
            continue
        opens = [b.start() for b in _BRANCH.finditer(body[:match.start()])]
        branch = body[opens[-1]:match.start()] if opens else body[:match.start()]
        if "Then" not in branch:
            continue
        owner = _OWNER_CALL.search(branch)
        if owner:
            codes = _owner_codes(code.get(owner.group(1), ""))
            types = _owner_types(code, owner.group(1))
            for name in names & set(codes):
                for constant in types.get(codes[name], set()):
                    out.setdefault(constant, set()).add(name)
            continue
        for constant in set(_GUARD.findall(branch)) or {ANY_TYPE}:
            out.setdefault(constant, set()).update(names)
    return out


def handled_heads(function_name: str) -> set[str]:
    """Those names reduced to the SDK member each one reaches through.

    Vertex.1.X and Corner.X are our own path syntax over a member the
    SDK does declare; the head segment is that member.
    """
    return {n.split(".", 1)[0] for n in handled_names(function_name)}


def delegated_heads(function_name: str) -> dict:
    """delegated_names reduced to the SDK member each name reaches."""
    return {constant: {n.split(".", 1)[0] for n in names}
            for constant, names in delegated_names(function_name).items()}


def supported_sch_types() -> dict[str, str]:
    """The schematic object types the resolver accepts, name to constant."""
    body = chain_body(_read("scripts/altium/Generic.pas"),
                      "ObjectTypeFromString")
    pairs = re.findall(r"N = '([a-z]+)'\s*Then Result := (e\w+)", body)
    extra = re.findall(r"Raw = '([a-z]+)' Then Result := (e\w+)", body)
    return dict(pairs + extra)


def supported_pcb_types() -> dict[str, str]:
    body = chain_body(_read("scripts/altium/Utils.pas"),
                      "ObjectTypeFromStringPCB")
    pairs = re.findall(r"N = '([a-z]+)'\s*Then Result := (e\w+)", body)
    return dict(pairs)


def interface_for(constant: str, domain: str) -> str:
    """The SDK interface an Altium object-type constant names.

    Mechanical for most of them: eRectangle is ISch_Rectangle. The two
    override tables carry the rest, which exist because Altium's own
    naming is not uniform.
    """
    overrides = (SCH_INTERFACE_OVERRIDES if domain == "SCH"
                 else PCB_INTERFACE_OVERRIDES)
    if constant in overrides:
        return overrides[constant]
    bare = constant[1:] if constant.startswith("e") else constant
    prefix = "ISch_" if domain == "SCH" else "IPCB_"
    return f"{prefix}{bare}"


@dataclass
class TypeCoverage:
    """One object type, measured against the interface that declares it."""

    type_name: str
    constant: str
    interface: str
    resolved: bool
    #: SDK properties reachable on this interface, parents included.
    declared: dict[str, Property] = field(default_factory=dict)
    read_covered: set[str] = field(default_factory=set)
    write_covered: set[str] = field(default_factory=set)
    #: False for a type the SDK declares no interface for (SCH_NOT_IN_SDK);
    #: ``note`` then says what reaches it instead.
    in_sdk: bool = True
    note: str = ""

    @property
    def readable(self) -> set[str]:
        return {n for n, p in self.declared.items() if p.readable}

    @property
    def writable(self) -> set[str]:
        return {n for n, p in self.declared.items() if p.writable}

    @property
    def missing_reads(self) -> set[str]:
        return self.readable - self.read_covered

    @property
    def missing_writes(self) -> set[str]:
        return self.writable - self.write_covered

    def ratio(self) -> tuple[int, int]:
        """Properties reachable either way, over properties declared."""
        reached = (self.read_covered | self.write_covered) & set(self.declared)
        return len(reached), len(self.declared)


@dataclass
class Report:
    """The whole measurement, in a shape a caller can print or assert on."""

    sdk_version: str
    sdk_source: str
    types: list[TypeCoverage]

    def by_domain(self, domain: str) -> list[TypeCoverage]:
        prefix = "ISch_" if domain == "SCH" else "IPCB_"
        return [t for t in self.types if t.interface.startswith(prefix)]

    def totals(self) -> tuple[int, int]:
        """Distinct interface/property pairs reached, over pairs declared.

        Counted per interface rather than globally, because the same
        property name on two interfaces is two different members and
        collapsing them would flatter the number.
        """
        reached = declared = 0
        for t in self.types:
            if not t.resolved:
                continue
            got, total = t.ratio()
            reached += got
            declared += total
        return reached, declared

    def unresolved(self) -> list[TypeCoverage]:
        """Types whose interface the reflection did not produce.

        Either our name for it is wrong, or the assembly does not
        declare it under that name. Both are worth seeing; neither is
        counted as a gap in coverage, since there is nothing to cover.
        A type known to have no interface of its own is not among them:
        see ``not_in_sdk``.
        """
        return [t for t in self.types if not t.resolved and t.in_sdk]

    def not_in_sdk(self) -> list[TypeCoverage]:
        """Types the SDK declares no interface for, so nothing measures them."""
        return [t for t in self.types if not t.in_sdk]


def _delegated_for(delegated: dict | None, constant: str) -> set[str]:
    """What a chain's delegations add for one object type.

    Its own entry, plus the calls no branch guards on a type, which are
    reachable for whatever object the chain is given.
    """
    delegated = delegated or {}
    return delegated.get(constant, set()) | delegated.get(ANY_TYPE, set())


def _coverage_for(model: SdkModel, type_name: str, constant: str,
                  domain: str, read_heads: set[str],
                  write_heads: set[str],
                  read_delegated: dict | None = None,
                  write_delegated: dict | None = None) -> TypeCoverage:
    iface_name = interface_for(constant, domain)
    # Reachable for THIS constant only, because that is what the branch
    # guarding the delegation tests. A flat union would credit a pin
    # with properties only a component can answer.
    read_heads = read_heads | _delegated_for(read_delegated, constant)
    write_heads = write_heads | _delegated_for(write_delegated, constant)
    iface: Interface | None = model.get(iface_name)
    declared = model.effective_properties(iface_name) if iface else {}
    # Listed as absent but declared after all: measure it like any other,
    # and the real-SDK test says the listing is stale.
    note = SCH_NOT_IN_SDK.get(constant, "") if domain == "SCH" else ""
    return TypeCoverage(
        type_name=type_name,
        constant=constant,
        interface=iface_name,
        resolved=iface is not None,
        declared=declared,
        read_covered=read_heads & set(declared),
        write_covered=write_heads & set(declared),
        in_sdk=iface is not None or not note,
        note=note,
    )


def build_report(model: SdkModel | None = None) -> Report:
    """Measure every supported object type against the SDK."""
    model = model or load_sdk()

    sch_read = handled_heads("GetSchProperty")
    sch_write = handled_heads("SetSchProperty")
    pcb_read = handled_heads("GetPCBProperty")
    pcb_write = handled_heads("SetPCBProperty")
    sch_read_delegated = delegated_heads("GetSchProperty")
    sch_write_delegated = delegated_heads("SetSchProperty")
    pcb_read_delegated = delegated_heads("GetPCBProperty")
    pcb_write_delegated = delegated_heads("SetPCBProperty")

    types: list[TypeCoverage] = []
    seen: set[tuple[str, str]] = set()

    for name, constant in sorted(supported_sch_types().items()):
        # Several words map to one constant (component/schcomponent);
        # measure the constant once and name it by its first word.
        if ("SCH", constant) in seen:
            continue
        seen.add(("SCH", constant))
        types.append(_coverage_for(model, name, constant, "SCH",
                                   sch_read, sch_write,
                                   sch_read_delegated, sch_write_delegated))

    for name, constant in sorted(supported_pcb_types().items()):
        if ("PCB", constant) in seen:
            continue
        seen.add(("PCB", constant))
        types.append(_coverage_for(model, name, constant, "PCB",
                                   pcb_read, pcb_write,
                                   pcb_read_delegated, pcb_write_delegated))

    return Report(sdk_version=model.file_version,
                  sdk_source=model.source,
                  types=types)


def format_report(report: Report, detail: int = 0) -> str:
    """Render the report for a terminal.

    ``detail`` is how many missing property names to list per type; 0
    gives the summary alone.
    """
    out: list[str] = []
    out.append(f"Altium SDK coverage, against {report.sdk_source}")
    reached, declared = report.totals()
    pct = (100.0 * reached / declared) if declared else 0.0
    out.append(f"  {reached} of {declared} declared properties reachable "
               f"({pct:.1f}%)")
    out.append("")

    for domain, label in (("SCH", "Schematic"), ("PCB", "Board")):
        rows = [t for t in report.by_domain(domain) if t.resolved]
        if not rows:
            continue
        got = sum(t.ratio()[0] for t in rows)
        tot = sum(t.ratio()[1] for t in rows)
        share = (100.0 * got / tot) if tot else 0.0
        out.append(f"{label}: {got} of {tot} ({share:.1f}%) "
                   f"across {len(rows)} object types")
        rows.sort(key=lambda t: t.ratio()[1] - t.ratio()[0], reverse=True)
        for t in rows:
            got_t, tot_t = t.ratio()
            out.append(f"  {t.type_name:<22} {t.interface:<28} "
                       f"{got_t:>3}/{tot_t:<3}")
            if detail and t.missing_reads:
                names = sorted(t.missing_reads)[:detail]
                out.append(f"      missing reads: {', '.join(names)}"
                           + (" ..." if len(t.missing_reads) > detail else ""))
        out.append("")

    missing = report.unresolved()
    if missing:
        out.append(f"No SDK interface resolved for {len(missing)} types "
                   f"(nothing to measure against):")
        out.append("  " + ", ".join(
            f"{t.type_name} -> {t.interface}" for t in missing))
    absent = report.not_in_sdk()
    if absent:
        out.append(f"Not declared by the SDK, so not measured ({len(absent)}):")
        for t in absent:
            out.append(f"  {t.type_name}: {t.note}")
    return "\n".join(out)


def report_as_dict(report: Report) -> dict:
    """The same measurement, for a caller that wants to diff it over time.

    Missing names are included in full rather than truncated: the point
    of the machine-readable form is to feed the generator and to let a
    later run show what closed, and a truncated list cannot do either.
    """
    reached, declared = report.totals()
    return {
        "sdk_source": report.sdk_source,
        "sdk_version": report.sdk_version,
        "reached": reached,
        "declared": declared,
        "types": [
            {
                "type": t.type_name,
                "constant": t.constant,
                "interface": t.interface,
                "resolved": t.resolved,
                "in_sdk": t.in_sdk,
                "declared": len(t.declared),
                "reached": t.ratio()[0],
                "missing_reads": sorted(t.missing_reads),
                "missing_writes": sorted(t.missing_writes),
            }
            for t in report.types
        ],
    }
