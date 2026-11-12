# SPDX-License-Identifier: Apache-2.0
"""IPC standards catalogue and rule database: definitions here, values per profile.

IPC standards are sold per copy, so this public repository ships only what
is ours to ship:

- ``standards_data/catalogue.json``: which IPC documents matter for PCB and
  PCBA work, their current revision (``revision_confirmed`` says whether it
  was checked on an IPC or ANSI page, and when), and their scope in our own
  words.
- ``standards_data/rules.json``: rule DEFINITIONS (an id, a category, the
  kind of limit, its unit, what it is keyed by, which standards govern it,
  which check uses it). No numbers.

The numbers come from the user's licensed copy and live in the active
profile, in ``<profile dir>/standards/values.json``. Every value records
where it came from (:class:`Provenance`): the licensed document and clause,
a public secondary source with its quote, URL and date, a derivation, or an
explicit user override. A rule without a value is reported as not
configured, never filled in: the same rule ``fab_profile`` and
``fab_library`` follow for fab numbers.

``class_floors`` turns the configured values into the per-class floor file
the fab rule builder reads (``<profile dir>/standards/ipc_class_floors.json``),
so the class floor and the fab minimum meet in one place.

All lengths are MILS, as everywhere else here. Pure offline, no Altium.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_DATA = Path(__file__).with_name("standards_data")

#: Where a profile keeps its standards data, relative to the profile directory.
STANDARDS_DIR = Path("standards")
VALUES_FILE = STANDARDS_DIR / "values.json"
CLASS_FLOORS_FILE = STANDARDS_DIR / "ipc_class_floors.json"

Domain = Literal["design", "fabrication", "materials", "assembly", "data"]
Kind = Literal["min", "max", "enum", "info"]
Unit = Literal["mils", "um", "percent", "ratio", "enum"]

#: The keys a rule may be keyed by, with the values each accepts (None = any
#: non-empty string).
KEY_DOMAINS: dict[str, Optional[tuple[str, ...]]] = {
    "class": ("1", "2", "3"),
    "producibility_level": ("A", "B", "C"),
    "density_level": ("A", "B", "C"),
    "layer": ("external", "internal"),
    "environment": None,
    "voltage_band": None,
    "via_use": None,
    "smt_present": ("yes", "no"),
}


class Standard(BaseModel):
    """One IPC document as catalogued: metadata and our own scope summary."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=3)
    revision: Optional[str] = None
    year: Optional[int] = Field(default=None, ge=1950, le=2100)
    revision_confirmed: bool
    title: str = Field(min_length=3)
    domain: Domain
    priority: Literal[1, 2, 3]
    scope: str = Field(min_length=3)
    use: str = Field(min_length=3)

    @property
    def label(self) -> str:
        """``IPC-2221C-2023`` style, with what is known."""
        out = self.id + (self.revision or "")
        return f"{out}-{self.year}" if self.year else out


class Catalogue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: int = Field(alias="schema")
    checked_on: str
    checked_against: list[str]
    standards: list[Standard]

    @model_validator(mode="after")
    def _unique(self) -> "Catalogue":
        ids = [s.id for s in self.standards]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate standards: {dupes}")
        return self

    def get(self, std_id: str) -> Optional[Standard]:
        return next((s for s in self.standards if s.id == std_id), None)


