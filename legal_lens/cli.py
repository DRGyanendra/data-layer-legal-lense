"""Command line for the data layer.

    python -m legal_lens check                      test every connection
    python -m legal_lens scan data/raw              every PDF in a folder: verdict and reasons
    python -m legal_lens inspect any_judgment.pdf   dry run on one PDF, no database
    python -m legal_lens mapping validate           check the mapping CSVs
    python -m legal_lens graph init                 create Neo4j constraints
    python -m legal_lens mapping load               load the mapping into Neo4j
    python -m legal_lens ingest --dir data/raw      load every judgment in a folder
    python -m legal_lens ingest --all               load the judgments listed in data/cases.yaml
    python -m legal_lens search "medical negligence" --statute IPC:304A
    python -m legal_lens evaluate data/eval.yaml    measure retrieval on known questions
    python -m legal_lens export --all --out chunks.jsonl   the same chunks, for Elasticsearch
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ROOT, Settings, load_settings
from .manifest import CaseMeta, load_manifest
from .mapping import Mapping, load_mapping_file, validate
from .pipeline import BuiltCase, DuplicateCaseError, build_case, ingest_case


def _embedder(settings: Settings, kind: str):
    from .embed import BgeM3Embedder, HashEmbedder

    if kind == "hash":
        print("! Using the hash embedder. Fine for a dry run, wrong for the shared collection.")
        return HashEmbedder()
    return BgeM3Embedder(settings.embedding_model)


def _store(settings: Settings, dim: int):
    from .vector_store import QdrantStore

    settings.require("qdrant_url")
    return QdrantStore(settings.qdrant_url, settings.qdrant_api_key, settings.qdrant_collection, dim)


def _graph(settings: Settings):
    from .graph_store import GraphWriter, Neo4jRunner

    settings.require("neo4j_uri", "neo4j_password")
    runner = Neo4jRunner(settings.neo4j_uri, settings.neo4j_username,
                         settings.neo4j_password, settings.neo4j_database)
    return GraphWriter(runner), runner


def _resolve_pdf(meta: CaseMeta) -> Path:
    path = Path(meta.file)
    return path if path.is_absolute() else ROOT / path


def _manifest_for(pdf: Path, cases: dict[str, CaseMeta]) -> CaseMeta | None:
    """The checked record for this PDF, if data/cases.yaml has one."""
    for meta in cases.values():
        if _resolve_pdf(meta).resolve() == pdf.resolve() or meta.case_id == pdf.stem.lower():
            return meta
    return None


def _targets(args, settings: Settings) -> list[tuple[CaseMeta | None, Path]] | None:
    """What a command should work on: (checked record or None, PDF path) pairs.

    A name is a case_id from data/cases.yaml or a path to a PDF. A PDF with no
    record is still accepted: its details are read from the PDF itself.
    """
    cases = load_manifest(settings.manifest_path)
    found: list[tuple[CaseMeta | None, Path]] = []
    seen: set[Path] = set()

    def add(meta: CaseMeta | None, pdf: Path) -> None:
        if pdf.resolve() not in seen:
            seen.add(pdf.resolve())
            found.append((meta, pdf))

    if getattr(args, "all", False):
        for meta in cases.values():
            add(meta, _resolve_pdf(meta))
    folder = getattr(args, "dir", None)
    if folder:
        if not Path(folder).is_dir():
            print(f"No folder at {folder}.")
            return None
        for pdf in sorted(Path(folder).glob("*.pdf")):
            add(_manifest_for(pdf, cases), pdf)
    for name in getattr(args, "case_ids", None) or []:
        if name in cases:
            add(cases[name], _resolve_pdf(cases[name]))
        elif Path(name).suffix.lower() == ".pdf" and Path(name).exists():
            add(_manifest_for(Path(name), cases), Path(name))
        else:
            print(f"'{name}' is neither a case_id in {settings.manifest_path} nor a PDF that exists.")
            return None
    if not found:
        print("Nothing to do. Name a case_id or a PDF, or pass --dir FOLDER or --all.")
        return None
    return found


def _print_verdict(built: BuiltCase) -> None:
    verdict = built.verdict
    label = {"ok": "OK", "review": "REVIEW", "reject": "REJECT"}[verdict.status]
    print(f"  verdict: {label}" + ("" if verdict.reasons else "  (nothing found to check by hand)"))
    for level, reason in verdict.reasons:
        print(f"    [{level}] {reason}")


def _print_report(built: BuiltCase, show: int) -> None:
    meta = built.meta
    print(f"\n{meta.name}  [{meta.case_id}]")
    if meta.source == "auto":
        print("  case details read from the PDF (no entry in data/cases.yaml):")
        print(f"    date {meta.date_iso or 'not found'}   court {meta.court}   citation {meta.citation or 'not found'}")
        print(f"    bench: {', '.join(meta.bench) or 'not found'}")
        if meta.case_number:
            print(f"    case number: {meta.case_number}")
    _print_verdict(built)
    for key, value in built.stats.items():
        print(f"  {key:22} {value}")
    if built.statute_refs:
        top = sorted(built.statute_refs, key=lambda s: -s["mentions"])[:10]
        print("  statutes (most cited):  " + ", ".join(f"{s['statute_id']} x{s['mentions']}" for s in top))
    if built.case_refs:
        print("  cases cited (first 5):")
        for ref in built.case_refs[:5]:
            print(f"    {ref['citations'][0]:24} {ref['name_hint'] or '(name not found)'}")
    for warning in built.warnings:
        print(f"  ! {warning}")
    for o in built.split.opinions:
        mapped = next((f"{p['opinion_author']} ({p['opinion_type']})" for p in built.payloads
                       if p["opinion_index"] == o.index and p["doc_type"] == "judgment"), "?")
        print(f"  opinion {o.index}: page {o.page}, {o.paragraphs} paras, numbering {o.numbering} ({o.style}), "
              f"header '{o.author_line}' -> {mapped}")
    if built.split.headings:
        print("  headings: " + " | ".join(built.split.headings[:12])
              + (f" ... ({len(built.split.headings)} in all)" if len(built.split.headings) > 12 else ""))
    if built.payloads:
        print(f"  context line: {built.payloads[0]['context']}")
    for chunk, payload in list(zip(built.chunks, built.payloads))[:show]:
        paras = f"{chunk.para_start}" if chunk.para_start == chunk.para_end else f"{chunk.para_start}-{chunk.para_end}"
        print(f"\n  --- {chunk.chunk_id}  paras {paras}  page {chunk.page_start}  ~{chunk.tokens} tokens")
        print(f"      role: {chunk.role}   heading: {chunk.heading}   quoted: {chunk.quoted_share:.0%}"
              f"   block: {payload['block_id']}#{payload['block_index']}"
              f"   {'searchable' if payload['substantive'] else 'not searchable (boilerplate)'}")
        print(f"      cite as: {payload['cite_as']}")
        print(f"      statutes: {payload['statutes']}  now: {payload['statutes_current']}")
        print("      " + chunk.text[:400] + ("..." if len(chunk.text) > 400 else ""))


# ---------------------------------------------------------------- commands

def cmd_check(args, settings: Settings) -> int:
    failures = 0

    def report(name: str, fn) -> None:
        nonlocal failures
        try:
            print(f"  ok    {name}: {fn()}")
        except SystemExit as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:   # report every failure, do not stop at the first
            failures += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")

    def qdrant():
        settings.require("qdrant_url")
        from qdrant_client import QdrantClient
        client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key, timeout=30)
        names = [c.name for c in client.get_collections().collections]
        return f"connected, collections: {names or 'none yet'}"

    def neo4j():
        graph, runner = _graph(settings)
        runner.verify()
        counts = graph.counts()
        runner.close()
        return f"connected, nodes: {counts or 'none yet'}"

    def embedding():
        if args.skip_model:
            return "skipped"
        from .embed import BgeM3Embedder
        embedder = BgeM3Embedder(settings.embedding_model)
        vector = embedder.embed(["Causing death by negligence"])[0]
        if len(vector) != settings.embedding_dim:
            raise RuntimeError(f"model gives {len(vector)} dimensions, EMBEDDING_DIM is {settings.embedding_dim}")
        return f"{settings.embedding_model} loaded, {len(vector)} dimensions"

    def postgres():
        if not settings.database_url:
            return "DATABASE_URL not set, skipped (the backend owns these tables)"
        import psycopg
        with psycopg.connect(settings.database_url, connect_timeout=15) as conn:
            return "connected, " + conn.execute("select version()").fetchone()[0].split(",")[0]

    def files():
        cases = load_manifest(settings.manifest_path)
        present = [c for c in cases.values() if _resolve_pdf(c).exists()]
        rows = Mapping.from_dir(settings.mappings_dir).rows
        return f"{len(present)} of {len(cases)} manifest PDFs present, {len(rows)} mapping rows"

    print("Checking connections")
    report("Qdrant", qdrant)
    report("Neo4j", neo4j)
    report("Embedding model", embedding)
    report("Supabase Postgres", postgres)
    report("Local data", files)
    return 1 if failures else 0


def cmd_inspect(args, settings: Settings) -> int:
    mapping = Mapping.from_dir(settings.mappings_dir)
    cases = load_manifest(settings.manifest_path)
    target = args.target
    if target in cases:
        meta, pdf = cases[target], _resolve_pdf(cases[target])
    elif Path(target).exists():
        pdf = Path(target)
        meta = _manifest_for(pdf, cases)          # None: read the details from the PDF
    else:
        print(f"'{target}' is neither a case_id in {settings.manifest_path} nor a PDF path.")
        return 2
    if not pdf.exists():
        print(f"No PDF at {pdf}. Download the judgment and save it there.")
        return 2
    built = build_case(meta, pdf, mapping, backend=args.backend, ocr=not args.no_ocr,
                           cache_dir=None if args.no_cache else ROOT / "data" / "cache")
    _print_report(built, args.show)
    if args.paragraphs:
        print("\n  opinion  para  page  ~tokens  role                 heading / first words")
        for para, role in zip(built.split.paragraphs, built.roles):
            print(f"  {para.segment:>7}  {para.number:>4}  {para.page:>4}  {round(len(para.text) / 4):>7}  "
                  f"{role:20} {(para.heading or '-')[:28]:28} {para.text[:70]}")
    return 0


def cmd_ingest(args, settings: Settings) -> int:
    targets = _targets(args, settings)
    if targets is None:
        return 2
    mapping = Mapping.from_dir(settings.mappings_dir)

    embedder = store = graph = runner = None
    if not args.no_vectors:
        if args.embedder == "hash" and settings.qdrant_collection == "judgment_chunks":
            print("Refusing to put hash vectors in 'judgment_chunks'. Set QDRANT_COLLECTION "
                  "to a scratch name for a dry run, or use --no-vectors.")
            return 2
        embedder = _embedder(settings, args.embedder)
        store = _store(settings, embedder.dim)
        store.ensure_collection()
    if not args.no_graph:
        graph, runner = _graph(settings)
        graph.init_schema()

    failed = 0
    loaded_ids: dict[str, Path] = {}
    for meta, pdf in targets:
        if not pdf.exists():
            print(f"\n{meta.case_id if meta else pdf.name}: no PDF at {pdf}, skipped")
            failed += 1
            continue
        built = build_case(meta, pdf, mapping, backend=args.backend, ocr=not args.no_ocr,
                           cache_dir=None if args.no_cache else ROOT / "data" / "cache")
        _print_report(built, show=0)
        case_id = built.meta.case_id
        if case_id in loaded_ids:
            print(f"  not loaded: {pdf.name} and {loaded_ids[case_id].name} both read as '{case_id}'. "
                  "Give one of them an entry in data/cases.yaml with its own case_id.")
            failed += 1
            continue
        if built.verdict.status == "reject" and not args.force:
            print("  not loaded: the verdict is REJECT. Fix the cause above, or pass --force.")
            failed += 1
            continue
        if built.verdict.status == "review" and args.only_ok:
            print("  not loaded: the verdict is REVIEW and --only-ok was given.")
            failed += 1
            continue
        loaded_ids[case_id] = pdf
        try:
            result = ingest_case(built, embedder, store, graph, force=args.force,
                                 with_context=not args.no_context)
        except DuplicateCaseError as exc:
            print(f"  not loaded: {exc}")
            failed += 1
            continue
        print(f"  loaded: {result['points']} points, {result['statute_edges']} statute edges, "
              f"{result['citation_edges']} citation edges")
    if runner:
        runner.close()
    return 1 if failed else 0


def cmd_mapping(args, settings: Settings) -> int:
    rows = []
    for path in sorted(settings.mappings_dir.glob("*.csv")):
        loaded = load_mapping_file(path)
        print(f"{path.name}: {len(loaded)} rows")
        rows.extend(loaded)
    errors, warnings = validate(rows)
    for warning in warnings:
        print(f"  ! {warning}")
    for error in errors:
        print(f"  ERROR {error}")
    if errors:
        print(f"{len(errors)} error(s). Nothing was loaded.")
        return 1
    if args.action == "validate":
        print("Mapping is valid.")
        return 0
    graph, runner = _graph(settings)
    graph.init_schema()
    replaced, omitted = graph.load_mapping(rows)
    runner.close()
    print(f"Loaded {replaced} REPLACED_BY edges and marked {omitted} provisions as omitted.")
    return 0


def cmd_graph(args, settings: Settings) -> int:
    graph, runner = _graph(settings)
    if args.action == "init":
        graph.init_schema()
        print("Constraints and indexes are in place.")
    for label, count in graph.counts().items():
        print(f"  {label:10} {count}")
    runner.close()
    return 0


def cmd_search(args, settings: Settings) -> int:
    embedder = _embedder(settings, args.embedder)
    store = _store(settings, embedder.dim)
    vector = embedder.embed([args.query])[0]
    hits = store.search(vector, limit=args.limit, statute=args.statute, case_id=args.case)
    if not hits:
        print("No results.")
    for hit in hits:
        p = hit.payload
        paras = str(p["paragraph_num"]) if p["paragraph_num"] == p["para_end"] else f"{p['paragraph_num']}-{p['para_end']}"
        print(f"\n{hit.score:.3f}  {p['case_name']}, {p['citation']}, para {paras}")
        print(f"       role: {p.get('rhetorical_role')}  opinion: {p.get('opinion_type')}  "
              f"statutes: {p['statutes']}  now: {p['statutes_current']}")
        print("       " + p["text"][:300].replace("\n", " ") + "...")
    return 0


def cmd_export(args, settings: Settings) -> int:
    """Write every chunk as one JSON line: the single source for all three stores."""
    import json

    from .ids import point_id

    targets = _targets(args, settings)
    if targets is None:
        return 2
    mapping = Mapping.from_dir(settings.mappings_dir)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out.open("w", encoding="utf-8") as handle:
        for meta, pdf in targets:
            if not pdf.exists():
                print(f"{meta.case_id if meta else pdf.name}: no PDF at {pdf}, skipped")
                continue
            built = build_case(meta, pdf, mapping, backend=args.backend, ocr=not args.no_ocr,
                               cache_dir=None if args.no_cache else ROOT / "data" / "cache")
            if built.verdict.status == "reject":
                print(f"{built.meta.case_id}: REJECT, not exported ({built.verdict.reasons[0][1]})")
                continue
            for chunk, payload, text in zip(built.chunks, built.payloads, built.embedding_texts()):
                handle.write(json.dumps(
                    {"id": point_id(chunk.chunk_id), "chunk_id": chunk.chunk_id,
                     "embedding_text": text, **payload}, ensure_ascii=False) + "\n")
                written += 1
            print(f"{built.meta.case_id}: {len(built.chunks)} chunks ({built.verdict.status})")
    print(f"Wrote {written} chunks to {out}")
    return 0


def cmd_scan(args, settings: Settings) -> int:
    """Run every PDF in a folder through the chunker and say which are fit to load."""
    import csv

    import yaml

    args.all, args.case_ids = False, []
    targets = _targets(args, settings)
    if targets is None:
        return 2
    mapping = Mapping.from_dir(settings.mappings_dir)
    rows, drafts = [], []
    for meta, pdf in targets:
        try:
            built = build_case(meta, pdf, mapping, backend=args.backend, ocr=not args.no_ocr,
                               cache_dir=None if args.no_cache else ROOT / "data" / "cache")
        except Exception as exc:                  # one unreadable file must not stop the scan
            rows.append({"file": pdf.name, "verdict": "reject", "case_id": "", "name": "",
                         "reasons": f"could not be read: {type(exc).__name__}: {exc}"})
            print(f"REJECT  {pdf.name}: could not be read ({type(exc).__name__}: {exc})")
            continue
        m, v, f = built.meta, built.verdict, built.verdict.figures
        rows.append({
            "file": pdf.name, "verdict": v.status, "case_id": m.case_id, "name": m.name,
            "date": m.date_iso or "", "details_from": m.source, "pages": f.get("pages", ""),
            "opinions": f.get("opinions", ""), "paragraphs": f.get("paragraphs", ""),
            "numbering": built.split.numbering, "chunks": f.get("chunks", ""),
            "reasons": " | ".join(r for level, r in v.reasons if level != "note"),
        })
        print(f"{v.status.upper():7} {pdf.name}: {m.name}, {f.get('opinions', 0)} opinion(s), "
              f"{f.get('paragraphs', 0)} paragraphs ({built.split.numbering}), {f.get('chunks', 0)} chunks")
        for level, reason in v.reasons:
            if level != "note":
                print(f"          [{level}] {reason}")
        if m.source == "auto":
            drafts.append({
                "case_id": m.case_id, "name": m.name, "summary": "",
                "date": m.date_iso or "", "court": m.court, "citations": m.citations, "bench": m.bench,
                "opinions": [{"author": o.author, "type": o.type, **({"joined_by": o.joined_by} if o.joined_by else {})}
                             for o in m.opinions],
                "file": str(pdf), "notes": "Read from the PDF by `scan`; check every line before relying on it.",
            })
    counts = {k: sum(1 for r in rows if r["verdict"] == k) for k in ("ok", "review", "reject")}
    print(f"\n{len(rows)} file(s): {counts['ok']} ok, {counts['review']} review, {counts['reject']} reject")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = ["file", "verdict", "case_id", "name", "date", "details_from", "pages", "opinions",
              "paragraphs", "numbering", "chunks", "reasons"]
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Report written to {out}")
    if drafts:
        draft = out.with_name("cases.draft.yaml")
        draft.write_text(yaml.safe_dump({"cases": drafts}, sort_keys=False, allow_unicode=True), encoding="utf-8")
        print(f"Draft records for {len(drafts)} judgment(s) with no entry in data/cases.yaml written to {draft}. "
              "Check them, add citations, summaries and opinion types, and copy them into data/cases.yaml.")
    return 1 if counts["reject"] else 0


def cmd_sample(args, settings: Settings) -> int:
    """Build the balanced validation sample from the AWS Open Data metadata."""
    from .dataset import (describe, download_pdf, fetch_metadata, select_sample,
                          write_draft_manifest, write_sample_csv)

    cache = ROOT / "data" / "cache" / "aws_metadata"
    print("Reading the AWS Open Data metadata (Dattam Labs, CC-BY 4.0), one parquet file per year...")
    judgments = fetch_metadata(cache)
    print(f"  {len(judgments)} English judgments, {min(j.year for j in judgments)}-{max(j.year for j in judgments)}")
    exclude = set(args.exclude or [])
    sample = select_sample(judgments, size=args.size, seed=args.seed,
                           criminal_share=args.criminal_share, exclude_paths=exclude)
    for line in describe(sample):
        print(line)
    out_dir = Path(args.dir)
    write_sample_csv(sample, out_dir / "sample.csv")
    write_draft_manifest(sample, out_dir / "cases.aws.draft.yaml")
    print(f"Sample list written to {out_dir / 'sample.csv'}; draft case records (unchecked) to "
          f"{out_dir / 'cases.aws.draft.yaml'}")
    if args.no_download:
        return 0
    got = 0
    for i, j in enumerate(sample, start=1):
        path = download_pdf(j, out_dir)
        if path:
            got += 1
            print(f"  [{i:3}/{len(sample)}] {path.name}  ({path.stat().st_size // 1024} KB)")
    print(f"Downloaded {got} of {len(sample)} PDFs to {out_dir}. Next: python -m legal_lens scan {out_dir}")
    return 0 if got == len(sample) else 1


def cmd_evaluate(args, settings: Settings) -> int:
    from .evaluate import load_questions, score

    questions = load_questions(args.questions)
    if not questions:
        print(f"No questions in {args.questions}.")
        return 2
    embedder = _embedder(settings, args.embedder)
    store = _store(settings, embedder.dim)

    def search(query: str, k: int):
        return store.search(embedder.embed([query])[0], limit=k)

    print(f"Collection: {settings.qdrant_collection}")
    for line in score(questions, search, args.k).lines():
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="legal_lens", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="test every connection")
    p.add_argument("--skip-model", action="store_true", help="do not load the embedding model")
    p.set_defaults(fn=cmd_check)

    def pdf_options(p):
        p.add_argument("--backend", choices=["auto", "pymupdf", "pdfplumber"], default="auto")
        p.add_argument("--no-ocr", action="store_true", help="do not OCR scanned pages")
        p.add_argument("--no-cache", action="store_true", help="read the PDF again instead of using data/cache")

    p = sub.add_parser("inspect", help="dry run on one PDF, no database needed")
    p.add_argument("target", help="a case_id from the manifest, or a path to a PDF")
    p.add_argument("--show", type=int, default=3, help="how many chunks to print")
    p.add_argument("--paragraphs", action="store_true", help="list every paragraph found, one per line")
    pdf_options(p)
    p.set_defaults(fn=cmd_inspect)

    p = sub.add_parser("scan", help="check every PDF in a folder: ok, review or reject, with reasons")
    p.add_argument("dir", help="folder of judgment PDFs")
    p.add_argument("--out", default="data/export/scan_report.csv")
    pdf_options(p)
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("ingest", help="load judgments into Qdrant and Neo4j")
    p.add_argument("case_ids", nargs="*", help="case_ids from data/cases.yaml, or paths to PDFs")
    p.add_argument("--all", action="store_true", help="every case in data/cases.yaml")
    p.add_argument("--dir", help="every PDF in this folder, with or without an entry in data/cases.yaml")
    p.add_argument("--only-ok", action="store_true", help="skip judgments whose verdict is REVIEW")
    p.add_argument("--no-graph", action="store_true")
    p.add_argument("--no-vectors", action="store_true")
    p.add_argument("--force", action="store_true", help="load despite duplicate or missing-OCR checks")
    p.add_argument("--embedder", choices=["bge", "hash"], default="bge")
    p.add_argument("--no-context", action="store_true",
                   help="embed the bare passage without the context line (for comparison runs)")
    pdf_options(p)
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("mapping", help="validate or load the IPC/CrPC mapping")
    p.add_argument("action", choices=["validate", "load"])
    p.set_defaults(fn=cmd_mapping)

    p = sub.add_parser("graph", help="create constraints, show node counts")
    p.add_argument("action", choices=["init", "counts"])
    p.set_defaults(fn=cmd_graph)

    p = sub.add_parser("search", help="query the loaded chunks")
    p.add_argument("query")
    p.add_argument("--statute", help="only chunks citing this provision, e.g. IPC:304A or BNS:106(1)")
    p.add_argument("--case", help="only this case_id")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--embedder", choices=["bge", "hash"], default="bge")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("export", help="write the chunks as JSON lines, for Elasticsearch or review")
    p.add_argument("case_ids", nargs="*", help="case_ids from data/cases.yaml, or paths to PDFs")
    p.add_argument("--all", action="store_true")
    p.add_argument("--dir", help="every PDF in this folder")
    p.add_argument("--out", default="data/export/chunks.jsonl")
    pdf_options(p)
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("sample", help="pick and download the balanced validation sample from AWS Open Data")
    p.add_argument("--dir", default="data/raw/validation", help="where the PDFs and sample.csv go")
    p.add_argument("--size", type=int, default=100)
    p.add_argument("--seed", type=int, default=20261007, help="same seed, same sample")
    p.add_argument("--criminal-share", type=float, default=0.4, help="share of criminal matters per era")
    p.add_argument("--exclude", nargs="*", help="bucket paths to leave out (e.g. the curated judgments)")
    p.add_argument("--no-download", action="store_true", help="only write sample.csv and the draft records")
    p.set_defaults(fn=cmd_sample)

    p = sub.add_parser("evaluate", help="hit rate and wrong-document rate on known questions")
    p.add_argument("questions", help="YAML file of questions, see data/eval.example.yaml")
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--embedder", choices=["bge", "hash"], default="bge")
    p.set_defaults(fn=cmd_evaluate)

    args = parser.parse_args(argv)
    return args.fn(args, load_settings())


if __name__ == "__main__":
    sys.exit(main())
