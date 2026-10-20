# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Turn a validated DesignPlan into an ordered list of EasyEDA calls.

WHY THIS IS NOT AN EXECUTOR. ``design/executor.py`` drives Altium
directly, and it names Altium bridge commands throughout, so it has no
seam an EasyEDA backend could be dropped into. Rather than abstract a
1100-line module that works, this follows the pattern
``lib_easyeda_import`` already uses: produce the ordered sequence of
this server's own tool calls and hand it back.

The consequence is the useful part. The sequence is data, so it can be
read, diffed and validated before anything touches a design, and the
Altium path is untouched by anything here.

WHAT IT DELIBERATELY DOES NOT DO. It never picks a library part. Altium
resolves a symbol by name; EasyEDA needs the ``{libraryUuid, uuid}``
pair a search returns, and a search for an MPN can come back with
several. Choosing one silently is how a board ends up with the wrong
footprint under a BOM line that reads correctly, so an unresolved part
becomes a search step plus an explicit hole in the plan, and emitting
stops short of pretending the design is placeable.

UNITS. Every coordinate here is in mils, matching the templates and
the rest of this project. The conversion to EasyEDA's schematic units
belongs to the tool layer, in ``MILS_PER_SCHEMATIC_UNIT``, and must not
be repeated here: applying it twice is a hundredfold error that still
draws a plausible-looking schematic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .plan import DesignPlan, PartStatus

__all__ = [
    "EmittedCall",
    "EasyEdaPlan",
    "emit_easyeda_plan",
    "emit_template_connections",
]


@dataclass
class EmittedCall:
    """One tool call, with why it is here."""

    tool: str
    arguments: dict[str, Any]
    #: What this step is for, in one line, for a human reading the plan.
    purpose: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": self.arguments,
            "purpose": self.purpose,
        }


@dataclass
class EasyEdaPlan:
    """The emitted sequence, plus what stopped it being complete."""

    calls: list[EmittedCall] = field(default_factory=list)
    #: Parts with no library uuid and uuid pair yet. Each one is a
    #: search step in ``calls``; this list is what the caller must
    #: resolve before the sequence can run.
    #:
    #: EITHER SPELLING IS ACCEPTED. ``lib.search_devices`` answers with
    #: ``libraryUuid`` and this emitter was written against
    #: ``library_uuid``, so feeding a search result straight back left
    #: every part unresolved even though the documented flow chains
    #: exactly those two steps. See ``_library_uuid``.
    unresolved_parts: list[dict[str, Any]] = field(default_factory=list)
    #: Reasons the plan cannot be run as emitted. Non-empty means do not
    #: run it.
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Nets this sequence does NOT connect, and what connects them.
    #:
    #: Placement can be emitted from a plan alone; a wire is drawn at a
    #: pin, and pin coordinates only exist once the symbols are placed.
    #: So a full schematic takes two passes, and the first reports
    #: `runnable` true with every net still missing. Carried as data so
    #: a caller can see the sequence is a stage rather than the whole
    #: job without having to read the note.
    nets_pending: int = 0
    next_step: Optional[str] = None

    @property
    def runnable(self) -> bool:
        return not self.blockers and not self.unresolved_parts

    @property
    def complete(self) -> bool:
        """Runnable AND nothing deferred to a later pass."""
        return self.runnable and not self.nets_pending

    def to_dict(self) -> dict[str, Any]:
        return {
            "runnable": self.runnable,
            "complete": self.complete,
            "calls": [c.to_dict() for c in self.calls],
            "unresolved_parts": self.unresolved_parts,
            "blockers": self.blockers,
            "nets_pending": self.nets_pending,
            "next_step": self.next_step,
            "notes": self.notes,
        }


