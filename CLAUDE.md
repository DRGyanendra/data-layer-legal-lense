# The Legal Lens — data layer

Context for Claude Code working in this repo. Read this before making changes.

## What this project is

The Legal Lens is an AI search engine for Indian Supreme Court judgments: RAG
(retrieval-augmented generation) over Qdrant (vectors), Elasticsearch
(keyword search) and Neo4j (a citation knowledge graph), plus an IPC → BNS /
CrPC → BNSS statute-mapping table (old penal codes to the new ones in force
since 1 July 2024).

Team: this folder (`legal_lens/`) is the **data layer** — turning judgment
PDFs into clean, citable chunks ready to load. Backend/RAG orchestration and
the frontend are owned by teammates and live outside this repo.

## What is built and tested (147 unit tests, `python -m unittest discover -s tests -t .`)

- **`pdf_extract.py`** — text + OCR extraction, two-column detection and
  correct reading order, page-damage scoring, site-furniture stripping
  (Indian Kanoon / SCC Online footers), on-disk cache (gzip JSON, keyed by
  file hash + extractor version).
- **`paragraphs.py`** + **`law_report.py`** — splits a judgment into
  paragraphs and opinions. Handles 5 numbering styles (dot, bare, bracket,
  paren, "Para N."), opinion boundaries with or without a heading, carried/
  continued numbering across opinions, and full law-report (SCR volume)
  layout with headnote stripping and page-header-driven judge attribution.
- **`autometa.py`** — reads case metadata (parties, date, bench, citation)
  directly from the PDF across 4 layouts (court PDF, JUDIS text, law report,
  Indian Kanoon printout) when there is no manifest entry.
- **`chunking.py`** / **`roles.py`** — paragraph-based, role-aware chunking;
  whole numbered paragraphs merged only within the same role/opinion, split
  at sentence boundaries over 384 tokens. Roles are currently rule-based
  (facts, issue, argument, statute, precedent, analysis, ruling, ...).
- **`citations.py`** / **`mapping.py`** — statute/case citation extraction
  and the IPC→BNS / CrPC→BNSS mapping (66-row seed table, offence-date rule).
- **`quality.py`** — every loaded file gets an `ok` / `review` / `reject`
  verdict checked against its own text (word-loss, numbering gaps, chunk
  size, OCR, two-column, law-report, auto-metadata-missing, etc.) so a
  badly-parsed file never loads silently.
- **`pipeline.py`** — orchestrates extraction → split → metadata → chunk →
  verdict → payload for one case (`BuiltCase`).
- **`cli.py`** / **`__main__.py`** — `scan`, `inspect`, `check`, `ingest`,
  `export`, `evaluate` commands (see below).
- **`dataset.py`** — the AWS Open Data bucket (Dattam Labs, CC-BY 4.0):
  per-year metadata, the balanced `sample`, IndicLegalQA matching
  (`questions`), and the dataset record as a third metadata source
  (`meta_source: "dataset"`, cross-checked against the PDF).
- **`vector_store.py`** / **`graph_store.py`** / **`embed.py`** — Qdrant,
  Neo4j and BGE-M3 loaders. Run against Qdrant Cloud and Neo4j Aura on
  2026-10-07 (Aura over its HTTPS Query API where Bolt cannot get out).
- **`evaluate.py`** — hit@k, MRR, DRM@k (document-level retrieval mismatch)
  scoring against a question set; a question with no paragraph counts the
  right case as a hit. First baseline recorded in `docs/BASELINE.md`.

Full field-level reference: `docs/SCHEMA.md`.

## Commands

```bash
pip install -r requirements.txt            # Tesseract must also be installed for scanned PDFs
cp .env.example .env                       # fill in Qdrant/Neo4j details; never commit .env

python -m unittest discover -s tests -t .  # test suite, no database needed
python -m legal_lens scan data/raw         # every PDF: ok/review/reject + reasons, writes scan_report.csv
python -m legal_lens inspect data/raw/x.pdf --paragraphs
python -m legal_lens check                 # test Qdrant/Neo4j/embedder connections
python -m legal_lens ingest --dir data/raw # load everything not rejected
python -m legal_lens export --dir data/raw # same chunks as JSON lines, for Elasticsearch
python -m legal_lens evaluate data/eval.yaml
```

Any Supreme Court judgment PDF can go in `data/raw/` without an entry in
`data/cases.yaml` — parties/date/bench/judges are auto-read from the PDF. A
manifest entry, when present, always wins and is where the reported
citation, one-line summary and each opinion's kind (majority/concurring/
dissenting) are recorded by hand.

## What has and has not been run

**Run (2026-10-07):** the whole pipeline end to end against live services.
- `scan` on 167 judgments: the 7 curated SCR copies (7 review), the balanced
  100-judgment sample from the AWS bucket (2 ok / 96 review / 2 reject; 41
  carry the court's paragraph numbers) and the 60-judgment IndicLegalQA set
  (60 review; 52 numbered). Reports sit beside each sample's `sample.csv`.
- `ingest` of 67 judgments into Qdrant (`judgment_chunks`: 7,917 points) and
  Neo4j (883 Case / 75 Judge / 740 Statute; 66-row mapping loaded).
- `evaluate`: 478 IndicLegalQA questions — hit@5 0.84, MRR 0.73, DRM@5 0.27;
  27 hand-written paragraph-exact questions (`data/eval.yaml`, **draft, not yet
  verified by a person**) — hit@5 0.74, MRR 0.53, provisional. Details in
  `docs/BASELINE.md`.

**Not yet run:** the PyMuPDF reader on real files; the full 974 IndicLegalQA
judgments; anything from the "planned improvements" list — by design, nothing
there starts until the baseline above has been reviewed.

## Immediate next steps (in order)

1. A person verifies every paragraph number in `data/eval.yaml` against the
   PDFs in `data/raw/` and changes `status: draft` to `verified`. Until then
   the paragraph-level baseline in `docs/BASELINE.md` is provisional.
2. Review the baseline. Only then start the planned improvements, one at a
   time, each behind a switch and each recorded in `docs/BASELINE.md`:
   per-chunk context sentences (Anthropic Contextual Retrieval style),
   OpenNyAI's trained rhetorical-role model, an embedding-model comparison
   (BGE-M3 vs a legal-tuned alternative), the IPC/BNS table past 66 rows.
3. Corpus scale-up (the rest of the AWS bucket) only if the baseline review
   finds coverage, not retrieval, to be the weak point.

## Known constraints to respect when editing

- Chunk size ceiling is 384 tokens; `quality.py` rejects anything over it.
- A chunk's `opinion_type` of `unknown` must never be presented by the
  backend as the court's holding — preserve that field, don't default it.
- Auto-read metadata (`meta_source: "auto"`) and dataset metadata
  (`"dataset"`, from the AWS bucket's record, cross-checked against the PDF)
  are marked separately from a hand-checked manifest entry (`"manifest"`) —
  never merge the three silently; only `manifest` was read by a person.
- `reject`-verdict files must not be loadable without `--force`; don't
  loosen this to "fix" a low pass rate — find a cleaner source PDF instead.
- A model-written context sentence (when added) is for search indexing
  only — never shown to the user, never cited as the court's text.
