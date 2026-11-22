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
    author: str | None        # the judge eCourts marks with *: always the first listed, NOT reliably the author
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
            "opinions": [],         # the record's author is the first-listed judge, not the author
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


# ------------------------------------------------------------- the record as case details

def sample_record(pdf_path: Path) -> dict | None:
    """The row of `sample.csv` (written by `sample`) for this PDF, if the file
    sits in a sample folder. The CSV is read once per folder."""
    pdf_path = Path(pdf_path)
    listing = pdf_path.parent / "sample.csv"
    if not listing.exists():
        return None
    rows = _sample_rows(listing.resolve())
    return rows.get(pdf_path.name)


_SAMPLE_CACHE: dict[Path, dict[str, dict]] = {}


def _sample_rows(listing: Path) -> dict[str, dict]:
    if listing not in _SAMPLE_CACHE:
        with listing.open(encoding="utf-8", newline="") as handle:
            _SAMPLE_CACHE[listing] = {row["file"]: row for row in csv.DictReader(handle)}
    return _SAMPLE_CACHE[listing]


def _party(text: str) -> str:
    from .autometa import _AND_OTHERS, _THROUGH, title_case

    text = _THROUGH.sub("", _AND_OTHERS.sub("", text.strip()))
    text = re.sub(r"\s+", " ", text).strip(" .,;:")
    if re.search(r"\b(?:[A-Za-z]\.)+[A-Za-z]$", text):
        text += "."
    return title_case(text)


def case_id_for(petitioner: str, respondent: str, date, stem: str) -> str:
    """The case_id the pipeline gives a judgment read without a manifest entry."""
    from .ids import slugify

    lead = petitioner if petitioner and not re.match(r"^(?:state|union|commissioner|director|collector)\b", petitioner, re.IGNORECASE) \
        else f"{petitioner} v {respondent}" if petitioner else stem
    lead = re.sub(r"\(.*?\)", "", re.sub(r"^(?:justice|dr|shri|smt|mr|mrs|ms|m/s|his holiness|sri)\.?\s+", "", lead, flags=re.IGNORECASE))
    return ("_".join(slugify(lead).split("_")[:6]) or slugify(stem)) + (f"_{date.year}" if date else "")


def _plausible_judge(name: str) -> bool:
    """A name that reads like a judge's, not OCR debris taken for one."""
    return bool(re.fullmatch(r"[A-Z][A-Za-z.'\- ]{3,35}", name)
                and len(re.sub(r"[^A-Za-z]", "", name.split()[-1])) >= 3)    # Roy, Ray, Sen, Beg are real surnames


# OCR debris in a case name read from a law-report scan: stray symbols, runs of
# punctuation, report-header words, and tokens that mix letters and digits
# ('J9G3', '5CX5'). A real case name can carry a number ('In Re: Article 370'),
# so digits alone do not make a name unreadable.
_NAME_NOISE = re.compile(
    r"[~<>{}|\\^_\[\]\"]|\.\.|,,|[.,;]{2,}|\bsupreme\s+co\w*\s+r\w*|\bs\.\s?c\.\s?r\b"
    r"|\b(?=[a-z]*\d)(?=\d*[a-z])[a-z\d]{2,}\b", re.IGNORECASE)


def _readable_name(name: str) -> bool:
    """Does a case name read from the PDF look like a case name, not scan debris?"""
    return bool(name) and len(name) <= 160 and not _NAME_NOISE.search(name)


def _parties_agree(pdf_name: str, petitioner: str) -> bool:
    """Does the PDF's case name carry a distinctive word of the record's first party?"""
    pdf_words = re.sub(r"[^a-z ]", "", pdf_name.lower())
    lead = re.sub(r"[^a-z ]", "", petitioner.lower()).split()
    return not lead or any(len(w) >= 4 and w in pdf_words for w in lead)


