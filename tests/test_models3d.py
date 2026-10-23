# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A part's STEP model is designed from its datasheet package drawing.

The solids come from the same PackageSpec the land pattern does, so the
leads sit on the lands. Every dimension the model uses must cite the
drawing, and a description that does not is refused rather than modelled
from guesses. Writing the STEP needs CadQuery or FreeCAD; those tests skip
where neither is installed (CI installs neither).
"""

from __future__ import annotations

import dataclasses

import pytest

from eda_agent.design import models3d
from eda_agent.design.landpattern.builder import build_footprint
from tests.design import _landpattern_cases as cases


def _plan(case):
    spec = case()
    return spec, models3d.plan_model(spec, build_footprint(spec))


def _extent(plan):
    xs, ys = [], []
    for s in plan.solids:
        if s.kind == "box":
            xs += [s.x0, s.x1]
            ys += [s.y0, s.y1]
        elif s.kind == "prism" and s.axis == "y":
            xs += [u for u, _ in s.profile]
            ys += [s.y0, s.y1]
        elif s.kind == "prism":
            xs += [s.x0, s.x1]
            ys += [u for u, _ in s.profile]
    return min(xs), max(xs), min(ys), max(ys)


def test_a_soic_has_a_body_and_one_lead_per_land_out_to_the_drawn_span():
    spec, plan = _plan(cases.soic8)
    assert sum(s.role == "body" for s in plan.solids) == 1
    assert sum(s.role == "metal" for s in plan.solids) == 8
    x0, x1, y0, y1 = _extent(plan)
    assert x1 == pytest.approx(spec.lead_span_x.nom / 2, abs=1e-6)
    assert y1 == pytest.approx(spec.body_y.nom / 2, abs=1e-6)


def test_the_height_is_the_drawing_maximum_not_the_midpoint_of_0_to_max():
    spec, plan = _plan(cases.soic8)
    assert spec.height.min == 0
    assert plan.height == pytest.approx(spec.height.max)


def test_each_lead_sits_on_its_land():
    spec = cases.qfn16()
    model = build_footprint(spec)
    plan = models3d.plan_model(spec, model)
    terminals = [s for s in plan.solids if s.role == "metal" and s.kind == "box"]
    for pad in (p for p in model.pads if p.role == "signal"):
        assert any(t.x0 <= pad.x <= t.x1 and t.y0 <= pad.y <= t.y1
                   for t in terminals), pad.number


def test_every_dimension_used_names_the_drawing():
    _, plan = _plan(cases.soic8)
    assert plan.from_drawing and not plan.uncited
    assert "lead_span_x" in plan.sources and "height" in plan.sources


def test_a_description_with_no_drawing_is_refused(tmp_path):
    spec = cases.soic8()
    bare = dataclasses.replace(spec, source=None)
    for name in ("lead_span_x", "lead_length", "lead_width", "body_x",
                 "body_y", "pitch"):
        d = getattr(bare, name)
        setattr(bare, name, dataclasses.replace(d, cite=None))
    plan = models3d.plan_model(bare, build_footprint(spec))
    assert not plan.from_drawing and "height" in plan.uncited
    got = models3d.write_step(plan, tmp_path / "x.step", writers=["cadquery"])
    assert not got["ok"] and "drawing" in got["reason"]
    assert not (tmp_path / "x.step").exists()


def test_what_the_drawing_leaves_out_is_listed_not_hidden():
    _, plan = _plan(cases.soic8)
    assert any("lead_thickness" in a for a in plan.assumed)


def test_a_family_not_modelled_gives_no_plan():
    spec = cases.to220()
    assert spec.family not in models3d.MODELLED_FAMILIES
    assert models3d.plan_model(spec, build_footprint(spec)) is None


def test_no_writer_says_how_to_get_one(tmp_path):
    _, plan = _plan(cases.soic8)
    got = models3d.write_step(plan, tmp_path / "x.step", writers=[])
    assert not got["ok"] and "CadQuery" in got["reason"]


@pytest.mark.skipif("cadquery" not in models3d.available_writers(),
                    reason="CadQuery is not installed")
def test_cadquery_writes_a_step_of_the_drawn_size(tmp_path):
    import cadquery as cq

    spec, plan = _plan(cases.soic8)
    got = models3d.write_step(plan, tmp_path / "soic8.step",
                              writers=["cadquery"])
    assert got["ok"] and got["writer"] == "cadquery"
    box = cq.importers.importStep(got["path"]).val().BoundingBox()
    assert box.xmax == pytest.approx(spec.lead_span_x.nom / 2, abs=0.01)
    assert box.zmax == pytest.approx(spec.height.max, abs=0.01)
    assert box.zmin == pytest.approx(0.0, abs=0.01)
