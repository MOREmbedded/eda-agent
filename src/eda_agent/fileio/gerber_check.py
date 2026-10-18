# SPDX-License-Identifier: Apache-2.0
"""Read an exported fab package back, and check it against the board.

eda-agent could already *generate* fab output through an OutJob
(`proj_run_outjob`), but nothing ever read it back: every mention of
"gerber" in this codebase was prose in a docstring. That left the one
artifact that actually gets manufactured write-only and unverified.

This reads the Gerber and Excellon files with
[gerbonara](https://pypi.org/project/gerbonara/) and compares what it
finds against what the live board says, so the question "does the
package I am about to send the fab match the board I designed" has an
answer.

gerbonara is an OPTIONAL dependency (`pip install eda-agent[gerber]`).
It is Apache-2.0, like this project, and handles Gerber and Excellon in
one library. It needs Python 3.12+, while eda-agent supports 3.11, which
is the other reason it is an extra rather than a core requirement.

**Everything here is in millimetres.** gerbonara normalises units on
read, whatever the file declared.

A note on robustness: gerbonara 1.6.3 raises UnboundLocalError from its
own `_common_prefix` when a fab file's basename is a single character
(``B.GTL``), because the range it iterates comes out empty and the loop
variable is never bound. Passing ``board_name`` explicitly skips the
inference that calls it, which this module always does. Calls are still
wrapped, because an upstream crash should arrive as a refusal naming
the files rather than as a traceback from inside a parser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: Severity names, matching the rest of the audit surface.
ERROR = "error"
WARNING = "warning"
INFO = "info"

#: How far the exported outline may differ from the board before it is
#: worth saying so. Gerber coordinates are exact; this absorbs the
#: rounding an exporter does, not a real size difference.
_OUTLINE_TOL_MM = 0.05


def _finding(severity: str, check: str, message: str,
             expected: Any = None, actual: Any = None) -> dict[str, Any]:
    return {"severity": severity, "check": check, "message": message,
            "expected": expected, "actual": actual}


def summarise_package(directory: str | Path,
                      board_name: str = "board") -> dict[str, Any]:
    """What is actually in an exported fab folder.

    Returns ``{ok, files, layer_map, copper_layers, outline_mm, drills,
    min_aperture_mm}``. ``layer_map`` is gerbonara's own human-readable
    classification, included so a misdetected layer is visible rather
    than silently counted as something else.

    ``board_name`` is passed through to gerbonara rather than letting it
    infer one; see the module docstring for why.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return {"ok": False, "reason": f"not a directory: {directory}"}

    files = sorted(p.name for p in directory.iterdir() if p.is_file())
    if not files:
        return {"ok": False, "reason": f"no files in {directory}"}

    try:
        from gerbonara import LayerStack
    except ImportError:
        return {"ok": False, "reason":
                "gerbonara is not installed, so fab output cannot be read. "
                "Install it with: pip install eda-agent[gerber] "
                "(needs Python 3.12 or newer)."}

    try:
        stack = LayerStack.open_dir(directory, board_name=board_name)
    except Exception as exc:  # noqa: BLE001 - upstream parser, any failure
        return {"ok": False, "reason":
                f"could not read the fab package in {directory}: "
                f"{type(exc).__name__}: {exc}",
                "files": files}

    try:
        copper = list(stack.copper_layers)
        drill_layers = list(stack.drill_layers)
        outline = _outline_size(stack)
        drills = _drill_summary(drill_layers)
        min_aperture = _min_aperture(copper)
        layer_map = stack.format_layer_map()
    except Exception as exc:  # noqa: BLE001 - same reasoning
        return {"ok": False, "reason":
                f"the fab package parsed but could not be summarised: "
                f"{type(exc).__name__}: {exc}",
                "files": files}

    return {
        "ok": True,
        "directory": str(directory),
        "files": files,
        "layer_map": layer_map,
        "copper_layers": len(copper),
        "outline_mm": outline,
        "drills": drills,
        "min_aperture_mm": min_aperture,
    }


def _outline_size(stack) -> dict[str, float] | None:
    """Width and height of the board outline, in mm, or None.

    A Gerber bounding box spans the drawn INK, and an outline is a
    stroked path whose centreline is the board edge. So a 20 mm board
    drawn with a 0.2 mm pen measures 20.2 mm, and comparing that raw
    against the board's own dimensions reports every board as
    oversized by one pen width.

    The stroke is therefore subtracted, using the outline layer's own
    aperture, and the amount is reported as ``stroke_mm`` so the
    adjustment is visible rather than assumed. An outline drawn as a
    filled region has no stroke to remove, and none is removed.
    """
    source = stack.outline if stack.outline is not None else None
    bounds = None
    if source is not None:
        bounds = source.bounding_box()
    if bounds is None:
        try:
            bounds = stack.board_bounds()
        except Exception:  # noqa: BLE001
            bounds = None
    if not bounds:
        return None

    (x1, y1), (x2, y2) = bounds
    width, height = abs(x2 - x1), abs(y2 - y1)

    stroke = 0.0
    if source is not None:
        widths = []
        try:
            for aperture in source.apertures():
                value = getattr(aperture, "diameter", None)
                if value is None:
                    w, h = getattr(aperture, "w", None), getattr(aperture, "h", None)
                    value = min(w, h) if (w and h) else (w or h)
                if value:
                    widths.append(float(value))
        except Exception:  # noqa: BLE001
            widths = []
        # The narrowest aperture on the outline layer is the pen; a wider
        # one is likely a corner or a legend and should not be subtracted.
        if widths:
            stroke = min(widths)

    return {"width": round(max(width - stroke, 0.0), 4),
            "height": round(max(height - stroke, 0.0), 4),
            "stroke_mm": round(stroke, 4),
            "raw_width": round(width, 4), "raw_height": round(height, 4)}


