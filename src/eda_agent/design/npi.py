# SPDX-License-Identifier: Apache-2.0
"""NPI phase gates: feasibility, prototype, EVT, DVT, PVT.

A PCBA program does not go from idea to volume in one step. It passes
through gates, and each gate asks a different question:

    feasibility  can this be built at all, at this cost, with parts that
                 exist? Decided BEFORE the design is finished, because
                 the point is to stop early when the answer is no.
    prototype    does a board exist and come up? First hardware. The
                 question is existence, not correctness.
    EVT          does the DESIGN meet its electrical specification?
    DVT          does it meet EVERY specification, including the ones
                 nobody enjoys: environmental, regulatory, sourcing.
    PVT          is the PROCESS able to build it repeatably at volume?
                 The design is frozen here; what is under test is the
                 line, the panel, the stencil and the test coverage.

Running those gates out of order is the common and expensive mistake.
A board that passes DVT on a design whose critical part went EOL during
EVT has validated the wrong thing.

WHAT THIS MODULE IS. The gates as data: every criterion carries what it
asks, why it belongs to that phase, how severely it binds, and - where
this server can actually check it - the tools that produce the
evidence. Evaluation is pure Python over recorded evidence, so it runs
without Altium and is fully testable.

WHAT IT IS NOT. It does not run the checks itself. A gate that silently
re-ran a dozen Altium reads would be slow, would need a focused
project, and would hide which evidence is fresh. Instead a criterion
names the tools, a client runs them, and the result is recorded against
the criterion with a timestamp. That is the same split the design
state machine already uses: the playbook is server-side data, the
driving loop is the client's.

Evidence does not transfer between phases. Passing a check at EVT says
nothing about the board at DVT, because the design changed in between.
``stale_after_days`` bounds how long a recorded result keeps counting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable, Optional

# --- phases ---------------------------------------------------------------


class Phase(str, Enum):
    """The gates, in the order a program passes them."""

    FEASIBILITY = "feasibility"
    PROTOTYPE = "prototype"
    EVT = "evt"
    DVT = "dvt"
    PVT = "pvt"
    MP = "mp"

    @property
    def label(self) -> str:
        return _PHASE_LABELS[self]


_PHASE_LABELS = {
    Phase.FEASIBILITY: "Feasibility",
    Phase.PROTOTYPE: "Prototype build",
    Phase.EVT: "EVT (Engineering Validation Test)",
    Phase.DVT: "DVT (Design Validation Test)",
    Phase.PVT: "PVT (Production Validation Test)",
    Phase.MP: "Mass production",
}

#: Gate order. MP is the terminal state, so it has no gate of its own:
#: a program reaches it by passing PVT.
PHASE_ORDER: tuple[Phase, ...] = (
    Phase.FEASIBILITY, Phase.PROTOTYPE, Phase.EVT, Phase.DVT, Phase.PVT,
    Phase.MP,
)

GATED_PHASES: tuple[Phase, ...] = PHASE_ORDER[:-1]


def next_phase(phase: Phase) -> Optional[Phase]:
    index = PHASE_ORDER.index(phase)
    return PHASE_ORDER[index + 1] if index + 1 < len(PHASE_ORDER) else None


def parse_phase(value: str) -> Phase:
    """Accept the spellings people actually type.

    "EVT", "evt", "Engineering Validation" and "eng validation" all mean
    the same gate, and refusing any of them teaches nothing.
    """
    key = (value or "").strip().lower().replace("-", " ").replace("_", " ")
    direct = {p.value: p for p in Phase}
    if key in direct:
        return direct[key]
    aliases = {
        "feasability": Phase.FEASIBILITY,   # the common misspelling
        "concept": Phase.FEASIBILITY,
        "proto": Phase.PROTOTYPE,
        "prototype build": Phase.PROTOTYPE,
        "ev": Phase.PROTOTYPE,
        "engineering validation": Phase.EVT,
        "engineering validation test": Phase.EVT,
        "eng validation": Phase.EVT,
        "design validation": Phase.DVT,
        "design validation test": Phase.DVT,
        "production validation": Phase.PVT,
        "production validation test": Phase.PVT,
        "mass production": Phase.MP,
        "production": Phase.MP,
    }
    if key in aliases:
        return aliases[key]
    raise ValueError(
        f"unknown phase {value!r}. Expected one of: "
        + ", ".join(p.value for p in PHASE_ORDER))


# --- criteria -------------------------------------------------------------

#: A criterion that fails stops the gate. Everything else is advisory:
#: worth recording, not worth blocking a build on.
BLOCKER = "blocker"
ADVISORY = "advisory"

#: How the evidence is produced.
AUTOMATIC = "automatic"   # a tool this server has can answer it
MANUAL = "manual"         # a human attests; no tool can know


@dataclass(frozen=True)
class Criterion:
    """One thing a gate asks, and how to answer it."""

    id: str
    phase: Phase
    title: str
    #: Why this belongs to THIS gate rather than a later one. The cost of
    #: discovering it late is the whole argument for phase gates, so it is
    #: recorded rather than assumed.
    why: str
    severity: str = BLOCKER
    kind: str = MANUAL
    #: eda-agent tools that produce the evidence, in the order to run them.
    tools: tuple[str, ...] = ()
    #: What a passing result looks like, in one sentence.
    passes_when: str = ""
    #: Evidence older than this stops counting. None means it never goes
    #: stale, which is true only of facts about the program, not the board.
    stale_after_days: Optional[int] = 30


def _c(**kwargs) -> Criterion:
    return Criterion(**kwargs)


# --- the gates ------------------------------------------------------------
#
# Written to be argued with. Every criterion that this server can check
# names the tools; the rest are honest about needing a human. The split
# is roughly half and half, and pretending otherwise would be the one
# failure mode that makes a gate worse than no gate.

FEASIBILITY_CRITERIA: tuple[Criterion, ...] = (
    _c(id="feas.requirement", phase=Phase.FEASIBILITY,
       title="Requirement captured with no open questions",
       why="Feasibility answers a question. An unstated supply range or "
           "temperature limit means the question is not yet asked, and "
           "every number computed after it is provisional.",
       kind=AUTOMATIC, tools=("design_validate_requirement",),
       passes_when="validate_requirement returns ok with open_questions empty",
       stale_after_days=None),
    _c(id="feas.critical_parts", phase=Phase.FEASIBILITY,
       title="Critical parts exist, are in stock, and are not end-of-life",
       why="The cheapest moment to discover an unobtainable regulator is "
           "before a schematic is drawn around it. After EVT it costs a "
           "respin; after PVT it costs the line.",
       kind=AUTOMATIC, tools=("part_search", "part_fetch"),
       passes_when="every critical part has stock and a lifecycle status "
                   "that is not EOL or NRND"),
    _c(id="feas.long_lead", phase=Phase.FEASIBILITY,
       title="Long-lead items identified with their quoted lead times",
       why="Lead time, not unit price, is what moves a build date. A "
           "26-week part found at DVT has already decided the schedule.",
       kind=AUTOMATIC, tools=("part_fetch", "part_search"),
       passes_when="every part over the program's lead-time threshold is "
                   "listed with a quoted lead time and a mitigation"),
    _c(id="feas.bom_cost", phase=Phase.FEASIBILITY,
       title="Rough BOM cost inside the target at the intended volume",
       why="Cost at volume is a design input, not an outcome. It decides "
           "layer count, package choices and whether the architecture is "
           "viable at all.",
       kind=MANUAL,
       passes_when="costed BOM at target volume is within the cost target, "
                   "or the gap has an agreed plan"),
    _c(id="feas.stackup", phase=Phase.FEASIBILITY,
       title="Stackup and layer count chosen, with impedance targets shown "
             "achievable",
       why="A controlled-impedance target that the chosen stackup cannot "
           "hit at a manufacturable trace width is found here or during "
           "routing, and during routing it is a re-layout.",
       kind=AUTOMATIC,
       tools=("design_list_stackups", "pcb_calc_trace_width_for_impedance",
              "pcb_calc_impedance"),
       passes_when="each controlled-impedance class has a trace width and "
                   "spacing the chosen stackup and fab can produce"),
    _c(id="feas.power_budget", phase=Phase.FEASIBILITY,
       title="Power budget closes, with headroom and worst-case dissipation",
       why="Thermal design is bounded by this number, and so is connector, "
           "regulator and copper choice. Finding it short later changes "
           "all four.",
       kind=MANUAL,
       passes_when="worst-case load is inside every rail's capability with "
                   "the agreed headroom, and dissipation is accounted for"),
    _c(id="feas.mechanical", phase=Phase.FEASIBILITY,
       title="Board outline, height limits and connector positions agreed "
             "with mechanical",
       why="The enclosure is usually further along than the board. An "
           "outline agreed late is the single most common cause of a "
           "mechanical respin.",
       kind=MANUAL,
       passes_when="outline, keep-outs, maximum component height and "
                   "connector positions are signed off against the "
                   "enclosure model"),
    _c(id="feas.regulatory", phase=Phase.FEASIBILITY,
       title="Regulatory and safety path identified",
       why="Target markets decide creepage and clearance, isolation "
           "ratings and EMC margin, all of which are layout constraints. "
           "They cannot be applied retroactively to a finished board.",
       kind=MANUAL,
       passes_when="applicable standards are named, with the insulation "
                   "class and pollution degree the layout must hold to"),
)

PROTOTYPE_CRITERIA: tuple[Criterion, ...] = (
    _c(id="proto.erc", phase=Phase.PROTOTYPE,
       title="Schematic compiles with ERC clean, or every violation waived "
             "in writing",
       why="An ERC error on the board that gets built is a bring-up day "
           "lost to something the compiler already knew.",
       kind=AUTOMATIC,
       tools=("proj_force_recompile", "design_validate", "proj_get_messages"),
       passes_when="no error-severity violations remain unwaived"),
    _c(id="proto.footprints", phase=Phase.PROTOTYPE,
       title="Every part has a footprint, and every footprint matches its "
             "land-pattern spec",
       why="A footprint checked against the datasheet costs minutes. A "
           "footprint checked by soldering costs the build.",
       kind=AUTOMATIC,
       tools=("lib_audit_footprint_vs_datasheet", "lib_compare_footprints"),
       passes_when="every component resolves to a footprint and each audited "
                   "one matches its spec within tolerance"),
    _c(id="proto.drc", phase=Phase.PROTOTYPE,
       title="Board fully routed and DRC clean",
       why="Existence of hardware is what this gate tests, and an "
           "unrouted net or a clearance violation means there is none.",
       kind=AUTOMATIC,
       tools=("pcb_run_drc", "pcb_get_unrouted_nets"),
       passes_when="zero unrouted connections and no unwaived DRC violations"),
    _c(id="proto.fab_package", phase=Phase.PROTOTYPE,
       title="Fab and assembly package generates and has been read back",
       why="An OutJob that generates is not the same as one that generates "
           "the right thing. Reading the Gerbers back catches a missing "
           "layer before the fab does.",
       kind=AUTOMATIC,
       tools=("proj_run_outjob", "pcb_get_layer_stackup"),
       passes_when="the package generates and its copper layer count, "
                   "outline and drill set match the board"),
    _c(id="proto.bom", phase=Phase.PROTOTYPE,
       title="BOM exports with an orderable part number on every line",
       why="A line with no MPN is a part nobody can buy, found by the "
           "person trying to buy it.",
       kind=AUTOMATIC, tools=("design_generate_bom", "part_search"),
       passes_when="every fitted line has an MPN that resolves at a "
                   "distributor"),
    _c(id="proto.mechanical_fit", phase=Phase.PROTOTYPE,
       title="3D fit against the enclosure, with no collisions",
       why="The first mechanical check that uses the real board rather "
           "than the agreed outline. Component height is where it usually "
           "fails.",
       kind=AUTOMATIC, tools=("proj_export_step", "pcb_get_components"),
       passes_when="the exported STEP fits the enclosure model with no "
                   "interference"),
    _c(id="proto.bringup_access", phase=Phase.PROTOTYPE,
       title="Bring-up access on power rails, resets, clocks and boot straps",
       why="This is the gate whose output a human has to debug. A rail "
           "with no probe point turns a ten-minute measurement into "
           "rework.",
       kind=MANUAL,
       passes_when="each rail, reset, clock and strap has a test point or "
                   "accessible pad"),
    _c(id="proto.bringup_plan", phase=Phase.PROTOTYPE,
       title="Bring-up plan written, with a power-on sequence and abort "
             "criteria",
       why="Powering a first board without a sequence and a current limit "
           "is how first boards die.",
       kind=MANUAL,
       passes_when="the plan states the order, the expected currents and "
                   "what aborts the power-on"),
)

EVT_CRITERIA: tuple[Criterion, ...] = (
    _c(id="evt.prototype_holds", phase=Phase.EVT,
       title="Every prototype criterion still passes on the current design",
       why="The design changed after the prototype build. Evidence from "
           "the old revision is evidence about a board that no longer "
           "exists.",
       kind=AUTOMATIC, tools=("design_npi_gate",),
       passes_when="the prototype gate passes against the current revision"),
    _c(id="evt.net_classes", phase=Phase.EVT,
       title="Every net belongs to a class, and every class has rules",
       why="A net in no class is routed to the default rule, which is "
           "nobody's intent. It is the quietest way for a design to be "
           "wrong while DRC stays green.",
       kind=AUTOMATIC,
       tools=("pcb_get_net_classes", "design_synthesize_rules"),
       passes_when="no net falls outside a class, and each class has "
                   "width, clearance and via rules"),
    _c(id="evt.impedance", phase=Phase.EVT,
       title="Controlled-impedance nets routed to a defined profile",
       why="Impedance stated in the stackup and impedance achieved on the "
           "board are different claims. This gate is where the second one "
           "has to be true.",
       kind=AUTOMATIC,
       tools=("pcb_get_impedance_profiles",
              "pcb_calc_trace_width_for_impedance"),
       passes_when="every controlled-impedance class maps to a profile "
                   "whose calculated impedance is inside tolerance"),
    _c(id="evt.diffpairs", phase=Phase.EVT,
       title="Differential pairs matched, and gaps held along the route",
       why="A pair that diverges mid-route still passes DRC and still "
           "fails the eye diagram.",
       kind=AUTOMATIC,
       tools=("pcb_audit_diff_pair", "pcb_get_trace_lengths",
              "pcb_calc_length_match"),
       passes_when="intra-pair skew and inter-pair matching are inside "
                   "their budgets"),
    _c(id="evt.decoupling", phase=Phase.EVT,
       title="Decoupling present per the datasheet, and placed close",
       why="Decoupling that is on the schematic but across the board is "
           "the defect this catches, and no ERC or DRC rule sees it.",
       kind=AUTOMATIC,
       tools=("audit_find_missing_decoupling", "design_find_loops"),
       passes_when="each supply pin has its datasheet decoupling within "
                   "the agreed distance, with a short return path"),
    _c(id="evt.return_paths", phase=Phase.EVT,
       title="Reference planes continuous under high-speed nets",
       why="A split crossed by a clock is a radiated-emissions failure "
           "found at DVT, in a chamber, at a day rate.",
       kind=AUTOMATIC, tools=("design_find_loops", "pcb_layout_audit"),
       passes_when="no high-speed net crosses a plane split or void"),
    _c(id="evt.thermal", phase=Phase.EVT,
       title="Thermal path sized for every dissipating part",
       why="Copper area and via count are layout decisions. Discovering "
           "them short at DVT means moving parts.",
       kind=AUTOMATIC, tools=("pcb_calc_thermal_vias", "pcb_get_fab_stats"),
       passes_when="each dissipating part has the copper area and via "
                   "count its junction temperature needs"),
    _c(id="evt.test_results", phase=Phase.EVT,
       title="EVT test cases executed, each tracing to a requirement",
       why="A test that traces to nothing proves nothing about the "
           "specification, and a requirement with no test is untested.",
       kind=MANUAL,
       passes_when="every functional requirement has a passing test case, "
                   "or a recorded deviation"),
    _c(id="evt.issue_list", phase=Phase.EVT,
       title="Prototype issues closed, or deferred with a named owner",
       why="An issue list that quietly carries forward is how a known "
           "defect reaches production.",
       kind=MANUAL,
       passes_when="each issue is closed, waived with rationale, or has an "
                   "owner and a target phase"),
)

DVT_CRITERIA: tuple[Criterion, ...] = (
    _c(id="dvt.evt_holds", phase=Phase.DVT,
       title="Every EVT criterion still passes on the current design",
       why="Same reason as at EVT, and more so: DVT changes are the ones "
           "most likely to have been made under schedule pressure.",
       kind=AUTOMATIC, tools=("design_npi_gate",),
       passes_when="the EVT gate passes against the current revision"),
    _c(id="dvt.dfm", phase=Phase.DVT,
       title="DFM clean against the fab profile that will actually build it",
       why="Minimum trace, annular ring and drill sizes are per-fab. A "
           "board that is clean against a generic profile and not against "
           "the chosen one gets quoted at a higher class or refused.",
       kind=AUTOMATIC,
       tools=("pcb_check_fab_output", "design_load_fab_profile",
              "pcb_get_drill_layer_pairs"),
       passes_when="no violation of the named fab's capability, at the "
                   "quoted class"),
    _c(id="dvt.creepage", phase=Phase.DVT,
       title="Creepage and clearance met for the working voltage and "
             "pollution degree",
       why="This is a safety limit, not a design preference. It is also "
           "the one most often checked against the wrong column of the "
           "standard.",
       kind=AUTOMATIC, tools=("pcb_calc_conductor_spacing", "pcb_layout_audit"),
       passes_when="measured spacing meets the standard for the working "
                   "voltage, insulation class and pollution degree"),
    _c(id="dvt.second_source", phase=Phase.DVT,
       title="Every critical part has an approved second source, or an "
             "accepted single-source risk",
       why="Single sourcing is a decision, and it should be one somebody "
           "made on purpose rather than one the BOM made by default.",
       kind=AUTOMATIC, tools=("part_find_replacements", "design_generate_bom"),
       passes_when="each critical line has a qualified alternate, or a "
                   "signed single-source acceptance"),
    _c(id="dvt.lifecycle", phase=Phase.DVT,
       title="No EOL or NRND parts on the BOM",
       why="Lifecycle moves while the design is being validated. The "
           "status checked at feasibility is months old by now.",
       kind=AUTOMATIC, tools=("design_generate_bom", "part_fetch"),
       passes_when="every fitted line is in active production"),
    _c(id="dvt.silkscreen", phase=Phase.DVT,
       title="Silkscreen legible, with polarity and pin-1 marks that "
             "survive assembly",
       why="Silk under a part is silk nobody reads, and a missing pin-1 "
           "mark is a reversed part at rework.",
       kind=AUTOMATIC, tools=("pcb_layout_audit", "audit_find_designator_collisions"),
       passes_when="no silk over pads, and polarity or pin-1 visible on "
                   "every polarised part after assembly"),
    _c(id="dvt.environmental", phase=Phase.DVT,
       title="Environmental testing executed over the full specified range",
       why="Temperature, humidity, vibration and ESD find the failures "
           "that bench testing at room temperature cannot.",
       kind=MANUAL,
       passes_when="the environmental plan has been executed over the "
                   "specified range with results recorded"),
    _c(id="dvt.emc_prescan", phase=Phase.DVT,
       title="EMC pre-scan passed with margin",
       why="A full chamber booking after a failure costs weeks. A pre-scan "
           "finds the obvious emitters while fixing them is still a layout "
           "change rather than a respin.",
       kind=MANUAL,
       passes_when="radiated and conducted pre-scan show the agreed margin "
                   "to the limit line"),
    _c(id="dvt.panel", phase=Phase.DVT,
       title="Panelisation agreed with the assembler",
       why="Panel rails, tooling holes and break-off method constrain the "
           "board edge. Agreeing them after layout freeze means moving "
           "parts away from the edge.",
       kind=MANUAL,
       passes_when="the assembler has accepted the panel drawing, rails "
                   "and depanel method"),
)

PVT_CRITERIA: tuple[Criterion, ...] = (
    _c(id="pvt.dvt_holds", phase=Phase.PVT,
       title="Every DVT criterion still passes, and the design is frozen",
       why="PVT tests the process, not the design. A design still moving "
           "makes the yield number meaningless.",
       kind=AUTOMATIC, tools=("design_npi_gate",),
       passes_when="the DVT gate passes and the revision is under change "
                   "control"),
    _c(id="pvt.assembly_data", phase=Phase.PVT,
       title="Assembly package complete: placement, paste, fiducials",
       why="The line consumes these files directly. A missing paste layer "
           "or an unflagged fine-pitch part stops a run.",
       kind=AUTOMATIC, tools=("proj_run_outjob", "pcb_get_components"),
       passes_when="pick-and-place, paste layers and an assembly drawing "
                   "all generate and agree with the board"),
    _c(id="pvt.fiducials", phase=Phase.PVT,
       title="Global fiducials present, and local fiducials on fine-pitch parts",
       why="Placement accuracy on fine-pitch parts depends on them. Added "
           "after the panel is agreed, they have nowhere to go.",
       kind=AUTOMATIC, tools=("pcb_get_components", "pcb_layout_audit"),
       passes_when="at least three global fiducials, non-collinear, plus "
                   "local pairs on fine-pitch and BGA parts"),
    _c(id="pvt.testability", phase=Phase.PVT,
       title="Test coverage measured, with access for the chosen method",
       why="Coverage is a number, and it is the one that decides how many "
           "defects reach the customer. Measuring it after the panel is "
           "fixed leaves no way to improve it.",
       kind=AUTOMATIC, tools=("pcb_layout_audit", "pcb_get_pad_properties"),
       passes_when="nodal coverage meets the target for the chosen test "
                   "method, with reachable probe points"),
    _c(id="pvt.library_release", phase=Phase.PVT,
       title="Every part released in the component library at an approved "
             "lifecycle state",
       why="A board built from unreleased library items cannot be rebuilt "
           "identically, which is the entire point of this gate.",
       kind=AUTOMATIC, tools=("eng_a365_library_components", "eng_a365_projects"),
       passes_when="every component resolves to a released item at an "
                   "approved state"),
    _c(id="pvt.documentation", phase=Phase.PVT,
       title="Full documentation set released under revision control",
       why="The build package is the deliverable. Anything outside "
           "revision control is not part of it.",
       kind=AUTOMATIC, tools=("proj_run_outjob", "eng_a365_project"),
       passes_when="schematic, fab, assembly, BOM and test documents are "
                   "released at one revision"),
    _c(id="pvt.pilot_yield", phase=Phase.PVT,
       title="Pilot run yield meets target, with failures analysed",
       why="This is the measurement PVT exists to take. A yield figure "
           "with no failure analysis is a number without a cause.",
       kind=MANUAL,
       passes_when="pilot yield is at or above target, and each failure "
                   "has a root cause and an action"),
    _c(id="pvt.process", phase=Phase.PVT,
       title="Line process validated: reflow profile, AOI and test programs",
       why="A process that works for the pilot operator is not a validated "
           "process. Programs and profiles have to exist as artefacts.",
       kind=MANUAL,
       passes_when="reflow profile, AOI program and test program are "
                   "recorded and repeatable"),
)

CRITERIA_BY_PHASE: dict[Phase, tuple[Criterion, ...]] = {
    Phase.FEASIBILITY: FEASIBILITY_CRITERIA,
    Phase.PROTOTYPE: PROTOTYPE_CRITERIA,
    Phase.EVT: EVT_CRITERIA,
    Phase.DVT: DVT_CRITERIA,
    Phase.PVT: PVT_CRITERIA,
    Phase.MP: (),
}

ALL_CRITERIA: tuple[Criterion, ...] = tuple(
    c for phase in PHASE_ORDER for c in CRITERIA_BY_PHASE[phase])

CRITERIA_BY_ID: dict[str, Criterion] = {c.id: c for c in ALL_CRITERIA}


def criteria_for(phase: Phase) -> tuple[Criterion, ...]:
    return CRITERIA_BY_PHASE[phase]


# --- evidence -------------------------------------------------------------

PASS = "pass"
FAIL = "fail"
WAIVED = "waived"
#: Recorded, but the criterion is not answered yet.
PENDING = "pending"

VERDICTS = (PASS, FAIL, WAIVED, PENDING)


@dataclass(frozen=True)
class Evidence:
    """One recorded answer to one criterion.

    ``by`` is who or what produced it: a tool name for an automatic
    check, a person or role for an attestation. A waiver without a note
    is refused at the recording layer, because an unexplained waiver is
    indistinguishable from a skipped check six months later.
    """

    criterion_id: str
    verdict: str
    at: datetime
    by: str = ""
    note: str = ""
    #: Design revision this was measured against, when known. Evidence
    #: from another revision is reported, never silently counted.
    revision: str = ""

    def is_stale(self, now: datetime, criterion: Criterion) -> bool:
        if criterion.stale_after_days is None:
            return False
        return now - self.at > timedelta(days=criterion.stale_after_days)


@dataclass
class CriterionStatus:
    """A criterion with whatever is known about it."""

    criterion: Criterion
    evidence: Optional[Evidence] = None
    stale: bool = False
    wrong_revision: bool = False

    @property
    def verdict(self) -> str:
        """What the criterion counts as right now.

        Stale evidence and evidence from another revision both revert to
        pending: the check was done, but not to a board anyone can point
        at today.
        """
        if self.evidence is None:
            return PENDING
        if self.stale or self.wrong_revision:
            return PENDING
        return self.evidence.verdict

    @property
    def blocks(self) -> bool:
        return (self.criterion.severity == BLOCKER
                and self.verdict in (FAIL, PENDING))


@dataclass
class GateResult:
    """The whole gate, evaluated."""

    phase: Phase
    revision: str
    statuses: list[CriterionStatus] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(s.blocks for s in self.statuses)

    @property
    def blockers(self) -> list[CriterionStatus]:
        return [s for s in self.statuses if s.blocks]

    @property
    def advisories(self) -> list[CriterionStatus]:
        return [s for s in self.statuses
                if s.criterion.severity == ADVISORY
                and s.verdict in (FAIL, PENDING)]

    def counts(self) -> dict[str, int]:
        tally = {v: 0 for v in VERDICTS}
        for status in self.statuses:
            tally[status.verdict] += 1
        return tally

    def next_tools(self) -> list[str]:
        """Tools to run next, for the blocking automatic checks only.

        Ordered by the criteria themselves and de-duplicated, so a client
        can run the list top to bottom without deciding anything.
        """
        seen: list[str] = []
        for status in self.blockers:
            if status.criterion.kind != AUTOMATIC:
                continue
            for tool in status.criterion.tools:
                if tool not in seen:
                    seen.append(tool)
        return seen

    def awaiting_attestation(self) -> list[CriterionStatus]:
        """Blocking criteria no tool can answer.

        Separated from next_tools because the action is different: these
        need a person, and telling a client to keep calling tools at them
        is how a loop spins.
        """
        return [s for s in self.blockers if s.criterion.kind == MANUAL]


def evaluate_gate(phase: Phase, evidence: Iterable[Evidence], *,
                  revision: str = "",
                  now: Optional[datetime] = None) -> GateResult:
    """Score one gate against the evidence recorded for it.

    The newest evidence per criterion wins; earlier records are history,
    not votes. Evidence for criteria outside this phase is ignored rather
    than rejected, so a caller can pass the whole programme's record.
    """
    now = now or datetime.now()
    latest: dict[str, Evidence] = {}
    for item in evidence:
        seen = latest.get(item.criterion_id)
        if seen is None or item.at >= seen.at:
            latest[item.criterion_id] = item

    statuses = []
    for criterion in criteria_for(phase):
        found = latest.get(criterion.id)
        statuses.append(CriterionStatus(
            criterion=criterion,
            evidence=found,
            stale=bool(found and found.is_stale(now, criterion)),
            # Only judged when both sides name a revision. Evidence with
            # no revision is older practice, not a mismatch.
            wrong_revision=bool(
                found and revision and found.revision
                and found.revision != revision),
        ))
    return GateResult(phase=phase, revision=revision, statuses=statuses)


def programme_status(evidence: Iterable[Evidence], *, revision: str = "",
                     now: Optional[datetime] = None) -> dict:
    """Which gate the programme is at, and what the next one needs.

    The current phase is the first gate that does not pass. That is
    deliberately strict: a programme is not at DVT because somebody said
    so, it is at DVT because the EVT gate closed.
    """
    now = now or datetime.now()
    recorded = list(evidence)
    gates = []
    current: Optional[Phase] = None
    for phase in GATED_PHASES:
        result = evaluate_gate(phase, recorded, revision=revision, now=now)
        gates.append(result)
        if current is None and not result.passed:
            current = phase
    reached = Phase.MP if current is None else current
    return {
        "phase": reached.value,
        "phase_label": reached.label,
        "complete": current is None,
        "gates": gates,
    }


# --- durable record -------------------------------------------------------


class NpiRecord:
    """Append-only evidence log for one programme.

    The same shape as the design session journal and for the same
    reason: one JSON object per line, so a crash mid-write loses at most
    the last record and never corrupts the history before it. Nothing is
    ever rewritten, so a waiver granted in March is still visible in
    September alongside the one that replaced it.
    """

    def __init__(self, path):
        from pathlib import Path
        self.path = Path(path)

    def record(self, criterion_id: str, verdict: str, *, by: str = "",
               note: str = "", revision: str = "",
               now: Optional[datetime] = None) -> Evidence:
        """Add one answer. Refuses what cannot be acted on later."""
        import json

        if criterion_id not in CRITERIA_BY_ID:
            raise ValueError(
                f"unknown criterion {criterion_id!r}. Call design_npi_gate "
                f"for the ids of the phase you are working on.")
        if verdict not in VERDICTS:
            raise ValueError(
                f"verdict must be one of {', '.join(VERDICTS)}, not "
                f"{verdict!r}")
        # A waiver is a decision someone has to be able to defend. Without
        # a note it is indistinguishable from a skipped check, which is
        # exactly the failure a gate exists to prevent.
        if verdict == WAIVED and not note.strip():
            raise ValueError(
                "a waiver needs a note saying why the criterion does not "
                "apply, or who accepted the risk")

        item = Evidence(criterion_id=criterion_id, verdict=verdict,
                        at=now or datetime.now(), by=by, note=note,
                        revision=revision)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "criterion_id": item.criterion_id,
            "verdict": item.verdict,
            "at": item.at.isoformat(),
            "by": item.by,
            "note": item.note,
            "revision": item.revision,
        }, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return item

    def replay(self) -> list[Evidence]:
        """Every record, oldest first. A damaged line is skipped, not fatal.

        The log outlives the process that wrote it, and a single torn
        line from a crash must not cost the rest of the programme's
        history.
        """
        import json

        if not self.path.is_file():
            return []
        out: list[Evidence] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                out.append(Evidence(
                    criterion_id=raw["criterion_id"],
                    verdict=raw["verdict"],
                    at=datetime.fromisoformat(raw["at"]),
                    by=raw.get("by", ""),
                    note=raw.get("note", ""),
                    revision=raw.get("revision", ""),
                ))
            except (ValueError, KeyError, TypeError):
                continue
        return out
