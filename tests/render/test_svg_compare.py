# SPDX-License-Identifier: Apache-2.0
from pathlib import Path
from xml.etree import ElementTree as ET

from eda_agent.render.svg_compare import compare_svg_files


def test_compare_svg_is_self_contained_and_reports_change(tmp_path: Path):
    before = tmp_path / "before.svg"
    after = tmp_path / "after.svg"
    output = tmp_path / "compare.svg"
    before.write_text(
        '<svg viewBox="0 0 100 50"><g data-designator="U1"/></svg>',
        encoding="utf-8",
    )
    after.write_text(
        '<svg viewBox="0 0 120 60"><g data-designator="U1"/>'
        '<g data-net="GND"/></svg>', encoding="utf-8",
    )
    result = compare_svg_files(before, after, output)
    ET.parse(output)
    assert result["changed"] is True
    assert result["before_structured_groups"] == 1
    assert result["after_structured_groups"] == 2
    assert "data:image/svg+xml;base64," in output.read_text(encoding="utf-8")
