# SPDX-License-Identifier: Apache-2.0
"""Compiled kernels for eda-agent. See docs/RUST_COMPUTE.md in eda-agent.

``API_VERSION`` is the contract with the Python side: eda-agent refuses a
build whose version it was not written against, so a stale wheel left
behind by an update falls back (or fails loudly) instead of answering
with different semantics.
"""

from ._native import API_VERSION, astar, __version__

__all__ = ["API_VERSION", "astar", "__version__"]
