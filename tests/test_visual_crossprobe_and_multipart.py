# SPDX-License-Identifier: Apache-2.0
"""Offline coverage for visual cross-probe and multipart symbol composition."""

from __future__ import annotations

import pytest


class _Mcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


class _Bridge:
    def __init__(self):
        self.calls = []

    async def send_command_async(self, command, params=None, timeout=None):
        self.calls.append((command, params or {}))
        return {"success": True, "command": command}


@pytest.mark.asyncio
async def test_visual_cross_probe_captures_result_without_live_altium(monkeypatch):
    from eda_agent.bridge import windows_ui
    from eda_agent.tools import project

    bridge = _Bridge()

    class Inspector:
        def main_window_handle(self):
            return 101

        def capture_window(self, handle, path):
            return {"handle": handle, "path": path, "width": 1200}

        def list_dialogs(self):
            return [{"title": "Compiler", "handle": 202}]

    monkeypatch.setattr(project, "get_bridge", lambda: bridge)
    monkeypatch.setattr(
        windows_ui, "get_altium_ui_inspector", lambda candidate: Inspector()
    )
    mcp = _Mcp()
    project.register_project_tools(mcp)

    result = await mcp.tools["proj_visual_cross_probe"](
        "U7", "D:/captures/U7.bmp", target="pcb", settle_ms=0
    )

    assert bridge.calls == [
        ("project.cross_probe", {"designator": "U7", "target": "pcb"})
    ]
    assert result["capture"]["handle"] == 101
    assert result["dialogs"][0]["handle"] == 202


@pytest.mark.asyncio
async def test_multipart_symbol_assigns_parts_and_shared_pins(monkeypatch):
    """Shared pins sit on owner part 0, so Altium shows them on every part.

    The shape is upstream's: an object of ``left_pins``/``right_pins``
    placed under each part's own pins, rather than this fork's earlier
    flat list of pins carrying their own x and y.
    """
    from eda_agent.tools import library

    bridge = _Bridge()
    monkeypatch.setattr(library, 'get_bridge', lambda: bridge)
    # No datasheet here: the pin-table check is switched off, as the
    # Config tab would, so the composition itself is what is tested.
    from eda_agent import preferences
    monkeypatch.setattr(preferences, '_read',
                        lambda workspace=None: {'checks_off': 'symbol_pins'})
    mcp = _Mcp()
    library.register_library_tools(mcp)

    result = await mcp.tools['lib_create_symbol_in_parts'](
        'DUAL_AMP',
        parts=[
            {'left_pins': [{'designator': '2', 'name': 'INA'}],
             'right_pins': [{'designator': '1', 'name': 'OUTA'}]},
            {'left_pins': [{'designator': '6', 'name': 'INB'}],
             'right_pins': [{'designator': '7', 'name': 'OUTB'}]},
        ],
        shared_pins={'left_pins': [{'designator': '8', 'name': 'V+',
                                    'electrical_type': 'power'}]},
    )

    assert result['part_count'] == 2 and result['shared'] == ['8']
    pins = next(p['pins'] for c, p in bridge.calls if c == 'library.add_pins')
    ops = pins.split('~~')
    assert len(ops) == 5
    shared = [op for op in ops if 'designator=8;' in op]
    assert shared and 'owner_part_id=0' in shared[0]
    assert 'electrical_type=power' in shared[0]
    assert 'owner_part_id=1' in pins and 'owner_part_id=2' in pins
    # Each body names its own part, so no active-part switching is needed.
    bodies = [p['owner_part_id'] for c, p in bridge.calls
              if c == 'library.add_symbol_rectangle']
    assert bodies == ['1', '2']


@pytest.mark.asyncio
async def test_multipart_validates_before_mutating(monkeypatch):
    from eda_agent.tools import library

    bridge = _Bridge()
    monkeypatch.setattr(library, 'get_bridge', lambda: bridge)
    # No datasheet here: the pin-table check is switched off, as the
    # Config tab would, so the composition itself is what is tested.
    from eda_agent import preferences
    monkeypatch.setattr(preferences, '_read',
                        lambda workspace=None: {'checks_off': 'symbol_pins'})
    mcp = _Mcp()
    library.register_library_tools(mcp)
    make = mcp.tools['lib_create_symbol_in_parts']
    two = [{'left_pins': [{'designator': '1', 'name': 'A'}]},
           {'left_pins': [{'designator': '2', 'name': 'B'}]}]

    # The flat list this fork once took is refused by shape, so a caller
    # written against it is told what to send rather than half-served.
    old = await make('BAD', parts=two, shared_pins=[{'designator': '8', 'name': 'V+'}])
    assert old == {'ok': False,
                   'reason': 'shared_pins is an object with left_pins and right_pins'}
    clash = await make('BAD', parts=two,
                       shared_pins={'left_pins': [{'designator': '2', 'name': 'V+'}]})
    assert clash == {'ok': False, 'reason': 'part 2: pin 2 is also shared'}
    blank = await make('BAD', parts=two, shared_pins={'left_pins': [{'name': 'GND'}]})
    assert not blank['ok'] and 'non-empty' in blank['reason']
    twice = await make('BAD', parts=two, shared_pins={
        'left_pins': [{'designator': '8', 'name': 'V+'}],
        'right_pins': [{'designator': '8', 'name': 'V-'}]})
    assert twice == {'ok': False, 'reason': 'shared pin given twice: 8'}
    assert bridge.calls == []
