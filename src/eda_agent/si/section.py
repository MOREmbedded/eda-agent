# SPDX-License-Identifier: Apache-2.0
"""A transmission-line cross-section: traces between reference planes.

Lengths are metres. ``y`` grows upwards. A section has one reference
plane below the traces, and for a stripline a second one above. Between
them lie dielectric slabs, each its own laminate, and an optional solder
mask over traces on the top surface (or under them on the bottom).

The same section feeds both solvers. The electrostatic solver (C, G)
sees the planes as the boundary they are for the electric field. The
eddy-current solver (R, L) sees them as copper slabs of their real
thickness, so the return current spreads, crowds and loses power in
them as it does on the board.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from ..engineering.toolkit import fdsolver as fd
from ..units import MM_PER_MIL
from .materials import Copper, Laminate

MIL = MM_PER_MIL / 1000.0


@dataclass(frozen=True)
class Trace:
    """A trace of bottom width ``width`` centred on ``x``, its bottom face
    at ``y``. ``etch`` narrows the top face by that much in total (0 for
    a rectangle)."""

    x: float
    width: float
    y: float
    thickness: float
    etch: float = 0.0

    @property
    def top_width(self) -> float:
        return max(self.width - self.etch, 0.05 * self.width)

    def metal(self, net: int) -> fd.Metal:
        tw = self.top_width
        return fd.Metal(self.x - self.width / 2, self.x + self.width / 2, self.y,
                        self.y + self.thickness, net, self.x - tw / 2, self.x + tw / 2)


@dataclass(frozen=True)
class Slab:
    y0: float
    y1: float
    laminate: Laminate


@dataclass(frozen=True)
class Mask:
    """Solder mask of ``thickness`` on the surface at ``y``, coating
    upwards (``direction`` 1) or downwards (-1)."""

    y: float
    thickness: float
    laminate: Laminate
    direction: int = 1


@dataclass
class Section:
    traces: list[Trace]
    slabs: list[Slab]
    plane_below: Optional[float] = 0.0          # y of the plane's top face
    plane_above: Optional[float] = None         # y of the plane's bottom face
    plane_thickness: float = 35e-6
    mask: Optional[Mask] = None
    copper: Copper = field(default_factory=Copper)
    #: Optional names for the traces, e.g. ("P", "N").
    names: tuple[str, ...] = ()
    kind: str = ""

    def __post_init__(self) -> None:
        if not self.traces:
            raise ValueError("a section needs at least one trace")
        if self.plane_below is None and self.plane_above is None:
            raise ValueError("a section needs a reference plane")
        for t in self.traces:
            if t.width <= 0 or t.thickness <= 0:
                raise ValueError("trace width and thickness must be more than 0")
        xs = sorted(self.traces, key=lambda t: t.x)
        for a, b in zip(xs, xs[1:]):
            if abs(a.y - b.y) < 1e-12 and b.x - b.width / 2 <= a.x + a.width / 2:
                raise ValueError("traces on one layer overlap; the spacing must be more than 0")
        for s in self.slabs:
            if s.y1 <= s.y0:
                raise ValueError("a dielectric slab needs y1 > y0")

    @property
    def n(self) -> int:
        return len(self.traces)

    def laminates(self) -> list[Laminate]:
        out = [s.laminate for s in self.slabs]
        if self.mask is not None:
            out.append(self.mask.laminate)
        return out

    def to_fd(self, eps: Optional[Sequence[float]] = None, tand: Optional[Sequence[float]] = None
              ) -> fd.CrossSection:
        """The electrostatic model. ``eps``/``tand`` override each slab's
        (and then the mask's) Er and tan d, in ``laminates()`` order."""
        lams = self.laminates()
        er = list(eps) if eps is not None else [lam.er for lam in lams]
        td = list(tand) if tand is not None else [lam.tand for lam in lams]
        diel = [fd.Dielectric(s.y0, s.y1, er[i], td[i]) for i, s in enumerate(self.slabs)]
        coats = []
        if self.mask is not None:
            k = len(self.slabs)
            coats.append(fd.Conformal(self.mask.y, self.mask.thickness, er[k], td[k],
                                      self.mask.direction))
        return fd.CrossSection(metals=[t.metal(i) for i, t in enumerate(self.traces)],
                               dielectrics=diel, coatings=coats,
                               ground_bottom=self.plane_below, ground_top=self.plane_above)

    def height(self) -> float:
        lo = self.plane_below if self.plane_below is not None else min(t.y for t in self.traces)
        hi = (self.plane_above if self.plane_above is not None
              else max(t.y + t.thickness for t in self.traces))
        return hi - lo

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "traces": [{"x_m": t.x, "width_m": t.width, "y_m": t.y, "thickness_m": t.thickness,
                        "etch_m": t.etch} for t in self.traces],
            "dielectrics": [{"y0_m": s.y0, "y1_m": s.y1, **s.laminate.to_dict()} for s in self.slabs],
            "plane_below_m": self.plane_below, "plane_above_m": self.plane_above,
            "plane_thickness_m": self.plane_thickness,
            "mask": ({"thickness_m": self.mask.thickness, **self.mask.laminate.to_dict()}
                     if self.mask else None),
            "copper": self.copper.to_dict(),
        }


def _xs(n: int, w: float, s: float) -> list[float]:
    """Centres of ``n`` traces of width ``w`` with edge gap ``s``, about 0."""
    pitch = w + s
    return [(i - (n - 1) / 2) * pitch for i in range(n)]


def microstrip(w: float, h: float, t: float, laminate: Laminate, *, n: int = 1, s: float = 0.0,
               etch: float = 0.0, mask: Optional[tuple[float, Laminate]] = None,
               copper: Optional[Copper] = None, plane_thickness: float = 35e-6) -> Section:
    """Outer-layer traces at height ``h`` over a plane. ``mask`` is
    (thickness, laminate) of the solder mask, or None for bare copper."""
    if n > 1 and s <= 0:
        raise ValueError("coupled traces need a spacing s > 0")
    traces = [Trace(x, w, h, t, etch) for x in _xs(n, w, s)]
    m = Mask(h, mask[0], mask[1], 1) if mask and mask[0] > 0 else None
    return Section(traces, [Slab(0.0, h, laminate)], 0.0, None, plane_thickness, m,
                   copper or Copper(), kind="microstrip")


def stripline(w: float, h1: float, h2: float, t: float, laminate: Laminate, *, n: int = 1,
              s: float = 0.0, etch: float = 0.0, laminate_above: Optional[Laminate] = None,
              copper: Optional[Copper] = None, plane_thickness: float = 35e-6) -> Section:
    """Inner-layer traces with ``h1`` of dielectric below them and ``h2``
    from their bottom face to the plane above (so ``h2`` > ``t``).
    ``laminate`` fills the space below the traces and ``laminate_above``
    (default the same) the space from their bottom face up; with a core
    under the traces and a prepreg pressed round them, as usual, that is
    the real build."""
    if n > 1 and s <= 0:
        raise ValueError("coupled traces need a spacing s > 0")
    if h2 <= t:
        raise ValueError("h2 (trace bottom to the plane above) must be more than the copper thickness")
    traces = [Trace(x, w, h1, t, etch) for x in _xs(n, w, s)]
    up = laminate_above or laminate
    return Section(traces, [Slab(0.0, h1, laminate), Slab(h1, h1 + h2, up)], 0.0, h1 + h2,
                   plane_thickness, None, copper or Copper(), kind="stripline")
