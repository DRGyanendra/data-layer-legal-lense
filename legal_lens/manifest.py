"""The case manifest: one checked record per judgment.

Case name, date, citations and bench come from data/cases.yaml, which a
person fills in from the judgment itself. The pipeline then checks each
record against the PDF text and warns when they disagree. A search tool
whose pitch is verified citations cannot guess its own metadata.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .ids import citation_key, judge_id

OPINION_TYPES = {"majority", "plurality", "concurring", "dissenting", "partly_dissenting", "unknown"}

_MONTHS = (
    "January February March April May June July August September October November December"
).split()


@dataclass
class Opinion:
    author: str
    type: str = "unknown"
    joined_by: list[str] = field(default_factory=list)


@dataclass
class CaseMeta:
    case_id: str
    name: str
    date: dt.date | None           # None only when read from a PDF that carries no date
    court: str
    citations: list[str]
    bench: list[str]
    file: str
    opinions: list[Opinion] = field(default_factory=list)
    outcome: str | None = None
    overrules: list[str] = field(default_factory=list)   # citations of cases this one overrules
    notes: str | None = None
    summary: str | None = None      # one line; embedded with every chunk of the case
    source: str = "manifest"        # 'manifest' (a person checked it) or 'auto' (read from the PDF)
    missing: list[str] = field(default_factory=list)     # what an automatic reading could not find
    case_number: str | None = None  # 'WRIT PETITION (CRIMINAL) NO. 194 OF 2017'
    layout: str | None = None       # how the PDF was laid out, when read automatically

    @property
    def date_iso(self) -> str | None:
        return self.date.isoformat() if self.date else None

    @property
    def date_words(self) -> str:
        return f"{self.date.day} {self.date.strftime('%B %Y')}" if self.date else ""

    @property
    def citation(self) -> str:
        return self.citations[0] if self.citations else ""

    @property
    def citation_keys(self) -> list[str]:
        return [citation_key(c) for c in self.citations]

    def judges(self) -> list[dict]:
        return [{"judge_id": judge_id(n), "name": n} for n in self.bench]


class ManifestError(ValueError):
    pass


def _parse_case(raw: dict) -> CaseMeta:
    missing = [k for k in ("case_id", "name", "date", "court", "citations", "bench", "file") if not raw.get(k)]
    if missing:
        raise ManifestError(f"{raw.get('case_id', '<no case_id>')}: missing {', '.join(missing)}")
    if not re.fullmatch(r"[a-z0-9_]+", raw["case_id"]):
        raise ManifestError(f"{raw['case_id']}: case_id must be lowercase letters, digits and underscores")
    date = raw["date"]
    if isinstance(date, str):
        date = dt.date.fromisoformat(date)
    elif isinstance(date, dt.datetime):
        date = date.date()
    opinions = []
    for o in raw.get("opinions") or []:
        kind = o.get("type", "unknown")
        if kind not in OPINION_TYPES:
            raise ManifestError(f"{raw['case_id']}: opinion type {kind!r} is not one of {sorted(OPINION_TYPES)}")
        opinions.append(Opinion(o["author"], kind, list(o.get("joined_by") or [])))
        for name in [o["author"], *(o.get("joined_by") or [])]:
            if judge_id(name) not in {judge_id(b) for b in raw["bench"]}:
                raise ManifestError(f"{raw['case_id']}: opinion names {name!r}, who is not on the bench list")
    return CaseMeta(
        case_id=raw["case_id"], name=raw["name"], date=date, court=raw["court"],
        citations=list(raw["citations"]), bench=list(raw["bench"]), file=raw["file"],
        opinions=opinions, outcome=raw.get("outcome"),
        overrules=list(raw.get("overrules") or []), notes=raw.get("notes"),
        summary=(raw.get("summary") or "").strip() or None,
    )


def load_manifest(path: str | Path) -> dict[str, CaseMeta]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cases: dict[str, CaseMeta] = {}
    for raw in data.get("cases", []):
        case = _parse_case(raw)
        if case.case_id in cases:
            raise ManifestError(f"Duplicate case_id {case.case_id}")
        cases[case.case_id] = case
    return cases


def _date_forms(date: dt.date) -> list[str]:
    month = _MONTHS[date.month - 1]
    day = date.day
    return [
        rf"{month}\s+0?{day}\s*,?\s+{date.year}",                    # August 5, 2005
        rf"0?{day}(?:st|nd|rd|th)?\s+{month}\s*,?\s+{date.year}",     # 5th August, 2005
        rf"0?{day}\s*[./-]\s*0?{date.month}\s*[./-]\s*{date.year}",   # 05.08.2005
    ]


def check_against_text(meta: CaseMeta, text: str) -> list[str]:
    """Compare the manifest record with the judgment text. Returns warnings."""
    warnings: list[str] = []
    squashed = re.sub(r"\s+", " ", text)
    if meta.source == "auto":
        return warnings              # the record was read from this text; nothing to compare

    if not any(re.search(p, squashed, re.IGNORECASE) for p in _date_forms(meta.date)):
        warnings.append(
            f"Date {meta.date.isoformat()} from the manifest does not appear in the PDF text."
        )

    lowered = squashed.lower()
    absent = []
    for name in meta.bench:
        surname = re.sub(r"[^a-z]", "", name.split()[-1].lower())
        if surname and surname not in re.sub(r"[^a-z ]", "", lowered):
            absent.append(name)
    if absent:
        warnings.append(
            "Bench members not found in the PDF text: " + ", ".join(absent)
            + ". Check the bench against the first and last pages."
        )

    parties = [p.strip() for p in re.split(r"\sv\.?\s|\svs\.?\s|\sversus\s", meta.name, flags=re.IGNORECASE)]
    first_party = parties[0].lower() if parties else ""
    if first_party and first_party not in lowered:
        warnings.append(f"The party name {parties[0]!r} does not appear in the PDF text. Is this the right file?")
    return warnings
