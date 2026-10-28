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


#: A chain delegates the generated accessors rather than carrying a
#: second copy of them, so the names it answers are not all written in
#: its own body. The callee is named for the job, ending in Generated.
_DELEGATE = re.compile(r"\b(\w+Generated)\(")

#: The ObjectId test that guards a branch. The last one before a
#: delegation call is the type that delegation is reachable for.
_GUARD = re.compile(r"ObjectId = (e\w+)")


def handled_names(function_name: str) -> set[str]:
    """Every property name a chain carries in its own body.

    Not what it ANSWERS: a chain may delegate to a generated routine,
    and those names are reported separately by delegated_names, because
    they are reachable only for the object type the delegation is
    guarded on.
    """
    path, _ = CHAINS[function_name]
    body = chain_body(_read(path), function_name)
    return set(re.findall(r"PropName = '([^']+)'", body))


def delegated_names(function_name: str) -> dict:
    """Names a chain answers through a generated routine, by object type.

    The branch reads `If Obj.ObjectId = eSchComponent Then ... Result :=
    GetSchComponentGenerated(...)`, so the names behind it are reachable
    for that constant and no other. Returning a flat set instead would
    credit every schematic type with properties only a component has.
    """
    path, _ = CHAINS[function_name]
    source = _read(path)
    body = chain_body(source, function_name)
    out: dict = {}
    for match in _DELEGATE.finditer(body):
        # The nearest ObjectId test before the call is its guard.
        guards = _GUARD.findall(body[:match.start()])
        if not guards:
            continue
        try:
            delegated = chain_body(source, match.group(1))
        except ValueError:
            continue
        names = set(re.findall(r"PropName = '([^']+)'", delegated))
        out.setdefault(guards[-1], set()).update(names)
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
        """
        return [t for t in self.types if not t.resolved]


def _coverage_for(model: SdkModel, type_name: str, constant: str,
                  domain: str, read_heads: set[str],
                  write_heads: set[str],
                  read_delegated: dict | None = None) -> TypeCoverage:
    iface_name = interface_for(constant, domain)
    # Reachable for THIS constant only, because that is what the branch
    # guarding the delegation tests. A flat union would credit a pin
    # with properties only a component can answer.
    read_heads = read_heads | (read_delegated or {}).get(constant, set())
    iface: Interface | None = model.get(iface_name)
    declared = model.effective_properties(iface_name) if iface else {}
    return TypeCoverage(
        type_name=type_name,
        constant=constant,
        interface=iface_name,
        resolved=iface is not None,
        declared=declared,
        read_covered=read_heads & set(declared),
        write_covered=write_heads & set(declared),
    )


def build_report(model: SdkModel | None = None) -> Report:
    """Measure every supported object type against the SDK."""
    model = model or load_sdk()

    sch_read = handled_heads("GetSchProperty")
    sch_read_delegated = delegated_heads("GetSchProperty")
    sch_write = handled_heads("SetSchProperty")
    pcb_read = handled_heads("GetPCBProperty")
    pcb_write = handled_heads("SetPCBProperty")

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
                                   sch_read_delegated))

    for name, constant in sorted(supported_pcb_types().items()):
        if ("PCB", constant) in seen:
            continue
        seen.add(("PCB", constant))
        types.append(_coverage_for(model, name, constant, "PCB",
                                   pcb_read, pcb_write))

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
                "declared": len(t.declared),
                "reached": t.ratio()[0],
                "missing_reads": sorted(t.missing_reads),
                "missing_writes": sorted(t.missing_writes),
            }
            for t in report.types
        ],
    }
