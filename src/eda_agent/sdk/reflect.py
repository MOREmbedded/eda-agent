# SPDX-License-Identifier: Apache-2.0
"""Load the reflected Altium SDK into a model that answers questions.

The raw JSON is a flat map of interface name to its OWN members. That
is not what a caller wants: ISch_Component declares 46 properties of
its own, but a component in Altium also has everything
ISch_GraphicalObject and ISch_BasicContainer declare, and code reading
a component must be able to reach those too. So the model resolves
inheritance transitively and reports, for any interface, the full set a
script can actually use.

Absence is a normal state, not an error. The JSON exists only on a
machine with Altium installed and only after someone runs the
reflection script, so every entry point here says so rather than
raising something a caller has to translate.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

#: Where the reflection script writes by default, relative to the repo.
DEFAULT_SDK_JSON = Path("reference") / "altium_sdk.json"

#: Override for a JSON produced elsewhere, or for a different Altium.
SDK_JSON_ENV = "EDA_AGENT_SDK_JSON"


class SdkUnavailable(RuntimeError):
    """The reflected SDK is not on this machine.

    Carries the instruction to produce it, because every caller would
    otherwise have to repeat it.
    """

    def __init__(self, path: Path):
        super().__init__(
            f"no reflected Altium SDK at {path}.\n"
            f"Produce it on a machine with Altium installed:\n"
            f"    powershell -File scripts/altium_sdk_reflect.ps1\n"
            f"It reads Altium.SDK.Interfaces.dll from the install beside "
            f"the running X2.EXE. The output stays local: it is derived "
            f"from a non-redistributable assembly and .gitignore excludes "
            f"reference/."
        )
        self.path = path


def sdk_json_path(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve where the reflected SDK should be read from."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(SDK_JSON_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    # parents[3] is the repo root: src/eda_agent/sdk/reflect.py
    return Path(__file__).resolve().parents[3] / DEFAULT_SDK_JSON


@dataclass(frozen=True)
class Property:
    """One GetState_/SetState_ pair, as the assembly declares it.

    ``get`` and ``set`` are the .NET type names the reflection saw
    (String, Int32, Boolean, Double, Byte and the Altium structs). A
    property with ``get`` and no ``set`` is read only, and the reverse
    happens too: several PCB properties are write-only triggers.

    ``get_index``/``set_index`` are non-empty for an indexed property
    such as AliasAt, where the script must pass an index. Those cannot
    be reached by a plain name lookup, which is why the generator and
    the coverage report both treat them separately.
    """

    name: str
    get: str | None = None
    set: str | None = None
    get_index: tuple[str, ...] = ()
    set_index: tuple[str, ...] = ()

    @property
    def readable(self) -> bool:
        return self.get is not None

    @property
    def writable(self) -> bool:
        return self.set is not None

    @property
    def indexed(self) -> bool:
        return bool(self.get_index or self.set_index)


@dataclass(frozen=True)
class Method:
    """A member that is not a property accessor."""

    name: str
    ret: str
    params: tuple[tuple[str, str], ...] = ()


@dataclass
class Interface:
    """One interface, with its own members and its declared parents."""

    full_name: str
    name: str
    namespace: str
    inherits: tuple[str, ...] = ()
    properties: dict[str, Property] = field(default_factory=dict)
    methods: tuple[Method, ...] = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"<Interface {self.full_name} "
                f"{len(self.properties)}p {len(self.methods)}m>")


class SdkModel:
    """Every interface the reflection resolved, with inheritance applied."""

    def __init__(self, doc: dict):
        self.source: str = doc.get("source", "")
        self.assembly: str = doc.get("assembly", "")
        self.file_version: str = doc.get("file_version", "")
        self.reflected_at: str = doc.get("reflected_at", "")
        #: Members the reflection could not read, so a consumer knows the
        #: picture is complete rather than assuming it.
        self.skipped_methods: int = int(doc.get("skipped_methods", 0) or 0)
        self.skipped_types: int = int(doc.get("skipped_types", 0) or 0)

        self.interfaces: dict[str, Interface] = {}
        for full_name, raw in (doc.get("interfaces") or {}).items():
            props = {}
            for pname, praw in (raw.get("properties") or {}).items():
                props[pname] = Property(
                    name=pname,
                    get=praw.get("get"),
                    set=praw.get("set"),
                    get_index=tuple(praw.get("get_index") or ()),
                    set_index=tuple(praw.get("set_index") or ()),
                )
            methods = tuple(
                Method(
                    name=m.get("name", ""),
                    ret=m.get("ret", ""),
                    params=tuple((p.get("name", ""), p.get("type", ""))
                                 for p in (m.get("params") or [])),
                )
                for m in (raw.get("methods") or [])
            )
            self.interfaces[full_name] = Interface(
                full_name=full_name,
                name=raw.get("name", full_name.rsplit(".", 1)[-1]),
                namespace=raw.get("namespace") or "",
                inherits=tuple(raw.get("inherits") or ()),
                properties=props,
                methods=methods,
            )

        # Short name to full name, for callers that say "ISch_Component".
        # A short name can collide across namespaces; first one wins and
        # the full name is always accepted, so a collision costs nothing
        # to a caller that qualifies.
        self._by_short: dict[str, str] = {}
        for full_name, iface in self.interfaces.items():
            self._by_short.setdefault(iface.name, full_name)

    # -- lookup --------------------------------------------------------

    def __len__(self) -> int:
        return len(self.interfaces)

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def get(self, name: str) -> Interface | None:
        """Find an interface by full name or by short name."""
        if name in self.interfaces:
            return self.interfaces[name]
        full = self._by_short.get(name)
        return self.interfaces.get(full) if full else None

    def in_namespace(self, namespace: str) -> list[Interface]:
        return [i for i in self.interfaces.values()
                if i.namespace == namespace]

    # -- inheritance ---------------------------------------------------

    def ancestors(self, name: str) -> list[Interface]:
        """Every interface ``name`` inherits from, transitively.

        Breadth first from the declared parents, and cycle safe: the
        assembly is generated, but a model that loops on bad input is a
        hang rather than a message.
        """
        start = self.get(name)
        if start is None:
            return []
        seen: set[str] = {start.full_name}
        order: list[Interface] = []
        queue = list(start.inherits)
        while queue:
            parent_name = queue.pop(0)
            if parent_name in seen:
                continue
            seen.add(parent_name)
            parent = self.get(parent_name)
            if parent is None:
                continue
            order.append(parent)
            queue.extend(parent.inherits)
        return order

    def effective_properties(self, name: str) -> dict[str, Property]:
        """Everything a script can reach on ``name``, parents included.

        A property an interface declares itself wins over an inherited
        one of the same name: that is what the compiler resolves to.
        """
        iface = self.get(name)
        if iface is None:
            return {}
        merged: dict[str, Property] = {}
        # Furthest ancestor first, so nearer declarations overwrite it.
        for parent in reversed(self.ancestors(name)):
            merged.update(parent.properties)
        merged.update(iface.properties)
        return merged

    def iter_properties(self) -> Iterator[tuple[Interface, Property]]:
        """Every own property of every interface, once each."""
        for iface in self.interfaces.values():
            for prop in iface.properties.values():
                yield iface, prop


def load_sdk(path: str | os.PathLike | None = None) -> SdkModel:
    """Read the reflected SDK, or say how to produce it."""
    resolved = sdk_json_path(path)
    if not resolved.is_file():
        raise SdkUnavailable(resolved)
    # PowerShell's Out-File -Encoding utf8 writes a BOM on Windows
    # PowerShell, and json.loads chokes on it.
    text = resolved.read_text(encoding="utf-8-sig")
    return SdkModel(json.loads(text))
