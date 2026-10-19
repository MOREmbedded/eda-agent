# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Each script unit reports which compile of it Altium is running.

Altium keeps compiled units: re-running StartMCPServer does not recompile
an edited one, and a unit that failed to compile kept serving its old
image while SCRIPT_VERSION read new, so app_ping said version_match true
while every library call died on an identifier the unit itself defines.
"""

from __future__ import annotations

import re
from pathlib import Path

from eda_agent import cli, script_stamps

PASCAL = Path(__file__).resolve().parents[1] / "scripts" / "altium"


def test_every_stamped_unit_carries_its_function_returning_dev():
    for unit in script_stamps.STAMPED_UNITS:
        text = (PASCAL / f"{unit}.pas").read_text(encoding="utf-8-sig")
        assert re.search(rf"Function UnitStamp_{unit}\(Dummy : Integer\) : String;\s*Begin\s*"
                         rf"Result := 'dev';", text), unit


def test_the_collector_asks_every_unit():
    text = (PASCAL / "StatusForm.pas").read_text(encoding="utf-8-sig")
    body = text[text.index("Function UnitStampsResponse("):]
    body = body[:body.index("End;")]
    for unit in script_stamps.STAMPED_UNITS:
        assert f'"{unit}":"\' + UnitStamp_{unit}(0)' in body, unit


def test_a_stamp_depends_on_content_not_on_itself_or_line_endings():
    src = ("x;\nFunction UnitStamp_A(Dummy : Integer) : String;\nBegin\n"
           "    Result := 'dev';\nEnd;\n")
    stamped = script_stamps.stamp_source(src)
    assert "Result := 'dev'" not in stamped
    assert script_stamps.unit_stamp(stamped) == script_stamps.unit_stamp(src)
    assert script_stamps.unit_stamp(src.replace("\n", "\r\n")) == \
        script_stamps.unit_stamp(src)
    assert script_stamps.unit_stamp(src.replace("x;", "y;")) != \
        script_stamps.unit_stamp(src)


def test_install_stamps_the_deployed_copy_only(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_AGENT_ASSUME_YES", "1")
    monkeypatch.setenv("EDA_AGENT_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setattr("eda_agent.config.write_workspace_pointer",
                        lambda *a, **k: None)
    dest = tmp_path / "scripts"
    assert cli.cmd_install_scripts(dest=str(dest), force=True) == 0
    for unit in script_stamps.STAMPED_UNITS:
        deployed = (dest / f"{unit}.pas").read_text(encoding="utf-8-sig")
        assert "Result := 'dev'" not in deployed.split(f"UnitStamp_{unit}")[-1][:80]
    source = (PASCAL / "Library.pas").read_text(encoding="utf-8-sig")
    assert re.search(r"UnitStamp_Library\(Dummy : Integer\) : String;\s*Begin\s*Result := 'dev'", source)
    expected = script_stamps.expected_stamps(dest)
    assert all(expected[u] for u in script_stamps.STAMPED_UNITS)


def test_a_unit_running_an_old_compile_is_named(tmp_path):
    for unit in script_stamps.STAMPED_UNITS:
        (tmp_path / f"{unit}.pas").write_text(
            script_stamps.stamp_source(
                f"// {unit}\nFunction UnitStamp_{unit}(Dummy : Integer) : String;\nBegin\n"
                f"    Result := 'dev';\nEnd;\n"), encoding="utf-8")
    reported = dict(script_stamps.expected_stamps(tmp_path))
    assert script_stamps.stale_units(reported, tmp_path) == ([], [])
    reported["Library"] = "0123456789ab"
    reported["Main"] = "dev"
    assert script_stamps.stale_units(reported, tmp_path) == (["Library"], ["Main"])
