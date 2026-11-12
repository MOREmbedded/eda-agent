# SPDX-License-Identifier: Apache-2.0
"""EMI/EMC pre-scan and the bridge to external field solvers.

* ``stack``: the copper layers of a board with their heights and the
  dielectric between them, from ``pcb_get_layer_stackup``.
* ``prescan``: fast rule checks on a ``LayoutBoard`` (reference plane
  gaps, fast nets near the board edge, layer changes without a return
  via) and a radiated-emission estimate per net.
* ``emission``: the emission arithmetic and the CISPR limit lines.
* ``touchstone``: read and write Touchstone (.sNp) files.
* ``openems``: build an openEMS model of a few nets, run it as a separate
  process and read its results back.

openEMS is GPL-3. Nothing of it is copied here: this package writes a
plain Python script that imports openEMS in its own interpreter, and only
reads the files that run leaves behind.
"""
