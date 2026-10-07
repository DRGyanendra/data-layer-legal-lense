# The Legal Lens data layer: schema for the team

What the data pipeline writes to Qdrant and Neo4j, and how to query it.
Written for Omjee (backend) and Priyanshu (frontend). This is task P7.

## Identifiers

| Thing | Form | Example |
| --- | --- | --- |
| Case | lowercase, underscores, year last | `jacob_mathew_2005` |
| Cited case not yet loaded | `cite:` plus the citation key | `cite:2004-6-scc-422` |
| Statute provision | `CODE:section` | `IPC:304A`, `BNS:106(1)`, `CrPC:156(3)`, `Constitution:21` |
| Other Acts | Act name, then section | `Hindu Succession Act, 1956:6` |
| Judge | lowercase name, no titles | `r_c_lahoti` |
| Chunk | case, opinion, paragraph range, part | `joseph_shine_2018_o0_p24`, `..._o0_p12-14`, `..._o3_p5.1-5.3`, `..._o0_p31_1` |
| Qdrant point ID | UUID derived from the chunk ID | Qdrant accepts only integers or UUIDs |

Section strings are always normalised: `304-A` becomes `304A`, `156 (3)` becomes `156(3)`.
Use `legal_lens.ids.statute_id(code, section)` to build one; do not format them by hand.

## Qdrant collection `judgment_chunks`

Vectors: 1024 dimensions, cosine, from `BAAI/bge-m3` with L2 normalisation.
**The backend must embed queries with the same model and normalisation.**

One point is one or more whole paragraphs of a judgment.

| Payload field | Type | Meaning |
| --- | --- | --- |
| `case_id` | keyword | |
| `case_name` | string | |
| `chunk_id` | string | readable ID |
| `paragraph_num` | integer | first paragraph in the chunk; the judgment's own number. Numbers restart in each opinion, so always read it together with `opinion_index` |
| `para_end` | integer | last paragraph in the chunk; equals `paragraph_num` for a single paragraph |
| `para_label` | string | the paragraph range as the court numbers it: `24`, `12-14`, `5.1-5.3`. Sub-paragraphs such as 5.1 appear only here; `paragraph_num` stays 5 |
| `chunk_part` | integer | 0, or 1, 2, ... when one long paragraph was split |
| `para_numbering` | keyword | `original`, or `synthetic` when this opinion had no paragraph numbers in the PDF. Set per opinion; the case card and the `Case` node say `mixed` when the opinions differ |
| `page_start` | integer | PDF page where the chunk starts |
| `court` | keyword | `Supreme Court` |
| `date` | datetime or null | `2005-08-05`. Null only when the details were read from a PDF that prints no date |
| `citation` / `citations` | string / list | primary citation, and all of them |
| `judges` | list | the full bench |
| `meta_source` | keyword | `manifest` when a person checked the case record in `data/cases.yaml`; `dataset` when it came from the source dataset's own metadata (the AWS Open Data bucket's `sample.csv` next to the PDF) and was cross-checked against the PDF; `auto` when it was read from the PDF alone. Never treat the three as equally checked: only `manifest` was read by a person |
| `parse_quality` | keyword | `ok` or `review`: the verdict on how well this judgment was read (see "Loading any judgment") |
| `opinion_index` | integer | 0 for the first opinion in the PDF, 1 for the next |
| `opinion_author` | string or null | the judge named at the head of the opinion (`R.F. Nariman, J.`), matched to the bench by surname. A joint opinion reads `J.M. Shelat and A.N. Grover`. Null when the PDF names nobody, as in an order of the whole bench |
| `opinion_type` | keyword | `majority`, `plurality`, `concurring`, `dissenting`, `partly_dissenting`, `unknown` |
| `statutes` | keyword list | provisions cited in this chunk, e.g. `["IPC:304A"]` |
| `statutes_current` | keyword list | their successors under the new codes, e.g. `["BNS:106(1)"]` |
| `rhetorical_role` | keyword | `facts`, `issue`, `argument_petitioner`, `argument_respondent`, `argument`, `lower_court`, `statute`, `precedent`, `analysis`, `ruling`, `unlabelled` |
| `role_source` | string | `heuristic` for now: phrase rules, not a trained model |
| `section_heading` | string or null | the judgment's own heading above this passage, e.g. `SUBMISSIONS` |
| `quoted_share` | float | 0 to 1, how much of the passage is inside quotation marks |
| `prev_chunk_id` / `next_chunk_id` | string or null | neighbours in reading order |
| `block_id` / `block_index` | keyword / integer | a block is a run of chunks with one role and one heading; index is the position inside it |
| `parent_id` | keyword | the paragraph a chunk belongs to; parts of one split paragraph share it |
| `tokens` | integer | approximate size, for budgeting the prompt |
| `substantive` | boolean | false for boilerplate such as "Leave granted."; search skips these |
| `keywords` | list | exact terms for the keyword index: `IPC 304A`, `BNS 106(1)`, cited citations and case names |
| `cited_cases` | keyword list | citation keys of cases cited in the passage; joins to `Case.citation_keys` in Neo4j |
| `cite_as` | string | the citation string to print, e.g. `Joseph Shine v. Union of India, (2019) 3 SCC 39, para 48 (per R.F. Nariman, concurring)`. Reads `page N` instead of `para N` when numbering is synthetic, and `decided 27 September 2018` in place of the citation when none is known |
| `footnotes` | list | footnotes from the pages the passage runs on, each starting with its number. In recent judgments the citations of the cases discussed are here, not in the text. Not embedded; already counted in `statutes`, `cited_cases` and `keywords` |
| `doc_type` | keyword | `judgment` for passages, `case_card` for the one summary record per case |
| `context` | string | the line embedded in front of the passage: case, court, date, citation, one-line summary, heading. With no summary in the manifest, the provisions the judgment cites most stand in for it (`Mainly concerns IPC 497, Article 14 of the Constitution`) |
| `embedded_with_context` | boolean | whether the vector was computed with that line |
| `text` | text (full-text indexed) | the passage, starting with its paragraph number |
| `doc_sha256` | keyword | hash of the source PDF, used to refuse duplicates |
| `embedding_model` | string | |

