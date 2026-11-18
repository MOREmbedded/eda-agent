# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The unit conversion factors, defined once.

Before this module, ``0.0254`` was defined independently in six places
across two backends, under four different names, plus one inline
division. All six agreed, by luck. A physical constant stated in many
places is the same defect as any fact stated twice: nothing enforces
agreement, and the divergence, when it comes, lands as geometry that is
25.4x off in exactly one code path.

Positional literals (a part placed AT 25.4 mm) are not conversions and
do not belong here.
"""

from __future__ import annotations

import math
from typing import Any

#: Millimetres per mil. The definition of the mil: one thousandth of an
#: inch, and the inch is defined as exactly 25.4 mm.
MM_PER_MIL: float = 0.0254

#: Millimetres per inch, exact by definition.
MM_PER_INCH: float = 25.4

#: Mils per millimetre, the inverse spelled once.
MILS_PER_MM: float = 1.0 / MM_PER_MIL

#: Finished copper thickness in mils for one ounce per square foot.
#:
#: A CONVENTION, not a derivation: the industry treats 1 oz/ft^2 as 35
#: micrometres, and 35 / 25.4 is 1.37795..., which everyone writes as
#: 1.378. Deriving it here instead would silently change every trace
#: width and impedance this server has ever quoted, so the conventional
#: rounded value is what is stated, once.
#:
#: It was previously written three times, in impedance_sizing, in
#: trace_sizing, and inline in the current-capacity tool. All three
#: agreed, by luck, which is the same position the mils-to-mm factor
#: was in before task #43.
OZ_TO_MILS: float = 1.378


#: The two units the authoring tools take a length in. "mil" is the
#: default everywhere; "mils" is accepted for it.
LENGTH_UNITS = ("mil", "mm")


def normalise_units(units: str) -> str | None:
    """``"mil"`` or ``"mm"``, or None for anything else."""
    u = str(units or "mil").strip().lower()
    if u == "mils":
        u = "mil"
    return u if u in LENGTH_UNITS else None


def length_in(units: str, mils: float) -> float:
    """A length given in mils, expressed in ``units``.

    For defaults: a pad defaulting to 60 mils must not become a 60 mm pad
    when the caller works in millimetres.
    """
    return mils * MM_PER_MIL if normalise_units(units) == "mm" else mils


def format_length(value: Any) -> str:
    """A length for the bridge wire: fixed point, never an exponent.

    The script's float parser checks its input before converting it and
    rejects an exponent, so ``1e-07`` would quietly fall back to the
    handler's default. Whole numbers go as integers, so a payload of
    whole mils reads exactly as it did when the tools took integers; the
    rest go to six decimal places, finer than Altium's internal unit in
    mils or millimetres.
    """
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"not a finite length: {value!r}")
    if v.is_integer():
        return str(int(v))
    return f"{v:.6f}".rstrip("0").rstrip(".")


def entry_length(entry: dict, stem: str, units: str, default: Any) -> Any:
    """``entry``'s ``stem`` length in ``units``, whatever unit it came in.

    One entry of a list (a via of a batch) may give a length plainly, in
    ``units``, or under a name that says its unit: ``size_mm``, or
    ``size_mils`` / ``size_mil``. The session boundary (lengths.py) renames
    a ``size_mm`` below the top level of a call to ``size_mils`` in a mil
    tool, so a reader that only knew ``size_mm`` missed both and placed
    the default instead. A name with a unit wins over the plain one;
    ``default`` (already in ``units``) when none is given.
    """
    want = normalise_units(units) or "mil"
    for suffix, unit in (("mm", "mm"), ("mils", "mil"), ("mil", "mil")):
        value = entry.get(f"{stem}_{suffix}")
        if value is None:
            continue
        v = float(value)
        if unit == want:
            return v
        return v * MILS_PER_MM if want == "mil" else v * MM_PER_MIL
    value = entry.get(stem)
    return default if value is None else value
