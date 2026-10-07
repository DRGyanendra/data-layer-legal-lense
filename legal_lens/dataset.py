"""The validation sample: 100 judgments drawn from the AWS Open Data bucket.

Source: "Indian Supreme Court Judgments", maintained by Dattam Labs, CC-BY 4.0,
s3://indian-supreme-court-judgments (public, read over plain HTTPS, no
account and no AWS CLI needed). One `metadata.parquet` per year carries the
title, parties, SCR citation, neutral citation (INSC), decision date and
disposal; the bench, the case number and the headnote are only inside the
`raw_html` column and are read out of it here.

The sample is balanced on purpose (see VALIDATION_DATASET.md):

  era      1950-1989 law-report style / 1990-2009 older text / 2010+ modern
  shape    bench size and page count stand in for single vs several opinions
           and short orders vs long constitutional judgments
  subject  enough criminal matters to exercise the IPC -> BNS mapping

Selection is deterministic for a given seed, so the sample can be rebuilt.
"""

from __future__ import annotations

import csv
import random
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

BUCKET = "https://indian-supreme-court-judgments.s3.amazonaws.com"
YEARS = range(1950, 2026)

ERAS = {"1950-1989": (1950, 1989), "1990-2009": (1990, 2009), "2010-2025": (2010, 2025)}

_CORAM = re.compile(r"Coram\s*:\s*(.*?)</strong>", re.S)
_CASE_NO = re.compile(r"Case No :</span><font[^>]*>\s*(.*?)</font>")
_BENCH_N = re.compile(r"Bench :</span><font[^>]*>\s*(\d+)")
_HEADNOTE = re.compile(r"</strong><br>(.*?)<br><strong class='caseDetailsTD'", re.S)
_TAG = re.compile(r"<[^>]+>")


@dataclass
class Judgment:
    """One row of the AWS metadata, with the HTML-only fields read out."""
    path: str                 # bucket file stem, e.g. S_2005_2_307_341
    year: int
    title: str
    petitioner: str
    respondent: str
    citation: str             # SCR citation, e.g. [2005] SUPP. 2 S.C.R. 307
    neutral_citation: str     # e.g. 2005 INSC 384 (the bucket calls this case_id)
    decision_date: str        # ISO
    disposal: str
    bench: list[str]          # names in coram order; the author is first when marked
    author: str | None        # the judge marked * in the coram, if any
    bench_size: int | None
    case_number: str | None   # e.g. CRIMINAL APPEAL No. 144/2004
    headnote: str             # the SCR headnote excerpt, for a draft summary only
    pages: int                # from the SCR page range in `path`

    @property
    def era(self) -> str:
        return next(k for k, (a, b) in ERAS.items() if a <= self.year <= b)

    @property
    def criminal(self) -> bool:
        return bool(re.search(r"\bCRIMINAL\b|\bCRL\b", self.case_number or "", re.IGNORECASE))

    @property
    def length(self) -> str:
        return "short" if self.pages <= 8 else "long" if self.pages >= 40 else "medium"

    @property
    def pdf_url(self) -> str:
        return f"{BUCKET}/data/pdf/year={self.year}/english/{self.path}_EN.pdf"

    @property
    def file_stem(self) -> str:
        """`<year>_<petitioner>_v_<respondent>_<path>`: readable, unique, traceable."""
        def slug(s: str) -> str:
            s = re.sub(r"\b(and|&|ors?|anr|etc|others?|another|the|thr|through|rep|by)\b\.?", " ", s.lower())
            return re.sub(r"[^a-z0-9]+", "_", s).strip("_")[:28]
        return f"{self.year}_{slug(self.petitioner)}_v_{slug(self.respondent)}_{self.path}"

    def as_row(self) -> dict:
        return {
            "file": self.file_stem + ".pdf", "path": self.path, "year": self.year, "era": self.era,
            "title": self.title, "citation": self.citation, "neutral_citation": self.neutral_citation,
            "decision_date": self.decision_date, "disposal": self.disposal,
            "bench": "; ".join(self.bench), "author": self.author or "", "bench_size": self.bench_size or "",
            "case_number": self.case_number or "", "criminal": self.criminal,
            "pages": self.pages, "length": self.length, "source_url": self.pdf_url,
        }


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", text)).strip()


def _iso(d: str) -> str:
    m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", (d or "").strip())
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else d or ""


def _pages(path: str) -> int:
    nums = [int(n) for n in re.findall(r"\d+", path)]
    return max(1, nums[-1] - nums[-2] + 1) if len(nums) >= 2 else 1


def judgment_from_row(row: dict) -> Judgment:
    html = row.get("raw_html") or ""
    bench, author = [], None
    m = _CORAM.search(html)
    if m:
        for part in m.group(1).split(","):
            marked = "tooltip-sup" in part or "*" in _TAG.sub("", part)
            name = _clean(part).replace("*", "").strip()
            if name:
                bench.append(name)
                if marked and author is None:
                    author = name
    case_no = _CASE_NO.search(html)
    bench_n = _BENCH_N.search(html)
    head = _HEADNOTE.search(html)
    return Judgment(
        path=row["path"], year=int(row["year"]), title=_clean(row.get("title") or ""),
        petitioner=_clean(row.get("petitioner") or ""), respondent=_clean(row.get("respondent") or ""),
        citation=(row.get("citation") or "").strip(), neutral_citation=(row.get("case_id") or "").strip(),
        decision_date=_iso(row.get("decision_date") or ""), disposal=(row.get("disposal_nature") or "").strip(),
        bench=bench, author=author, bench_size=int(bench_n.group(1)) if bench_n else None,
        case_number=_clean(case_no.group(1)) if case_no else None,
        headnote=_clean(head.group(1))[:600] if head else "", pages=_pages(row["path"]),
    )