Rules for the backend:

1. **Cite `paragraph_num` only when `para_numbering` is `original`.** Synthetic numbers are positions, not the court's numbering; `cite_as` already falls back to the page. In a judgment with several opinions a paragraph number is ambiguous without the judge, which is why `cite_as` names the author.
2. **Do not present a passage as the law unless `opinion_type` is `majority` or `plurality`.** Concurring and dissenting passages need to be labelled as such. `unknown` means nobody has checked.
3. **Say who is speaking.** A passage with role `argument_petitioner`, `argument_respondent` or `argument` is what a party submitted, not what the court held. Put the role in the prompt next to each passage and show it on the citation card. A high `quoted_share` means the passage is mostly an extract from a statute or an earlier case.
4. **Treat roles as hints, not filters.** They come from phrase rules and have not been measured on real judgments. `unlabelled` means no clear cue, which is common. Use roles to label and to break ties; do not drop passages because of them.
5. **Exact section lookups go through a filter, not the query text.** Filter on `statutes` or `statutes_current`. A filter on `statutes_current = "BNS:106(1)"` returns passages that cite IPC 304A, which is how a search under the new numbering finds the old case law. The `text` field also has a full-text index for word matches.
6. **Embed the query alone.** The context line is added to passages only. Show `text` to users; `context` is for retrieval.
7. **Retrieve small, read wide, stay in the block.** Chunks are deliberately short. Before generation, fetch the other chunks with the hit's `block_id`. A block has one speaker, so expanding inside it never mixes counsel's argument into the court's reasoning. `prev_chunk_id` and `next_chunk_id` cross block boundaries; use them only when that is wanted.
8. **Print `cite_as` verbatim.** Give it to the model as the label of each passage and reject any citation in the answer that does not match one.
9. **Treat `parse_quality: review` with care.** The passage text is complete, but something about the judgment needs a person: no paragraph numbers, OCR, or opinions whose kind is unknown. Showing such passages with a "not yet verified" label is safer than hiding them.

### Which stage uses which field

| Pipeline stage | Fields it needs |
| --- | --- |
| Vector search (Qdrant) | vector of `context` + `text`; filters on `statutes`, `statutes_current`, `substantive` |
| Keyword search (Elasticsearch) | `text`, `context`, `keywords`; exact fields `statutes`, `statutes_current`, `case_id` |
| Graph (Neo4j) | `case_id`, `cited_cases`, `statutes` join to nodes; edge `paragraphs` join back to `paragraph_num` |
| Fusion and top-k | `chunk_id` is the same in every store; deduplicate on `parent_id`, cap chunks per `case_id` |
| Cross-encoder | `text`, at most about 384 tokens, so passage plus question fit a 512-token window |
| Context expansion | fetch the rest of the hit's `block_id`, or `prev_chunk_id` / `next_chunk_id` |
| Grounded prompt | `cite_as`, `rhetorical_role`, `opinion_type`, `quoted_share`, `tokens` |
| Citation check | every citation in the answer must equal a `cite_as` that was in the prompt |

