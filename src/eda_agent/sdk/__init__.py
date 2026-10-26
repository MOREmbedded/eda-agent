# SPDX-License-Identifier: Apache-2.0
"""Altium's own SDK declarations, read as data.

``Altium.SDK.Interfaces.dll`` ships with every Altium Designer install
and declares every interface the scripting engine exposes: the ISch_*,
IPCB_*, IDXP_*, EDP and ComponentLib families. Delphi properties cross
the COM boundary as ``GetState_<Name>`` / ``SetState_<Name>`` methods,
so the assembly states, for each property, its exact type and whether it
is readable, writable or both.

That makes it the ground truth the Pascal property chains were being
hand-written against. ``scripts/altium_sdk_reflect.ps1`` turns it into
JSON under ``reference/``; this package reads that JSON, measures what
the chains actually cover, and generates the rest.

The JSON is derived from a non-redistributable assembly and is excluded
by .gitignore. Nothing here embeds it: these modules read whatever the
local install produced, and report plainly when it is absent.
"""

from .reflect import Interface, Property, SdkModel, load_sdk, sdk_json_path

__all__ = ["Interface", "Property", "SdkModel", "load_sdk", "sdk_json_path"]
