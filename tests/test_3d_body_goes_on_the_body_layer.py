"""A generated 3D body goes on the library's "3D Body Top" kind layer.

lib_create_3d_model left the QPA9903 body on Mechanical1, where Altium
creates it, while the library keeps its bodies on Mechanical13 (kind
"3D Body Top") (2026-11-27).
"""
from __future__ import annotations

import asyncio

from eda_agent.tools import library


class Bridge:
    def __init__(self, layers):
        self.layers, self.sent = layers, []

    async def send_command_async(self, command, params=None, timeout=None):
        self.sent.append((command, dict(params or {})))
        if command == "library.get_mech_layers":
            return {"layers": self.layers}
        if command == "library.move_bodies_to_layer":
            return {"moved": 1}
        return {}


def test_the_body_moves_to_the_3d_body_top_layer():
    b = Bridge([{"layer": "Mechanical1", "kind": "Assembly Top"},
                {"layer": "Mechanical13", "kind": "3D Body Top"}])
    out = asyncio.run(library._move_body_to_kind_layer(b, "QFN16"))
    assert out["layer"] == "Mechanical13" and out["moved"] == 1
    assert ("library.move_bodies_to_layer",
            {"layer": "Mechanical13", "footprint_names": "QFN16"}) in b.sent


def test_a_library_without_that_kind_leaves_the_body_alone():
    b = Bridge([{"layer": "Mechanical1", "kind": "Not Set"}])
    assert asyncio.run(library._move_body_to_kind_layer(b, "QFN16")) is None
    assert not any(c == "library.move_bodies_to_layer" for c, _ in b.sent)