def _library_uuid(ref: dict) -> Optional[str]:
    """The library uuid from a resolution entry, either spelling.

    ``lib.search_devices`` answers with ``libraryUuid``, because that is
    what the editor's own API returns. This emitter asked for
    ``library_uuid``. The documented flow chains the two directly, emit
    a search step, run it, feed the result back as a resolution, and it
    did not join: a caller who passed the search result through
    unchanged had every part reported as unresolved.

    Both spellings are accepted, snake_case first since it matches the
    argument the place call takes. Converting at the boundary beats
    making every caller rename a field the editor chose.
    """
    return ref.get("library_uuid") or ref.get("libraryUuid")


def _search_terms(part: Any) -> Optional[str]:
    """What to search the EasyEDA library for, for one part.

    MPN first: it identifies one physical part, and it is what the
    atomic-parts contract requires every existing part to carry. A
    library reference is a name in somebody's library and can match
    several things, so it is a fallback rather than a choice.
    """
    if getattr(part, "mpn", None) and part.mpn.strip():
        return part.mpn.strip()
    if getattr(part, "lib_ref", None) and part.lib_ref.strip():
        return part.lib_ref.strip()
    return None


def emit_easyeda_plan(
    plan: DesignPlan,
    *,
    placements: Optional[dict[str, tuple[float, float, float]]] = None,
    resolved_parts: Optional[dict[str, dict[str, str]]] = None,
) -> EasyEdaPlan:
    """Emit the EasyEDA call sequence for ``plan``.

    Args:
        plan: a validated DesignPlan.
        placements: refdes -> (x_mils, y_mils, rotation), from the hand
            template. Parts with no
            placement are reported rather than dropped at the origin,
            where they would stack invisibly on top of each other.
        resolved_parts: refdes -> {"library_uuid": ..., "uuid": ...} for
            parts already looked up. Anything absent gets a search step
            and is listed as unresolved.

    Returns:
        An EasyEdaPlan. ``runnable`` is false whenever anything was left
        undecided, and the sequence should not be run in that state.
    """
    out = EasyEdaPlan()
    placements = placements or {}
    resolved_parts = resolved_parts or {}

    cross = plan.cross_check()
    if cross:
        out.blockers.extend(cross)
        return out

    needs_creation = [p.refdes for p in plan.parts
                      if p.status == PartStatus.NEEDS_CREATION]
    if needs_creation:
        # Same refusal the Altium executor makes, for the same reason: a
        # partially placed design reads as a finished one.
        out.blockers.append(
            "plan contains parts that need creating "
            f"({', '.join(sorted(needs_creation))}); emitting a sequence "
            "that places the rest would produce a design that looks "
            "complete and is not")
        return out

    out.calls.append(EmittedCall(
        tool="easyeda_ping",
        arguments={},
        purpose="confirm an editor is connected before changing anything",
    ))

    for part in plan.parts:
        ref = resolved_parts.get(part.refdes)

        # A RESOLUTION THAT DID NOT PARSE IS NOT AN UNRESOLVED PART.
        #
        # An entry present under the wrong keys used to fall through to
        # the search branch, so a caller who HAD looked the part up was
        # told to look it up again, with nothing saying why. The keys
        # here are snake_case, and this file described them both ways:
        # the field comment above said {libraryUuid, uuid} and the
        # docstring said {library_uuid, uuid}. Following the wrong one
        # cost a silent downgrade.
        if ref and not (_library_uuid(ref) and ref.get("uuid")):
            out.unresolved_parts.append({
                "refdes": part.refdes,
                "reason": (
                    f"resolved_parts has an entry for {part.refdes} but it "
                    f"carries {sorted(ref)} instead of a library uuid "
                    f"(library_uuid or libraryUuid) and uuid, so it "
                    f"could not be used"),
            })
            continue

        if ref and _library_uuid(ref) and ref.get("uuid"):
            placement = placements.get(part.refdes)
            if placement is None:
                out.unresolved_parts.append({
                    "refdes": part.refdes,
                    "reason": "no placement was computed for this part",
                })
                continue
            x, y, rotation = placement
            out.calls.append(EmittedCall(
                tool="easyeda_place_schematic_component",
                arguments={
                    "library_uuid": _library_uuid(ref),
                    "uuid": ref["uuid"],
                    "x": x,
                    "y": y,
                    "rotation": rotation,
                },
                purpose=f"place {part.refdes} ({part.value or part.lib_ref})",
            ))
            continue

        terms = _search_terms(part)
        if terms is None:
            out.unresolved_parts.append({
                "refdes": part.refdes,
                "reason": "no mpn and no lib_ref to search for",
            })
            continue

        out.calls.append(EmittedCall(
            tool="easyeda_search_devices",
            arguments={"query": terms},
            purpose=f"find a library part for {part.refdes} ({terms})",
        ))
        out.unresolved_parts.append({
            "refdes": part.refdes,
            "search": terms,
            "reason": "no library uuid pair yet; run the search and choose",
        })

    if out.unresolved_parts:
        out.notes.append(
            f"{len(out.unresolved_parts)} of {len(plan.parts)} parts are "
            "not placeable yet. Resolve each one to a "
            "{library_uuid, uuid} pair and emit again; nothing here picks "
            "among search results, because a wrong pick produces a board "
            "that is wrong in a way the BOM does not show.")

    # Connectivity cannot be emitted alongside placement, and saying so
    # is better than emitting coordinates that would be guesses. A wire
    # or a net label is drawn AT a pin, and a pin's position is not
    # known until its symbol is placed and the editor is asked where its
    # pins landed. The template positions parts, not pins.
    count = len(plan.nets)
    out.notes.append(
        f"{count} net{'' if count == 1 else 's'} "
        f"{'is' if count == 1 else 'are'} not in this sequence. Connecting "
        "them is a second pass: place the parts, read the pin coordinates "
        "back with easyeda_get_schematic_pins, then emit the template's "
        "wires and labels against real positions. Emitting them now would "
        "mean inventing coordinates, and a schematic wired to the wrong "
        "points still looks like a schematic.")

    # SAY IT IN DATA, not only in prose.
    #
    # `runnable` is the flag a caller branches on, and it is true here
    # while every net is still missing. That is correct for what this
    # pass is, and it reads as "the sequence is complete" to anything
    # that does not also parse the note. Running it and stopping leaves
    # parts placed and nothing wired, which looks like a finished
    # schematic and is not one.
    out.nets_pending = count
    out.next_step = (
        "easyeda_emit_template_connections" if count else None)

    return out


