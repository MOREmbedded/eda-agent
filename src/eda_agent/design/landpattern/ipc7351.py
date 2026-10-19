# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""IPC-7351B land pattern arithmetic: goals, tolerances, rounding, equations.

Everything here is a number with a source. The full IPC-7351B text is not
public, so each value is cited to the public document it was checked
against. Short names used in the comments:

* ``IPC-7351:2005``: IPC-7351, February 2005, the first revision. A public
  copy is on datasheetarchive.com (``fancort.com`` original). Its Tables
  3-2 to 3-14 are on pp. 12-14, the tolerance equations on pp. 9-11.
* ``IPC-7351B``: IPC-7351B, June 2010. The table titles and numbers come
  from the public table of contents (electronics.org/TOC/IPC-7351B.pdf).
* ``Hausherr CADlib``: T. Hausherr (chair of the IPC-7351 committee),
  "The CAD Library" column series, PDF reprint at
  frontdoor.biz/HowToPCB/HowToPCB-extra/CADlib.pdf. It reproduces the
  IPC-7351B gull-wing table (p. 52), the BGA tables (pp. 34-36) and the
  IPC-2221/2222 through-hole tables (pp. 55-56).
* ``Hausherr IPC paper``: T. Hausherr, "PCB Design Perfection Starts in
  the CAD Library, Part 1", IPC technical paper E6&S17_01 (ipc.org).
* ``PCBL calc``: PCB Libraries forum topic 785 (Feb 2021), which quotes
  IPC-7351B section 3.1.1 and shows the SMD Reference Calculator cells.
* ``KEMET C1002``: KEMET (YAGEO) C1002 X7R MLCC datasheet, 2026-09-01,
  Table 3 "Land Pattern Design Recommendations per IPC-7351" (p. 12) and
  the dimension table (p. 3). With P = 0.10 this module reproduces that
  table exactly for 0402, 0603, 0805, 1206 and 1210 at density levels A
  and B, which is the strongest public check of the chip goals, the RMS
  form of S and the rounding below.
* ``KEMET T491``: KEMET (YAGEO) T491 tantalum datasheet, 2026-07-08,
  Table 2 "Land Dimensions/Courtyard" (p. 15), an IPC-7351 table for
  inward L leads; its Z, X and the spacing of G across the three levels
  confirm the inward L goals and P = 0.05.

Where IPC-7351B could not be checked for a table, the IPC-7351:2005 value
is used and the comment says so. Disagreements are noted where they occur.

Density levels: ``L`` (least, level C), ``N`` (nominal, level B, the
default) and ``M`` (most, level A).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

__all__ = [
    "DENSITIES",
    "DENSITY_LEVEL",
    "FAB_TOL",
    "PLACE_TOL",
    "Goals",
    "GoalTable",
    "LandCalc",
    "GOAL_TABLES",
    "goal_table",
    "land_calc",
    "round_nearest",
    "round_up",
    "round_out",
    "bga_land",
    "pth_padstack",
]

DENSITIES = ("L", "N", "M")
#: IPC-7351:2005 p. 12 and Hausherr IPC paper: L = Least (level C),
#: N = Nominal (level B), M = Most (level A).
DENSITY_LEVEL = {"L": "C", "N": "B", "M": "A"}

# --- tolerances ---------------------------------------------------------

#: F, the land pattern fabrication tolerance, as the full MMC-to-LMC
#: range IPC-7351:2005 Table 3-1 (p. 11) defines it to be. The 2005 worked
#: example assumes 0.1 mm (p. 10); Hausherr CADlib p. 51 gives 0.05 mm,
#: which the PCBL calculator enters as +/-0.05 and doubles (its "Toe Tol"
#: cell, 0.36742, is sqrt(0.35^2 + 0.10^2 + 0.05^2)). All sources agree on
#: 0.10 in the equation.
FAB_TOL = 0.10

#: P, the placement tolerance (diameter of true position, IPC-7351:2005
#: Table 3-1). The sources disagree:
#:
#: * 0.05: IPC-7351:2005 p. 10 ("P is 0.05 mm (assumed assembly equipment
#:   placement tolerance)"); the PCBL calculator (+/-0.025, doubled), whose
#:   cells match the IPC-7351B section 3.1.1 example per that thread; and
#:   KEMET T491 Table 2 (2026-07-08, p. 15), whose pad widths and Z only
#:   fit 0.05.
#: * 0.10: KEMET C1002 Table 3, which only fits 0.10; and Hausherr CADlib
#:   p. 51 ("The IPC-7351 assembly tolerance is 0.05mm"), if read as +/-.
#:
#: The standard's own figure, 0.05, is the default; pass ``P=0.10`` to
#: reproduce tables built the other way.
PLACE_TOL = 0.05

