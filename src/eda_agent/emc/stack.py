# SPDX-License-Identifier: Apache-2.0
"""The copper layers of a board, with heights, for field and emission work.

``pcb_get_layer_stackup`` answers one entry per copper layer, top first,
each with the dielectric that lies beneath it. ``LayoutBoard.layers``
names the same layers by Altium's layer id (``TopLayer``, ``MidLayer1``,
``InternalPlane1``, ``BottomLayer``). This joins the two on the numeric
layer id the stackup reply carries, and falls back to stack order when an
entry has none.

Heights are measured upwards from the bottom of the lowest dielectric, in
mils. Copper is treated as a sheet at its interface: its own thickness is
left out of the heights, the usual simplification for a field model and
well inside the tolerance of a pressed stackup.

With no stackup at all, ``assumed_stack`` gives a 1.6 mm FR-4 board of the
right layer count and the result says it is assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

#: Board thickness used when the real stackup is not known: 1.6 mm.
DEFAULT_THICKNESS_MILS = 62.99
DEFAULT_ER = 4.3
#: FR-4 loss tangent near 1 GHz; Altium's stackup reply does not carry it.
DEFAULT_TAN_D = 0.02
#: Outer prepreg in the assumed multilayer stack.
DEFAULT_PREPREG_MILS = 8.0


def layer_id_name(layer_id: int) -> str:
    """Altium's layer id number as the name ``get_layout_model`` uses."""
    if layer_id == 1:
        return "TopLayer"
    if 2 <= layer_id <= 31:
        return f"MidLayer{layer_id - 1}"
    if layer_id == 32:
        return "BottomLayer"
    if 39 <= layer_id <= 54:
        return f"InternalPlane{layer_id - 38}"
    return ""


@dataclass
class CopperLayer:
    name: str                      # LayoutBoard layer name (TopLayer ...)
    label: str = ""                # the name the stackup shows
    z_mils: float = 0.0            # height above the board's bottom face
    copper_mils: float = 1.4
    kind: str = "signal"           # signal | plane


@dataclass
class Dielectric:
    z_bottom_mils: float
    z_top_mils: float
    er: float = DEFAULT_ER
    tan_d: float = DEFAULT_TAN_D
    kind: str = ""                 # core | prepreg | ...

    @property
    def height_mils(self) -> float:
        return self.z_top_mils - self.z_bottom_mils


@dataclass
class Stack:
    layers: list[CopperLayer] = field(default_factory=list)   # top first
    dielectrics: list[Dielectric] = field(default_factory=list)  # top first
    assumed: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def thickness_mils(self) -> float:
        return self.layers[0].z_mils if self.layers else 0.0

    def get(self, name: str) -> Optional[CopperLayer]:
        return next((l for l in self.layers if l.name == name), None)

    def index(self, name: str) -> int:
        for i, l in enumerate(self.layers):
            if l.name == name:
                return i
        return -1

    def neighbours(self, name: str) -> list[CopperLayer]:
        """The copper layers directly above and below, nearest first."""
        i = self.index(name)
        if i < 0:
            return []
        out = []
        if i > 0:
            out.append(self.layers[i - 1])
        if i + 1 < len(self.layers):
            out.append(self.layers[i + 1])
        me = self.layers[i]
        out.sort(key=lambda l: abs(l.z_mils - me.z_mils))
        return out

    def gap_mils(self, a: str, b: str) -> Optional[float]:
        la, lb = self.get(a), self.get(b)
        if la is None or lb is None:
            return None
        return abs(la.z_mils - lb.z_mils)

    def er_between(self, a: str, b: str) -> float:
        """Thickness-weighted dielectric constant between two layers."""
        la, lb = self.get(a), self.get(b)
        if la is None or lb is None:
            return DEFAULT_ER
        lo, hi = sorted((la.z_mils, lb.z_mils))
        num = den = 0.0
        for d in self.dielectrics:
            top, bot = min(hi, d.z_top_mils), max(lo, d.z_bottom_mils)
            if top > bot:
                num += d.er * (top - bot)
                den += top - bot
        return num / den if den else DEFAULT_ER

    def to_dict(self) -> dict:
        return {
            "assumed": self.assumed,
            "thickness_mils": round(self.thickness_mils, 3),
            "layers": [{"name": l.name, "label": l.label, "z_mils": round(l.z_mils, 4),
                        "kind": l.kind} for l in self.layers],
            "dielectrics": [{"z_bottom_mils": round(d.z_bottom_mils, 4),
                             "z_top_mils": round(d.z_top_mils, 4),
                             "er": d.er, "tan_d": d.tan_d, "kind": d.kind}
                            for d in self.dielectrics],
            "notes": list(self.notes),
        }


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _no_dielectric(dtype: Any) -> bool:
    return str(dtype or "").strip().lower() in ("", "none", "no", "nodielectric")


def _build(entries: list[tuple[str, str, str, float, float, float, str]],
           assumed: bool, notes: list[str]) -> Stack:
    """entries: (name, label, kind, copper, height below, er below, type),
    top first. Heights accumulate from the bottom."""
    stack = Stack(assumed=assumed, notes=notes)
    z = 0.0
    built: list[CopperLayer] = []
    diel: list[Dielectric] = []
    for name, label, kind, cu, h, er, dtype in reversed(entries):
        if h > 0:
            diel.append(Dielectric(z, z + h, er or DEFAULT_ER, DEFAULT_TAN_D, dtype))
            z += h
        built.append(CopperLayer(name, label, z, cu, kind))
    stack.layers = list(reversed(built))
    stack.dielectrics = list(reversed(diel))
    return stack