Load Elasticsearch from `python -m legal_lens export --all`, which writes exactly the chunks that go to Qdrant, one JSON object per line, with the same IDs.

### The case card

Each case has one extra record with `doc_type: "case_card"`: its name, citations, bench, one-line summary, most-cited provisions and cases. It is written from the manifest, not by the court, so searches by case name or citation have something to match. Use it to answer "which cases", never as a quotation: it has no paragraph number.

### How chunks are cut

- One chunk is one or more whole numbered paragraphs, up to about 200 tokens when merging.
- Paragraphs merge only with neighbours of the same role, under the same heading, in the same opinion. A submission and the finding that answers it are never in one chunk.
- A paragraph over about 384 tokens is split at sentence ends; the last two sentences are repeated at the start of the next part. The limit keeps a passage and a question inside a cross-encoder's 512-token window.
- Section headings are removed from the passage text and kept in `section_heading`.
- Footnotes, page numbers, running headers and the digital-signature stamp are taken out of the passage text. Footnotes go to `footnotes`.

### How the PDF is read

Checked against seven real Supreme Court PDFs and fifteen generated layouts; `tests/test_real_layouts.py` and `tests/test_any_judgment.py` hold one test per layout that broke.

- **Opinions** are found three ways: the `J U D G M E N T` / `ORDER` heading with the cause title repeated above it; a judge's name standing alone after the end of a sentence (`M. DAS, J. (dissenting)`); and a name in capitals opening a paragraph in the law-report manner (`RAY, J.-The validity ...`). Each opinion keeps its own numbering. Where a later opinion carries on the numbering of the one before (16, 17, ...), those numbers are kept.
- **Paragraph numbers** are read in five styles: `24. The`, `24 The`, `[24] The`, `(24) The` and `Para 24.`. A first paragraph with no number, with numbering starting at `2.`, is handled. A numbered or bracketed list inside a judgment is not mistaken for paragraph numbering.
- **Headings** come from the judgment's own index or contents table when it has one, otherwise from capitals or lettered lines.
- **No paragraph numbers at all** (older copies): paragraphs are found from first-line indents, or from the blank space between them, and numbered by position, marked `synthetic`.
- **Law reports** (Supreme Court Reports volumes) are recognised from their page headers. The reporter's headnote, the page headers and the margin letters are left out. The judge named in each page's header decides whose opinion the page belongs to, which survives poor OCR. Checked on the 1006-page Kesavananda Bharati report: eleven opinions, in the right order.
- **Two-column pages** (printed reporters) are read one column after the other.
- **Site furniture** is removed: the Indian Kanoon and SCC OnLine lines printed on every page, page numbers, the signature stamp.
- **Scanned PDFs** are read with Tesseract when a page has no text layer. This is slow: about 13 minutes for a 121-page judgment.
- **Cache.** The text read from each PDF is kept in `data/cache/`, keyed by the file's hash, so OCR runs once per machine. `--no-cache` forces a fresh read. The folder is not in git.
- **Reader.** pdfplumber is the default because it is the one that has been tested. `--backend pymupdf` exists and is faster, and has not been run.

`python -m legal_lens inspect <case_id or pdf> --paragraphs` prints every paragraph found, with its opinion, number, page, heading and role.

### Loading any judgment

A judgment no longer needs an entry in `data/cases.yaml` before it can be loaded.

```
python -m legal_lens scan data/raw            # every PDF: verdict and reasons, nothing is written to a database
python -m legal_lens ingest --dir data/raw    # load them; REJECT files are skipped
python -m legal_lens ingest some_case.pdf     # or one file
```

For a PDF with no entry, the case details are read from the PDF: parties, date, bench, neutral citation, case number and the author of each opinion. Four layouts are understood: the court's own PDF, the old JUDIS text (`PETITIONER:` / `RESPONDENT:` / `BENCH:`), a law report, and an Indian Kanoon printout. An entry in `data/cases.yaml` always wins over what was read. A PDF downloaded by `sample` has a record in the `sample.csv` beside it (parties, date, SCR and neutral citation, full bench, author, case number from the dataset); that record is used, the PDF reading is kept as a cross-check, and any disagreement on date, parties or bench becomes a `review` reason. `meta_source` says which of the three was used.

`scan` writes `scan_report.csv` and `cases.draft.yaml`. The draft holds one record per judgment read automatically. Check it, add the reported citation, a one-line summary and each opinion's kind, and copy it into `data/cases.yaml`.