# --- rounding -----------------------------------------------------------

#: Land size round-off, nearest 0.05 mm (Hausherr CADlib p. 52 and the
#: IPC-7351B gull-wing table row "Round off to the nearest 0.05").
LAND_SIZE_ROUND = 0.05
#: Land placement round-off: the centre-to-centre span goes to 0.1 mm so
#: each land centre sits on a 0.05 mm grid from the origin (Hausherr
#: CADlib p. 51). Nearest, as the PCBL calculator shows (C 4.96398 to
#: 4.96 on its 0.02 grid).
LAND_SPAN_ROUND = 0.10
#: Chips smaller than 1608: land size to 0.01 mm and land snap grid to
#: 0.02 mm (Hausherr IPC paper, rule changes 1 and 2; Hausherr CADlib
#: p. 9). Confirmed by the KEMET C1002 0402 row (0.62, 0.45).
SMALL_LAND_SIZE_ROUND = 0.01
SMALL_LAND_SPAN_ROUND = 0.02
#: Courtyard edges go outward to a 0.05 mm grid. The IPC-7351B table
#: round-off row is 0.05 (Hausherr CADlib pp. 36 and 52), and outward
#: rounding reproduces every KEMET C1002 V1/V2 courtyard value. IPC-7351:
#: 2005 p. 15 describes rounding the courtyard UP to a placement grid
#: (its example uses 0.5 mm) and Hausherr CADlib p. 18 calls 0.5 mm the
#: placement courtyard snap grid; a caller can pass 0.5 for that style.
COURTYARD_ROUND = 0.05

_EPS = 1e-9


def round_nearest(value: float, step: float) -> float:
    """``value`` to the nearest multiple of ``step``, halves away from zero.

    Python's ``round`` rounds halves to even, which would move 0.875 to
    0.88 on a 0.01 grid but 0.825 to 0.82; land pattern round-off must be
    the same rule for every value.
    """
    if step <= 0:
        return value
    q = abs(value) / step
    n = math.floor(q + 0.5 + _EPS)
    return round(math.copysign(n * step, value), 6)


def round_up(value: float, step: float) -> float:
    """``value`` up to the next multiple of ``step`` (towards +inf)."""
    if step <= 0:
        return value
    return round(math.ceil(value / step - _EPS) * step, 6)


def round_out(value: float, step: float) -> float:
    """``value`` away from zero to a multiple of ``step`` (courtyard edges)."""
    if step <= 0:
        return value
    if value >= 0:
        return round_up(value, step)
    return -round_up(-value, step)


# --- solder joint goals -------------------------------------------------


@dataclass(frozen=True)
class Goals:
    """Solder joint goals for one lead form at one density, mm.

    ``outer`` sets Z (the outer land edge), ``inner`` sets G (the inner
    edge). For gull-wing, chip and no-lead parts the outer goal is the toe
    and the inner goal is the heel. IPC swaps the names for J leads and
    inward L leads (IPC-7351:2005 Tables 3-4 and 3-11 label them "Heel (to
    find Z dim)" and "Toe (to find G dim)"); keeping the roles here, not
    the names, stops that swap reaching the equations.
    """

    outer: float
    inner: float
    side: float
    courtyard: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GoalTable:
    key: str
    title: str
    source: str
    least: Goals
    nominal: Goals
    most: Goals

    def at(self, density: str) -> Goals:
        d = normalise_density(density)
        return {"L": self.least, "N": self.nominal, "M": self.most}[d]


def normalise_density(density: Optional[str]) -> str:
    d = str(density or "N").strip().upper()
    aliases = {"LEAST": "L", "C": "L", "NOMINAL": "N", "B": "N",
               "MOST": "M", "A": "M"}
    d = aliases.get(d, d)
    if d not in DENSITIES:
        raise ValueError(
            f"density must be L, N or M (least, nominal, most); got {density!r}")
    return d


def _g(outer, inner, side, cy) -> Goals:
    return Goals(float(outer), float(inner), float(side), float(cy))