def fill_dielectrics(stackup: dict, physical: dict) -> dict:
    """``stackup`` (a ``pcb_get_layer_stackup`` reply) with the dielectric
    beneath each copper layer taken from ``physical`` (a
    ``pcb_physical_stack`` reply) where the stackup has none.

    Altium keeps core and prepreg as layers of their own between the
    copper. The stackup reply carries only what each copper layer says
    about the dielectric beneath it, and on some boards that is nothing.
    The copper layers of the two replies are matched in order; several
    dielectric layers between two coppers add up, with the thickness
    weighted permittivity. Returns a copy; ``stackup`` is left as it is
    when the two do not have the same number of copper layers."""
    rows = [dict(r) for r in (stackup.get("layers") or [])]
    kept = [r for r in (physical.get("layers") or []) if r.get("kind") in ("copper", "dielectric")]
    while kept and kept[0].get("kind") != "copper":
        kept.pop(0)
    gaps: list[list[dict]] = []
    for r in kept:
        if r.get("kind") == "copper":
            gaps.append([])
        elif gaps:
            gaps[-1].append(r)
    if not rows or len(gaps) != len(rows):
        return stackup
    filled = []
    for row, gap in zip(rows[:-1], gaps[:-1]):
        has = not _no_dielectric(row.get("dielectric_type")) \
            and _f(row.get("dielectric_height_mils")) > 0
        h = sum(_f(g.get("thickness_mils")) for g in gap)
        if has or h <= 0:
            continue
        er = sum(_f(g.get("thickness_mils")) * (_f(g.get("constant"), DEFAULT_ER) or DEFAULT_ER)
                 for g in gap) / h
        kind = {1: "Core", 2: "Prepreg"}.get(int(_f(gap[0].get("dielectric_type"), 0)), "Core")
        row.update(dielectric_height_mils=round(h, 4), dielectric_constant=round(er, 4),
                   dielectric_type=kind,
                   dielectric_material=str(gap[0].get("material") or ""))
        filled.append(str(row.get("name") or ""))
    if not filled:
        return stackup
    return {**stackup, "layers": rows, "dielectric_from_physical_stack": filled}


def stack_from_stackup(stackup: dict, board_layers: Optional[list] = None) -> Stack:
    """Build the stack from a ``pcb_get_layer_stackup`` reply.

    ``board_layers`` (``LayoutBoard.layers``) supplies plane/signal kinds
    and, for entries without a layer id, the names in stack order.
    """
    rows = list(stackup.get("layers") or [])
    by_order = [l for l in sorted(board_layers or [], key=lambda l: l.order)]
    kinds = {l.name: l.kind for l in by_order}
    notes: list[str] = []
    entries = []
    for i, row in enumerate(rows):
        name = layer_id_name(int(_f(row.get("layer_id"), 0)))
        if not name and i < len(by_order):
            name = by_order[i].name
        if not name:
            name = str(row.get("name") or f"L{i + 1}")
        dtype = row.get("dielectric_type")
        h = 0.0 if _no_dielectric(dtype) else _f(row.get("dielectric_height_mils"))
        if i == len(rows) - 1:
            h = 0.0  # nothing beneath the bottom copper but solder mask
        er = _f(row.get("dielectric_constant"), DEFAULT_ER) or DEFAULT_ER
        kind = kinds.get(name) or ("plane" if name.startswith("InternalPlane") else "signal")
        entries.append((name, str(row.get("name") or ""), kind,
                        _f(row.get("copper_thickness_mils"), 1.4), h, er,
                        str(dtype or "")))
    if not entries:
        return assumed_stack(board_layers or [])
    if any(e[4] <= 0 for e in entries[:-1]):
        notes.append("a layer reported no dielectric beneath it; adjacent "
                     "layers on either side of it share one height")
    notes.append(f"loss tangent {DEFAULT_TAN_D} assumed (the stackup does not carry it)")
    return _build(entries, assumed=False, notes=notes)


def assumed_stack(board_layers: list, thickness_mils: float = DEFAULT_THICKNESS_MILS) -> Stack:
    """A 1.6 mm FR-4 stack with the board's copper layers, evenly built:
    8 mil outer prepregs and the rest split among the inner gaps."""
    names = [l.name for l in sorted(board_layers, key=lambda l: l.order)] or [
        "TopLayer", "BottomLayer"]
    kinds = {l.name: l.kind for l in board_layers}
    n = len(names)
    if n == 1:
        heights = [0.0]
    elif n == 2:
        heights = [thickness_mils, 0.0]
    elif n == 3:
        heights = [thickness_mils / 2, thickness_mils / 2, 0.0]
    else:
        inner = (thickness_mils - 2 * DEFAULT_PREPREG_MILS) / (n - 3)
        heights = [DEFAULT_PREPREG_MILS] + [inner] * (n - 3) + [DEFAULT_PREPREG_MILS, 0.0]
    entries = [(name, name, kinds.get(name, "signal"), 1.4, heights[i], DEFAULT_ER, "assumed")
               for i, name in enumerate(names)]
    return _build(entries, assumed=True,
                  notes=["stackup assumed: 1.6 mm FR-4, er 4.3; read the real "
                         "one with pcb_get_layer_stackup for real numbers"])