Every judgment gets a verdict from checks on its own text (`legal_lens/quality.py`):

| Verdict | Meaning | Typical reasons |
| --- | --- | --- |
| `ok` | nothing found | |
| `review` | loaded, with something for a person to check | no paragraph numbers (cited by page); OCR; several opinions whose kind is unknown; law-report copy; details missing |
| `reject` | not loaded without `--force` | text missing from the chunks; over a quarter of the PDF in no paragraph; unreadable scan; badly damaged OCR; not a judgment |

What the automatic reading cannot know:

- **Which opinion is the majority.** One opinion is the court's. With several, an opinion is marked from its own heading (`(Concurring)`, `(dissenting)`) or when its author and those joining outnumber the rest of the bench. Otherwise it is `unknown` and rule 2 applies.
- **The reported citation.** The court's PDFs print a neutral citation (`2018 INSC 790`) at most, never the SCC one.
- **A summary.**

### Measuring it

`python -m legal_lens evaluate data/eval.yaml --k 5` reports hit@k, MRR and DRM@k (the share of retrieved chunks that come from the wrong case). To compare with and without the context line, load a second collection with `ingest --no-context` and run the same questions against both.

## Neo4j

### Nodes

```
(:Case    {case_id, name, date: date, court, citation, citations, citation_keys,
           outcome, is_stub: boolean, paragraph_count, para_numbering, doc_sha256})
(:Statute {statute_id, code, section, base_section, title, is_active: boolean,
           repealed_on: date, omitted: boolean, omitted_note})
(:Judge   {judge_id, name})
(:Concept {name, definition, simple_definition})      -- constraint exists; nothing loads these yet
```

A `Case` with `is_stub: true` was cited by a loaded judgment but is not itself loaded. It has a citation and usually a `name` taken from the citing text, which is a best guess. When the full judgment is loaded later, the stub is replaced and its incoming edges move to the real node.

### Relationships

```
(Case)-[:CITES {paragraph, paragraphs, type}]->(Case)
(Case)-[:OVERRULES {paragraph}]->(Case)
(Case)-[:INTERPRETS {paragraph, paragraphs, mentions}]->(Statute)
(Case)-[:HEARD_BY]->(Judge)
(Case)-[:AUTHORED_BY {opinion_type, opinion_index}]->(Judge)
(Statute)-[:REPLACED_BY {effective_date: date, relation, change_note, source, verified}]->(Statute)
```

- `CITES.type` is `unclassified` for every automatically found citation. Working out whether a case was followed, distinguished or overruled is not done yet. The only typed edges are `overrules`, set from the `overrules` list in `data/cases.yaml`.
- `INTERPRETS` means "mentions this provision". `mentions` is the count; treat low counts as passing references.
- `paragraphs` on a multi-opinion judgment are per-opinion numbers.

### The mapping: `REPLACED_BY.relation`

| Value | Meaning | What to show |
| --- | --- | --- |
| `same` | text materially unchanged | "now BNS X" |
| `modified` | the provision changed | "now BNS X", plus `change_note` |
| `split` | one old provision, several new ones | list every successor |
| `merged` | several old provisions, one new one | "now part of BNS X" |
| `unreviewed` | numbers correspond; texts not yet compared | "corresponds to BNS X", no claim that it is unchanged |

An omitted provision has no `REPLACED_BY` edge. Its node has `omitted: true` and `omitted_note`. IPC 377 is one, so Navtej Singh Johar exercises this path.

`verified: false` means the sub-section number has not been confirmed against a published table. Do not show unverified rows to users as settled.

**Which code applies depends on the date of the offence, not today's date.** The IPC and CrPC still govern offences committed before 1 July 2024. An alert should read "IPC 304A corresponds to BNS 106(1) for offences on or after 1 July 2024", not "has been replaced by".

### Queries

These have not been run against a live Neo4j yet. Treat them as a starting point and report anything that fails.

