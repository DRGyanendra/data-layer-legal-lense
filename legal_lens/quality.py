"""A verdict on every judgment that goes through the chunker.

No splitter can be shown to handle every layout the Supreme Court has ever
used. What it can do is refuse to be silently wrong: each judgment is
checked against its own text and given one of three verdicts.

- `ok`      nothing found; load it.
- `review`  it loaded, but something a person should look at is listed.
            Typically: no paragraph numbers (cited by page), OCR, or several
            opinions whose kind is not known.
- `reject`  the result is not fit to load: text is missing, or unreadable.

The verdict is stored on every chunk as `parse_quality`, so the backend can
choose to hold back or label what came from a `review` judgment.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .pdf_extract import text_quality

_WORD = re.compile(r"[A-Za-z0-9]+")
MAX_TOKENS = 384


@dataclass
class Verdict:
    status: str = "ok"                                   # 'ok', 'review' or 'reject'
    reasons: list[tuple[str, str]] = field(default_factory=list)      # (level, what was found)
    figures: dict = field(default_factory=dict)

    def add(self, level: str, reason: str) -> None:
        self.reasons.append((level, reason))             # 'note' never changes the verdict
        if level == "reject" or (level == "review" and self.status == "ok"):
            self.status = level

    def lines(self) -> list[str]:
        return [f"{level}: {reason}" for level, reason in self.reasons]


def _words(text: str) -> Counter:
    return Counter(w.lower() for w in _WORD.findall(text))


def assess(built) -> Verdict:
    """Check one built case against its own text. `built` is a pipeline.BuiltCase."""
    verdict = Verdict()
    split, extraction, meta = built.split, built.extraction, built.meta
    passages = [p for p in built.payloads if p["doc_type"] == "judgment"]
    pages = len(extraction.pages)

    # ---- is there a judgment here at all
    unread = [p.number for p in extraction.pages if p.needs_ocr]
    if unread:
        verdict.add("reject", f"{len(unread)} page(s) are scans that could not be read. Install Tesseract and run again.")
    body_words = sum(len(_WORD.findall(p.text)) for p in split.paragraphs)
    if not passages or body_words < 30:
        verdict.add("reject", f"Only {body_words} words of judgment text were found. This may not be a judgment, or the PDF has no readable text.")
        verdict.figures = {"pages": pages, "opinions": len(split.opinions), "paragraphs": split.count, "chunks": len(passages)}
        return verdict
    if (meta.source == "auto" and "parties" in meta.missing and "bench" in meta.missing
            and not any(o.author_line for o in split.opinions)):
        verdict.add("reject", "No parties, no bench and no judge's name were found. This does not look like a judgment. "
                              "If it is one, give it an entry in data/cases.yaml.")

    # ---- nothing lost: PDF -> paragraphs -> chunks
    in_pdf = Counter()
    for line in extraction.lines:
        in_pdf.update(w.lower() for w in _WORD.findall(line.text))
    in_paragraphs = Counter()
    for p in split.paragraphs:
        in_paragraphs.update(w.lower() for w in _WORD.findall(p.text))
    in_chunks = Counter()
    for p in passages:
        in_chunks.update(w.lower() for w in _WORD.findall(p["text"]))
    accounted = in_paragraphs + _words(split.header) + _words(" ".join(split.headings))
    total = sum(in_pdf.values()) or 1
    outside = sum((in_pdf - accounted).values()) / total
    dropped = sum((in_paragraphs - in_chunks).values())
    if dropped:
        verdict.add("reject", f"{dropped} word(s) of paragraph text did not reach any chunk.")
    if outside > 0.25:
        verdict.add("reject", f"{outside:.0%} of the PDF's text is in no paragraph. The layout was not understood.")
    elif outside > 0.06:
        verdict.add("review", f"{outside:.0%} of the PDF's text is in no paragraph (cause titles and signatures usually account for 1 to 6%).")

    # ---- chunk sizes
    over = [p["chunk_id"] for p in passages if p["tokens"] > MAX_TOKENS]
    if over:
        verdict.add("reject", f"{len(over)} chunk(s) are over {MAX_TOKENS} tokens, for example {over[0]}.")

    # ---- paragraph numbering
    by_opinion: dict[int, list[int]] = defaultdict(list)
    for p in split.paragraphs:
        by_opinion[p.segment].append(p.number)
    gaps = []
    for opinion in split.opinions:
        numbers = sorted(set(by_opinion.get(opinion.index, [])))
        if opinion.numbering != "original" or not numbers:
            continue
        missing = sorted(set(range(numbers[0], numbers[-1] + 1)) - set(numbers))
        if missing:
            gaps.append(f"opinion {opinion.index + 1} skips {missing[:6]}{'...' if len(missing) > 6 else ''}")
    if gaps:
        verdict.add("review", "Paragraph numbers are not continuous: " + "; ".join(gaps) + ". A number may have been misread, or the court skipped it.")
    unnumbered = [o for o in split.opinions if o.numbering == "synthetic"]
    if unnumbered:
        which = "The judgment has" if len(unnumbered) == len(split.opinions) else f"{len(unnumbered)} of {len(split.opinions)} opinions have"
        verdict.add("review", f"{which} no paragraph numbers. Those passages are cited by page, not by paragraph.")
    if pages >= 12 and split.count < pages / 8:
        verdict.add("review", f"Only {split.count} paragraphs were found in {pages} pages. Paragraph breaks may have been missed.")

    # ---- opinions
    if len(split.opinions) > 1:
        kinds = [next((p["opinion_type"] for p in passages if p["opinion_index"] == o.index), "unknown") for o in split.opinions]
        unknown = kinds.count("unknown")
        if unknown:
            verdict.add("review", f"{unknown} of {len(split.opinions)} opinions are not marked majority, concurring or dissenting. "
                                  "Passages from them must not be shown as the court's holding until that is filled in.")
    nameless = [o for o in split.opinions if not o.author_line]
    if nameless and len(nameless) < len(split.opinions):
        verdict.add("review", f"{len(nameless)} of {len(split.opinions)} opinions do not name their author.")
    rough = [o for o in split.opinions if o.approximate]
    if rough:
        verdict.add("review", f"{len(rough)} opinion(s) start at a point placed from the page header, not found in the text.")

    # ---- how the text was read
    scanned = sum(1 for p in extraction.pages if p.ocr)
    if scanned:
        verdict.add("review", f"{scanned} of {pages} pages were read by OCR. Check section numbers and names against the PDF.")
    garbled = text_quality(extraction.pages)
    if garbled > 0.03:
        verdict.add("reject", f"About {garbled:.0%} of words are malformed. The text is too damaged to cite; find a cleaner copy.")
    elif garbled > 0.008:
        verdict.add("review", f"About {garbled:.1%} of words are malformed (poor OCR in the PDF). Names, numbers and citations from it are unreliable.")
    columns = sum(1 for p in extraction.pages if getattr(p, "columns", 1) == 2)
    if columns:
        verdict.add("review", f"{columns} page(s) are in two columns; check that the columns were read in order.")
    if split.layout == "law_report":
        verdict.add("review", "This is a law-report copy: the headnote was left out and page numbers are pages of this PDF.")

    # ---- the case record
    if meta.source == "auto":
        if "parties" in meta.missing:
            verdict.add("review", "The parties' names were not found; the case is named after the file.")
        if "date" in meta.missing:
            verdict.add("review", "No decision date was found.")
        if "bench" in meta.missing:
            verdict.add("review", "The bench was not found.")
        if "citation" in meta.missing:
            verdict.add("note", "No reported citation is printed in the PDF (the court's own PDFs rarely carry one). "
                                "Passages are cited by case name and date until one is added in data/cases.yaml.")
    if meta.source == "dataset":
        for conflict in meta.conflicts:
            verdict.add("review", conflict)
    if meta.court != "Supreme Court":
        verdict.add("review", f"The court is '{meta.court}', not the Supreme Court.")

    sizes = sorted(p["tokens"] for p in passages)
    verdict.figures = {
        "pages": pages, "opinions": len(split.opinions), "paragraphs": split.count, "chunks": len(passages),
        "tokens_median": sizes[len(sizes) // 2], "tokens_max": sizes[-1], "text_outside_paragraphs": round(outside, 3),
    }
    return verdict