# Each table: (least, nominal, most). Values in mm.
GOAL_TABLES: dict[str, GoalTable] = {
    # IPC-7351B Table 3-2, flat ribbon L and gull-wing leads, pitch above
    # 0.625 mm. Reproduced in Hausherr CADlib p. 52 (Table 1): toe 0.15 /
    # 0.35 / 0.55, heel 0.25 / 0.35 / 0.45, side 0.01 / 0.03 / 0.05,
    # courtyard 0.1 / 0.25 / 0.5. Same as IPC-7351:2005 Table 3-2.
    "gullwing": GoalTable(
        "gullwing", "Flat ribbon L and gull-wing leads, pitch > 0.625 mm",
        "IPC-7351B Table 3-2 (Hausherr CADlib p. 52)",
        _g(0.15, 0.25, 0.01, 0.10), _g(0.35, 0.35, 0.03, 0.25),
        _g(0.55, 0.45, 0.05, 0.50)),
    # IPC-7351B Table 3-3, pitch 0.625 mm or less. Side goals -0.04 /
    # -0.02 / 0.01 from IPC-7351:2005 Table 3-3 (p. 12); the nominal side
    # goal -0.02 is confirmed for B by Hausherr CADlib p. 23. Toe, heel and
    # courtyard as Table 3-2.
    "gullwing_fine": GoalTable(
        "gullwing_fine", "Flat ribbon L and gull-wing leads, pitch <= 0.625 mm",
        "IPC-7351B Table 3-3 (IPC-7351:2005 p. 12; Hausherr CADlib p. 23)",
        _g(0.15, 0.25, -0.04, 0.10), _g(0.35, 0.35, -0.02, 0.25),
        _g(0.55, 0.45, 0.01, 0.50)),
    # Outward L-bend leads (SOT-23, SOD-123 style): the heel goal drops to
    # 0.05 / 0.15 / 0.25 when Smin <= Amax and Tmax - Tmin < 0.5 mm.
    # IPC-7351:2005 Table 3-2 and 3-3 note 1 (p. 12); the B nominal 0.15
    # and the rule are in Hausherr CADlib p. 23 (Figure 8).
    "outward_l": GoalTable(
        "outward_l", "Outward L-bend (flat ribbon) leads, pitch > 0.625 mm",
        "IPC-7351B Table 3-2 note (IPC-7351:2005 p. 12; Hausherr CADlib p. 23)",
        _g(0.15, 0.05, 0.01, 0.10), _g(0.35, 0.15, 0.03, 0.25),
        _g(0.55, 0.25, 0.05, 0.50)),
    "outward_l_fine": GoalTable(
        "outward_l_fine", "Outward L-bend leads, pitch <= 0.625 mm",
        "IPC-7351B Table 3-3 note (IPC-7351:2005 p. 12; Hausherr CADlib p. 23)",
        _g(0.15, 0.05, -0.04, 0.10), _g(0.35, 0.15, -0.02, 0.25),
        _g(0.55, 0.25, 0.01, 0.50)),
    # J leads. IPC-7351B Table 3-4 could not be checked publicly; these
    # are IPC-7351:2005 Table 3-4 (p. 13): "Heel (to find Z dim)" 0.15 /
    # 0.35 / 0.55, "Toe (to find G dim)" -0.30 / -0.20 / -0.10, side 0.01 /
    # 0.03 / 0.05, courtyard 0.1 / 0.25 / 0.5.
    "jlead": GoalTable(
        "jlead", "J leads",
        "IPC-7351:2005 Table 3-4 p. 13 (B Table 3-4 not publicly checkable)",
        _g(0.15, -0.30, 0.01, 0.10), _g(0.35, -0.20, 0.03, 0.25),
        _g(0.55, -0.10, 0.05, 0.50)),
    # Rectangular or square-end chips, 1608 (0603) and larger. IPC-7351B
    # Table 3-5. Toe 0.15 / 0.35 / 0.55, side -0.05 / 0.00 / 0.05 and
    # courtyard 0.1 / 0.25 / 0.5 as IPC-7351:2005 Table 3-5 (p. 13). The
    # heel is 0.00, where the 2005 table had -0.05: KEMET C1002 Table 3
    # only fits heel 0.00 (0603 B: Y = 0.95 with 0.00, 0.85 with -0.05).
    "chip": GoalTable(
        "chip", "Rectangular or square-end chips, 1608 and larger",
        "IPC-7351B Table 3-5 (IPC-7351:2005 p. 13; heel per KEMET C1002 Table 3)",
        _g(0.15, 0.00, -0.05, 0.10), _g(0.35, 0.00, 0.00, 0.25),
        _g(0.55, 0.00, 0.05, 0.50)),
    # Chips smaller than 1608. IPC-7351B Table 3-6. Nominal toe 0.20 and
    # nominal courtyard 0.15 per Hausherr IPC paper (rule changes 3 and 5).
    # Least/most toe 0.10 / 0.30, side -0.05 / 0.00 / 0.05 and heel 0.00
    # are the values the KEMET C1002 0402 row reproduces exactly. They
    # differ from IPC-7351:2005 Table 3-6 (toe 0.00 / 0.10 / 0.20, heel
    # -0.05, side 0.00 / 0.00 / 0.05), which is superseded.
    "chip_small": GoalTable(
        "chip_small", "Rectangular or square-end chips smaller than 1608",
        "IPC-7351B Table 3-6 (Hausherr IPC paper; KEMET C1002 Table 3, 0402)",
        _g(0.10, 0.00, -0.05, 0.10), _g(0.20, 0.00, 0.00, 0.15),
        _g(0.30, 0.00, 0.05, 0.20)),
    # Cylindrical end cap (MELF). IPC-7351:2005 Table 3-7 (p. 13); the B
    # table (also 3-7) could not be checked publicly.
    "melf": GoalTable(
        "melf", "Cylindrical end cap terminations (MELF)",
        "IPC-7351:2005 Table 3-7 p. 13 (B Table 3-7 not publicly checkable)",
        _g(0.20, 0.02, 0.01, 0.10), _g(0.40, 0.10, 0.05, 0.25),
        _g(0.60, 0.20, 0.10, 0.50)),
    # Inward flat ribbon L leads (molded diodes, tantalum, molded
    # inductors). IPC-7351B Table 3-13 per the B table of contents; values
    # from IPC-7351:2005 Table 3-11 (p. 14): "Heel (to find Z)" 0.2 / 0.5 /
    # 0.8, "Toe (to find G)" 0.07 / 0.15 / 0.25, side -0.10 / -0.05 / 0.01.
    # KEMET T491 Table 2 (a 2026 IPC-7351 table) agrees: its case A Z
    # (3.82 / 4.42 / 5.02), widths (1.13 / 1.23 / 1.35) and the steps in
    # its gap (0.98 / 0.82 / 0.62) follow exactly from these goals.
    "inward_l": GoalTable(
        "inward_l", "Inward flat ribbon L leads (molded body)",
        "IPC-7351:2005 Table 3-11 p. 14, matching KEMET T491 Table 2",
        _g(0.20, 0.07, -0.10, 0.10), _g(0.50, 0.15, -0.05, 0.25),
        _g(0.80, 0.25, 0.01, 0.50)),
    # Flat lug leads (DPAK tab, SOT-89). IPC-7351B Table 3-14; values from
    # IPC-7351:2005 Table 3-12 (p. 14). The 2005 table marks the courtyard
    # "depends on thermal requirements".
    "flat_lug": GoalTable(
        "flat_lug", "Flat lug leads",
        "IPC-7351:2005 Table 3-12 p. 14 (B Table 3-14 not publicly checkable)",
        _g(0.15, 0.25, -0.04, 0.10), _g(0.35, 0.35, 0.01, 0.25),
        _g(0.55, 0.45, 0.05, 0.50)),
    # Quad flat no-lead. IPC-7351B Table 3-15; values from IPC-7351:2005
    # Table 3-13 (p. 14): toe 0.20 / 0.30 / 0.40, heel 0.00, side -0.04.
    "nolead": GoalTable(
        "nolead", "Quad flat no-lead (QFN)",
        "IPC-7351:2005 Table 3-13 p. 14 (B Table 3-15 not publicly checkable)",
        _g(0.20, 0.00, -0.04, 0.10), _g(0.30, 0.00, -0.04, 0.25),
        _g(0.40, 0.00, -0.04, 0.50)),
    # Small outline no-lead (SON/DFN). IPC-7351B Table 3-16; values from
    # IPC-7351:2005 Table 3-14 (p. 14), the same numbers as QFN.
    "son": GoalTable(
        "son", "Small outline no-lead (SON, DFN)",
        "IPC-7351:2005 Table 3-14 p. 14 (B Table 3-16 not publicly checkable)",
        _g(0.20, 0.00, -0.04, 0.10), _g(0.30, 0.00, -0.04, 0.25),
        _g(0.40, 0.00, -0.04, 0.50)),
}


