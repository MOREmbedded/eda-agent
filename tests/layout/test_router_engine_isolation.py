# SPDX-License-Identifier: Apache-2.0
"""A developer's own engine choice never reaches the tests (conftest)."""
from __future__ import annotations

import json
import os


def test_routing_tests_use_the_default_engine_whatever_the_machine_says(tmp_path, monkeypatch):
    from eda_agent.layout.route import engine
    assert os.environ.get(engine.ENV) == "default"
    # Even with the active profile asking for Euan's router.
    (tmp_path / engine.FILE).write_text(json.dumps({"engine": "euan"}))
    monkeypatch.setattr(engine, "_profile_dir", lambda: tmp_path)
    assert engine.current() == "default"