# ------------------------------------------------------------- fetching

def fetch_metadata(cache_dir: Path, years=YEARS, log=print) -> list[Judgment]:
    """All years' metadata, downloaded once into `cache_dir` and read with pandas."""
    import pandas as pd

    cache_dir.mkdir(parents=True, exist_ok=True)
    judgments: list[Judgment] = []
    for year in years:
        target = cache_dir / f"{year}.parquet"
        if not target.exists():
            url = f"{BUCKET}/metadata/parquet/year={year}/metadata.parquet"
            try:
                urllib.request.urlretrieve(url, target)
            except Exception as exc:          # a year with no file must not stop the rest
                log(f"  {year}: no metadata ({exc})")
                continue
        frame = pd.read_parquet(target)
        for row in frame.to_dict("records"):
            if row.get("path") and "ENG" in (row.get("available_languages") or "ENG"):
                judgments.append(judgment_from_row(row))
    return judgments


def download_pdf(judgment: Judgment, folder: Path, log=print) -> Path | None:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (judgment.file_stem + ".pdf")
    if target.exists() and target.stat().st_size > 0:
        return target
    try:
        urllib.request.urlretrieve(judgment.pdf_url, target)
    except Exception as exc:
        log(f"  could not download {judgment.path}: {exc}")
        target.unlink(missing_ok=True)
        return None
    return target


# ------------------------------------------------------------- selection

def _quota(total: int, shares: dict[str, float]) -> dict[str, int]:
    """Integer counts per key that add up to `total` (largest remainders get the leftovers)."""
    raw = {k: total * v for k, v in shares.items()}
    counts = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - counts[k], reverse=True)[: total - sum(counts.values())]:
        counts[k] += 1
    return counts


def select_sample(
    judgments: list[Judgment], *, size: int = 100, seed: int = 20261007,
    criminal_share: float = 0.4, exclude_paths: set[str] = frozenset(),
) -> list[Judgment]:
    """A balanced sample: a third per era; within each era short/medium/long
    judgments and about `criminal_share` criminal matters, plus at least a few
    constitution benches (5 judges or more) per era where the data has them.

    Rows without a bench or a date are left out: they cannot be checked.
    """
    rng = random.Random(seed)
    pool = [j for j in judgments if j.bench and j.decision_date and j.path not in exclude_paths]
    per_era = _quota(size, {era: 1 / len(ERAS) for era in ERAS})
    chosen: list[Judgment] = []
    for era, want in per_era.items():
        in_era = [j for j in pool if j.era == era]
        rng.shuffle(in_era)
        picked: list[Judgment] = []
        taken: set[str] = set()

        def take(candidates, n):
            for j in candidates:
                if n <= 0:
                    break
                if j.path not in taken:
                    picked.append(j)
                    taken.add(j.path)
                    n -= 1

        # large benches first: they are rare and carry the multi-opinion layouts
        take([j for j in in_era if (j.bench_size or 0) >= 5], max(3, want // 10))
        # then criminal matters, spread across lengths
        crim = [j for j in in_era if j.criminal]
        for length, share in (("short", 0.25), ("medium", 0.5), ("long", 0.25)):
            take([j for j in crim if j.length == length], round(want * criminal_share * share))
        # fill the rest with civil / constitutional matters, spread across lengths
        for length, share in (("short", 0.25), ("medium", 0.5), ("long", 0.25)):
            remaining = want - len(picked)
            take([j for j in in_era if j.length == length and not j.criminal],
                 min(remaining, round(want * (1 - criminal_share) * share)))
        take(in_era, want - len(picked))          # top up if any bucket ran short
        chosen.extend(picked[:want])
    chosen.sort(key=lambda j: (j.year, j.path))
    return chosen


def write_sample_csv(sample: list[Judgment], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = [j.as_row() for j in sample]
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_draft_manifest(sample: list[Judgment], out: Path) -> None:
    """Draft case records from the dataset metadata. These are NOT checked by a
    person: copy an entry into data/cases.yaml only after reading the judgment."""
    import yaml

    def title_case(name: str) -> str:
        return " ".join(w if re.fullmatch(r"[A-Z]\.([A-Z]\.)*", w) else w.capitalize() for w in name.split())

    cases = []
    for j in sample:
        cases.append({
            "case_id": re.sub(r"[^a-z0-9_]", "", j.file_stem.lower().replace("-", "_"))[:60],
            "name": f"{title_case(j.petitioner)} v. {title_case(j.respondent)}",
            "summary": "",
            "date": j.decision_date, "court": "Supreme Court",
            "citations": [c for c in (j.citation, j.neutral_citation) if c],
            "bench": [title_case(b) for b in j.bench],
            "opinions": [{"author": title_case(j.author), "type": "unknown"}] if j.author else [],
            "file": f"data/raw/validation/{j.file_stem}.pdf",
            "notes": "Drafted from the AWS Open Data metadata (Dattam Labs, CC-BY 4.0); not yet checked "
                     "against the judgment. Headnote: " + (j.headnote[:300] or "none"),
        })
    out.write_text(yaml.safe_dump({"cases": cases}, sort_keys=False, allow_unicode=True, width=100),
                   encoding="utf-8")


def describe(sample: list[Judgment]) -> list[str]:
    from collections import Counter

    lines = [f"{len(sample)} judgments"]
    for name, key in (("era", lambda j: j.era), ("length", lambda j: j.length),
                      ("criminal", lambda j: "criminal" if j.criminal else "civil / other"),
                      ("bench", lambda j: f"{j.bench_size} judges" if j.bench_size else "bench unknown")):
        counts = Counter(key(j) for j in sample)
        lines.append(f"  by {name:9} " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    return lines
