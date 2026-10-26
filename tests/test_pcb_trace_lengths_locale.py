# SPDX-License-Identifier: Apache-2.0
from pathlib import Path


PCB_SOURCE = (
    Path(__file__).resolve().parent.parent / "scripts" / "altium" / "PCB.pas"
)


def test_trace_length_accumulator_uses_locale_independent_strings() -> None:
    """Intermediate lengths must round-trip under comma-decimal locales."""
    source = PCB_SOURCE.read_text(encoding="utf-8")
    start = source.index("Function PCB_GetTraceLengths")
    end = source.index("Function PCB_GetNetRouting", start)
    body = source[start:end]

    assert "NetLengthStrs[FoundIdx] := FloatToJsonStr(Accum);" in body
    assert "NetLengthStrs.Add(FloatToJsonStr(SegLen));" in body
    assert "NetLengthStrs[FoundIdx] := FloatToStr(Accum);" not in body
    assert "NetLengthStrs.Add(FloatToStr(SegLen));" not in body