def _drill_summary(drill_layers) -> dict[str, Any]:
    """Hits, sizes and plating across every drill file.

    Note the asymmetry in gerbonara's API: ``copper_layers`` yields
    ``(key, file)`` pairs while ``drill_layers`` yields bare
    ExcellonFile objects.
    """
    total = 0
    sizes: set[float] = set()
    plating: set[str] = set()
    for layer in drill_layers:
        try:
            total += sum(layer.hit_count().values())
            sizes.update(round(float(d), 4) for d in layer.drill_sizes())
            plating.add(str(layer.plating_type))
        except Exception:  # noqa: BLE001
            continue
    return {"files": len(drill_layers), "hits": total,
            "sizes_mm": sorted(sizes),
            "smallest_mm": min(sizes) if sizes else None,
            "plating": sorted(plating)}


def _min_aperture(copper_layers) -> float | None:
    """Smallest drawn feature across the copper layers, in mm.

    A circle's diameter, or a rectangle's shorter side: both are the
    narrowest thing the fab has to reproduce.
    """
    smallest: float | None = None
    for _key, layer in copper_layers:
        try:
            apertures = layer.apertures()
        except Exception:  # noqa: BLE001
            continue
        for aperture in apertures:
            for value in (getattr(aperture, "diameter", None),
                          getattr(aperture, "w", None),
                          getattr(aperture, "h", None)):
                if value is None:
                    continue
                size = round(float(value), 4)
                if size > 0 and (smallest is None or size < smallest):
                    smallest = size
    return smallest


def compare_with_board(summary: dict[str, Any],
                       board: dict[str, Any]) -> dict[str, Any]:
    """Check an exported package against what the board says.

    ``board`` is a plain dict so this stays pure and testable without
    Altium; the tool layer assembles it from existing reads. Recognised
    keys, all optional, all in millimetres:

        copper_layers       int, from the stackup
        outline_mm          {"width", "height"}
        hole_count          int, pads with a drill plus vias
        min_drill_mm        the fab's smallest drill
        min_feature_mm      the fab's smallest track/space

    A key that is absent is NOT checked, and the result says which
    checks ran. A missing input must not read as a pass.
    """
    if not summary.get("ok"):
        return {"ok": False, "reason": summary.get("reason", "unreadable package")}

    findings: list[dict[str, Any]] = []
    checked: list[str] = []

    expected_layers = board.get("copper_layers")
    if expected_layers is not None:
        checked.append("copper_layers")
        actual = summary["copper_layers"]
        if actual != expected_layers:
            findings.append(_finding(
                ERROR, "copper_layers",
                f"the package has {actual} copper layer(s) but the board "
                f"stackup has {expected_layers}",
                expected_layers, actual))

    expected_outline = board.get("outline_mm")
    actual_outline = summary.get("outline_mm")
    if expected_outline and actual_outline:
        checked.append("outline")
        for axis in ("width", "height"):
            want, got = expected_outline.get(axis), actual_outline.get(axis)
            if want is None or got is None:
                continue
            if abs(float(want) - float(got)) > _OUTLINE_TOL_MM:
                findings.append(_finding(
                    ERROR, "outline",
                    f"exported board {axis} is {got} mm but the board is "
                    f"{want} mm",
                    want, got))
    elif expected_outline and not actual_outline:
        findings.append(_finding(
            WARNING, "outline",
            "no board outline layer was found in the package, so its size "
            "could not be checked"))

    expected_holes = board.get("hole_count")
    if expected_holes is not None:
        checked.append("hole_count")
        actual_hits = summary["drills"]["hits"]
        if actual_hits != expected_holes:
            findings.append(_finding(
                ERROR, "hole_count",
                f"the drill files have {actual_hits} hit(s) but the board "
                f"has {expected_holes} hole(s)",
                expected_holes, actual_hits))

    min_drill = board.get("min_drill_mm")
    smallest = summary["drills"]["smallest_mm"]
    if min_drill is not None and smallest is not None:
        checked.append("min_drill")
        if smallest < float(min_drill) - 1e-9:
            findings.append(_finding(
                ERROR, "min_drill",
                f"smallest drill is {smallest} mm, below the fab's "
                f"{min_drill} mm minimum",
                min_drill, smallest))

    min_feature = board.get("min_feature_mm")
    min_aperture = summary.get("min_aperture_mm")
    if min_feature is not None and min_aperture is not None:
        checked.append("min_feature")
        if min_aperture < float(min_feature) - 1e-9:
            findings.append(_finding(
                WARNING, "min_feature",
                f"smallest copper feature is {min_aperture} mm, below the "
                f"fab's {min_feature} mm minimum. Apertures include pads and "
                f"text, so check what the small one actually is before "
                f"treating this as a track-width violation",
                min_feature, min_aperture))

    if summary["drills"]["files"] == 0:
        findings.append(_finding(
            ERROR, "drill_files",
            "no drill file was found in the package"))

    counts = {level: sum(1 for f in findings if f["severity"] == level)
              for level in (ERROR, WARNING, INFO)}
    return {
        "ok": True,
        "passed": counts[ERROR] == 0,
        "checks_run": checked,
        "counts": counts,
        "findings": findings,
        "summary": {k: summary[k] for k in
                    ("copper_layers", "outline_mm", "drills", "min_aperture_mm")},
    }
