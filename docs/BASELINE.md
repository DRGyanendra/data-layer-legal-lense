# Phase 2 baseline — recorded 7 October 2026

The first measured retrieval numbers for the data layer. Every planned
improvement (per-chunk context sentences, OpenNyAI role model, embedding-model
comparison, fuller IPC/BNS table) is judged against these; nothing is adopted
unless it beats them on the same question sets.

## What was measured

| | |
| --- | --- |
| Embedding model | `BAAI/bge-m3`, dense, 1024 dimensions, L2-normalised, context line + passage embedded |
| Vector store | Qdrant Cloud, collection `judgment_chunks`, cosine; search over `substantive` chunks only, no filters, no reranker |
| Graph | Neo4j Aura (HTTPS Query API) — loaded alongside, not used by these scores |
| Chunking | whole numbered paragraphs, merged within one role/opinion to ~200 tokens, split at sentence ends over 384 |
| Judgments loaded | **67**: the 7 curated cases (`data/raw/`) and the 60-judgment IndicLegalQA subset (`data/raw/indiclegalqa/`) |
| Points in Qdrant | **7,917** = 7,850 passages (4,360 cited by the court's paragraph number, 3,490 by page) + 67 case cards |
| Neo4j | 883 `Case` (67 loaded, 816 cited-case stubs), 75 `Judge`, 740 `Statute`; 66-row IPC/CrPC mapping loaded |

## 5a. Right case — IndicLegalQA, 478 questions over 60 judgments

Source: IndicLegalQA (NIT Srinagar, CC BY 4.0), `data/eval.indiclegalqa.60.yaml`.
The set has no paragraph numbers, so a hit is any passage from the right case.

| k | hit@k | MRR | DRM@k (share of retrieved chunks from the wrong case; lower is better) |
| --- | --- | --- | --- |
| 5 | **0.84** | **0.73** | **0.27** |
| 10 | 0.86 | 0.73 | 0.31 |

77 of 478 questions miss at k=5. They clustered on four cases, but three of
those clusters were labelling errors, not retrieval misses: the IndicLegalQA →
bucket matcher accepted a same-day judgment sharing one word with the case
name, so 25 questions about *Satpal v. State of Haryana*, *M.D. Jain v.
Bhagyavathi* and *Western Coalfields v. Commissioner of Central Excise* were
scored against *Dilawar*, *Greater Bombay Co-operative Bank* and *PCIT
Shimla*. The two-sided matcher (October 2026) removes them; the corrected set
has 453 questions and scores hit@5 0.88 (see the record below). *Menoka Malik*
(5 misses) is the one genuine retrieval cluster. Many IndicLegalQA questions name the case in the
question ("Who is the respondent in X v. Y?"), which favours the case card;
questions that do not name the case are the harder, more realistic ones.

## 5b. Right paragraph — hand-written set, 27 questions over 3 judgments — VERIFIED

Source: `data/eval.yaml` (**status: verified**, Kanan Kotwani, October 2026:
all 27 paragraph citations checked against the PDFs, none changed). This is
the citation-level gate for every planned improvement.

| k | paragraph-level hit@k (right case *and* a chunk covering the gold paragraph, in the right opinion) | MRR | case-level hit@k (same questions, paragraph ignored) |
| --- | --- | --- | --- |
| 5 | **0.74** | **0.53** | 0.93 |
| 10 | 0.85 | 0.55 | 0.96 |

DRM@5 is 0.05: when these questions miss, it is almost always the right case
but a neighbouring paragraph (e.g. "Is Section 497 unconstitutional?" returns
Misra CJI's paragraphs 1 and 11, not the holding at 56–58). That gap between
0.93 and 0.74 is the citation problem the context-sentence and role-model
improvements are meant to close.

## How to reproduce

```bash
python -m legal_lens ingest --dir data/raw --no-graph          # 7 curated (graph loaded separately)
python -m legal_lens ingest --dir data/raw/indiclegalqa        # 60 IndicLegalQA judgments
python -m legal_lens evaluate data/eval.indiclegalqa.60.yaml --k 5
python -m legal_lens evaluate data/eval.yaml --k 5             # provisional until verified
```

## Record of candidates (append one row per trial)

| Date | Change tested | 5a hit@5 / MRR / DRM@5 | 5b hit@5 / MRR | Kept? |
| --- | --- | --- | --- | --- |
| 2026-10-07 | baseline as above | 0.84 / 0.73 / 0.27 | 0.74 / 0.53 (provisional) | — |
| 2026-10-22 | IndicLegalQA labels corrected (two-sided matcher; 478 → 453 questions), stores unchanged; eval.yaml verified | 0.88 / 0.77 / 0.23 (k=10: 0.90 / 0.77 / 0.27) | 0.74 / 0.53 (verified) | yes — corrects the measure, not the system |
