# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""The router: a grid of exact clearances, searched net by net, with
nets negotiating for contested space until none share it.

The "euan" engine: Euan Mutch's version of the router, from his fork
(TechAUmNu/eda-agent, branch carrier-local at 030575e5a, 2026-10-27; its
router last changed on 2026-10-25). The files are his as they were, but
for the default router's via guards, ported in: ``Router._via_guards``,
``Router._guard_pad_vias`` and the checks fanout.py and stages.py make
with ``via_clear_of_hidden`` and ``via_leaves_web`` keep a new via off
drill holes, off copper and out of keep-outs the grid does not see
(excluded and plane layers) and clear of solder mask slivers. His gloss (gloss.py) stays here, but the routing job does not
use it: the job glosses with ours, whichever engine routed.

Off by default; ``eda-agent router-use euan`` or ``EDA_AGENT_ROUTER=euan``
turns it on (see layout/route/engine.py)."""

from .router import Router, route_adaptive, route_board

__all__ = ["Router", "route_adaptive", "route_board"]
