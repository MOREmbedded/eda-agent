# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Everything a project option can be set to can also be read back.

The writer is ``proj_project_setup``, which has written the net naming
options, the net identifier scope, the output path and the pin swap
options all along. What it could not write was the two defaults a new
document inherits, and what ``proj_get_options`` could not read was four
of the options the writer sets.

That gap is the point of these tests. A caller holding a project against
a stored policy needs a read that covers everything the write covers;
without it, applying a policy and checking it had to go through two
different tools that reported different fields.

Deliberately ONE writer. A second tool over the same state, under
different option names, is one more place for the two to disagree about
what a project is set to.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from eda_agent.tools import project as project_tools
from tests.pascal_source import load

_PROJECT_PAS = (Path(__file__).resolve().parents[1] / "scripts" / "altium"
                / "Project.pas")

#: Every option the writer can set, with the getter that reads it back.
#: Both sides are checked against the Pascal below.
WRITABLE = {
    "allow_port_net_names": "DM_GetAllowPortNetNames",
    "allow_sheet_entry_net_names": "DM_GetAllowSheetEntryNetNames",
    "append_sheet_number_to_local_nets": "DM_GetAppendSheetNumberToLocalNets",
    "pin_swap_by_pin": "DM_GetPinSwapBy_Pin",
    "pin_swap_by_netlabel": "DM_GetPinSwapBy_Netlabel",
    "output_path": "DM_GetOutputPath",
    "hierarchy_mode": "DM_HierarchyMode",
    "default_pcb_type": "DM_GetDefaultPcbType",
    "default_configuration_name": "DM_GetDefaultConfigurationName",
}


def _code(text: str) -> str:
    return re.sub(r"\{[^}]*\}", "", text)


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class _Bridge:
    def __init__(self, reply=None):
        self.reply = reply if reply is not None else {
            "project": "C:/p/B.PrjPcb", "changed": True, "not_applied": []}
        self.sent = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.sent.append((command, dict(params or {})))
        return self.reply


def _tools(monkeypatch, bridge):
    monkeypatch.setattr(project_tools, "get_bridge", lambda: bridge)
    mcp = _Mcp()
    project_tools.register_project_tools(mcp)
    return mcp.tools


# --- the reader covers the writer ----------------------------------------

def test_the_reader_reports_every_option_the_writer_can_write():
    """Without this, applying a policy and checking it went through two
    tools that reported different fields."""
    reader = load(_PROJECT_PAS)["Proj_GetProjectOptions"]
    for key in WRITABLE:
        assert f'"{key}"' in reader, key


def test_the_reader_uses_each_option_s_own_getter():
    # Not _code(): a Pascal brace comment opens with the same character a
    # JSON object does, so stripping comments eats the body's literals.
    reader = load(_PROJECT_PAS)["Proj_GetProjectOptions"]
    for key, getter in WRITABLE.items():
        assert getter in reader, key


def test_the_channel_settings_are_read_only_and_only_read():
    """Altium's engine registers no setter for either under any spelling,
    so they are reported and never offered as writable."""
    reader = load(_PROJECT_PAS)["Proj_GetProjectOptions"]
    writer = load(_PROJECT_PAS)["Proj_ProjectSetup"]
    assert '"channel_designator_format"' in reader
    assert '"channel_room_separator"' in reader
    assert "DM_SetChannelDesignatorFormat" not in writer


# --- the writer ----------------------------------------------------------

def test_there_is_exactly_one_project_options_writer():
    """A second handler over the same state, under its own option names,
    is one more place for the two to disagree about what a project is set
    to. Counted by handler, not by call site: the two defaults are written
    twice inside this one, to put the previous value back when the write
    did not take."""
    text = _PROJECT_PAS.read_text(encoding="utf-8")
    assert "Proj_SetProjectOptions" not in text
    functions = re.split(r"^Function (\w+)\(", text, flags=re.M)
    writers = set()
    for name, body in zip(functions[1::2], functions[2::2]):
        for setter in ("AllowPortNetNames", "AllowSheetEntryNetNames",
                       "AppendSheetNumberToLocalNets", "PinSwapBy_Pin",
                       "PinSwapBy_Netlabel", "OutputPath", "HierarchyMode",
                       "DefaultPcbType", "DefaultConfigurationName"):
            if f"DM_Set{setter}(" in body:
                writers.add(name)
    assert writers == {"Proj_ProjectSetup"}, sorted(writers)


