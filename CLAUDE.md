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

## What is built and tested (117 unit tests, `python -m unittest discover -s tests -t .`)

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
- **`vector_store.py`** / **`graph_store.py`** / **`embed.py`** — Qdrant,
  Neo4j and BGE-M3 loaders. **Written but never run against live services**
  — no network access in the environment these were built in.
- **`evaluate.py`** — hit@k, MRR, DRM@k (document-level retrieval mismatch)
  scoring against a question set. No question set or baseline exists yet.

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

**Run:** reading, splitting, chunking, metadata, and the quality verdict —
on 7 real judgments (`data/raw/*.pdf`) and 15 synthetic layout fixtures
(`tests/test_any_judgment.py`). 3 of the 7 real judgments pass clean
(Jarnail Singh, Joseph Shine, Navtej Johar); 4 are flagged `review`
(Vineeta Sharma — full OCR; Jacob Mathew — no paragraph numbers;
Kesavananda Bharati — law-report scan, OCR-damaged; Puttaswamy — this file
is the 2015 referral order, not the 2017 privacy judgment, and is partly
unnumbered).

**Never run:** the Qdrant loader, the Neo4j loader, the real BGE-M3
embedder, and the PyMuPDF reader — this environment has no network access.
Retrieval quality (as opposed to parsing quality) is completely unmeasured:
no evaluation dataset or test questions exist yet.

## Immediate next steps (in order)

1. `python -m legal_lens check` then `ingest --dir data/raw` against your
   own Qdrant/Neo4j instances; fix whatever errors come up first.
2. Pull a balanced ~100-judgment sample from the AWS Open Data "Indian
   Supreme Court Judgments" registry (Dattam Labs, CC-BY 4.0), combine with
   the originals, run `scan`, review `scan_report.csv`.
3. Write 20-30 test questions with exact known paragraph answers (plus run
   IndicLegalQA questions through `evaluate`) to get a first real hit@k/MRR
   baseline — nothing past this point should be adopted without beating
   this baseline.
4. Only after a baseline exists: per-chunk context sentences (Anthropic
   Contextual Retrieval style), OpenNyAI's trained rhetorical-role model,
   an embedding-model comparison (BGE-M3 vs a legal-tuned alternative),
   finishing the IPC/BNS mapping table past its 66-row seed.

## Known constraints to respect when editing

- Chunk size ceiling is 384 tokens; `quality.py` rejects anything over it.
- A chunk's `opinion_type` of `unknown` must never be presented by the
  backend as the court's holding — preserve that field, don't default it.
- Auto-read metadata (`meta_source: "auto"`) is marked separately from a
  hand-checked manifest entry (`"manifest"`) — never merge the two silently.
- `reject`-verdict files must not be loadable without `--force`; don't
  loosen this to "fix" a low pass rate — find a cleaner source PDF instead.
- A model-written context sentence (when added) is for search indexing
  only — never shown to the user, never cited as the court's text.