class Rule(BaseModel):
    """A rule definition. Carries no value, by construction (extra=forbid)."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(pattern=r"^ipc(\.[a-z0-9_]+)+$")
    title: str
    category: str
    kind: Kind
    unit: Unit
    keyed_by: list[str]
    standards: list[str] = Field(min_length=1)
    #: The fab rule builder's key (design.fab_rules.RULE_KEYS) this rule
    #: floors, when it has one.
    fab_key: Optional[str] = None
    #: Which existing code applies the rule, or None when nothing does yet.
    check: Optional[str] = None

    @model_validator(mode="after")
    def _keys_known(self) -> "Rule":
        unknown = [k for k in self.keyed_by if k not in KEY_DOMAINS]
        if unknown:
            raise ValueError(f"{self.rule_id}: unknown keys {unknown}")
        if self.fab_key and self.unit not in ("mils", "ratio"):
            raise ValueError(f"{self.rule_id}: a fab floor must be in mils or a ratio")
        return self


class RuleBook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: int = Field(alias="schema")
    note: str
    rules: list[Rule]

    @model_validator(mode="after")
    def _unique(self) -> "RuleBook":
        ids = [r.rule_id for r in self.rules]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate rule ids: {dupes}")
        return self

    def get(self, rule_id: str) -> Optional[Rule]:
        return next((r for r in self.rules if r.rule_id == rule_id), None)


class Provenance(BaseModel):
    """Where one value came from. Each kind demands what makes it checkable."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["licensed_copy", "secondary", "derived", "user_override"]
    document: Optional[str] = Field(
        default=None, description="Standard and revision read, e.g. IPC-6012F")
    clause: Optional[str] = None
    url: Optional[str] = None
    quote: Optional[str] = None
    read_on: Optional[str] = None
    formula: Optional[str] = None
    reason: Optional[str] = None
    entered_by: Optional[str] = None
    entered_at: Optional[str] = None

    @model_validator(mode="after")
    def _complete(self) -> "Provenance":
        need = {
            "licensed_copy": ("document", "clause"),
            "secondary": ("url", "quote", "read_on"),
            "derived": ("formula",),
            "user_override": ("reason",),
        }[self.kind]
        missing = [f for f in need if not getattr(self, f)]
        if missing:
            raise ValueError(f"{self.kind} provenance needs {missing}")
        return self

    def cite(self) -> str:
        if self.kind == "licensed_copy":
            return f"{self.document} {self.clause}"
        if self.kind == "secondary":
            return f"{self.url} (read {self.read_on})"
        if self.kind == "derived":
            return f"derived: {self.formula}"
        return f"user override: {self.reason}"


class RuleValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    key: dict[str, str] = Field(default_factory=dict)
    value: Union[float, int, str]
    note: Optional[str] = None
    provenance: Provenance


class ValuesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_: int = Field(alias="schema")
    values: list[RuleValue]