def goal_table(key: str) -> GoalTable:
    try:
        return GOAL_TABLES[key]
    except KeyError:
        raise ValueError(
            f"unknown lead form {key!r}; known: {', '.join(sorted(GOAL_TABLES))}"
        ) from None


#: Pitch at or below which the fine-pitch gull-wing table applies
#: (IPC-7351:2005 Table 3-3 title, "less than or equal to 0.625 mm").
FINE_PITCH = 0.625

#: Outward L-bend test (Hausherr CADlib p. 23, Figure 8): a gull wing is
#: an outward L-bend when Smin <= Amax AND Tmax - Tmin < 0.5 mm.
OUTWARD_L_TTOL = 0.5


def gullwing_form(*, pitch: Optional[float], s_min: float,
                  body_across_max: Optional[float], t_tol: float) -> str:
    """The gull-wing goal table key for one row of leads."""
    fine = pitch is not None and pitch <= FINE_PITCH + _EPS
    outward = (body_across_max is not None and s_min <= body_across_max + _EPS
               and t_tol < OUTWARD_L_TTOL - _EPS)
    if outward:
        return "outward_l_fine" if fine else "outward_l"
    return "gullwing_fine" if fine else "gullwing"


# --- the equations ------------------------------------------------------


@dataclass(frozen=True)
class LandCalc:
    """One IPC-7351 land calculation across a pair of opposed terminals.

    ``L``, ``T``, ``W`` are (min, max) pairs. All lengths in mm. The raw
    values are unrounded; ``pad_length``, ``span`` and ``pad_width`` are
    the rounded results that make the pads. ``span`` is centre-to-centre.
    """

    L: tuple[float, float]
    T: tuple[float, float]
    W: tuple[float, float]
    goals: Goals
    F: float
    P: float
    s_min: float
    s_max: float
    s_tol: float
    s_tol_rms: float
    s_max_rms: float
    Z: float
    G: float
    X: float
    pad_length_raw: float
    span_raw: float
    pad_width_raw: float
    size_round: float
    span_round: float
    pad_length: float
    span: float
    pad_width: float
    nominal_terminal: bool = False
    lead_form: str = ""
    density: str = "N"
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def z_rounded(self) -> float:
        return round(self.span + self.pad_length, 6)

    @property
    def g_rounded(self) -> float:
        return round(self.span - self.pad_length, 6)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["goals"] = self.goals.to_dict()
        d["z_rounded"] = self.z_rounded
        d["g_rounded"] = self.g_rounded
        d["notes"] = list(self.notes)
        return d