Alerts for the provisions in a retrieved chunk (pass the chunk's `statutes` list):

```cypher
UNWIND $statute_ids AS sid
MATCH (old:Statute {statute_id: sid})
WHERE old.is_active = false
OPTIONAL MATCH (old)-[r:REPLACED_BY]->(new:Statute)
RETURN old.statute_id AS old, old.omitted AS omitted, old.omitted_note AS omitted_note,
       new.statute_id AS new, r.relation AS relation, r.change_note AS change_note,
       r.effective_date AS effective_date, r.verified AS verified
```

Cases on a provision, under either numbering:

```cypher
MATCH (s:Statute {statute_id: $statute_id})
OPTIONAL MATCH (old:Statute)-[:REPLACED_BY]->(s)
WITH [s] + collect(old) AS provisions
UNWIND provisions AS p
MATCH (c:Case)-[i:INTERPRETS]->(p)
RETURN c.case_id, c.name, c.date, p.statute_id AS cited_as, i.paragraphs, i.mentions
ORDER BY i.mentions DESC
```

Citation graph around one case, shaped as an edge list for D3:

```cypher
MATCH (a:Case)-[e:CITES]->(b:Case)
WHERE a.case_id = $case_id OR b.case_id = $case_id
RETURN a.case_id AS source, a.name AS source_name,
       b.case_id AS target, b.name AS target_name, b.is_stub AS target_is_stub,
       e.type AS type, e.paragraph AS paragraph
```

## Where this differs from the December brief, and why

| Brief | Now | Reason |
| --- | --- | --- |
| Point ID `jacob_mathew_2005_chunk_3` | UUID; readable ID in `chunk_id` | Qdrant rejects string IDs |
| `chunk_size=500` with a character splitter | whole numbered paragraphs | 500 was being counted in characters, and a character splitter loses the paragraph number the product cites |
| 100-token sliding overlap | no overlap between paragraphs; two sentences when a long paragraph is split | whole paragraphs do not cut sentences, so overlap between them only duplicates text |
| bare chunk text embedded | context line embedded in front of each chunk | passages from different judgments look alike; published legal RAG work reports large drops in wrong-document retrieval from a document-level line |
| `judges: [Arijit Pasayat, C.K. Thakker]` for Jacob Mathew | Lahoti CJI, G.P. Mathur, P.K. Balasubramanyan | the brief's example was wrong; metadata now comes from a checked manifest |
| a hand-written record for every case | details read from the PDF when there is no record, with a verdict on each file | the corpus is meant to grow past the seed cases |
| `AUTHORED_BY` only | `HEARD_BY` for the bench, `AUTHORED_BY` per opinion | most of the seed cases have several opinions |
| BNS 106 stored as `"106"` in one place and `"106(1)"` in another | always `106(1)`, with `base_section: "106"` | `MERGE` on two spellings makes two nodes |
| `REPLACED_BY {effective_date}` | adds `relation`, `change_note`, `source`, `verified` | the mapping is not one-to-one |
| no stub cases | `is_stub` | most cited cases are not in the corpus |

## Known limits

- The mapping tables hold 66 rows, not the full codes. See the header of each CSV.
- Neo4j's `Case` node does not yet carry `parse_quality` or `meta_source`; they are on the Qdrant payload only.
- A section cited with no Act named ("Section 304A" alone) is not captured.
- "Section 154 of the Penal Code" is read as the IPC unless a foreign country is named within a sentence or two. In a long passage on foreign law a later mention can still be filed under the IPC (Joseph Shine, Chandrachud J. para 28, on Uganda). `inspect` lists provisions with no mapping row; read that list before loading.
- A wrong section number printed in the judgment itself is kept as printed (Joseph Shine has "Section 479 I.P.C." for 497).
- Rhetorical roles come from phrase rules and are unmeasured. The OpenNyAI rhetorical-role model is the upgrade path; it labels sentences and would replace `legal_lens/roles.py`.
- Headings are recognised from the judgment's index, or when in capitals or lettered (`B. Analysis`). An epigraph printed under a heading stays with the paragraph before it.
- A later opinion that starts with neither a heading nor the judge's name is not split off, and its passages carry the first judge's name.
- In a law report, passages are cited by page of the PDF, not of the report, and the short closing order signed by the whole bench is attributed to the last judge. Two of Kesavananda's eleven opinion starts were placed from the page header because OCR lost the opening line; up to a page near each may be attributed to the neighbouring judge.
- Details read automatically are a reading of the page, not checked facts. On the seven real files every party name, date and bench was read correctly from clean text; from the poorly scanned report two judges' names came out damaged.
- Hindi and other Indian-language judgments are not handled. Two-column pages that are scans (read by OCR) are not put back in column order.
- Only PDFs are read: no Word, HTML or text files.
- Case names are not picked up for citations that sit in footnotes; the citation itself is.
- `tokens` is an estimate (characters divided by four). The 384 limit should be re-checked with the real BGE-M3 tokenizer.
- Statute text is not loaded. The `statute_sections` collection in the brief does not exist yet.
- `Concept` nodes are not populated.
