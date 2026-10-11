# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""SchDoc coordinates keep their ``_Frac`` part.

The reader used to drop it, so an off-grid pin end landed short of the
wire drawn to it and one net came back as two. A sheet drawn on the grid
has no fractions and must read exactly as it did, as plain ints. The
readers are fed records directly, so no binary fixture is needed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from eda_agent.fileio import altium_sch
from eda_agent.fileio.altium_sch import (
    _coord,
    read_schematic_junctions,
    read_schematic_net_label_locations,
    read_schematic_pins,
    read_schematic_power_ports,
    read_schematic_wires,
)


@pytest.fixture
def records(monkeypatch):
    def use(rows):
        monkeypatch.setattr(altium_sch, "read_schdoc_records", lambda _: rows)
    return use


@pytest.mark.parametrize("rec, value", [
    ({}, None),
    ({"X1": "n/a"}, None),
    ({"X1": "840"}, 840),
    ({"X1": "840", "X1_Frac": "0"}, 840),
    ({"X1": "840", "X1_Frac": ""}, 840),
    ({"X1": "840", "X1_Frac": "30000"}, Decimal("840.3")),
    ({"X1": "840", "X1_FRAC": "30000"}, Decimal("840.3")),
    # The fraction is added to a negative value too, not subtracted.
    ({"X1": "-841", "X1_Frac": "70000"}, Decimal("-840.3")),
    ({"X1": "-840", "X1_Frac": "30000"}, Decimal("-839.7")),
])
def test_a_coordinate_is_its_whole_part_plus_its_fraction(rec, value):
    got = _coord(rec, "X1")
    assert got == value
    assert not isinstance(got, float)
    if isinstance(value, int):
        assert type(got) is int, "a whole value must stay an int"


def test_a_whole_value_and_an_equal_fraction_are_one_point():
    whole = _coord({"X1": "840"}, "X1")
    carried = _coord({"X1": "839", "X1_Frac": "100000"}, "X1")
    assert len({(whole, 0), (carried, 0)}) == 1


ON_GRID = [
    {"RECORD": "2", "OwnerIndex": "3", "Designator": "1", "Name": "VIN",
     "Location.X": "100", "Location.Y": "200", "PinLength": "10",
     "PinConglomerate": "2"},
    {"RECORD": "27", "LocationCount": "2",
     "X1": "90", "Y1": "200", "X2": "150", "Y2": "200"},
    {"RECORD": "25", "Text": "VBUS", "Location.X": "120", "Location.Y": "200"},
    {"RECORD": "17", "Text": "GND", "Location.X": "150", "Location.Y": "200",
     "Orientation": "1"},
    {"RECORD": "29", "Location.X": "120", "Location.Y": "200"},
]


def test_a_sheet_on_the_grid_reads_as_plain_ints(records):
    records(ON_GRID)
    pin = read_schematic_pins("x")[0]
    wire = read_schematic_wires("x")[0]
    label = read_schematic_net_label_locations("x")[0]
    port = read_schematic_power_ports("x")[0]
    junction = read_schematic_junctions("x")[0]
    assert (pin["x"], pin["y"], pin["length"]) == (100, 200, 10)
    assert wire == {"x1": 90, "y1": 200, "x2": 150, "y2": 200}
    assert (label["x"], label["y"]) == (120, 200)
    assert (port["x"], port["y"]) == (150, 200)
    assert junction == {"x": 120, "y": 200}
    values = [pin["x"], pin["y"], pin["length"], *wire.values(), label["x"],
              label["y"], port["x"], port["y"], *junction.values()]
    assert all(type(v) is int for v in values)


def test_every_reader_keeps_the_fraction(records):
    records([
        {"RECORD": "2", "OwnerIndex": "3", "Designator": "1",
         "Location.X": "100", "Location.X_Frac": "30000",
         "Location.Y": "200", "PinLength": "10", "PinLength_Frac": "70000",
         "PinConglomerate": "0"},
        {"RECORD": "27", "LocationCount": "2", "X1": "111", "Y1": "200",
         "Y1_Frac": "50000", "X2": "150", "Y2": "200", "Y2_Frac": "50000"},
        {"RECORD": "25", "Text": "VBUS", "Location.X": "120",
         "Location.X_Frac": "25000", "Location.Y": "200"},
        {"RECORD": "17", "Text": "GND", "Location.X": "150",
         "Location.Y": "200", "Location.Y_Frac": "50000"},
        {"RECORD": "29", "Location.X": "120", "Location.X_Frac": "25000",
         "Location.Y": "200"},
    ])
    pin = read_schematic_pins("x")[0]
    assert (pin["x"], pin["length"]) == (Decimal("100.3"), Decimal("10.7"))
    wire = read_schematic_wires("x")[0]
    assert (wire["y1"], wire["y2"]) == (Decimal("200.5"), Decimal("200.5"))
    assert read_schematic_net_label_locations("x")[0]["x"] == Decimal("120.25")
    assert read_schematic_power_ports("x")[0]["y"] == Decimal("200.5")
    assert read_schematic_junctions("x")[0]["x"] == Decimal("120.25")


def test_an_off_grid_pin_end_meets_its_wire(records):
    """Anchor 100.3 plus length 10.7 ends at 111, where the wire starts.

    Without the fractions it ended at 110, a unit short, and the two pins
    came back on separate nets.
    """
    from eda_agent.fileio.netlist_solver import pin_electrical_end, solve_nets

    records([
        {"RECORD": "2", "OwnerIndex": "0", "Designator": "1",
         "Location.X": "100", "Location.X_Frac": "30000",
         "Location.Y": "200", "PinLength": "10", "PinLength_Frac": "70000",
         "PinConglomerate": "0"},
        {"RECORD": "2", "OwnerIndex": "1", "Designator": "2",
         "Location.X": "160", "Location.X_Frac": "60000",
         "Location.Y": "200", "PinLength": "9", "PinLength_Frac": "40000",
         "PinConglomerate": "2"},
        {"RECORD": "27", "LocationCount": "2", "X1": "111", "Y1": "200",
         "X2": "151", "Y2": "200", "X2_Frac": "20000"},
        {"RECORD": "17", "Text": "VTEST", "Location.X": "111",
         "Location.Y": "200"},
    ])
    pins = []
    for p in read_schematic_pins("x"):
        x, y = pin_electrical_end(p["x"], p["y"], p["length"],
                                  p["orientation"])
        pins.append({"component": f"U{p['owner_index']}",
                     "pin": p["designator"], "x": x, "y": y})
    assert [(p["x"], p["y"]) for p in pins] == [
        (Decimal("111"), 200), (Decimal("151.2"), 200)]

    solved = solve_nets(pins, read_schematic_wires("x"),
                        read_schematic_power_ports("x"), [], [])
    assert set(solved["pin_nets"].values()) == {"VTEST"}
    assert len(solved["nets"]["VTEST"]) == 2