def land_calc(L: tuple[float, float], T: tuple[float, float],
              W: tuple[float, float], goals: Goals, *,
              F: float = FAB_TOL, P: float = PLACE_TOL,
              size_round: float = LAND_SIZE_ROUND,
              span_round: float = LAND_SPAN_ROUND,
              nominal_terminal: bool = False, t_nom: Optional[float] = None,
              lead_form: str = "", density: str = "N") -> LandCalc:
    """Z, G and X for two opposed terminals, then the rounded lands.

    The equations (IPC-7351:2005 section 3.1.5, p. 10, kept in IPC-7351B):

        Zmax = Lmin + 2 Jt + sqrt(CL^2 + F^2 + P^2)
        Gmin = Smax - 2 Jh - sqrt(CS^2 + F^2 + P^2)
        Xmax = Wmin + 2 Js + sqrt(CW^2 + F^2 + P^2)

    with S taken from L and T by the RMS method. IPC-7351:2005 p. 9 gave
    Smax(RMS) = Smin + Stol(RMS); IPC-7351B moved to the symmetric form
    the PCBL calculator uses (its "New Smax" cell is Smax - Sdiff / 2):

        Smin = Lmin - 2 Tmax          Smax = Lmax - 2 Tmin
        Stol(RMS) = sqrt(Ltol^2 + 2 Ttol^2)
        Smax(RMS) = Smax - (Smax - Smin - Stol(RMS)) / 2

    and CS = Stol(RMS). Only the B form reproduces KEMET C1002 Table 3.

    ``nominal_terminal`` uses the nominal T for both limits, the rule for
    chips smaller than 1608 (Hausherr IPC paper, rule 6: "the 'Nominal'
    Terminal dimensions are used for both the min & max fields").
    """
    l_min, l_max = float(L[0]), float(L[1])
    t_min, t_max = float(T[0]), float(T[1])
    w_min, w_max = float(W[0]), float(W[1])
    notes: list[str] = []
    if nominal_terminal:
        tn = (t_min + t_max) / 2.0 if t_nom is None else float(t_nom)
        t_min = t_max = tn
        notes.append("nominal terminal length used for both limits (chip < 1608)")
    l_tol = l_max - l_min
    t_tol = t_max - t_min
    w_tol = w_max - w_min

    s_min = l_min - 2.0 * t_max
    s_max = l_max - 2.0 * t_min
    s_tol = s_max - s_min
    s_tol_rms = math.sqrt(l_tol ** 2 + 2.0 * t_tol ** 2)
    s_max_rms = s_max - (s_tol - s_tol_rms) / 2.0

    fp2 = F ** 2 + P ** 2
    z = l_min + 2.0 * goals.outer + math.sqrt(l_tol ** 2 + fp2)
    g = s_max_rms - 2.0 * goals.inner - math.sqrt(s_tol_rms ** 2 + fp2)
    x = w_min + 2.0 * goals.side + math.sqrt(w_tol ** 2 + fp2)

    pad_length_raw = (z - g) / 2.0
    span_raw = (z + g) / 2.0
    pad_length = round_nearest(pad_length_raw, size_round)
    span = round_nearest(span_raw, span_round)
    pad_width = round_nearest(x, size_round)
    if span - pad_length <= 0:
        notes.append(
            f"lands meet or overlap across the gap (G = {g:.3f} mm): the "
            "terminal tolerances leave no room between them")
    return LandCalc(
        L=(l_min, l_max), T=(t_min, t_max), W=(w_min, w_max), goals=goals,
        F=F, P=P, s_min=s_min, s_max=s_max, s_tol=s_tol,
        s_tol_rms=s_tol_rms, s_max_rms=s_max_rms, Z=z, G=g, X=x,
        pad_length_raw=pad_length_raw, span_raw=span_raw,
        pad_width_raw=x, size_round=size_round, span_round=span_round,
        pad_length=pad_length, span=span, pad_width=pad_width,
        nominal_terminal=nominal_terminal, lead_form=lead_form,
        density=density, notes=tuple(notes))


