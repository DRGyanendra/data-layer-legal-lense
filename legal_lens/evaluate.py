"""Measure retrieval instead of assuming it.

Give it questions whose answer you know: the case and the paragraph(s)
where the answer is. It reports, for the top k results:

  hit@k   share of questions where a retrieved chunk covers a gold paragraph
  MRR     mean of 1/rank of the first such chunk (0 when none)
  DRM@k   document-level retrieval mismatch: the share of retrieved chunks
          that come from the wrong case altogether

Run it before and after any change to chunking, the context line or the
embedding model. Twenty to thirty questions written by someone who has
read the judgments are enough to see a real difference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from .vector_store import Hit


@dataclass
class Question:
    query: str
    case_id: str
    paragraphs: list[int]
    opinion_index: int | None = None


@dataclass
class Report:
    k: int
    questions: int
    hit_at_k: float
    mrr: float
    drm_at_k: float
    misses: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"questions   {self.questions}",
            f"hit@{self.k:<6} {self.hit_at_k:.2f}",
            f"MRR         {self.mrr:.2f}",
            f"DRM@{self.k:<6} {self.drm_at_k:.2f}   (lower is better)",
        ]
        if self.misses:
            out.append("missed:")
            out.extend(f"  - {m}" for m in self.misses)
        return out


def load_questions(path: str | Path) -> list[Question]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    questions = []
    for raw in data.get("questions", []):
        paragraphs = raw["paragraphs"]
        questions.append(Question(
            raw["query"], raw["case_id"],
            [paragraphs] if isinstance(paragraphs, int) else list(paragraphs),
            raw.get("opinion_index"),
        ))
    return questions


def _covers(hit: Hit, question: Question) -> bool:
    p = hit.payload
    if p.get("case_id") != question.case_id:
        return False
    if question.opinion_index is not None and p.get("opinion_index") != question.opinion_index:
        return False
    return any(p["paragraph_num"] <= n <= p["para_end"] for n in question.paragraphs)


def score(questions: list[Question], search: Callable[[str, int], list[Hit]], k: int = 5) -> Report:
    """`search(query, k)` returns ranked hits."""
    hits_found = 0
    reciprocal = 0.0
    wrong_doc = retrieved = 0
    misses: list[str] = []
    for question in questions:
        hits = search(question.query, k)
        retrieved += len(hits)
        wrong_doc += sum(1 for h in hits if h.payload.get("case_id") != question.case_id)
        rank = next((i for i, h in enumerate(hits, start=1) if _covers(h, question)), None)
        if rank:
            hits_found += 1
            reciprocal += 1 / rank
        else:
            got = ", ".join(f"{h.payload.get('case_id')} p{h.payload.get('paragraph_num')}" for h in hits[:3])
            misses.append(f"{question.query!r} wanted {question.case_id} para {question.paragraphs}; got {got or 'nothing'}")
    n = max(1, len(questions))
    return Report(k, len(questions), hits_found / n, reciprocal / n,
                  wrong_doc / max(1, retrieved), misses)
