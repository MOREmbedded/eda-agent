# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The router: a grid of exact clearances, searched net by net, with
nets negotiating for contested space until none share it.

The "euan" engine: Euan Mutch's version of the router, from his fork
(TechAUmNu/eda-agent, branch carrier-local, 2026-10-18). Off by default;
``eda-agent router-use euan`` or ``EDA_AGENT_ROUTER=euan`` turns it on
(see layout/route/engine.py)."""

from .router import Router, route_adaptive, route_board

__all__ = ["Router", "route_adaptive", "route_board"]