@dataclass(frozen=True)
class AsymLandCalc:
    """Two opposed terminals of different lengths (DPAK lead and tab,
    SOT-89 lead and tab). Side 1 is at -X, side 2 at +X, both measured
    from the centre of the span L.

    The same equations as :func:`land_calc` with each side's own goal and
    its own T in the S stack-up:

        Z = Lmin + Jo1 + Jo2 + sqrt(CL^2 + F^2 + P^2)
        S = L - T1 - T2,  Stol(RMS) = sqrt(Ltol^2 + T1tol^2 + T2tol^2)
        G = Smax(RMS) - Ji1 - Ji2 - sqrt(Stol(RMS)^2 + F^2 + P^2)

    which reduce to the symmetric ones when the sides match. The outer
    edges sit at -Z/2 and +Z/2; the gap G is centred where the nominal
    gap between the terminals is, (T1nom - T2nom) / 2 from the centre.
    A negative G means the lands overlap; that is only acceptable when
    both sides are the same terminal (SOT-89 pin 2 and its tab).
    """

    L: tuple[float, float]
    T1: tuple[float, float]
    T2: tuple[float, float]
    goals1: Goals
    goals2: Goals
    F: float
    P: float
    s_min: float
    s_max: float
    s_tol: float
    s_tol_rms: float
    s_max_rms: float
    Z: float
    G: float
    gap_centre: float
    length1_raw: float
    centre1_raw: float
    length2_raw: float
    centre2_raw: float
    size_round: float
    centre_round: float
    length1: float
    centre1: float
    length2: float
    centre2: float

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["goals1"] = self.goals1.to_dict()
        d["goals2"] = self.goals2.to_dict()
        return d


