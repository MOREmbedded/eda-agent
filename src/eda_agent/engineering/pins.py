"""Pin table import and validation.

Input is a CSV pin table with header: number,name,type[,side][,description]
The table is produced from the datasheet either by a human, by a model, or by
optional PDF table extraction (pdfplumber, see extract_tables_from_pdf). In all
cases it is treated as UNTRUSTED until validated here and approved by a human
against the cited datasheet page.

Electrical types map one-to-one to Altium's TPinElectrical enumeration
(eElectricInput, eElectricIO, eElectricOutput, eElectricOpenCollector,
eElectricPassive, eElectricHiZ, eElectricOpenEmitter, eElectricPower)."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import asdict, dataclass

from .errors import ValidationFailed

ELECTRICAL_TYPES = {
    "input": "eElectricInput", "io": "eElectricIO", "output": "eElectricOutput",
    "open_collector": "eElectricOpenCollector", "passive": "eElectricPassive",
    "hiz": "eElectricHiZ", "open_emitter": "eElectricOpenEmitter", "power": "eElectricPower",
}
SIDES = ("left", "right", "top", "bottom", "auto")
PIN_NUMBER = re.compile(r"[A-Z]{0,2}[0-9]{1,4}|EP|TAB")
PIN_NAME = re.compile(r"[A-Za-z0-9_+\-/#~.]{1,40}")
POWER_NAMES = re.compile(r"^(V(CC|DD|IN|OUT|BAT|REF|S)\w*|GND\w*|VSS\w*|AGND|DGND|PGND)$", re.I)


@dataclass
class Pin:
    number: str
    name: str
    type: str
    side: str = "auto"
    description: str = ""


def parse_pin_table(csv_text: str) -> list[Pin]:
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    need = {"number", "name", "type"}
    if not reader.fieldnames or not need.issubset({f.strip().lower() for f in reader.fieldnames}):
        raise ValidationFailed("pin table must have columns number,name,type[,side][,description]")
    pins = []
    for i, row in enumerate(reader, start=2):
        r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        pins.append(Pin(number=r["number"], name=r["name"], type=r["type"].lower(),
                        side=(r.get("side") or "auto").lower(), description=r.get("description", "")))
    return pins


def validate_pins(pins: list[Pin], expected_count: int | None = None) -> list[str]:
    """Raise on errors; return a list of warnings for human review."""
    errors, warnings = [], []
    if not pins:
        errors.append("pin table is empty")
    seen = {}
    for p in pins:
        if not PIN_NUMBER.fullmatch(p.number):
            errors.append(f"pin {p.number!r}: invalid pin number")
        if not PIN_NAME.fullmatch(p.name):
            errors.append(f"pin {p.number}: invalid pin name {p.name!r}")
        if p.type not in ELECTRICAL_TYPES:
            errors.append(f"pin {p.number}: type {p.type!r} not in {sorted(ELECTRICAL_TYPES)}")
        if p.side not in SIDES:
            errors.append(f"pin {p.number}: side {p.side!r} not in {SIDES}")
        if p.number in seen:
            errors.append(f"duplicate pin number {p.number}")
        seen[p.number] = p
        if POWER_NAMES.match(p.name) and p.type not in ("power", "passive", "output"):
            warnings.append(f"pin {p.number} {p.name}: looks like a supply pin but type is {p.type}")
        if p.type == "power" and not POWER_NAMES.match(p.name):
            warnings.append(f"pin {p.number} {p.name}: typed power but name is not a recognised supply name")
    if expected_count is not None and len(pins) != expected_count:
        errors.append(f"pin count {len(pins)} != package pin count {expected_count}")
    names = [p.name for p in pins]
    for n in set(names):
        if names.count(n) > 1 and not POWER_NAMES.match(n) and n.upper() not in ("NC", "DNC"):
            warnings.append(f"signal name {n} appears on {names.count(n)} pins")
    if errors:
        raise ValidationFailed("pin table invalid", details={"errors": errors})
    return warnings


def pins_to_dicts(pins: list[Pin]) -> list[dict]:
    return [asdict(p) for p in pins]


def extract_tables_from_pdf(pdf_path: str, pages: list[int]) -> list[list[list[str]]]:  # pragma: no cover
    """Optional helper (requires pdfplumber). Returns raw tables for a human or
    model to convert to the CSV format above. It never auto-approves data."""
    import pdfplumber  # optional dependency
    out = []
    with pdfplumber.open(pdf_path) as pdf:
        for n in pages:
            out.extend(pdf.pages[n - 1].extract_tables())
    return out
