# SPDX-License-Identifier: Apache-2.0
"""PCB engineering calculators that eda-agent did not have.

These fill the gap between what this project already computes (trace
width from current, single-ended impedance, thermal vias, termination,
length match) and what a designer reaches a calculator for: conductor
spacing from voltage, fusing current, via parasitics, planar
inductance, reactance and resonance, PDN target impedance, padstack
geometry and wire gauge.

Pure Python, no Altium, no network, no external binary. That is
deliberate: the obvious way to get these was to shell out to
`pcb-toolkit <https://github.com/akiselev/pcb-toolkit>`_ (Rust, MIT OR
Apache-2.0), whose coverage is wider than this and whose figures are
upstream-validated. But its CLI is not published as a binary -- the
crate on crates.io is library-only -- so using it means every user
installs a Rust toolchain and builds from source. Calculators that
only work for people who did that are not calculators this server can
rely on. ``pcb_calc_cross_check`` uses the binary when it IS present,
as a second opinion rather than as the engine.

EVERY FUNCTION STATES ITS CONFIDENCE, in a ``status`` field, because
these are not equally trustworthy and presenting them as if they were
is how a heuristic gets used as a specification:

``exact``
    A definition or an algebraic identity. Reactance, resonance,
    wavelength, AWG, padstack geometry. Wrong only if the inputs are.
``published``
    A closed form from a named standard or textbook, implemented as
    published and accurate within the range that source states.
    Onderdonk fusing, IPC-2221 spacing, modified-Wheeler inductance.
``estimate``
    A rule of thumb that the industry uses and that no one should
    design a margin against. Via parasitics, PDN target impedance.

The convention is borrowed from pcb-toolkit's own
validated/compatibility/experimental labelling, which is the right
instinct and the same one this project applies to tool maturity.

Lengths are MILS unless a name says otherwise, matching the rest of
this codebase. Every function returns a plain dict; nothing raises for
a physically impossible input, it refuses with a reason, because these
are reached from a tool surface where a traceback helps nobody.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from ..units import MM_PER_MIL, OZ_TO_MILS

__all__ = [
    "awg_properties",
    "fusing_current",
    "ipc2221_spacing",
    "padstack_geometry",
    "pdn_target_impedance",
    "reactance",
    "resonant_frequency",
    "spiral_inductor",
    "via_parasitics",
    "wavelength_in_dielectric",
]

EXACT = "exact"
PUBLISHED = "published"
ESTIMATE = "estimate"

#: Speed of light in vacuum, m/s, exact by definition of the metre.
_C = 299_792_458.0

#: Copper resistivity at 20 degC, ohm-metre. The IACS reference value.
_RHO_CU = 1.724e-8

#: Temperature coefficient of resistance for copper, per degC.
_ALPHA_CU = 0.00393

#: Melting point of copper, degC.
_CU_MELT_C = 1083.0


def _fail(reason: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason}


def _positive(**values: Any) -> Optional[dict[str, Any]]:
    """Refuse non-positive or non-numeric inputs, naming the offender."""
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _fail(f"{name} must be a number, got "
                         f"{type(value).__name__}")
        if not math.isfinite(value):
            return _fail(f"{name} must be finite")
        if value <= 0:
            return _fail(f"{name} must be greater than zero, got {value}")
    return None


# =====================================================================
# Conductor spacing from voltage -- IPC-2221
# =====================================================================
#
# PROVENANCE, stated at length because this is a safety table and the
# number it returns can be the difference between a board that passes
# hipot and one that arcs.
#
# These values are TRANSCRIBED FROM SECONDARY SOURCES, not from the IPC
# document, which is not freely available. Two independent
# reproductions were compared and they DISAGREE on two cells:
#
#   B2 at 251-300 V : one says 1.25 mm, the other 2.5 mm
#   A6 at 31-50 V   : one says 0.4 mm,  the other 0.6 mm
#
# Where they disagree this table takes the LARGER value. A wider
# spacing is never unsafe; a narrower one can be, and the asymmetry of
# that mistake is the whole argument. Both figures are reported in
# ``disputed`` so the caller sees the disagreement rather than a
# confident number that was a coin toss.
#
# WHICH REVISION. These are the IPC-2221 **A/B** classes and values:
# B1-B4 for bare board and A5-A7 for assembly, with the above-500 V
# step of 0.0025 mm/V on B1 and so on.
#
# IPC-2221**C** is NOT a renumbering of these. It REDEFINES the
# columns: B3 is extended to vacuum, B4 becomes conductors covered
# with solder mask, a new B5 covers coated external conductors, the
# A-series shifts with it, and the above-500 V step is different. So a
# class letter does not carry across revisions, and reading "B4" here
# against a Rev C document silently selects a different environment.
#
# Match the class by its DESCRIPTION, which is why every reply carries
# `environment_meaning`, and use the IPC publication itself for
# compliance work. This is a starting point, not a citation.

#: Minimum spacing in MILLIMETRES by voltage band and environment.
#: Bands are upper-inclusive bounds on the DC voltage or AC peak
#: BETWEEN CONDUCTORS -- not the supply rail, which is the commonest
#: way this table is read wrong.
_IPC2221_MM: tuple[tuple[float, dict[str, float]], ...] = (
    (15.0, {"B1": 0.05, "B2": 0.10, "B3": 0.10, "B4": 0.05,
            "A5": 0.13, "A6": 0.13, "A7": 0.13}),
    (30.0, {"B1": 0.05, "B2": 0.10, "B3": 0.10, "B4": 0.05,
            "A5": 0.13, "A6": 0.25, "A7": 0.13}),
    (50.0, {"B1": 0.10, "B2": 0.60, "B3": 0.60, "B4": 0.13,
            "A5": 0.13, "A6": 0.60, "A7": 0.13}),
    (100.0, {"B1": 0.10, "B2": 0.60, "B3": 1.50, "B4": 0.13,
             "A5": 0.13, "A6": 0.50, "A7": 0.13}),
    (150.0, {"B1": 0.20, "B2": 0.60, "B3": 3.20, "B4": 0.40,
             "A5": 0.40, "A6": 0.80, "A7": 0.40}),
    (170.0, {"B1": 0.20, "B2": 1.25, "B3": 3.20, "B4": 0.40,
             "A5": 0.40, "A6": 0.80, "A7": 0.40}),
    (250.0, {"B1": 0.20, "B2": 1.25, "B3": 6.40, "B4": 0.40,
             "A5": 0.40, "A6": 0.80, "A7": 0.40}),
    (300.0, {"B1": 0.20, "B2": 2.50, "B3": 12.50, "B4": 0.40,
             "A5": 0.40, "A6": 0.80, "A7": 0.80}),
    (500.0, {"B1": 0.25, "B2": 2.50, "B3": 12.50, "B4": 0.80,
             "A5": 0.80, "A6": 1.50, "A7": 0.80}),
)

#: Above 500 V the table gives a per-volt slope added to the 500 V row.
_IPC2221_PER_VOLT_MM = {
    "B1": 0.0025, "B2": 0.005, "B3": 0.025, "B4": 0.00305,
    "A5": 0.00305, "A6": 0.00305, "A7": 0.00305,
}

#: Cells where the two reproductions disagreed; the larger was taken.
_IPC2221_DISPUTED = {
    ("B2", 300.0): {"taken": 2.50, "other_source_says": 1.25},
    ("A6", 50.0): {"taken": 0.60, "other_source_says": 0.40},
}

IPC2221_ENVIRONMENTS = {
    "B1": "internal conductors",
    "B2": "uncoated external conductors, up to 3050 m",
    "B3": "uncoated external conductors, above 3050 m",
    "B4": "polymer-coated external conductors, any elevation",
    "A5": "conformal-coated external conductors, any elevation",
    "A6": "uncoated component leads and terminations",
    "A7": "conformal-coated component leads and terminations",
}


def ipc2221_spacing(voltage: float, environment: str = "B2") -> dict[str, Any]:
    """Minimum conductor spacing for a voltage, per IPC-2221 Table 6-1.

    ``voltage`` is DC or AC PEAK **between the two conductors**, which
    is not the same as the supply rail and is the commonest way this
    table is misread: two 400 V rails of opposite polarity are an
    800 V problem.

    Returns the spacing in mm and mils, the band it came from, and -- if
    the cell is one the sources disagreed on -- a ``disputed`` entry
    giving both figures. See this module's provenance note: the values
    are from secondary reproductions, the larger was taken wherever
    they conflicted, and none of it substitutes for the IPC document.

    **IPC-2221 A/B classes.** Revision C redefines these columns rather
    than renumbering them -- B4 becomes solder-mask covered, a new B5
    appears, the A-series shifts, and the above-500 V step changes --
    so a letter does not carry across revisions. Match on
    ``environment_meaning``, not on the letter, and use the IPC
    document for compliance work.

    status: published
    """
    bad = _positive(voltage=voltage)
    if bad:
        return bad
    env = str(environment or "").strip().upper()
    if env not in IPC2221_ENVIRONMENTS:
        return _fail(
            f"unknown environment {environment!r}; expected one of "
            f"{sorted(IPC2221_ENVIRONMENTS)} -- "
            + "; ".join(f"{k} = {v}" for k, v in
                        sorted(IPC2221_ENVIRONMENTS.items())))

    for upper, row in _IPC2221_MM:
        if voltage <= upper:
            mm = row[env]
            band = f"up to {upper:g} V"
            break
    else:
        top = _IPC2221_MM[-1][1][env]
        mm = top + (voltage - 500.0) * _IPC2221_PER_VOLT_MM[env]
        band = (f"above 500 V: {top:g} mm plus "
                f"{_IPC2221_PER_VOLT_MM[env]:g} mm per volt over 500")
        upper = None

    result: dict[str, Any] = {
        "ok": True,
        "status": PUBLISHED,
        "source": ("IPC-2221 A/B Table 6-1, transcribed from secondary "
                   "reproductions -- verify against the IPC document"),
        "revision": ("A/B. Revision C REDEFINES these columns rather "
                     "than renumbering them (B4 becomes solder-mask "
                     "covered, a new B5 appears, the A-series shifts, "
                     "and the above-500 V step differs), so match the "
                     "class on environment_meaning rather than on its "
                     "letter."),
        "voltage_v": float(voltage),
        "environment": env,
        "environment_meaning": IPC2221_ENVIRONMENTS[env],
        "spacing_mm": round(mm, 4),
        "spacing_mils": round(mm / MM_PER_MIL, 3),
        "band": band,
        "note": ("voltage is between CONDUCTORS, not the supply rail; "
                 "two opposite-polarity rails add"),
    }
    disputed = _IPC2221_DISPUTED.get((env, upper))
    if disputed:
        result["disputed"] = {
            **disputed,
            "why": ("two reproductions of the table disagree on this "
                    "cell; the larger value was taken because a wider "
                    "spacing is never the unsafe mistake"),
        }
    return result


# =====================================================================
# Fusing current -- Onderdonk
# =====================================================================

def fusing_current(width_mils: float, copper_oz: float = 1.0,
                   time_s: float = 1.0,
                   ambient_c: float = 25.0) -> dict[str, Any]:
    """The current that melts a copper track in ``time_s``, per Onderdonk.

    This is the DESTRUCTION limit, not a rating. A track carrying its
    fusing current for the stated time is being destroyed on purpose;
    continuous current capacity is `pcb_calc_track_current_capacity`
    (IPC-2221), and the two differ by a large factor. Quoting this as
    an ampacity is the mistake the distinction exists to prevent.

    Onderdonk, as universally published for copper::

        I = A * sqrt( log10((Tm - Ta)/(234 + Ta) + 1) / (33 * t) )

    with ``A`` in circular mils, ``t`` in seconds, ``Tm`` the melting
    point of copper (1083 degC) and ``Ta`` ambient.

    ADIABATIC, and this matters more for a PCB track than for the
    grounding conductors the equation was written for. It assumes ALL
    the heat stays in the copper. A track is bonded to a laminate that
    conducts heat away, so past a second or so the assumption fails and
    the answer comes back far too low -- low enough to cross under the
    track's own continuous rating, which is the visible symptom. A
    100 mil 1 oz track fuses at about 26 A in one second by this
    equation and reads as 8 A at ten seconds, while IPC-2221 rates it
    for 5 A indefinitely; the second figure is the formula leaving its
    range, not a real number. Results above one second say so.

    status: published
    """
    bad = _positive(width_mils=width_mils, copper_oz=copper_oz,
                    time_s=time_s)
    if bad:
        return bad
    if not isinstance(ambient_c, (int, float)) or isinstance(ambient_c, bool):
        return _fail("ambient_c must be a number")
    if ambient_c >= _CU_MELT_C:
        return _fail(f"ambient_c must be below copper's melting point "
                     f"({_CU_MELT_C:g} degC)")
    if ambient_c <= -234.0:
        return _fail("ambient_c must be above -234 degC, where the "
                     "Onderdonk denominator vanishes")

    thickness_mils = copper_oz * OZ_TO_MILS
    area_sq_mils = width_mils * thickness_mils
    # Onderdonk is stated in circular mils: the area of a circle one mil
    # across, which is pi/4 square mils. Square mils divided by that is
    # the conversion, and getting it backwards scales the answer by 1.27.
    area_circular_mils = area_sq_mils * 4.0 / math.pi

    ratio = (_CU_MELT_C - ambient_c) / (234.0 + ambient_c)
    current = area_circular_mils * math.sqrt(
        math.log10(ratio + 1.0) / (33.0 * time_s))

    result: dict[str, Any] = {
        "ok": True,
        "status": PUBLISHED,
        "source": "Onderdonk's equation for copper",
        "fusing_current_a": round(current, 3),
        "width_mils": float(width_mils),
        "thickness_mils": round(thickness_mils, 4),
        "area_sq_mils": round(area_sq_mils, 4),
        "area_circular_mils": round(area_circular_mils, 2),
        "time_s": float(time_s),
        "ambient_c": float(ambient_c),
        "warning": ("this is the current that DESTROYS the track in the "
                    "stated time, not a rating; for continuous current "
                    "use pcb_calc_track_current_capacity"),
    }
    if time_s > 1.0:
        result["out_of_range"] = (
            f"Onderdonk is ADIABATIC and {time_s:g} s is long enough for a "
            f"track to lose heat into the laminate, which this does not "
            f"model. The figure is too low, and past a few seconds it "
            f"falls below the track's own continuous IPC-2221 rating, "
            f"which is the formula leaving its range rather than a "
            f"result. Trust it under about one second.")
    return result


# =====================================================================
# Via parasitics
# =====================================================================

def via_parasitics(drill_mils: float, length_mils: float,
                   pad_mils: float = 0.0, antipad_mils: float = 0.0,
                   dielectric_constant: float = 4.3,
                   plating_mils: float = 1.0) -> dict[str, Any]:
    """Lumped inductance, capacitance and barrel resistance of a via.

    The inductance and capacitance are the rules of thumb from Johnson
    and Graham's *High-Speed Digital Design*::

        L(nH) = 5.08 * h * (ln(4h/d) + 1)          h, d in inches
        C(pF) = 1.41 * er * T * D1 / (D2 - D1)     T, D1, D2 in inches

    They are ESTIMATES. A real via is not a cylinder in free space, the
    return path is not accounted for, and the capacitance form needs an
    antipad larger than the pad or it diverges. Use them to tell a
    via that matters from one that does not, never to claim a margin.

    Barrel resistance is exact for the geometry given: a copper annulus
    of the plating thickness, at 20 degC.

    status: estimate (resistance: exact for the stated geometry)
    """
    bad = _positive(drill_mils=drill_mils, length_mils=length_mils,
                    plating_mils=plating_mils,
                    dielectric_constant=dielectric_constant)
    if bad:
        return bad

    h_in = length_mils / 1000.0
    d_in = drill_mils / 1000.0
    inductance_nh = 5.08 * h_in * (math.log(4.0 * h_in / d_in) + 1.0)

    result: dict[str, Any] = {
        "ok": True,
        "status": ESTIMATE,
        "source": "Johnson & Graham, High-Speed Digital Design",
        "inductance_nh": round(inductance_nh, 4),
        "drill_mils": float(drill_mils),
        "length_mils": float(length_mils),
    }

    if pad_mils and antipad_mils:
        if antipad_mils <= pad_mils:
            result["capacitance_note"] = (
                f"antipad ({antipad_mils:g} mil) must be LARGER than the "
                f"pad ({pad_mils:g} mil); the published form divides by "
                f"their difference, so it was not evaluated")
        else:
            cap_pf = (1.41 * dielectric_constant * h_in
                      * (pad_mils / 1000.0)
                      / ((antipad_mils - pad_mils) / 1000.0))
            result["capacitance_pf"] = round(cap_pf, 4)
            result["pad_mils"] = float(pad_mils)
            result["antipad_mils"] = float(antipad_mils)
            result["dielectric_constant"] = float(dielectric_constant)

    # Barrel: an annulus of plating, inner radius = drill/2.
    r_inner_m = (drill_mils / 2.0) * MM_PER_MIL / 1000.0
    r_outer_m = r_inner_m + plating_mils * MM_PER_MIL / 1000.0
    area_m2 = math.pi * (r_outer_m ** 2 - r_inner_m ** 2)
    length_m = length_mils * MM_PER_MIL / 1000.0
    resistance_ohm = _RHO_CU * length_m / area_m2
    result["barrel_resistance_mohm"] = round(resistance_ohm * 1000.0, 4)
    result["plating_mils"] = float(plating_mils)
    result["warning"] = ("L and C are rules of thumb that ignore the "
                         "return path; they rank vias, they do not "
                         "size a margin")
    return result


# =====================================================================
# Planar spiral inductor -- modified Wheeler
# =====================================================================

#: Layout coefficients for the modified-Wheeler expression, from Mohan
#: et al., "Simple Accurate Expressions for Planar Spiral Inductances",
#: IEEE JSSC 1999. Named rather than inlined because swapping two of
#: them is a silent 20 % error with no symptom.
_WHEELER_K = {
    "square": (2.34, 2.75),
    "hexagonal": (2.33, 3.82),
    "octagonal": (2.25, 3.55),
}


def spiral_inductor(turns: float, outer_diameter_mils: float,
                    inner_diameter_mils: float,
                    shape: str = "square") -> dict[str, Any]:
    """Inductance of a planar spiral, by the modified Wheeler expression.

        L = K1 * mu0 * n^2 * d_avg / (1 + K2 * rho)

    with ``d_avg = (d_out + d_in)/2`` and the fill ratio
    ``rho = (d_out - d_in)/(d_out + d_in)``.

    Mohan et al. report roughly 3 % typical error against field solves
    for the geometries they fitted. It assumes a planar spiral with no
    ground plane close beneath; one changes the answer materially.

    status: published
    """
    bad = _positive(turns=turns, outer_diameter_mils=outer_diameter_mils,
                    inner_diameter_mils=inner_diameter_mils)
    if bad:
        return bad
    if inner_diameter_mils >= outer_diameter_mils:
        return _fail(f"inner_diameter_mils ({inner_diameter_mils:g}) must "
                     f"be smaller than outer_diameter_mils "
                     f"({outer_diameter_mils:g})")
    key = str(shape or "").strip().lower()
    if key not in _WHEELER_K:
        return _fail(f"unknown shape {shape!r}; expected one of "
                     f"{sorted(_WHEELER_K)}")

    k1, k2 = _WHEELER_K[key]
    d_out_m = outer_diameter_mils * MM_PER_MIL / 1000.0
    d_in_m = inner_diameter_mils * MM_PER_MIL / 1000.0
    d_avg = (d_out_m + d_in_m) / 2.0
    rho = (d_out_m - d_in_m) / (d_out_m + d_in_m)
    mu0 = 4.0e-7 * math.pi
    inductance_h = k1 * mu0 * (turns ** 2) * d_avg / (1.0 + k2 * rho)

    return {
        "ok": True,
        "status": PUBLISHED,
        "source": ("Mohan et al., Simple Accurate Expressions for Planar "
                   "Spiral Inductances, IEEE JSSC 1999 (modified Wheeler)"),
        "inductance_nh": round(inductance_h * 1e9, 4),
        "inductance_uh": round(inductance_h * 1e6, 6),
        "turns": float(turns),
        "shape": key,
        "average_diameter_mils": round(
            (outer_diameter_mils + inner_diameter_mils) / 2.0, 3),
        "fill_ratio": round(rho, 4),
        "note": ("assumes no close ground plane beneath the spiral; one "
                 "lowers the inductance materially"),
    }


# =====================================================================
# Reactance, resonance, wavelength -- exact
# =====================================================================

def reactance(frequency_hz: float, capacitance_f: float = 0.0,
              inductance_h: float = 0.0) -> dict[str, Any]:
    """Capacitive and inductive reactance at a frequency.

        Xc = 1 / (2*pi*f*C)      Xl = 2*pi*f*L

    status: exact
    """
    bad = _positive(frequency_hz=frequency_hz)
    if bad:
        return bad
    if not capacitance_f and not inductance_h:
        return _fail("give capacitance_f, inductance_h, or both")

    out: dict[str, Any] = {"ok": True, "status": EXACT,
                           "frequency_hz": float(frequency_hz)}
    omega = 2.0 * math.pi * frequency_hz
    if capacitance_f:
        bad = _positive(capacitance_f=capacitance_f)
        if bad:
            return bad
        out["capacitive_reactance_ohm"] = round(1.0 / (omega * capacitance_f),
                                                6)
        out["capacitance_f"] = float(capacitance_f)
    if inductance_h:
        bad = _positive(inductance_h=inductance_h)
        if bad:
            return bad
        out["inductive_reactance_ohm"] = round(omega * inductance_h, 6)
        out["inductance_h"] = float(inductance_h)
    return out


def resonant_frequency(inductance_h: float,
                       capacitance_f: float) -> dict[str, Any]:
    """LC resonance, ``f = 1 / (2*pi*sqrt(L*C))``.

    status: exact
    """
    bad = _positive(inductance_h=inductance_h, capacitance_f=capacitance_f)
    if bad:
        return bad
    f = 1.0 / (2.0 * math.pi * math.sqrt(inductance_h * capacitance_f))
    return {
        "ok": True,
        "status": EXACT,
        "resonant_frequency_hz": round(f, 4),
        "resonant_frequency_mhz": round(f / 1e6, 6),
        "inductance_h": float(inductance_h),
        "capacitance_f": float(capacitance_f),
        "characteristic_impedance_ohm": round(
            math.sqrt(inductance_h / capacitance_f), 4),
    }


def wavelength_in_dielectric(frequency_hz: float,
                             dielectric_constant: float = 4.3
                             ) -> dict[str, Any]:
    """Wavelength in a medium, and the quarter- and twentieth-wave lengths.

    The twentieth is included because that is the length at which a
    track stops behaving as a lumped connection and starts behaving as
    a transmission line, which is the question this is usually asked
    to settle.

    status: exact (given er; the EFFECTIVE er of a real microstrip is
    lower than the bulk figure, so this is a conservative short answer)
    """
    bad = _positive(frequency_hz=frequency_hz,
                    dielectric_constant=dielectric_constant)
    if bad:
        return bad
    lam_m = _C / (frequency_hz * math.sqrt(dielectric_constant))
    lam_mils = lam_m * 1000.0 / MM_PER_MIL
    return {
        "ok": True,
        "status": EXACT,
        "wavelength_mm": round(lam_m * 1000.0, 4),
        "wavelength_mils": round(lam_mils, 3),
        "quarter_wave_mils": round(lam_mils / 4.0, 3),
        "twentieth_wave_mils": round(lam_mils / 20.0, 3),
        "frequency_hz": float(frequency_hz),
        "dielectric_constant": float(dielectric_constant),
        "note": ("a track longer than about a twentieth of a wavelength "
                 "behaves as a transmission line; for microstrip the "
                 "EFFECTIVE er is lower than the bulk value, which makes "
                 "this estimate the short, conservative one"),
    }


# =====================================================================
# PDN target impedance
# =====================================================================

def pdn_target_impedance(voltage_v: float, ripple_percent: float,
                         transient_current_a: float) -> dict[str, Any]:
    """Target power-distribution impedance.

        Z_target = (Vdd * ripple) / I_transient

    status: estimate

    The formula is a definition; calling the result a target is the
    estimate. It assumes the whole ripple budget is the PDN's to spend
    and that the transient figure is right, and the transient figure is
    the one nobody actually knows.
    """
    bad = _positive(voltage_v=voltage_v, ripple_percent=ripple_percent,
                    transient_current_a=transient_current_a)
    if bad:
        return bad
    if ripple_percent >= 100.0:
        return _fail("ripple_percent is a percentage of the rail, so it "
                     "must be below 100")
    z = (voltage_v * ripple_percent / 100.0) / transient_current_a
    return {
        "ok": True,
        "status": ESTIMATE,
        "target_impedance_ohm": round(z, 6),
        "target_impedance_mohm": round(z * 1000.0, 4),
        "voltage_v": float(voltage_v),
        "ripple_percent": float(ripple_percent),
        "ripple_v": round(voltage_v * ripple_percent / 100.0, 6),
        "transient_current_a": float(transient_current_a),
        "warning": ("spends the ENTIRE ripple budget on the PDN and "
                    "assumes the transient figure is right, which is "
                    "the number least often known"),
    }


# =====================================================================
# Padstack geometry -- exact
# =====================================================================

def padstack_geometry(drill_mils: float, annular_ring_mils: float,
                      pitch_mils: float = 0.0,
                      clearance_mils: float = 0.0) -> dict[str, Any]:
    """Pad and antipad diameters from a drill and an annular ring.

    Also reports the copper left between two adjacent padstacks at a
    given pitch, which is the figure that actually fails a design rule
    and is tedious to work out by hand.

    status: exact
    """
    bad = _positive(drill_mils=drill_mils,
                    annular_ring_mils=annular_ring_mils)
    if bad:
        return bad
    pad = drill_mils + 2.0 * annular_ring_mils
    out: dict[str, Any] = {
        "ok": True,
        "status": EXACT,
        "drill_mils": float(drill_mils),
        "annular_ring_mils": float(annular_ring_mils),
        "pad_diameter_mils": round(pad, 4),
        "pad_diameter_mm": round(pad * MM_PER_MIL, 4),
    }
    if clearance_mils:
        bad = _positive(clearance_mils=clearance_mils)
        if bad:
            return bad
        out["antipad_diameter_mils"] = round(pad + 2.0 * clearance_mils, 4)
    if pitch_mils:
        bad = _positive(pitch_mils=pitch_mils)
        if bad:
            return bad
        gap = pitch_mils - pad
        out["pitch_mils"] = float(pitch_mils)
        out["copper_gap_mils"] = round(gap, 4)
        if gap <= 0:
            out["warning"] = (
                f"pads OVERLAP at this pitch: a {pad:g} mil pad on a "
                f"{pitch_mils:g} mil pitch leaves {gap:g} mil between "
                f"them")
        # Diagonal neighbours on a grid array are pitch*sqrt(2) apart,
        # which is the pair people forget to check.
        out["diagonal_gap_mils"] = round(
            pitch_mils * math.sqrt(2.0) - pad, 4)
    return out


# =====================================================================
# Wire gauge -- exact
# =====================================================================

def awg_properties(awg: float, length_m: float = 1.0,
                   temperature_c: float = 20.0) -> dict[str, Any]:
    """Diameter, area and resistance of an AWG wire.

    AWG is defined geometrically, so the diameter is exact::

        d(mm) = 0.127 * 92 ** ((36 - n) / 39)

    which gives 0.127 mm at AWG 36 and 8.252 mm at AWG 0000 (n = -3).
    Resistance uses copper at 20 degC with the standard temperature
    coefficient.

    status: exact (diameter), published (resistance)
    """
    if isinstance(awg, bool) or not isinstance(awg, (int, float)):
        return _fail("awg must be a number")
    if not math.isfinite(awg) or not -3 <= awg <= 40:
        return _fail("awg must be between -3 (0000) and 40")
    bad = _positive(length_m=length_m)
    if bad:
        return bad
    if not isinstance(temperature_c, (int, float)) or isinstance(
            temperature_c, bool):
        return _fail("temperature_c must be a number")

    diameter_mm = 0.127 * (92.0 ** ((36.0 - awg) / 39.0))
    radius_m = diameter_mm / 2000.0
    area_m2 = math.pi * radius_m ** 2
    rho = _RHO_CU * (1.0 + _ALPHA_CU * (temperature_c - 20.0))
    resistance = rho * length_m / area_m2

    return {
        "ok": True,
        "status": EXACT,
        "awg": float(awg),
        "diameter_mm": round(diameter_mm, 5),
        "diameter_mils": round(diameter_mm / MM_PER_MIL, 3),
        "area_mm2": round(area_m2 * 1e6, 6),
        "area_circular_mils": round(
            (diameter_mm / MM_PER_MIL) ** 2, 2),
        "resistance_ohm": round(resistance, 6),
        "resistance_ohm_per_m": round(rho / area_m2, 6),
        "length_m": float(length_m),
        "temperature_c": float(temperature_c),
    }
