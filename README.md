# The Legal Lens: data layer

Turns Supreme Court judgment PDFs into citable chunks for Qdrant, Elasticsearch and Neo4j.
`docs/SCHEMA.md` is the reference for the backend and frontend.

## Use

```
pip install -r requirements.txt          # Tesseract must also be installed for scanned PDFs
cp .env.example .env                     # fill in Qdrant and Neo4j details; never commit .env

python -m unittest discover -s tests -t .        # the test suite; no database needed
python -m legal_lens scan data/raw               # every PDF in the folder: ok / review / reject, with reasons
python -m legal_lens inspect data/raw/x.pdf --paragraphs   # look closely at one judgment
python -m legal_lens check                       # test the Qdrant, Neo4j and model connections
python -m legal_lens ingest --dir data/raw       # load everything that was not rejected
python -m legal_lens export --dir data/raw       # the same chunks as JSON lines, for Elasticsearch
python -m legal_lens evaluate data/eval.yaml     # hit rate and wrong-document rate on your questions
```

Put any Supreme Court judgment PDF in `data/raw/`. It does not need an entry in `data/cases.yaml`:
the parties, date, bench and judges are read from the PDF. An entry in `data/cases.yaml`, when there
is one, always wins, and is where the reported citation, the one-line summary and each opinion's
kind (majority, concurring, dissenting) are recorded.

## What has and has not been run

Run (2026-10-07): reading, splitting, chunking, case details and the verdict on 167 real judgments
(the 7 curated cases, a balanced 100-judgment sample and a 60-judgment IndicLegalQA set, all from
the AWS Open Data bucket), and the loaders against Qdrant Cloud and Neo4j Aura: 67 judgments
loaded (7,917 points; 883 Case / 75 Judge / 740 Statute nodes). First retrieval baseline:
hit@5 0.84 and MRR 0.73 on 478 IndicLegalQA questions; 0.74 / 0.53 on 27 paragraph-exact
questions that still await human verification. See `docs/BASELINE.md`.

Not yet run: the PyMuPDF reader on real files, and any of the planned improvements.

Other commands added since the first brief: `python -m legal_lens sample` (build the balanced
validation sample from the AWS bucket) and `python -m legal_lens questions` (turn IndicLegalQA
into a question file and fetch the judgments it covers).