def land_calc_asym(L: tuple[float, float], T1: tuple[float, float],
                   T2: tuple[float, float], goals1: Goals, goals2: Goals, *,
                   F: float = FAB_TOL, P: float = PLACE_TOL,
                   size_round: float = LAND_SIZE_ROUND,
                   centre_round: float = LAND_SPAN_ROUND / 2.0,
                   t1_nom: Optional[float] = None,
                   t2_nom: Optional[float] = None) -> AsymLandCalc:
    l_min, l_max = float(L[0]), float(L[1])
    a_min, a_max = float(T1[0]), float(T1[1])
    b_min, b_max = float(T2[0]), float(T2[1])
    l_tol = l_max - l_min
    fp2 = F ** 2 + P ** 2
    z = l_min + goals1.outer + goals2.outer + math.sqrt(l_tol ** 2 + fp2)
    s_min = l_min - a_max - b_max
    s_max = l_max - a_min - b_min
    s_tol = s_max - s_min
    s_tol_rms = math.sqrt(l_tol ** 2 + (a_max - a_min) ** 2 + (b_max - b_min) ** 2)
    s_max_rms = s_max - (s_tol - s_tol_rms) / 2.0
    g = s_max_rms - goals1.inner - goals2.inner - math.sqrt(s_tol_rms ** 2 + fp2)
    t1n = (a_min + a_max) / 2.0 if t1_nom is None else float(t1_nom)
    t2n = (b_min + b_max) / 2.0 if t2_nom is None else float(t2_nom)
    c = (t1n - t2n) / 2.0
    x1a, x1b = -z / 2.0, c - g / 2.0
    x2a, x2b = c + g / 2.0, z / 2.0
    len1, cen1 = x1b - x1a, (x1a + x1b) / 2.0
    len2, cen2 = x2b - x2a, (x2a + x2b) / 2.0
    return AsymLandCalc(
        L=(l_min, l_max), T1=(a_min, a_max), T2=(b_min, b_max),
        goals1=goals1, goals2=goals2, F=F, P=P, s_min=s_min, s_max=s_max,
        s_tol=s_tol, s_tol_rms=s_tol_rms, s_max_rms=s_max_rms, Z=z, G=g,
        gap_centre=c, length1_raw=len1, centre1_raw=cen1, length2_raw=len2,
        centre2_raw=cen2, size_round=size_round, centre_round=centre_round,
        length1=round_nearest(len1, size_round),
        centre1=round_nearest(cen1, centre_round),
        length2=round_nearest(len2, size_round),
        centre2=round_nearest(cen2, centre_round))


def width_calc(W: tuple[float, float], side: float, *, F: float = FAB_TOL,
               P: float = PLACE_TOL, size_round: float = LAND_SIZE_ROUND
               ) -> tuple[float, float]:
    """Xmax for a terminal width alone: (raw, rounded)."""
    w_min, w_max = float(W[0]), float(W[1])
    x = w_min + 2.0 * side + math.sqrt((w_max - w_min) ** 2 + F ** 2 + P ** 2)
    return x, round_nearest(x, size_round)


# --- BGA ----------------------------------------------------------------

#: IPC-7351B "Land Approximation (mm) for Collapsible Solder Balls", as
#: reproduced in Hausherr CADlib p. 34 (Table 1): nominal ball, reduction,
#: nominal land, land variation (max, min). "The IPC-7351B LP Calculator
#: uses this chart" and "IPC prefers the Maximum Material Condition for
#: all BGA Land sizes ... uses the Maximum Land Variation Diameter".
COLLAPSING_BALL_LANDS: tuple[tuple[float, float, float, float, float], ...] = (
    (0.75, 0.25, 0.55, 0.60, 0.50),
    (0.65, 0.25, 0.50, 0.55, 0.45),
    (0.60, 0.25, 0.45, 0.50, 0.40),
    (0.55, 0.25, 0.40, 0.45, 0.35),
    (0.50, 0.20, 0.40, 0.45, 0.35),
    (0.45, 0.20, 0.35, 0.40, 0.30),
    (0.40, 0.20, 0.30, 0.35, 0.25),
    (0.35, 0.20, 0.30, 0.35, 0.25),
    (0.30, 0.20, 0.25, 0.25, 0.20),
    (0.25, 0.20, 0.20, 0.20, 0.17),
    (0.20, 0.15, 0.17, 0.20, 0.14),
    (0.17, 0.15, 0.15, 0.18, 0.12),
    (0.15, 0.15, 0.13, 0.15, 0.10),
)

#: Non-collapsing balls: the land grows instead (Hausherr CADlib p. 35,
#: Table 2): nominal ball, increase, nominal land, land variation (max, min).
NON_COLLAPSING_BALL_LANDS: tuple[tuple[float, float, float, float, float], ...] = (
    (0.75, 0.15, 0.85, 0.90, 0.80),
    (0.65, 0.15, 0.75, 0.80, 0.70),
    (0.60, 0.15, 0.70, 0.75, 0.65),
    (0.55, 0.15, 0.65, 0.70, 0.60),
    (0.50, 0.10, 0.55, 0.60, 0.50),
    (0.45, 0.10, 0.50, 0.55, 0.45),
    (0.40, 0.10, 0.45, 0.50, 0.40),
    (0.35, 0.10, 0.40, 0.45, 0.35),
    (0.30, 0.10, 0.35, 0.40, 0.30),
    (0.25, 0.10, 0.30, 0.35, 0.25),
    (0.20, 0.05, 0.21, 0.26, 0.16),
    (0.17, 0.05, 0.18, 0.22, 0.13),
    (0.15, 0.05, 0.16, 0.21, 0.11),
)