@lru_cache(maxsize=1)
def load_catalogue() -> Catalogue:
    return Catalogue.model_validate(
        json.loads((_DATA / "catalogue.json").read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def load_rules() -> RuleBook:
    book = RuleBook.model_validate(
        json.loads((_DATA / "rules.json").read_text(encoding="utf-8")))
    cat = load_catalogue()
    unknown = sorted({s for r in book.rules for s in r.standards if cat.get(s) is None})
    if unknown:
        raise ValueError(f"rules cite standards missing from the catalogue: {unknown}")
    return book


def _check_value(rule: Rule, rv: RuleValue) -> Optional[str]:
    extra = sorted(set(rv.key) - set(rule.keyed_by))
    if extra:
        return f"{rule.rule_id}: keyed by {rule.keyed_by}, not {extra}"
    missing = sorted(set(rule.keyed_by) - set(rv.key))
    if missing:
        return f"{rule.rule_id}: value needs key(s) {missing}"
    for k, v in rv.key.items():
        allowed = KEY_DOMAINS[k]
        if (allowed is not None and v not in allowed) or not v:
            return f"{rule.rule_id}: {k}={v!r} not in {allowed}"
    numeric = rule.unit in ("mils", "um", "percent", "ratio")
    if numeric and (isinstance(rv.value, (str, bool)) or rv.value < 0):
        return f"{rule.rule_id}: needs a non-negative number in {rule.unit}"
    if rule.unit == "enum" and not isinstance(rv.value, str):
        return f"{rule.rule_id}: needs a text value"
    return None


def load_values(profile_dir: Optional[Path]) -> dict[str, Any]:
    """The profile's values, checked against the rule book.

    ``{"ok": True, "values": [RuleValue], "path": str|None}`` (an absent
    file is an empty, valid set) or ``{"ok": False, "reason", "errors"}``.
    Strict: a value for an unknown rule, a wrong key or a value without
    complete provenance fails the whole file rather than half-loading it.
    """
    if profile_dir is None:
        return {"ok": True, "values": [], "path": None, "warnings": []}
    path = Path(profile_dir) / VALUES_FILE
    if not path.is_file():
        return {"ok": True, "values": [], "path": None, "warnings": []}
    try:
        vf = ValuesFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as exc:
        return {"ok": False, "reason": f"{path}: {exc}", "errors": [str(exc)]}
    book = load_rules()
    errors: list[str] = []
    seen: set[tuple[str, tuple]] = set()
    for rv in vf.values:
        rule = book.get(rv.rule_id)
        if rule is None:
            errors.append(f"unknown rule {rv.rule_id!r}")
            continue
        err = _check_value(rule, rv)
        if err:
            errors.append(err)
        ident = (rv.rule_id, tuple(sorted(rv.key.items())))
        if ident in seen:
            errors.append(f"{rv.rule_id}: two values for {dict(rv.key)}")
        seen.add(ident)
    if errors:
        return {"ok": False, "reason": f"{path}: {len(errors)} problem(s)", "errors": errors}
    return {"ok": True, "values": vf.values, "path": str(path),
            "warnings": revision_warnings(vf.values)}


def revision_warnings(values: list[RuleValue]) -> list[str]:
    """Values read from a revision other than the catalogue's current one.

    Not an error: an older copy may be what a customer contract names. But
    a value from IPC-6012E on a profile that claims the current standard is
    worth saying out loud every time it is loaded.
    """
    cat = load_catalogue()
    out: list[str] = []
    for rv in values:
        doc = rv.provenance.document
        if rv.provenance.kind != "licensed_copy" or not doc:
            continue
        std = max((s for s in cat.standards if doc.startswith(s.id)),
                  key=lambda s: len(s.id), default=None)
        if std is None:
            out.append(f"{rv.rule_id}: {doc} is not in the catalogue")
            continue
        cited = doc[len(std.id):].split("-")[0].strip() or None
        if std.revision and cited != std.revision:
            out.append(f"{rv.rule_id}: cites {doc}, the catalogue's current "
                       f"revision is {std.label}")
    return out


def resolve(rule_id: str, key: dict[str, str], values: list[RuleValue]) -> dict[str, Any]:
    """One rule's value for one key, or why there is none."""
    rule = load_rules().get(rule_id)
    if rule is None:
        return {"status": "unknown_rule", "rule_id": rule_id}
    want = {k: str(v) for k, v in key.items() if k in rule.keyed_by}
    for rv in values:
        if rv.rule_id == rule_id and rv.key == want:
            return {"status": "configured", "rule_id": rule_id, "key": want,
                    "value": rv.value, "unit": rule.unit,
                    "source": rv.provenance.kind, "cite": rv.provenance.cite()}
    return {"status": "not_configured", "rule_id": rule_id, "key": want,
            "unit": rule.unit, "standards": rule.standards,
            "reason": "no value in the profile; enter it from your copy of "
                      + ", ".join(rule.standards)}


def class_floors(values: list[RuleValue]) -> dict[str, Any]:
    """The fab rule builder's class floor document, from configured values.

    Shape: ``{"source": {...}, "classes": {"2": {fab_key: {"mils"|"value",
    "clause", "note"}}}}``. Only rules with a ``fab_key`` and keyed by
    class alone take part; a class with no floor is left out, never guessed.
    """
    book = load_rules()
    classes: dict[str, dict[str, Any]] = {}
    docs: set[str] = set()
    for rv in values:
        rule = book.get(rv.rule_id)
        if rule is None or not rule.fab_key or set(rule.keyed_by) != {"class"}:
            continue
        cls = rv.key["class"]
        entry: dict[str, Any] = {"mils" if rule.unit == "mils" else "value": rv.value,
                                 "clause": rv.provenance.cite(),
                                 "rule_id": rule.rule_id,
                                 "source": rv.provenance.kind}
        if rv.note:
            entry["note"] = rv.note
        classes.setdefault(cls, {})[rule.fab_key] = entry
        if rv.provenance.document:
            docs.add(rv.provenance.document)
    return {"source": {"standard": " / ".join(sorted(docs)) or "profile values",
                       "generated_from": str(VALUES_FILE).replace("\\", "/")},
            "classes": classes}


def write_class_floors(profile_dir: Path) -> dict[str, Any]:
    """Regenerate ``<profile>/standards/ipc_class_floors.json`` from values.json."""
    loaded = load_values(profile_dir)
    if not loaded["ok"]:
        return loaded
    doc = class_floors(loaded["values"])
    if not doc["classes"]:
        return {"ok": False, "reason": "no class floor values configured in "
                f"{Path(profile_dir) / VALUES_FILE}; nothing written"}
    out = Path(profile_dir) / CLASS_FLOORS_FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(out), "classes": sorted(doc["classes"])}


def coverage(values: list[RuleValue]) -> list[dict[str, Any]]:
    """Every rule with how many values the profile holds for it."""
    counts: dict[str, int] = {}
    for rv in values:
        counts[rv.rule_id] = counts.get(rv.rule_id, 0) + 1
    return [{"rule_id": r.rule_id, "title": r.title, "category": r.category,
             "values": counts.get(r.rule_id, 0),
             "standards": r.standards, "check": r.check} for r in load_rules().rules]


__all__ = [
    "Catalogue", "Rule", "RuleBook", "RuleValue", "Provenance", "Standard",
    "VALUES_FILE", "CLASS_FLOORS_FILE", "load_catalogue", "load_rules",
    "load_values", "revision_warnings", "resolve", "class_floors", "write_class_floors", "coverage",
]