def dataset_meta(pdf_path: str | Path, auto) -> "CaseMeta | None":
    """Case details for a PDF that has a record in the dataset: the PDF first,
    the record only where the PDF could not supply a detail. Returns None when
    the PDF has no record.

    The PDF is the judgment itself, so whatever it says wins. The dataset's
    record (scraped from eCourts) fills what the PDF does not print or the
    scan could not read: usually the reported and neutral citations, sometimes
    the date, the bench or the parties. Where both have a detail and they
    disagree, the PDF's reading is kept and the disagreement goes into
    `conflicts`, which the quality verdict turns into a review reason; nothing
    is overridden silently. `field_sources` records, detail by detail, which
    source each came from.

    Two things are taken from the record even when the PDF has them:
    - the case_id, which is an identifier, not a fact shown to anyone. It has
      to stay stable: the stores and the question files are keyed by it.
    - the spelling of a judge's name, when the PDF names the same judge
      (`same_judge`). Who sat is the PDF's reading; the record's spelling only
      repairs OCR damage, so one judge is one Judge node in the graph.

    Three limits on "the PDF wins", each measured on the 160-judgment sample
    (October 2026):
    - Parties: a name read from the PDF that is scan debris (`_readable_name`)
      or shares no party word with the record is not a reading of the parties
      at all (it is usually the tail of the previous case or the page header),
      so the record's name is used. A clean-looking name that disagrees is
      also recorded as a conflict: the PDF may hold a different judgment.
    - Date: a PDF date more than a year away from the record's is a date from
      the case's story or a misread year, so the record's date is used; the
      disagreement is still recorded as a conflict.
    - Author: the record's author is never used. eCourts marks the first-listed
      judge as the author in all 38,097 multi-judge records, and the PDFs'
      "delivered by" lines show that is often wrong.
    """
    import datetime as dt

    from .autometa import clean_judge, same_judge
    from .manifest import CaseMeta, Opinion

    row = sample_record(pdf_path)
    if row is None:
        return None
    title = row.get("title", "")
    rec_petitioner, rec_respondent = (re.split(r"\s+versus\s+", title, maxsplit=1, flags=re.IGNORECASE) + [""])[:2]
    rec_petitioner, rec_respondent = _party(rec_petitioner), _party(rec_respondent)
    rec_date = dt.date.fromisoformat(row["decision_date"]) if row.get("decision_date") else None
    rec_bench = [clean_judge(b) for b in row.get("bench", "").split(";") if b.strip()]
    rec_citations = [c for c in (row.get("citation", ""), row.get("neutral_citation", "")) if c]

    sources: dict[str, str] = {}
    conflicts: list[str] = []

    # ---- parties: the PDF's name, unless it is debris or names other parties
    rec_name = f"{rec_petitioner} v. {rec_respondent}" if rec_petitioner and rec_respondent else \
        (_party(title) if title else "")
    pdf_readable = "parties" not in auto.missing and _readable_name(auto.name)
    if pdf_readable and (not rec_petitioner or _parties_agree(auto.name, rec_petitioner)):
        name, sources["parties"] = auto.name, ("pdf+dataset" if rec_petitioner else "pdf")
    elif pdf_readable and rec_name:
        conflicts.append(f"The dataset names the case {rec_name}; the PDF reads {auto.name}. "
                         "The dataset's name is used; check that the PDF is this judgment.")
        name, sources["parties"] = rec_name, "dataset, pdf differs"
    elif "parties" not in auto.missing and rec_name:
        name, sources["parties"] = rec_name, "dataset, pdf unreadable"
    elif rec_petitioner and rec_respondent:
        name, sources["parties"] = f"{rec_petitioner} v. {rec_respondent}", "dataset"
    elif title:
        name, sources["parties"] = _party(title), "dataset"
    else:
        name, sources["parties"] = auto.name, "missing"

    # ---- date
    if auto.date and rec_date and abs(auto.date.year - rec_date.year) > 1:
        conflicts.append(f"The dataset gives the decision date as {rec_date.isoformat()}; the PDF reads "
                         f"{auto.date.isoformat()}, more than a year away. The dataset's date is used.")
        date, sources["date"] = rec_date, "dataset, pdf differs"
    elif auto.date:
        date = auto.date
        if rec_date and rec_date != auto.date:
            conflicts.append(f"The dataset gives the decision date as {rec_date.isoformat()}; "
                             f"the PDF reads {auto.date.isoformat()}. The PDF's date is kept.")
            sources["date"] = "pdf, dataset differs"
        else:
            sources["date"] = "pdf+dataset" if rec_date else "pdf"
    elif rec_date:
        date, sources["date"] = rec_date, "dataset"
    else:
        date, sources["date"] = None, "missing"

    # ---- bench: the PDF's reading when every name in it reads like a name
    def spelt(judge: str) -> str:
        return next((d for d in rec_bench if same_judge(judge, d)), judge)

    if auto.bench and all(_plausible_judge(b) for b in auto.bench):
        bench = [spelt(b) for b in auto.bench]
        strangers = [b for b in auto.bench if rec_bench and not any(same_judge(b, d) for d in rec_bench)]
        unread = [d for d in rec_bench if not any(same_judge(b, d) for b in auto.bench)]
        if strangers:
            conflicts.append("Judge(s) read from the PDF are not in the dataset's bench: " + ", ".join(strangers)
                             + ". The PDF may hold a different judgment, or a name was misread. The PDF's bench is kept.")
            sources["bench"] = "pdf, dataset differs"
        elif unread:
            # the PDF named part of the bench (often only the signatures it could read); the record completes it
            bench += unread
            sources["bench"] = "pdf+dataset"
        else:
            sources["bench"] = "pdf+dataset" if rec_bench else "pdf"
    elif rec_bench:
        bench, sources["bench"] = rec_bench, "dataset"
    else:
        bench, sources["bench"] = list(auto.bench), ("pdf" if auto.bench else "missing")

    # ---- citations: the PDF's first; the record adds what the PDF does not print
    citations = list(auto.citations) + [c for c in rec_citations if c not in auto.citations]
    if auto.citations:
        sources["citation"] = "pdf+dataset" if rec_citations else "pdf"
    else:
        sources["citation"] = "dataset" if rec_citations else "missing"

    # ---- opinions: always the PDF's (only the text says how many there are and who heads each)
    # (the record's author is never used: see the docstring)
    opinions = [Opinion(o.author and spelt(o.author), o.type, list(o.joined_by)) for o in auto.opinions]
    if len(opinions) == 1:
        opinions[0].type = "majority"
        sources["author"] = "pdf" if opinions[0].author else "missing"

    # ---- the identifier stays the record's (see the docstring)
    case_id = case_id_for(rec_petitioner, rec_respondent, rec_date, Path(pdf_path).stem) if rec_petitioner \
        else auto.case_id

    missing = [k for k in ("parties", "date", "bench", "citation") if sources.get(k) == "missing"]
    return CaseMeta(
        case_id=case_id, name=name, date=date, court="Supreme Court", citations=citations, bench=bench,
        file=str(pdf_path), opinions=opinions, source="dataset", missing=missing, conflicts=conflicts,
        field_sources=sources, case_number=auto.case_number or row.get("case_number"), layout=auto.layout,
        notes="Case details read from the PDF; the Indian Supreme Court Judgments dataset (Dattam Labs, "
              "CC-BY 4.0) filled what the PDF did not give." + (" " + auto.notes if auto.notes else ""),
    )


