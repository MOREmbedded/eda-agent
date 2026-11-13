# SPDX-License-Identifier: Apache-2.0
"""Signal integrity: transmission-line models from the real stackup.

* :mod:`.materials` - laminates with the causal Djordjevic-Sarkar model,
  copper with its roughness.
* :mod:`.section` - a cross-section of one or more traces between
  reference planes, built from numbers or from the board's stackup.
* :mod:`.eddy` - the 2D eddy-current (magneto-quasistatic) solver that
  gives R(f) and L(f) with skin and proximity effect.
* :mod:`.rlgc` - the frequency-dependent RLGC matrices of a section.
* :mod:`.mtl` - S-parameters of uniform coupled lines by modal analysis.
* :mod:`.planner` - trace widths and spacings for target impedances.
"""
from __future__ import annotations
