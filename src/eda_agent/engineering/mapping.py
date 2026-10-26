# SPDX-License-Identifier: Apache-2.0
"""Pin-to-pad mapping and 3D model association checks."""
from __future__ import annotations

from pathlib import Path

from .errors import ValidationFailed
from hashlib import sha256


def sha256_file(path: Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build_pin_pad_map(pins: list[dict], pads: list[dict], explicit: dict | None = None,
                      unconnected_pads: list[str] | None = None) -> dict:
    """Default mapping is pin number == pad name. `explicit` maps pin->pad or
    pin->[pads] (for example a SOT-223 tab pad shorted to an output pin).
    Every pad must be mapped to a pin or explicitly listed as unconnected
    (mechanical/NC) and every pin must land on at least one pad."""
    pad_names = {p["name"] for p in pads}
    explicit = explicit or {}
    unconnected = set(unconnected_pads or [])
    mapping: dict[str, list[str]] = {}
    for pin in pins:
        num = pin["number"]
        target = explicit.get(num, num)
        targets = target if isinstance(target, list) else [target]
        mapping[num] = [str(t) for t in targets]
    errors = []
    used = set()
    for num, tl in mapping.items():
        for t in tl:
            if t not in pad_names:
                errors.append(f"pin {num} maps to missing pad {t}")
            if t in used:
                errors.append(f"pad {t} mapped from more than one pin")
            used.add(t)
    orphan = sorted(pad_names - used - unconnected)
    if orphan:
        errors.append(f"pads with no pin and not declared unconnected: {orphan}")
    bad_nc = sorted(unconnected & used)
    if bad_nc:
        errors.append(f"pads declared unconnected but mapped: {bad_nc}")
    if errors:
        raise ValidationFailed("pin-to-pad mapping invalid", details={"errors": errors})
    return mapping


def check_model_3d(model: dict | None, library_root: Path) -> dict | None:
    if not model:
        return None
    p = (library_root / model["path"]).resolve()
    if p.suffix.lower() not in (".step", ".stp"):
        raise ValidationFailed("3D model must be a STEP file")
    if not p.exists():
        raise ValidationFailed(f"3D model not found: {p}")
    digest = sha256_file(p)
    if model.get("sha256") and model["sha256"] != digest:
        raise ValidationFailed("3D model hash differs from recorded hash")
    return {"path": model["path"], "sha256": digest,
            "rotation_deg": model.get("rotation_deg", [0, 0, 0]),
            "offset_mm": model.get("offset_mm", [0, 0, 0])}