#: BGA courtyard excess by density: 0.50 / 1.00 / 2.00 mm (IPC-7351B
#: Table 3-17 as reproduced in Hausherr CADlib p. 36, Table 3). The same
#: column (p. 18) also states the three tiers by ball size instead (above
#: 0.50 mm ball: 2 mm, 0.25-0.50: 1 mm, below 0.25: 0.5 mm); this module
#: keys them by density, as the table does.
BGA_COURTYARD = {"L": 0.50, "N": 1.00, "M": 2.00}


def bga_land(ball_nom: float, *, collapsing: bool = True,
             use_mmc: bool = True) -> dict[str, Any]:
    """Land diameter for a BGA ball (nominal diameter ``ball_nom``, mm).

    A ball in the IPC table gets the table's maximum land variation (MMC),
    or its nominal land with ``use_mmc=False``. A ball outside the table
    gets the table's reduction (or increase) for its size band applied to
    the ball, rounded to 0.05 mm (0.01 mm below a 0.25 mm ball, as the
    table's small rows are), and a note saying the table had no row.
    """
    rows = COLLAPSING_BALL_LANDS if collapsing else NON_COLLAPSING_BALL_LANDS
    for ball, pct, nom, mx, mn in rows:
        if abs(ball - ball_nom) < 0.005:
            return {"land": mx if use_mmc else nom, "nominal": nom,
                    "max": mx, "min": mn, "percent": pct, "ball": ball,
                    "collapsing": collapsing, "in_table": True}
    if ball_nom >= 0.55:
        pct = 0.25 if collapsing else 0.15
    elif ball_nom >= 0.25:
        pct = 0.20 if collapsing else 0.10
    else:
        pct = 0.15 if collapsing else 0.05
    raw = ball_nom * (1.0 - pct) if collapsing else ball_nom * (1.0 + pct)
    step = 0.05 if ball_nom >= 0.25 else 0.01
    nom = round_nearest(raw, step)
    return {"land": nom, "nominal": nom, "max": None, "min": None,
            "percent": pct, "ball": ball_nom, "collapsing": collapsing,
            "in_table": False,
            "note": "ball not in the IPC-7351B land table; band percentage "
                    "applied to the nominal ball, no MMC step"}


# --- through-hole -------------------------------------------------------

#: Minimum hole = maximum lead + 0.25 / 0.20 / 0.15 mm for levels A / B / C
#: (IPC-2222 Table 9-3, reproduced in Hausherr CADlib p. 55).
PTH_HOLE_OVER_LEAD = {"M": 0.25, "N": 0.20, "L": 0.15}
#: Minimum external annular ring, supported holes, 0.050 mm (IPC-2221
#: Table 9-2, reproduced in Hausherr CADlib p. 56).
PTH_ANNULAR_RING = 0.05
#: Minimum fabrication allowance 0.6 / 0.5 / 0.4 mm for levels A / B / C
#: (IPC-2221 Table 9-1, reproduced in Hausherr CADlib p. 56).
PTH_FAB_ALLOWANCE = {"M": 0.60, "N": 0.50, "L": 0.40}
#: Hole and pad sizes go up to 0.05 mm steps (Hausherr CADlib p. 34: land
#: and hole sizes "in 0.05 mm increments"; p. 52: "all numbers ... should
#: be in 0.05 mm increments"). Up, so the IPC minimum is kept.
PTH_ROUND = 0.05


def pth_padstack(lead_max: float, density: str = "N") -> dict[str, float]:
    """Hole and round pad for a lead of maximum size ``lead_max`` (mm).

    pad = hole + 2 x annular ring + fabrication allowance (Hausherr CADlib
    p. 56). For a rectangular lead pass its diagonal (Hausherr CADlib
    p. 55, Figure 2).
    """
    d = normalise_density(density)
    hole_raw = lead_max + PTH_HOLE_OVER_LEAD[d]
    hole = round_up(hole_raw, PTH_ROUND)
    pad_raw = hole + 2.0 * PTH_ANNULAR_RING + PTH_FAB_ALLOWANCE[d]
    pad = round_up(pad_raw, PTH_ROUND)
    return {"hole": hole, "pad": pad, "hole_raw": hole_raw, "pad_raw": pad_raw,
            "lead_max": lead_max}


# --- exposed pads and drafting constants ---------------------------------

#: Thermal pad paste coverage. TB389 (Intersil/Renesas, rev. 7, May 2012,
#: p. 3): "an array of smaller apertures ... that results in 50 to 80%
#: solder paste coverage". 65 % is the middle of that range.
EP_PASTE_COVERAGE = 0.65