def test_the_two_defaults_are_written_and_read_back():
    """Read the old value, write, read back. The first read is what makes
    putting the old value back possible when the write does not take."""
    body = _code(load(_PROJECT_PAS)["Proj_ProjectSetup"])
    for setter, getter, was in (
            ("DM_SetDefaultPcbType", "DM_GetDefaultPcbType", "WasType"),
            ("DM_SetDefaultConfigurationName",
             "DM_GetDefaultConfigurationName", "WasConfig")):
        first_read = body.index(f"{was} := Project.{getter};")
        write = body.index(f"Try Project.{setter}(", first_read)
        check = body.index(f"If Project.{getter} <> ", write)
        assert first_read < write < check, setter


def test_a_default_that_did_not_take_is_named():
    body = load(_PROJECT_PAS)["Proj_ProjectSetup"]
    assert '"default_pcb_type",' in body
    assert '"default_configuration_name",' in body


def test_the_defaults_are_not_lower_cased_on_the_way_in():
    """They are names, not flags; the flags beside them are folded."""
    body = _code(load(_PROJECT_PAS)["Proj_ProjectSetup"])
    assert "PcbTypeStr := ExtractJsonValue(Params, 'default_pcb_type');" in body
    assert ("ConfigStr := ExtractJsonValue(Params, "
            "'default_configuration_name');") in body


# --- the Python side -----------------------------------------------------

def test_the_defaults_reach_the_bridge(monkeypatch):
    bridge = _Bridge()
    asyncio.run(_tools(monkeypatch, bridge)["proj_project_setup"](
        default_pcb_type="PcbDoc", default_configuration_name="Release"))
    assert bridge.sent[0][1] == {"default_pcb_type": "PcbDoc",
                                 "default_configuration_name": "Release"}


@pytest.mark.parametrize("kwargs", [
    {"default_pcb_type": ""}, {"default_configuration_name": "   "},
])
def test_an_empty_default_leaves_the_option_alone(monkeypatch, kwargs):
    """Every option on this tool keeps its value unless named, so a caller
    changing one thing cannot blank another."""
    bridge = _Bridge()
    asyncio.run(_tools(monkeypatch, bridge)["proj_project_setup"](**kwargs))
    assert bridge.sent[0][1] == {}


def test_reading_takes_no_change_parameters(monkeypatch):
    bridge = _Bridge()
    asyncio.run(_tools(monkeypatch, bridge)["proj_project_setup"]())
    assert bridge.sent == [("project.project_setup", {})]


def test_a_relative_output_path_is_not_reported_as_not_applied():
    """MEASURED: a relative output path is resolved against the project
    folder, so the read-back is absolute and an exact compare called a
    write that worked not_applied."""
    body = load(_PROJECT_PAS)["Proj_ProjectSetup"]
    assert "GotOut := UpperCase(NoTrailingSlash(Project.DM_GetOutputPath));" in body
    assert "<> '" + chr(92) + "' + WantOut) Then" in body


def test_a_default_that_did_not_take_is_put_back():
    """MEASURED: naming a configuration the project does not have both
    failed and cleared the name already set, so a failed write left the
    project worse off than not calling at all."""
    body = _code(load(_PROJECT_PAS)["Proj_ProjectSetup"])
    for getter, setter, was in (
            ("DM_GetDefaultPcbType", "DM_SetDefaultPcbType", "WasType"),
            ("DM_GetDefaultConfigurationName",
             "DM_SetDefaultConfigurationName", "WasConfig")):
        assert f"{was} := Project.{getter};" in body
        assert f"Try Project.{setter}({was}); Except End;" in body
