# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Datasheet and package tools that need no editor, on every backend.

The client reads the datasheet; these make a misread number hard to miss
before a footprint is drawn from it:

* ``datasheet_fetch`` downloads the PDF once into the workspace and hands
  back its path and page count;
* ``footprint_check_package`` runs the physical sanity checks on a package
  description and cross-checks its IPC land pattern against the vendor's
  recommended one from the same datasheet;
* ``footprint_render_package`` draws the land pattern the engine would
  make, with the package's terminals over it, for a side-by-side look at
  the datasheet drawing.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from ..config import get_config

__all__ = ["register_footprint_tools"]


def _metrics_lines(model) -> list[str]:
    from ..design.landpattern.compare import pattern_metrics

    met = pattern_metrics(model)

    def mm(v) -> str:
        return f"{v:g}" if isinstance(v, (int, float)) else str(v)

    lines = []
    bits = []
    if met.get("pitch") is not None:
        bits.append(f"pitch {mm(met['pitch'])}")
    if met.get("span") is not None:
        bits.append(f"span {mm(met['span'])} c-c")
    if met.get("span_y") is not None:
        bits.append(f"span_y {mm(met['span_y'])} c-c")
    if met.get("pad_length") is not None:
        bits.append(f"land {mm(met['pad_length'])} x {mm(met['pad_width'])}")
    if met.get("land_diameter") is not None:
        bits.append(f"land {mm(met['land_diameter'])} dia")
    if met.get("ep_x") is not None:
        bits.append(f"exposed pad {mm(met['ep_x'])} x {mm(met['ep_y'])}")
    if met.get("tab_length") is not None:
        bits.append(f"tab {mm(met['tab_length'])} x {mm(met['tab_width'])}")
    if bits:
        lines.append("IPC land pattern, mm: " + ", ".join(bits))
    return lines