# ------------------------------------------------------------- IndicLegalQA

_STOP = {"state", "union", "india", "others", "another", "anr", "ors", "versus", "through", "limited",
         "private", "company", "singh", "kumar", "govt", "government", "secretary", "ltd"}


# Words that sit on one side of thousands of cases, so sharing one says nothing
# about whether two case names are the same case: "State of Haryana",
# "Commissioner of Income Tax". Used only for matching, never for naming.
_GENERIC = {
    "andhra", "arunachal", "assam", "bihar", "chhattisgarh", "goa", "gujarat", "haryana", "himachal", "jharkhand",
    "karnataka", "kerala", "madhya", "maharashtra", "manipur", "meghalaya", "mizoram", "nagaland", "odisha",
    "orissa", "punjab", "rajasthan", "sikkim", "tamil", "nadu", "telangana", "tripura", "uttar", "uttarakhand",
    "pradesh", "west", "bengal", "delhi", "jammu", "kashmir", "ladakh", "chandigarh", "puducherry", "pondicherry",
    "territory", "national", "capital",
    "commissioner", "principal", "income", "central", "excise", "customs", "service", "director", "general",
    "collector", "deputy", "assistant", "officer", "registrar", "chief", "department", "ministry", "inspector",
    "police", "station", "investigation", "bureau", "authority", "board", "corporation", "municipal",
    "appellant", "respondent", "petitioner", "anor", "etc",
}


