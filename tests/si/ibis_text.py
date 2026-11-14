# SPDX-License-Identifier: Apache-2.0
"""A small IBIS file for the tests: a 1.8 V driver of 40 ohm with a
60 ps 20-80 % edge and 0.6 pF, and an input of 0.4 pF."""

R_OUT = 40.0
VCC = 1.8


def _table(r: float) -> str:
    rows = []
    for k in range(-10, 21):
        v = k * VCC / 10
        rows.append(f"{v:8.3f}  {v / r:.6e}  {v / (r * 1.2):.6e}  {v / (r * 0.8):.6e}")
    return "\n".join(rows)


TEXT = f"""[IBIS Ver]   5.0
[File Name]  test.ibs
[Component]  TESTPART
[Manufacturer] none
[Package]
| typ min max
R_pkg  0.1  0.08  0.12
L_pkg  1nH  0.8nH  1.2nH
C_pkg  0.2pF 0.15pF 0.25pF
[Pin] signal_name model_name
1  DQ   DRV18
2  DIN  RX18
|
[Model]  DRV18
Model_type  Output
C_comp  0.6pF  0.5pF  0.7pF
[Voltage Range]  {VCC}  1.7  1.9
[Pulldown]
| V  I(typ)  I(min)  I(max)
{_table(R_OUT)}
[Pullup]
{_table(R_OUT)}
[Ramp]
| typ min max
dV/dt_r  1.08/60p  1.0/70p  1.2/50p
dV/dt_f  1.08/62p  1.0/72p  1.2/52p
R_load = 50
|
[Model]  RX18
Model_type  Input
C_comp  0.4pF  0.3pF  0.5pF
[Voltage Range]  {VCC}  1.7  1.9
[End]
"""


def _real(r: float, weak: float = 1.0) -> str:
    """A buffer's V-I table as vendors write it: a clamp diode below the
    rail (steep), the transistor above it (``r`` ohm near the rail,
    saturating further up). ``weak`` scales the transistor's current."""
    rows = []
    for k in range(-10, 21):
        v = k * VCC / 10
        if v < 0:
            i = v / 0.5                          # the diode: 0.5 ohm
        else:
            i = weak * (VCC / r) * (1 - (1 - min(v, VCC) / VCC) ** 2) / 2
        rows.append(f"{v:8.3f}  {i:.6e}  NA  NA")
    return "\n".join(rows)


def _clamp(r: float) -> str:
    """A clamp table that carries an on-die termination of ``r`` ohm."""
    return "\n".join(f"{k * VCC / 10:8.3f}  {k * VCC / 10 / r:.6e}  NA  NA"
                     for k in range(-10, 21))


#: Buffers shaped like vendor files: DRV_REAL (about 40 ohm near its rails,
#: clamp diodes below them), DRV_NPU (a pull-up that barely conducts near
#: its rail), RX_ODT (100 ohm to each rail: 50 ohm) and RX_HIZ (diodes only).
REAL = f"""[IBIS Ver]   4.2
[File Name]  real.ibs
[Component]  REALPART
[Manufacturer] none
[Model]  DRV_REAL
Model_type  I/O
C_comp  1.2pF  NA  NA
[Voltage Range]  {VCC}  1.7  1.9
[Pulldown]
{_real(R_OUT)}
[Pullup]
{_real(R_OUT)}
[GND Clamp]
{_real(1e9)}
[Ramp]
dV/dt_r  1.08/60p  NA  NA
dV/dt_f  1.08/62p  NA  NA
[Model]  DRV_NPU
Model_type  3-state
C_comp  1.2pF  NA  NA
[Voltage Range]  {VCC}  1.7  1.9
[Pulldown]
{_real(R_OUT)}
[Pullup]
{_real(R_OUT, weak=1e-4)}
[Model]  RX_ODT
Model_type  Input
C_comp  0.5pF  NA  NA
[Voltage Range]  {VCC}  1.7  1.9
[GND Clamp]
{_clamp(100.0)}
[POWER Clamp]
{_clamp(100.0)}
[Model]  RX_HIZ
Model_type  Input
C_comp  0.5pF  NA  NA
[Voltage Range]  {VCC}  1.7  1.9
[GND Clamp]
{_real(1e9)}
[End]
"""