def register_footprint_tools(mcp):
    """Register the datasheet and package tools."""

    @mcp.tool()
    async def datasheet_fetch(url: str, part_number: str = "") -> dict[str, Any]:
        """Download a datasheet PDF into the workspace and return its path.

        Read the ``path`` yourself; nothing here parses the PDF. Files are
        cached under ``<workspace>/datasheets`` with a sidecar (URL, part
        number, SHA-256), so a repeat call is a ``cache: "hit"``; a copy
        whose hash no longer matches is fetched again. Refused, storing
        nothing: a non-http(s) URL, over 50 MB, no answer in 60 s, or a
        reply that is not a PDF (a login page); download those in a
        browser instead.

        Args:
            url: the datasheet's http or https URL, ideally the
                manufacturer's own.
            part_number: recorded in the sidecar, for the citation.

        Returns:
            ``{ok, path, sidecar, cache, url, final_url, sha256, size,
            pages, pages_approximate, pages_method, fetched_at}``. ``pages``
            is counted from the page objects in the file without a PDF
            parser and can be None or off for a compressed file.
        """
        from ..design.datasheet_fetch import fetch_datasheet

        return await asyncio.to_thread(fetch_datasheet, url, part_number)

    @mcp.tool()
    async def footprint_check_package(package: Any) -> dict[str, Any]:
        """Check a package description for numbers that cannot be right.

        Run this on every package transcribed from a datasheet, before
        drawing a footprint from it. Offline.

        Sanity checks (each finding names the dimensions, their values and
        citations, and why the combination is impossible or unusual):
        limits in order; lead width below the pitch; each row of pins fits
        the body it comes out of; lead span larger than the body for
        leaded parts; the foot shorter than the lead's reach and two
        terminals fitting the span; the exposed pad inside the body and
        clear of the terminals; ball diameter below the pitch; values 25.4
        times off their neighbours (mils typed as mm); pin counts the
        family can have; the vendor land pattern consistent with itself.
        ``error`` is impossible, ``warning`` unusual.

        Cross-check, when the package carries the datasheet's recommended
        land pattern as ``vendor``: the IPC pattern computed from the
        package against it, per quantity (pitch, span, land length and
        width, tab, exposed pad, grid land), with both numbers, both
        citations and the difference. Beyond the tolerance measured
        between real vendor patterns and IPC, or a factor of two apart,
        the pair is reported as a likely transcription error in one of the
        two sources.

        Args:
            package: the PackageSpec as an object or JSON text, the same
                schema lib_create_footprint_from_package takes.

        Returns:
            ``{ok, errors, warnings, findings, cross_check, examined,
            notes}``. ``ok`` is true when no sanity finding is an error and
            the cross-check (if it ran) agreed.
        """
        from ..design.landpattern.check import check_package, read_package
        from ..design.landpattern.compare import cross_check
        from ..design.landpattern.spec import SpecError

        report = check_package(package)
        out: dict[str, Any] = dict(report)
        cross: dict[str, Any]
        if report["errors"]:
            cross = {"ran": False, "ok": True, "checks": [],
                     "reason": "the package has sanity errors; the IPC pattern "
                               "from it would compare a misread number"}
        else:
            try:
                spec = read_package(package)
                cross = cross_check(spec)
            except SpecError as exc:
                cross = {"ran": False, "ok": False, "checks": [],
                         "reason": f"the engine refused the package: {exc}"}
                out["ok"] = False
        out["cross_check"] = cross
        if cross.get("ran") and not cross.get("ok"):
            out["ok"] = False
        bad = [c for c in cross.get("checks", []) if not c.get("ok")]
        out["summary"] = (
            f"{report['errors']} error(s), {report['warnings']} warning(s) in "
            f"the package; cross-check "
            + ("not run" if not cross.get("ran") else
               "agrees" if not bad else
               f"disagrees on {', '.join(c['quantity'] for c in bad)}"))
        return out

    @mcp.tool()
    async def footprint_render_package(
        package: Any,
        density: str = "N",
        path: str = "",
        png: bool = True,
    ) -> dict[str, Any]:
        """Draw the land pattern a package gives, to compare with its datasheet.

        Offline. Renders the footprint the engine would build (lands with
        their numbers, paste windows, silkscreen with the pin 1 dot,
        assembly outline, courtyard, origin and a 1 mm scale bar) and draws
        the package's terminals over the lands as dashed green outlines,
        so a land that misses its terminal shows. A caption gives the
        pattern's pitch, span and land sizes. Read the returned image next
        to the datasheet's package drawing and recommended land pattern.

        Args:
            package: the PackageSpec as an object or JSON text.
            density: L, N or M (least, nominal, most).
            path: where to write the SVG; default
                ``<workspace>/renders/<name>_<density>.svg``.
            png: also write a PNG beside it through the headless browser
                the board renders use, when one is installed.

        Returns:
            ``{ok, svg_path, png_path, name, density, metrics, warnings}``,
            with ``rasterize_note`` when no PNG could be made.
        """
        from ..design.landpattern.audit import terminal_outlines
        from ..design.landpattern.check import read_package
        from ..design.landpattern.compare import pattern_metrics
        from ..design.landpattern.emit import model_from_package
        from ..design.landpattern.spec import SpecError
        from ..design.landpattern.svg import render_svg

        try:
            spec = read_package(package)
            model = model_from_package(spec, density=density or "N")
        except SpecError as exc:
            return {"ok": False, "reason": str(exc)}
        terms = terminal_outlines(spec, model)
        notes = _metrics_lines(model)
        if terms:
            notes.append("dashed green: the package's terminals, nominal size")
        svg = render_svg(model, notes=notes, terminals=terms)
        if path:
            target = Path(path)
            if target.suffix.lower() != ".svg":
                target = target.with_suffix(".svg")
        else:
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", model.name).strip("._") \
                or "footprint"
            target = get_config().workspace_dir / "renders" / \
                f"{safe}_{model.density}.svg"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(svg, encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "reason": f"could not write {target}: {exc}"}
        out: dict[str, Any] = {
            "ok": True, "svg_path": str(target), "png_path": None,
            "name": model.name, "density": model.density,
            "metrics": pattern_metrics(model), "terminals_drawn": len(terms),
            "warnings": list(model.warnings),
            "legend": {"copper": "lands", "dark": "paste windows",
                       "black": "silkscreen", "blue dashed": "assembly",
                       "magenta": "courtyard",
                       "green dashed": "package terminals (nominal)"},
        }
        if png:
            from ..render import rasterize_svg, size_svg_for_raster

            sized, w_px, h_px = size_svg_for_raster(svg, target_width=1400)
            raster = target.with_suffix(".raster.svg")
            try:
                raster.write_text(sized, encoding="utf-8")
                rr = await asyncio.to_thread(
                    rasterize_svg, str(raster), str(target.with_suffix(".png")),
                    w_px, h_px)
            finally:
                try:
                    raster.unlink()
                except OSError:
                    pass
            if rr.get("ok"):
                out["png_path"] = rr["png_path"]
            else:
                out["rasterize_note"] = rr.get("reason")
        out["next_step"] = (f"Read {out['png_path'] or out['svg_path']} beside "
                            "the datasheet's package drawing")
        return out