def _name_words(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", name.lower()) if w not in _STOP and w not in _GENERIC}


def _sides_agree(qa_name: str, title: str) -> bool:
    """Every side of "X vs. Y" that has a distinctive word must find one in the
    bucket title. One shared word is not enough: on a busy day "Satpal vs. State
    of Haryana" and "Dilawar v. State of Haryana" share "Haryana" and nothing else."""
    title_words = _name_words(title)
    sides = [_name_words(s) for s in re.split(r"\s+(?:vs?\.?|versus)\s+", qa_name, flags=re.IGNORECASE)]
    sides = [w for w in sides if w]
    return bool(sides) and all(w & title_words for w in sides)


def _qa_date(text: str):
    import datetime as dt

    text = re.sub(r"(\d+)(?:st|nd|rd|th)", r"\1", (text or "").strip())
    for fmt in ("%d %B %Y", "%d %B, %Y", "%B %d, %Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def match_indiclegalqa(pairs: list[dict], judgments: list[Judgment]) -> tuple[dict[tuple, Judgment], list[tuple]]:
    """Which bucket judgment each IndicLegalQA case is: same decision date,
    both sides of the case name found in the title (`_sides_agree`), and the
    most party words in common, taken only when one candidate stands out.
    Returns {(case_name, date_text): judgment} and the unmatched cases."""
    by_date: dict = {}
    for j in judgments:
        by_date.setdefault(j.decision_date, []).append(j)
    matched: dict[tuple, Judgment] = {}
    unmatched: list[tuple] = []
    for key in dict.fromkeys((r["case_name"], r.get("judgement_date") or r.get("judgment_date") or "") for r in pairs):
        date = _qa_date(key[1])
        words = _name_words(key[0])
        scored = sorted(((len(words & _name_words(j.title)), j) for j in by_date.get(date.isoformat() if date else "", [])
                         if _sides_agree(key[0], j.title)),
                        key=lambda x: -x[0])
        if scored and scored[0][0] >= 1 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            matched[key] = scored[0][1]
        else:
            unmatched.append(key)
    return matched, unmatched


def write_indiclegalqa_questions(pairs: list[dict], matched: dict[tuple, Judgment], out: Path) -> int:
    """The question file `evaluate` reads. No paragraph is known for these
    questions, so `paragraphs` is empty and a hit means the right case."""
    import yaml

    questions = []
    for r in pairs:
        key = (r["case_name"], r.get("judgement_date") or r.get("judgment_date") or "")
        j = matched.get(key)
        if j is None:
            continue
        petitioner, respondent = _party(j.petitioner), _party(j.respondent)
        questions.append({
            "query": r["question"], "case_id": case_id_for(petitioner, respondent, _qa_date(key[1]), j.file_stem),
            "paragraphs": [], "answer": r["answer"], "source": "IndicLegalQA", "file": j.file_stem + ".pdf",
        })
    out.parent.mkdir(parents=True, exist_ok=True)
    header = ("# Questions from IndicLegalQA (NIT Srinagar, Mendeley Data, doi:10.17632/gf8n8cnmvc.2, CC BY 4.0),\n"
              "# matched to judgments in the AWS Open Data bucket by decision date and party names.\n"
              "# No paragraph numbers are known, so a hit means a chunk from the right case. `answer` is kept\n"
              "# for reading, not scoring.\n")
    out.write_text(header + yaml.safe_dump({"questions": questions}, sort_keys=False, allow_unicode=True, width=120),
                   encoding="utf-8")
    return len(questions)
