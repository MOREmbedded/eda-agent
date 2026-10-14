# SPDX-License-Identifier: Apache-2.0
"""LCSC reel/bag label QR payload -> part identity and quantity.

LCSC prints a QR code on each reel and bag whose decoded text is a
brace-wrapped list of ``key:value`` pairs, measured from a real label::

    {pbn:PICK2403120102,on:GB2403120671,pc:C916399,pm:JSM2301S,qty:50,
     mc:,cc:1,pdi:107160244,hp:0,wc:ZH}

Only three keys have a documented meaning, and the source for even those
is community documentation rather than anything LCSC publishes:

    pc    LCSC part number, always starting with "C"
    pm    manufacturer part number
    qty   number of parts in the reel or bag

pbn, on, mc, cc, pdi, hp and wc appear in real payloads with no published
meaning. They are returned verbatim under ``fields`` and NOT given
invented names: a guessed label ("wc is the warehouse") would be
indistinguishable from a known one to the caller, which is how a guess
becomes a fact nobody can trace.

This module only decodes text that something else has already read from
the QR code (a handheld scanner types it, or an image decoder produces
it). It does no image processing.
"""

from __future__ import annotations

from typing import Any

#: The keys whose meaning is documented. Everything else is passed through.
_DOCUMENTED = ("pc", "pm", "qty")


def parse_lcsc_barcode(text: str) -> dict[str, Any]:
    """Decode an LCSC label payload.

    Returns ``{ok, lcsc_part, mpn, quantity, fields, warnings}``. A payload
    that does not parse returns ``ok=False`` with a reason rather than
    raising: this is scanner input, and a mis-scan is ordinary.

    ``quantity`` is None when qty is absent or not a whole number, never 0
    -- "the label did not say" and "the bag is empty" are different facts.
    """
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "reason": "empty barcode payload"}

    payload = text.strip()
    # Tolerate a payload that arrives without its braces: some scanners are
    # configured to strip them, and the content is unambiguous either way.
    if payload.startswith("{") and payload.endswith("}"):
        payload = payload[1:-1]
    elif "{" in payload or "}" in payload:
        return {"ok": False,
                "reason": "payload has a brace but is not wrapped in {...}; "
                          "it may be truncated"}

    fields: dict[str, str] = {}
    for part in payload.split(","):
        if not part.strip():
            continue
        if ":" not in part:
            return {"ok": False,
                    "reason": f"segment {part.strip()!r} is not key:value"}
        key, value = part.split(":", 1)
        key = key.strip()
        if key:
            # Empty values are normal: a real label carries "mc:".
            fields[key] = value.strip()

    if not fields:
        return {"ok": False, "reason": "no key:value pairs in payload"}

    warnings: list[str] = []
    lcsc_part = fields.get("pc", "")
    if not lcsc_part:
        warnings.append("no 'pc' field: this may not be an LCSC label")
    elif not lcsc_part.upper().startswith("C"):
        warnings.append(
            f"LCSC part {lcsc_part!r} does not start with 'C', which every "
            f"documented LCSC part number does")

    quantity = None
    raw_qty = fields.get("qty", "")
    if raw_qty:
        try:
            quantity = int(raw_qty)
        except ValueError:
            warnings.append(f"quantity {raw_qty!r} is not a whole number")

    return {
        "ok": True,
        "lcsc_part": lcsc_part,
        "mpn": fields.get("pm", ""),
        "quantity": quantity,
        "fields": fields,
        "undocumented_fields": sorted(k for k in fields if k not in _DOCUMENTED),
        "warnings": warnings,
    }
