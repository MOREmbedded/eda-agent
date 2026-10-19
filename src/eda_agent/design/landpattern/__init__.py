# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Land patterns from datasheet package drawings, IPC-7351B, in mm.

Pure Python and offline. A caller transcribes the package drawing into a
:class:`PackageSpec` (each dimension min / nom / max, with its page and
figure), calls :func:`build_footprint`, and gets a neutral
:class:`FootprintModel`: lands, silkscreen, assembly outline, courtyard,
3D body box and the record of the IPC calculation, ready for any backend
to emit. :func:`render_svg` draws it for a person to check, and
:func:`compare_to_vendor` measures it against the datasheet's own
recommended land pattern.

Module map: ``ipc7351`` (the standard's numbers and equations, each
cited), ``spec`` (the input), ``model`` (the output), ``drafting``
(silkscreen, assembly, courtyard, paste), ``smd`` and ``tht`` (one builder
per family), ``builder`` (shared finishing), ``svg``, ``compare``.
"""

from eda_agent.design.landpattern.builder import build_footprint
from eda_agent.design.landpattern.compare import compare_to_vendor
from eda_agent.design.landpattern.drafting import DEFAULT_RULES, DraftingRules
from eda_agent.design.landpattern.model import (
    Body3D,
    Circle,
    FootprintModel,
    Line,
    Pad,
    Polygon,
    Rect,
    Text,
)
from eda_agent.design.landpattern.spec import (
    FAMILIES,
    Citation,
    CustomPad,
    Dim,
    PackageSpec,
    SpecError,
    VendorLandPattern,
    dim,
)
from eda_agent.design.landpattern.svg import render_svg

__all__ = [
    "Body3D",
    "Circle",
    "Citation",
    "CustomPad",
    "DEFAULT_RULES",
    "Dim",
    "DraftingRules",
    "FAMILIES",
    "FootprintModel",
    "Line",
    "PackageSpec",
    "Pad",
    "Polygon",
    "Rect",
    "SpecError",
    "Text",
    "VendorLandPattern",
    "build_footprint",
    "compare_to_vendor",
    "dim",
    "render_svg",
]
