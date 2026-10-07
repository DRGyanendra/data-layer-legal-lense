"""PDF in, chunks and graph facts out.

`build_case` does everything that needs no database: read the PDF, split
it into paragraphs, chunk, find statute references and case citations, and
check the manifest record against the text. `ingest_case` then embeds the
chunks and writes to Qdrant and Neo4j.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .chunking import Chunk, approx_tokens, chunk_paragraphs, context_line
from .citations import find_case_refs, find_statute_refs
from .embed import Embedder
from .graph_store import GraphWriter
from .ids import base_section, citation_key, judge_id, point_id
from .autometa import opinion_from, read_meta
from .manifest import CaseMeta, check_against_text
from .mapping import OLD_CODES, Mapping
from .paragraphs import SplitResult, split_paragraphs
from .roles import label_paragraphs, state_side
from .pdf_extract import Extraction, extract_pdf
from .vector_store import Point, VectorStore


class DuplicateCaseError(RuntimeError):
    pass


@dataclass
class BuiltCase:
    meta: CaseMeta
    extraction: Extraction
    split: SplitResult
    chunks: list[Chunk]
    payloads: list[dict]
    statute_refs: list[dict]          # one per provision, with the paragraphs citing it
    case_refs: list[dict]             # one per cited case
    unmapped: list[str]               # IPC/CrPC provisions cited but absent from the mapping table
    warnings: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)     # one per paragraph
    verdict: object | None = None                      # quality.Verdict: ok, review or reject, with reasons

    def embedding_texts(self, with_context: bool = True) -> list[str]:
        """What is embedded: the context line, then the passage."""
        if not with_context:
            return [c.text for c in self.chunks]
        return [f"{p['context']}\n\n{c.text}" if p["context"] else c.text
                for c, p in zip(self.chunks, self.payloads)]

    @property
    def stats(self) -> dict:
        sizes = [c.tokens for c in self.chunks] or [0]
        return {
            "pages": len(self.extraction.pages),
            "backend": self.extraction.backend,
            "paragraphs": self.split.count,
            "para_numbering": self.split.numbering,
            "opinion_segments": self.split.segments,
            "chunks": len(self.chunks),
            "chunk_tokens_min": min(sizes),
            "chunk_tokens_median": int(statistics.median(sizes)),
            "chunk_tokens_max": max(sizes),
            "statutes_cited": len(self.statute_refs),
            "cases_cited": len(self.case_refs),
            "headings_found": len(self.split.headings),
            "blocks": len({p["block_id"] for p in self.payloads if p["block_id"]}),
            "not_substantive": sum(1 for p in self.payloads if not p["substantive"]),
            "roles": dict(Counter(c.role for c in self.chunks).most_common()),
        }


def _surname(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.split()[-1].lower())


def _opinion_for_segment(meta: CaseMeta, split: SplitResult, warnings: list[str]):
    """Which opinion each segment belongs to: {segment: (author, type)}.

    The judge's name printed at the head of each opinion decides. Position is
    used only when no names were found and the counts agree.
    """
    if not meta.opinions:
        return {}
    if meta.source == "auto":                 # read from these very opinions, in the same order
        return {o.index: (meta.opinions[o.index].author or None, meta.opinions[o.index].type)
                for o in split.opinions if o.index < len(meta.opinions)}
    found: dict[int, tuple[str, str]] = {}
    for opinion in split.opinions:
        line = re.sub(r"[^a-z ]", "", (opinion.author_line or "").lower())
        # 'for himself and A.M. Khanwilkar, J.' names a second judge; the author comes first
        hits = [(line.find(_surname(o.author)), o) for o in meta.opinions if _surname(o.author) in line.split()]
        if hits:
            author = min(hits, key=lambda h: h[0])[1]
            found[opinion.index] = (author.author, author.type)
    if not found and len(meta.opinions) == split.segments:
        return {i: (o.author, o.type) for i, o in enumerate(meta.opinions)}
    missing = [o for o in split.opinions if o.index not in found]
    if missing or len(found) != len(meta.opinions):
        seen = "; ".join(f"page {o.page}: {o.author_line or 'no name found'}" for o in split.opinions)
        warnings.append(
            f"The manifest lists {len(meta.opinions)} opinion(s); the PDF has {split.segments} "
            f"({seen}). {len(missing)} could not be matched to a judge and are left 'unknown', "
            "because a passage must not be labelled majority when it may be a dissent."
        )
    return found


_BOILERPLATE = re.compile(
    r"^\W*\d*\W*(?:leave granted|delay condoned|heard(?: the)? learned counsel|permission to file"
    r"|application for [a-z ]+ is allowed|list (?:the matter|after)|issue notice|tag with)\b[^.]{0,120}\.?\s*$",
    re.IGNORECASE)


def _is_substantive(chunk: Chunk, has_refs: bool) -> bool:
    """False for passages that carry no law or fact worth retrieving.

    'Leave granted.' matches almost any query a little and answers none. Such
    chunks stay in the store, so neighbour expansion still reads them, but
    search skips them. The operative order is always kept.
    """
    if chunk.role == "ruling" or has_refs:
        return True
    if _BOILERPLATE.match(chunk.text):
        return False
    return chunk.tokens >= 25


def _cite_as(meta: CaseMeta, chunk: Chunk, numbering: str, author: str | None, kind: str, several: bool) -> str:
    """The exact citation string for the prompt and the citation checker."""
    label = meta.name + (f", {meta.citation}" if meta.citation else
                         f", decided {meta.date_words}" if meta.date else "")
    if numbering != "original":
        label += f", page {chunk.page_start}"
    elif "-" not in chunk.span:
        label += f", para {chunk.span}"
    else:
        label += f", paras {chunk.span}"
    if author and several:
        label += f" (per {author}, {kind})" if kind != "unknown" else f" (per {author})"
    return label


def _case_card(meta: CaseMeta, statutes: list[dict], cases: list[dict]) -> str:
    """A short description of the case, built from the manifest, not the judgment.

    No paragraph of a judgment says 'this is Jacob Mathew'. The card gives
    searches by case name, citation or subject something to land on.
    """
    lines = [f"{meta.name}. {meta.court}" + (f", decided {meta.date_words}." if meta.date else ".")]
    if meta.citations:
        lines.append("Citations: " + "; ".join(meta.citations) + ".")
    if meta.bench:
        lines.append("Bench: " + ", ".join(meta.bench) + ".")
    if meta.summary:
        lines.append(meta.summary.rstrip(".") + ".")
    top = sorted(statutes, key=lambda s: -s["mentions"])[:8]
    if top:
        lines.append("Provisions discussed: " + ", ".join(f"{s['code']} {s['section']}" for s in top) + ".")
    named = [c["name_hint"] for c in sorted(cases, key=lambda c: -len(c["paragraphs"])) if c["name_hint"]][:6]
    if named:
        lines.append("Cases referred to: " + "; ".join(named) + ".")
    return " ".join(lines)


def _auto_summary(statutes: list[dict]) -> str | None:
    """A stand-in for the one-line summary when nobody has written one: the
    provisions the judgment is mostly about. Facts from the text, not a view."""
    top = [s for s in sorted(statutes, key=lambda s: -s["mentions"]) if s["mentions"] >= 2][:4]
    if not top:
        return None
    def label(s: dict) -> str:
        return f"Article {s['section']} of the Constitution" if s["code"] == "Constitution" else f"{s['code']} {s['section']}"
    return "Mainly concerns " + ", ".join(label(s) for s in top)


def build_case(
    meta: CaseMeta | None,
    pdf_path: str | Path,
    mapping: Mapping,
    *,
    backend: str = "auto",
    ocr: bool = True,
    target_tokens: int = 200,
    max_tokens: int = 384,
    cache_dir: str | Path | None = None,
) -> BuiltCase:
    """Read, split and chunk one judgment. With `meta=None` the case details
    are read from the PDF itself."""
    extraction = extract_pdf(pdf_path, backend=backend, ocr=ocr, cache_dir=cache_dir)
    split = split_paragraphs(extraction.lines)
    warnings = list(extraction.warnings) + list(split.notes)
    if meta is None:
        meta = read_meta(pdf_path, extraction.lines, split)
        if meta.notes:
            warnings.append(meta.notes)
    warnings += check_against_text(meta, extraction.text)
    unnumbered = [o for o in split.opinions if o.numbering == "synthetic"]
    if unnumbered:
        where = "the judgment" if len(unnumbered) == len(split.opinions) else \
            "the opinion(s) starting on page " + ", ".join(str(o.page) for o in unnumbered)
        warnings.append(
            f"No paragraph numbers were found in {where}, so paragraphs there are numbered by "
            "position. These are not the court's numbers; those chunks carry "
            "para_numbering='synthetic' and are cited by page."
        )
    opinions = _opinion_for_segment(meta, split, warnings)
    # An opinion the record does not cover still says who wrote it. Keep the name;
    # its kind stays 'unknown' until a person fills it in.
    for found in split.opinions:
        if found.index not in opinions and found.author_line:
            author, joined, _ = opinion_from(found.author_line, meta.bench)
            opinions[found.index] = (" and ".join([author, *joined]), "unknown")
    several = len(split.opinions) > 1
    own_keys = set(meta.citation_keys)

    # Footnotes belong to the paragraph that is running on their page. Citations
    # often sit in footnotes, so they count towards that paragraph's references.
    notes: dict[tuple[int, int], list[str]] = {}
    for page, items in sorted(extraction.footnotes_by_page.items()):
        owner = next((p for p in reversed(split.paragraphs) if p.page <= page), None)
        if owner:
            notes.setdefault((owner.segment, owner.label), []).extend(items)

    # Graph facts, gathered paragraph by paragraph so each keeps its paragraph numbers.
    statutes: dict[str, dict] = {}
    cases: dict[str, dict] = {}
    for para in split.paragraphs:
        with_notes = para.text + " " + " ".join(notes.get((para.segment, para.label), []))
        for ref in find_statute_refs(with_notes):
            entry = statutes.setdefault(
                ref.statute_id,
                {"statute_id": ref.statute_id, "code": ref.code, "section": ref.section,
                 "base_section": base_section(ref.section),
                 "is_active": ref.code not in OLD_CODES, "paragraphs": [], "mentions": 0},
            )
            entry["mentions"] += 1
            if para.number not in entry["paragraphs"]:
                entry["paragraphs"].append(para.number)
        for ref in find_case_refs(with_notes):
            if own_keys & set(ref.keys):
                continue
            entry = cases.setdefault(
                ref.keys[0],
                {"citation_keys": [], "citations": [], "name_hint": ref.name_hint, "paragraphs": []},
            )
            entry["name_hint"] = entry["name_hint"] or ref.name_hint
            for key, citation in zip(ref.keys, ref.citations):
                if key not in entry["citation_keys"]:
                    entry["citation_keys"].append(key)
                    entry["citations"].append(citation)
            if para.number not in entry["paragraphs"]:
                entry["paragraphs"].append(para.number)
    for entry in statutes.values():
        entry["paragraphs"].sort()

    unmapped = sorted(
        sid for sid, s in statutes.items()
        if s["code"] in OLD_CODES and mapping.lookup(s["code"], s["section"])[1] == "none"
    )
    if unmapped:
        warnings.append(
            f"{len(unmapped)} IPC/CrPC provision(s) cited here are not in the mapping table yet: "
            + ", ".join(unmapped[:12]) + ("..." if len(unmapped) > 12 else "")
        )

    roles = label_paragraphs(split.paragraphs, state_side(meta.name))
    chunks = chunk_paragraphs(meta.case_id, split.paragraphs, roles,
                              target_tokens=target_tokens, max_tokens=max_tokens)
    summary = meta.summary or _auto_summary(list(statutes.values()))
    if not meta.summary and meta.source == "manifest":
        warnings.append(
            "No `summary` in the manifest for this case. Add one line saying what the case "
            "decided; it is embedded with every chunk and helps retrieval pick the right case."
        )
    # A block is a run of chunks with one speaker and one heading. Expanding a
    # hit within its block never drags counsel's argument into the court's reasoning.
    block_of: list[tuple[str, int]] = []
    block_no, position, previous_key = -1, 0, None
    for chunk in chunks:
        key = (chunk.segment, chunk.role, chunk.heading)
        if key != previous_key:
            block_no, position, previous_key = block_no + 1, 0, key
        block_of.append((f"{meta.case_id}_b{block_no}", position))
        position += 1

    payloads = []
    for index, chunk in enumerate(chunks):
        footnotes = []
        if chunk.part == 0:
            for label in chunk.labels:
                footnotes.extend(notes.get((chunk.segment, label), []))
        searchable = chunk.text + " " + " ".join(footnotes)
        numbering = split.numbering_of(chunk.segment)
        refs = find_statute_refs(searchable)
        case_refs = [r for r in find_case_refs(searchable) if not own_keys & set(r.keys)]
        cited = list(dict.fromkeys(r.statute_id for r in refs))
        current = list(dict.fromkeys(
            new_id for r in refs for new_id in mapping.successors(r.code, r.section)
        ))
        author, kind = opinions.get(chunk.segment, (None, "unknown"))
        keywords = list(dict.fromkeys(
            [f"{r.code} {r.section}" for r in refs]
            + [sid.replace(":", " ") for sid in current]
            + [c for r in case_refs for c in r.citations]
            + [r.name_hint for r in case_refs if r.name_hint]
        ))
        payloads.append({
            "doc_type": "judgment",
            "case_id": meta.case_id,
            "case_name": meta.name,
            "chunk_id": chunk.chunk_id,
            "paragraph_num": chunk.para_start,
            "para_end": chunk.para_end,
            "para_label": chunk.span,
            "chunk_part": chunk.part,
            "para_numbering": numbering,
            "page_start": chunk.page_start,
            "court": meta.court,
            "date": meta.date_iso,
            "citation": meta.citation,
            "citations": meta.citations,
            "judges": meta.bench,
            "meta_source": meta.source,
            "opinion_index": chunk.segment,
            "opinion_author": author,
            "opinion_type": kind,
            "statutes": cited,
            "statutes_current": current,
            "rhetorical_role": chunk.role,
            "role_source": "heuristic",
            "section_heading": chunk.heading,
            "quoted_share": chunk.quoted_share,
            "prev_chunk_id": chunks[index - 1].chunk_id if index else None,
            "next_chunk_id": chunks[index + 1].chunk_id if index + 1 < len(chunks) else None,
            "block_id": block_of[index][0],
            "block_index": block_of[index][1],
            "parent_id": re.sub(r"_\d+$", "", chunk.chunk_id),
            "tokens": chunk.tokens,
            "substantive": _is_substantive(chunk, bool(refs or case_refs)),
            "cited_cases": list(dict.fromkeys(k for r in case_refs for k in r.keys)),
            "keywords": keywords,
            "cite_as": _cite_as(meta, chunk, numbering, author, kind, several),
            "footnotes": footnotes,
            "context": context_line(
                case_name=meta.name, court=meta.court,
                date=meta.date_words,
                citation=meta.citation, summary=summary, heading=chunk.heading,
            ),
            "text": chunk.text,
            "doc_sha256": extraction.sha256,
        })

    # One extra record per case: the case card. It is last, and outside the reading chain.
    card_text = _case_card(meta, list(statutes.values()), list(cases.values()))
    top_statutes = [s["statute_id"] for s in sorted(statutes.values(), key=lambda s: -s["mentions"])[:8]]
    card = Chunk(f"{meta.case_id}_card", 0, 0, 0, 0, 1, card_text, approx_tokens(card_text), "case_card")
    chunks.append(card)
    payloads.append({
        "doc_type": "case_card",
        "case_id": meta.case_id, "case_name": meta.name, "chunk_id": card.chunk_id,
        "paragraph_num": 0, "para_end": 0, "para_label": "", "chunk_part": 0, "para_numbering": split.numbering,
        "page_start": 1, "court": meta.court, "date": meta.date_iso,
        "citation": meta.citation, "citations": meta.citations, "judges": meta.bench, "meta_source": meta.source,
        "opinion_index": 0, "opinion_author": None, "opinion_type": "unknown",
        "statutes": top_statutes,
        "statutes_current": list(dict.fromkeys(
            n for s in top_statutes for n in mapping.successors(*s.split(":", 1)))),
        "rhetorical_role": "case_card", "role_source": "manifest", "section_heading": None,
        "quoted_share": 0.0, "prev_chunk_id": None, "next_chunk_id": None,
        "block_id": None, "block_index": 0, "parent_id": card.chunk_id, "tokens": card.tokens,
        "substantive": True, "cited_cases": [],
        "keywords": [meta.name, *meta.citations],
        "cite_as": meta.name + (f", {meta.citation}" if meta.citation else ""),
        "footnotes": [], "context": "", "text": card_text, "doc_sha256": extraction.sha256,
    })

    built = BuiltCase(meta, extraction, split, chunks, payloads,
                      list(statutes.values()), list(cases.values()), unmapped, warnings, roles)
    from .quality import assess
    built.verdict = assess(built)
    for payload in payloads:
        payload["parse_quality"] = built.verdict.status
    return built


def ingest_case(
    built: BuiltCase,
    embedder: Embedder,
    store: VectorStore | None,
    graph: GraphWriter | None,
    *,
    force: bool = False,
    with_context: bool = True,
) -> dict:
    """Write one built case to the vector store and the graph. Returns counts.

    `with_context=False` embeds the bare passage, for comparing against the
    default in an evaluation. Use a separate collection for that.
    """
    meta = built.meta
    result = {"case_id": meta.case_id, "points": 0, "statute_edges": 0, "citation_edges": 0}

    if store is not None:
        duplicate = store.find_duplicate(built.extraction.sha256, meta.case_id)
        if duplicate and not force:
            raise DuplicateCaseError(
                f"This PDF is already loaded as '{duplicate}'. Loading it again as "
                f"'{meta.case_id}' would duplicate every passage. Use --force if that is intended."
            )
        vectors = embedder.embed(built.embedding_texts(with_context))
        points = [
            Point(point_id(c.chunk_id), v,
                  {**p, "embedding_model": embedder.name, "embedded_with_context": with_context})
            for c, v, p in zip(built.chunks, vectors, built.payloads)
        ]
        result["points"] = store.replace_case(meta.case_id, points)

    if graph is not None:
        graph.upsert_case(
            case_id=meta.case_id, name=meta.name, date=meta.date_iso, court=meta.court,
            citations=meta.citations, citation_keys=meta.citation_keys, outcome=meta.outcome,
            doc_sha256=built.extraction.sha256, paragraph_count=built.split.count,
            para_numbering=built.split.numbering,
        )
        graph.set_bench(meta.case_id, meta.judges())
        graph.set_opinions(meta.case_id, [
            {"judge_id": judge_id(o.author), "name": o.author, "type": o.type, "index": i}
            for i, o in enumerate(meta.opinions)
        ])
        graph.link_statutes(meta.case_id, built.statute_refs)
        result["statute_edges"] = len(built.statute_refs)
        # A case named under `overrules` in the manifest gets an edge even if the
        # text cites it in a form the extractor did not recognise.
        case_refs = list(built.case_refs)
        found = {k for r in case_refs for k in r["citation_keys"]}
        for citation in meta.overrules:
            if citation_key(citation) not in found:
                case_refs.append({"citation_keys": [citation_key(citation)], "citations": [citation],
                                  "name_hint": None, "paragraphs": []})
        result["citation_edges"] = graph.link_citations(meta.case_id, case_refs)
        graph.mark_overruled(meta.case_id, [citation_key(c) for c in meta.overrules])
    return result