def emit_template_connections(drawing: Any) -> EasyEdaPlan:
    """The calls that draw a checked hand template (design/sch_template).

    Every segment, power or ground flag and net label the template
    planned, nothing more: the drawing was decided by whoever wrote the
    template and checked against the real pins before this runs. A
    drawing with errors emits nothing and lists them as blockers.
    """
    out = EasyEdaPlan()
    if drawing.errors:
        out.blockers.extend(
            f"{e.get('check')}: {e.get('where')}: {e.get('detail')}"
            for e in drawing.errors)
        return out
    for s in drawing.segments:
        out.calls.append(EmittedCall(
            tool="easyeda_add_wire",
            arguments={"points": [[s.x1, s.y1], [s.x2, s.y2]],
                       **({"net": s.net} if s.net else {})},
            purpose=f"wire {s.net or 'segment'} {s.x1},{s.y1} to {s.x2},{s.y2}",
        ))
    for o in drawing.ports:
        kind = "Ground" if o["style"].startswith("gnd") else "Power"
        out.calls.append(EmittedCall(
            tool="easyeda_create_net_flag",
            arguments={"name": o["net"], "x": o["x"], "y": o["y"], "kind": kind},
            purpose=f"{kind.lower()} flag {o['net']}",
        ))
    for o in drawing.labels:
        out.calls.append(EmittedCall(
            tool="easyeda_create_net_label",
            arguments={"name": o["net"], "x": o["x"], "y": o["y"]},
            purpose=f"label {o['net']}",
        ))
    out.notes.extend(
        f"{w.get('check')}: {w.get('where')}: {w.get('detail')}"
        for w in drawing.warnings)
    return out
