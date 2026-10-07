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

Run here: reading, splitting, chunking, case details and the verdict, on seven real judgments and
fifteen generated layouts.

Never run: the Qdrant loader, the Neo4j loader, the BGE-M3 embedder and the PyMuPDF reader. Expect
to fix small things the first time `check` and `ingest` run on a machine that can reach them.
