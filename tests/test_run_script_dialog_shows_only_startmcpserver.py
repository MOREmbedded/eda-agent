# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Altium's Run Script dialog lists StartMCPServer and nothing else.

The dialog offers every procedure and function that takes no parameters,
so each helper carries a Dummy parameter to stay out of it. Twelve new
parameterless helpers once filled the list a person picks StartMCPServer
from.
"""

from __future__ import annotations

import re

from tests.test_pas_constants_declared import _build_files, _code


def test_only_startmcpserver_takes_no_parameters():
    found = []
    for path in _build_files():
        code = _code(path.read_text(encoding="utf-8-sig", errors="replace"))
        found += [f"{path.name}:{name}" for name in re.findall(
            r"^\s*(?:Function|Procedure)\s+(\w+)\s*[:;]", code,
            re.MULTILINE | re.IGNORECASE)]
    assert found == ["Dispatcher.pas:StartMCPServer"], (
        f"these show in Altium's Run Script dialog: {found}. Give each a "
        f"Dummy : Integer parameter")
