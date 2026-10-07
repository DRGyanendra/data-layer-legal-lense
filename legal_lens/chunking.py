"""Turn paragraphs into chunks for embedding.

The rules, and where each comes from:

1. A chunk is one or more whole numbered paragraphs. The paragraph is the
   unit Indian courts and lawyers cite, and a structure-based unit keeps a
   holding and its reasoning together.
2. Paragraphs are merged only with neighbours of the same rhetorical role,
   under the same section heading, in the same opinion. A chunk never
   mixes counsel's submission with the court's finding. (Role-aware work on
   Supreme Court judgments groups consecutive sentences of one role.)
3. Chunks stay under about 384 tokens, so a passage and the question fit
   together in a cross-encoder's 512-token window without being cut off.
   A longer paragraph is split at sentence ends, with the last two
   sentences repeated at the start of the next part.
4. Each chunk gets a short context line (case, court, date, citation, a
   one-line summary, section heading). It is embedded with the chunk and
   stored separately, so the displayed text stays exactly what the court
   wrote. Legal retrieval often returns a passage from the wrong document,
   because judgments share so much boilerplate; a document-level line on
   every chunk is the published fix.
5. Each chunk knows its neighbours, so the backend can retrieve a small
   passage and hand the model the paragraphs around it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .paragraphs import Paragraph
from .roles import quoted_share

TINY_TOKENS = 30     # 'Leave granted.' is too small to stand alone

# Abbreviations after which a full stop does not end a sentence.
_ABBREVIATIONS = {
    "v", "vs", "dr", "mr", "mrs", "ms", "no", "nos", "sec", "secs", "art", "arts",
    "cr", "hon", "ltd", "co", "anr", "ors", "etc", "viz", "para", "paras", "pp",
    "ed", "vol", "rs", "st", "smt", "shri", "sri", "jj", "cji", "ibid", "supra",
    "i.e", "e.g", "u/s", "r/w",
}


def approx_tokens(text: str) -> int:
    """Rough token count: about four characters per token for English prose.

    Swap in the real BGE-M3 tokenizer through the `count_tokens` argument if
    you need exact budgets; the limit of 8192 tokens is far above these sizes.
    """
    return max(1, round(len(text) / 4))


def split_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    start = 0
    for m in re.finditer(r"[.?!][\"”’)\]]*\s+(?=[A-Z0-9(\[\"“‘])", text):
        before = text[start:m.start()]
        last_word = before.rsplit(None, 1)[-1] if before.split() else ""
        token = last_word.strip("([\"“‘").lower()
        if token in _ABBREVIATIONS or re.fullmatch(r"[a-z]", token) or re.fullmatch(r"\d{1,4}", token):
            continue  # 'v.', an initial, or a paragraph/list number
        sentences.append(text[start:m.end()].strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


@dataclass
class Chunk:
    chunk_id: str
    segment: int
    para_start: int
    para_end: int
    part: int                 # 0 unless a long paragraph was split
    page_start: int
    text: str
    tokens: int
    role: str = "unlabelled"
    heading: str | None = None
    quoted_share: float = 0.0
    labels: tuple[str, ...] = ()      # the court's own numbers for the paragraphs inside

    @property
    def span(self) -> str:
        """'24', '12-14' or '5.1-5.3', as the court numbers them."""
        if not self.labels:
            return str(self.para_start) if self.para_start == self.para_end else f"{self.para_start}-{self.para_end}"
        return self.labels[0] if len(set(self.labels)) == 1 else f"{self.labels[0]}-{self.labels[-1]}"


def _chunk_id(case_id: str, segment: int, first: str, last: str, part: int | None) -> str:
    cid = f"{case_id}_o{segment}_p{first}"
    if last != first:
        cid += f"-{last}"
    if part is not None:
        cid += f"_{part}"
    return cid


def _split_long(text: str, max_tokens: int, count: Callable[[str], int], overlap_sentences: int) -> list[str]:
    sentences: list[str] = []
    for sentence in split_sentences(text):
        if count(sentence) <= max_tokens:
            sentences.append(sentence)
            continue
        # A statute extract or a long quotation can be one 'sentence' far over the
        # limit. Cut it at clause ends where possible, then at words.
        piece: list[str] = []
        for word in sentence.split():
            piece.append(word)
            size = count(" ".join(piece))
            if size >= max_tokens * 0.9 or (size >= max_tokens * 0.6 and word[-1:] in ";:,—"):
                sentences.append(" ".join(piece))
                piece = []
        if piece:
            sentences.append(" ".join(piece))
    parts: list[str] = []
    current: list[str] = []
    fresh = 0                       # sentences in `current` that are not repeated overlap
    for sentence in sentences:
        if current and count(" ".join(current + [sentence])) > max_tokens:
            if fresh:
                parts.append(" ".join(current))
                carry = current[-overlap_sentences:] if overlap_sentences else []
                # Repeating long sentences would leave no room for new text.
                while carry and count(" ".join(carry + [sentence])) > max_tokens:
                    carry = carry[1:]
                current = list(carry)
            else:
                current = []        # overlap plus this sentence would not fit: drop the overlap
            fresh = 0
        current.append(sentence)
        fresh += 1
    if fresh:
        parts.append(" ".join(current))
    return parts


def chunk_paragraphs(
    case_id: str,
    paragraphs: list[Paragraph],
    roles: list[str] | None = None,
    *,
    target_tokens: int = 200,
    max_tokens: int = 384,
    overlap_sentences: int = 2,
    count_tokens: Callable[[str], int] = approx_tokens,
) -> list[Chunk]:
    """Group paragraphs into chunks. `roles` is one label per paragraph."""
    roles = roles or ["unlabelled"] * len(paragraphs)
    chunks: list[Chunk] = []
    group: list[tuple[Paragraph, str]] = []

    def group_role() -> str:
        return next((r for _, r in group if r != "unlabelled"), "unlabelled")

    def group_is_tiny_lead() -> bool:
        """Only tiny, unlabelled paragraphs so far: let them ride with what follows."""
        return all(r == "unlabelled" and count_tokens(p.text) < TINY_TOKENS for p, r in group)

    def flush() -> None:
        if not group:
            return
        paras = [p for p, _ in group]
        text = " ".join(p.text for p in paras)
        heading = next((p.heading for p in reversed(paras) if p.heading), None)
        chunks.append(
            Chunk(
                _chunk_id(case_id, paras[0].segment, paras[0].label, paras[-1].label, None),
                paras[0].segment, paras[0].number, paras[-1].number, 0, paras[0].page,
                text, count_tokens(text), group_role(), heading, quoted_share(text),
                tuple(p.label for p in paras),
            )
        )
        group.clear()

    for para, role in zip(paragraphs, roles):
        size = count_tokens(para.text)
        if group:
            same_opinion = group[0][0].segment == para.segment
            fits = count_tokens(" ".join(p.text for p, _ in group)) + size <= target_tokens
            compatible = group_is_tiny_lead() or (
                role == group_role() and para.heading == group[-1][0].heading
            )
            if not (same_opinion and fits and compatible and size <= max_tokens):
                flush()
        if size > max_tokens:
            flush()
            pieces = _split_long(para.text, max_tokens, count_tokens, overlap_sentences)
            for part, piece in enumerate(pieces):
                chunks.append(
                    Chunk(
                        _chunk_id(case_id, para.segment, para.label, para.label, part),
                        para.segment, para.number, para.number, part, para.page,
                        piece, count_tokens(piece), role, para.heading, quoted_share(piece),
                        (para.label,),
                    )
                )
            continue
        group.append((para, role))
    flush()
    return chunks


def context_line(*, case_name: str, court: str, date: str, citation: str,
                 summary: str | None, heading: str | None) -> str:
    """The document-level line embedded in front of every chunk of a case.

    Kept generic on purpose: the study that introduced this found a plain
    summary worked better than one engineered around legal elements.
    """
    parts = [", ".join(p for p in (case_name, court, date) if p)]
    if citation:
        parts[0] += f", {citation}"
    line = parts[0] + "."
    if summary:
        line += " " + summary.strip().rstrip(".") + "."
    if heading:
        line += f" Section: {heading}."
    return line
