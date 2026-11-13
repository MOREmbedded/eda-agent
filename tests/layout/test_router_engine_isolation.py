# SPDX-License-Identifier: Apache-2.0
"""A developer's own engine choice never reaches the tests (conftest)."""
from __future__ import annotations

import json
import os


def test_routing_tests_use_the_default_engine_whatever_the_machine_says(tmp_path, monkeypatch):
    from eda_agent.design import live
    from eda_agent.layout.model import Layer, LayoutBoard, Pad, PadCopper
    from eda_agent.layout.route import engine
    from eda_agent.layout.route.job import route_job
    assert os.environ.get(engine.ENV) == "default"
    # Even with the active profile asking for Euan's router.
    (tmp_path / engine.FILE).write_text(json.dumps({"engine": "euan"}))
    monkeypatch.setattr(engine, "_profile_dir", lambda: tmp_path)
    assert engine.describe()["engine"] == "default"
    monkeypatch.setattr(live, "publish_safe", lambda *a, **k: None)
    b = LayoutBoard(name="r", outline=[(0, 0), (300, 0), (300, 200), (0, 200)],
                    layers=[Layer("TopLayer", "signal", 0)])
    b.pads = [Pad(c, "1", x, 100, net="A", copper=[PadCopper("TopLayer", "rect", 20, 20)])
              for c, x in (("R1", 50), ("R2", 250))]
    assert route_job({"board": b})["router"] == "default"
