"""The IPC to BNS and CrPC to BNSS mapping table.

The mapping is not a one-to-one renumbering, so each row says what kind of
correspondence it is:

  same        text is materially unchanged, only the number moved
  modified    the provision changed; change_note says how
  split       one old provision became several new ones (one row each)
  merged      several old provisions became one new one
  omitted     no successor in the new code
  unreviewed  the correspondence is known, the two texts have not been compared

'unreviewed' is the honest default. Moving a row to 'same' or 'modified' is
a legal judgment someone has to make with both texts open.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from .ids import base_section, normalize_section, statute_id

RELATIONS = {"same", "modified", "split", "merged", "omitted", "unreviewed"}
OLD_CODES = {"IPC": "BNS", "CrPC": "BNSS"}
COLUMNS = [
    "old_code", "old_section", "new_code", "new_section", "relation",
    "heading", "change_note", "effective_date", "source", "verified",
]
_SECTION = re.compile(r"^\d{1,3}[A-Z]{0,2}(?:\([0-9A-Za-z]{1,3}\))*$")


@dataclass(frozen=True)
class MappingRow:
    old_code: str
    old_section: str
    new_code: str | None
    new_section: str | None
    relation: str
    heading: str
    change_note: str | None
    effective_date: dt.date
    source: str
    verified: bool

    @property
    def old_id(self) -> str:
        return statute_id(self.old_code, self.old_section)

    @property
    def new_id(self) -> str | None:
        if self.new_code and self.new_section:
            return statute_id(self.new_code, self.new_section)
        return None

    def as_params(self) -> dict:
        return {
            "old_id": self.old_id, "old_code": self.old_code, "old_section": self.old_section,
            "old_base": base_section(self.old_section),
            "new_id": self.new_id, "new_code": self.new_code, "new_section": self.new_section,
            "new_base": base_section(self.new_section) if self.new_section else None,
            "relation": self.relation, "heading": self.heading or None,
            "change_note": self.change_note, "effective_date": self.effective_date.isoformat(),
            "source": self.source, "verified": self.verified,
        }


class MappingError(ValueError):
    pass


def load_mapping_file(path: str | Path) -> list[MappingRow]:
    rows: list[MappingRow] = []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(line for line in handle if not line.startswith("#"))
        if reader.fieldnames != COLUMNS:
            raise MappingError(f"{path}: columns must be exactly {COLUMNS}, found {reader.fieldnames}")
        for line_no, raw in enumerate(reader, start=2):
            raw = {k: (v or "").strip() for k, v in raw.items()}
            try:
                rows.append(
                    MappingRow(
                        old_code=raw["old_code"],
                        old_section=normalize_section(raw["old_section"]),
                        new_code=raw["new_code"] or None,
                        new_section=normalize_section(raw["new_section"]) if raw["new_section"] else None,
                        relation=raw["relation"],
                        heading=raw["heading"],
                        change_note=raw["change_note"] or None,
                        effective_date=dt.date.fromisoformat(raw["effective_date"]),
                        source=raw["source"],
                        verified=raw["verified"].lower() in {"yes", "true", "1"},
                    )
                )
            except (KeyError, ValueError) as exc:
                raise MappingError(f"{path}, data row {line_no}: {exc}") from exc
    return rows


def validate(rows: list[MappingRow]) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). Errors block loading into Neo4j."""
    errors: list[str] = []
    warnings: list[str] = []
    seen: set[tuple[str, str | None]] = set()
    by_old: dict[str, list[MappingRow]] = {}
    by_new: dict[str, list[MappingRow]] = {}

    for row in rows:
        label = f"{row.old_code} {row.old_section}"
        if row.old_code not in OLD_CODES:
            errors.append(f"{label}: old_code must be one of {sorted(OLD_CODES)}")
        if row.relation not in RELATIONS:
            errors.append(f"{label}: relation {row.relation!r} is not one of {sorted(RELATIONS)}")
        if not _SECTION.match(row.old_section):
            errors.append(f"{label}: old_section is not a valid section number")
        if row.relation == "omitted":
            if row.new_code or row.new_section:
                errors.append(f"{label}: an omitted provision must not name a new section")
            if not row.change_note:
                errors.append(f"{label}: an omitted provision needs a change_note saying why")
        else:
            if not (row.new_code and row.new_section):
                errors.append(f"{label}: new_code and new_section are required unless relation is 'omitted'")
            elif row.old_code in OLD_CODES and row.new_code != OLD_CODES[row.old_code]:
                errors.append(f"{label}: {row.old_code} maps to {OLD_CODES[row.old_code]}, not {row.new_code}")
            elif not _SECTION.match(row.new_section):
                errors.append(f"{label}: new_section {row.new_section!r} is not a valid section number")
        if row.relation == "modified" and not row.change_note:
            errors.append(f"{label}: relation 'modified' needs a change_note")
        if not row.source:
            errors.append(f"{label}: source is empty; every row must say where it came from")
        key = (row.old_id, row.new_id)
        if key in seen:
            errors.append(f"{label}: duplicate row")
        seen.add(key)
        by_old.setdefault(row.old_id, []).append(row)
        if row.new_id:
            by_new.setdefault(row.new_id, []).append(row)

    for old_id, group in by_old.items():
        if len(group) > 1 and any(r.relation != "split" for r in group):
            errors.append(f"{old_id}: has {len(group)} rows, so every one must have relation 'split'")
    for new_id, group in by_new.items():
        if len(group) > 1 and any(r.relation not in {"merged", "split"} for r in group):
            warnings.append(
                f"{new_id}: {len(group)} old sections point here "
                f"({', '.join(r.old_id for r in group)}); mark them 'merged' if that is right"
            )

    unverified = sum(1 for r in rows if not r.verified)
    unreviewed = sum(1 for r in rows if r.relation == "unreviewed")
    if unverified:
        warnings.append(f"{unverified} row(s) are not marked verified against a published table")
    if unreviewed:
        warnings.append(f"{unreviewed} row(s) are 'unreviewed': old and new text not yet compared")
    return errors, warnings


class Mapping:
    """Look up successors of an old provision."""

    def __init__(self, rows: list[MappingRow]):
        self.rows = rows
        self._by_old: dict[str, list[MappingRow]] = {}
        for row in rows:
            self._by_old.setdefault(row.old_id, []).append(row)

    @classmethod
    def from_dir(cls, directory: str | Path) -> "Mapping":
        rows: list[MappingRow] = []
        for path in sorted(Path(directory).glob("*.csv")):
            rows.extend(load_mapping_file(path))
        return cls(rows)

    def lookup(self, code: str, section: str) -> tuple[list[MappingRow], str]:
        """Rows for this provision and how it matched: 'exact', 'base' or 'none'.

        'IPC 376(2)(g)' is not in the table, but 'IPC 376' is, so it matches
        on the base section. The caller should say so, since sub-sections may
        have moved differently.
        """
        exact = self._by_old.get(statute_id(code, section))
        if exact:
            return exact, "exact"
        base = self._by_old.get(statute_id(code, base_section(section)))
        if base:
            return base, "base"
        return [], "none"

    def successors(self, code: str, section: str) -> list[str]:
        rows, _ = self.lookup(code, section)
        return [r.new_id for r in rows if r.new_id]
