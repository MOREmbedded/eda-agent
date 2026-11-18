# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""pcb_query_region and property names the bridge does not read.

MEASURED 2026-11-18 on AD 26.11.1: pcb_query_region asked for Radius,
StartAngle and BoundingRectangle put up Altium's "Wrong number of params"
error and the script loop had to be restarted, three times. The refusal
for the unknown BoundingRectangle named KnownPCBPropertyList, which takes
a (dummy) parameter, bare: DelphiScript runs that as a call with no
arguments and stops the script.

Now the names are checked in Python first, and never reach Altium unless
the script reads them; the bare call is fixed, and a lint rule keeps any
other routine with parameters from being named without them.
"""
from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from pathlib import Path

import pytest

from eda_agent import pcb_properties
from eda_agent.tools import pcb
from tests.pascal_source import load

ALTIUM = Path(__file__).resolve().parents[1] / "scripts" / "altium"


class _Bridge:
    def __init__(self):
        self.sent = []

    async def send_command_async(self, command, params, **kwargs):
        self.sent.append((command, params))
        return {"objects": [], "count": 0}


def _query(monkeypatch, **kwargs):
    bridge = _Bridge()
    monkeypatch.setattr(pcb, "get_bridge", lambda: bridge)
    captured = {}

    class _Mcp:
        def tool(self, *a, **k):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

        def __getattr__(self, name):
            return lambda *a, **k: (lambda fn: fn)

    pcb.register_pcb_tools(_Mcp())
    args = {"x1": 0, "y1": 0, "x2": 100, "y2": 100, **kwargs}
    return asyncio.run(captured["pcb_query_region"](**args)), bridge.sent


def test_an_unknown_name_is_refused_before_altium_with_the_valid_ones(monkeypatch):
    out, sent = _query(monkeypatch, object_type="eArcObject",
                       properties="Radius,StartAngle,BoundingRectangle")
    assert sent == []
    assert out["ok"] is False and out["unknown"] == ["BoundingRectangle"]
    assert out["did_you_mean"]["BoundingRectangle"] == [
        "BoundsLeft", "BoundsBottom", "BoundsRight", "BoundsTop"]
    assert "Radius" in out["valid_properties"]


def test_a_name_in_the_wrong_case_is_pointed_at_the_right_one(monkeypatch):
    out, sent = _query(monkeypatch, object_type="eArcObject", properties="radius")
    assert sent == [] and out["did_you_mean"] == {"radius": ["Radius"]}


def test_arc_names_on_an_arc_go_through_without_warnings(monkeypatch):
    out, sent = _query(monkeypatch, object_type="eArcObject",
                       properties="Radius,StartAngle,EndAngle,BoundsLeft")
    assert len(sent) == 1 and sent[0][0] == "pcb.query_region"
    assert "warnings" not in out


def test_arc_names_on_a_track_are_sent_and_warned_about(monkeypatch):
    out, sent = _query(monkeypatch, object_type="track", properties="X1,Radius")
    assert len(sent) == 1
    assert out["warnings"] == [
        "Radius is read on arc objects only; it comes back empty on track."]


def test_the_python_list_is_the_scripts_list():
    funcs = load(ALTIUM / "PCBGeneric.pas")
    names = set()
    for f in ("IsKnownPCBProperty", "ExtraPCBPropertyOwner", "IsGeneratedPCBProperty"):
        names |= set(re.findall(r"PropName = '([^']+)'", funcs[f]))
    assert names == set(pcb_properties.PCB_PROPERTIES)


def test_the_region_refusal_calls_the_list_with_its_argument():
    body = load(ALTIUM / "PCB.pas")["PCB_QueryRegion"]
    assert "KnownPCBPropertyList(0)" in body
    assert not re.search(r"KnownPCBPropertyList\s*[^\s(]", body)
    # The bounds are doubles: IntToStr of one is not an integer read.
    assert not re.search(r"IntToStr\([XY][12]\)", body)


@pytest.fixture(scope="module")
def lint():
    spec = importlib.util.spec_from_file_location("_altium_lint_bare", ALTIUM / "lint.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_altium_lint_bare"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_lint_flags_a_routine_with_parameters_named_bare(lint, tmp_path):
    src = tmp_path / "t.pas"
    src.write_text(
        "Function Names(Dummy : Integer) : String;\n"
        "Begin\n"
        "    Names := 'a';\n"
        "End;\n"
        "Function Refuse(Bad : String) : String;\n"
        "Var\n"
        "    Width : Integer;\n"
        "Begin\n"
        "    { Names in a comment is prose }\n"
        "    Result := 'Bad: ' + Bad + ' Available: ' + Names + '.';\n"
        "    Result := Result + Names(0) + Obj.Names;\n"
        "End;\n", encoding="utf-8")
    found = lint._scan_bare_call_missing_args([str(src)])
    assert [(f.rule, f.line) for f in found] == [("bare-call-missing-args", 10)]


def test_no_routine_with_parameters_is_named_bare_in_the_scripts(lint):
    targets = [str(ALTIUM / f) for f in lint.PAS_FILES]
    assert lint._scan_bare_call_missing_args(targets) == []
