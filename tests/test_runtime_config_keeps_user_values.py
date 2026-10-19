# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""A value the user set in mcp_config.json survives the next connect.

ensure_workspace() writes the file on every connect, and used to replace
it with defaults whenever the two differed, so a hand-set
auto_shutdown_ms reverted to ten minutes.
"""

from __future__ import annotations

import json

from eda_agent.config import AltiumConfig, MCPRuntimeConfig


def _cfg(tmp_path) -> AltiumConfig:
    return AltiumConfig(workspace_dir=tmp_path)


def test_a_user_value_is_kept_and_adopted(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.write_runtime_config()
    data = json.loads(cfg.config_file_path.read_text(encoding="utf-8"))
    data["auto_shutdown_ms"] = 3_600_000
    cfg.config_file_path.write_text(json.dumps(data), encoding="utf-8")

    fresh = _cfg(tmp_path)
    fresh.write_runtime_config()
    after = json.loads(fresh.config_file_path.read_text(encoding="utf-8"))
    assert after["auto_shutdown_ms"] == 3_600_000
    assert fresh.runtime.auto_shutdown_ms == 3_600_000


def test_an_invalid_value_falls_back_for_that_key_only(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.config_file_path.write_text(json.dumps(
        {"auto_shutdown_ms": "never", "idle_threshold": 7}), encoding="utf-8")
    cfg.write_runtime_config()
    after = json.loads(cfg.config_file_path.read_text(encoding="utf-8"))
    assert after["auto_shutdown_ms"] == MCPRuntimeConfig().auto_shutdown_ms
    assert after["idle_threshold"] == 7


def test_missing_keys_are_filled_and_unknown_keys_kept(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.config_file_path.write_text(json.dumps({"from_a_newer_version": 1}),
                                    encoding="utf-8")
    cfg.write_runtime_config()
    after = json.loads(cfg.config_file_path.read_text(encoding="utf-8"))
    assert after["from_a_newer_version"] == 1
    assert set(MCPRuntimeConfig().model_dump()) <= set(after)


def test_a_corrupt_file_is_rewritten_with_defaults(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.config_file_path.write_text("{not json", encoding="utf-8")
    cfg.write_runtime_config()
    after = json.loads(cfg.config_file_path.read_text(encoding="utf-8"))
    assert after == MCPRuntimeConfig().model_dump()
