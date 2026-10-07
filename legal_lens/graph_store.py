"""The Neo4j knowledge graph.

GraphWriter holds the Cypher and takes any `run(query, params)` callable,
so the logic can be tested without a database. Neo4jRunner is the real
connection. Every write uses MERGE on a unique ID, so re-running a load
updates the graph instead of duplicating it.
"""

from __future__ import annotations

from typing import Callable

from .mapping import MappingRow

Runner = Callable[[str, dict], list[dict]]

SCHEMA = [
    "CREATE CONSTRAINT case_id IF NOT EXISTS FOR (c:Case) REQUIRE c.case_id IS UNIQUE",
    "CREATE CONSTRAINT statute_id IF NOT EXISTS FOR (s:Statute) REQUIRE s.statute_id IS UNIQUE",
    "CREATE CONSTRAINT judge_id IF NOT EXISTS FOR (j:Judge) REQUIRE j.judge_id IS UNIQUE",
    "CREATE CONSTRAINT concept_name IF NOT EXISTS FOR (k:Concept) REQUIRE k.name IS UNIQUE",
    "CREATE INDEX statute_code_section IF NOT EXISTS FOR (s:Statute) ON (s.code, s.section)",
]

UPSERT_CASE = """
MERGE (c:Case {case_id: $case_id})
SET c.name = $name,
    c.date = date($date),
    c.court = $court,
    c.citation = $citation,
    c.citations = $citations,
    c.citation_keys = $citation_keys,
    c.outcome = $outcome,
    c.doc_sha256 = $doc_sha256,
    c.paragraph_count = $paragraph_count,
    c.para_numbering = $para_numbering,
    c.is_stub = false
"""

# A case cited earlier exists as a stub keyed by its citation. When the full
# judgment arrives, move the stub's incoming CITES edges onto it and drop the stub.
ABSORB_STUBS = """
MATCH (c:Case {case_id: $case_id})
MATCH (stub:Case {is_stub: true})
WHERE stub.case_id <> $case_id AND any(k IN stub.citation_keys WHERE k IN $citation_keys)
OPTIONAL MATCH (src:Case)-[r:CITES]->(stub)
WHERE src.case_id <> $case_id
FOREACH (_ IN CASE WHEN src IS NULL THEN [] ELSE [1] END |
    MERGE (src)-[n:CITES]->(c)
    SET n += properties(r)
)
WITH DISTINCT stub
DETACH DELETE stub
"""

# Deleting a stub also deletes any OVERRULES edge that pointed at it, so rebuild
# those from the CITES edges that were just moved across.
REBUILD_OVERRULES = """
MATCH (a:Case)-[e:CITES {type: 'overrules'}]->(b:Case {case_id: $case_id})
MERGE (a)-[o:OVERRULES]->(b)
SET o.paragraph = e.paragraph
"""

SET_BENCH = """
MATCH (c:Case {case_id: $case_id})
UNWIND $judges AS j
MERGE (g:Judge {judge_id: j.judge_id})
SET g.name = j.name
MERGE (c)-[:HEARD_BY]->(g)
"""

SET_OPINIONS = """
MATCH (c:Case {case_id: $case_id})
UNWIND $opinions AS o
MERGE (g:Judge {judge_id: o.judge_id})
ON CREATE SET g.name = o.name
MERGE (c)-[a:AUTHORED_BY]->(g)
SET a.opinion_type = o.type, a.opinion_index = o.index
"""

LINK_STATUTES = """
MATCH (c:Case {case_id: $case_id})
UNWIND $refs AS r
MERGE (s:Statute {statute_id: r.statute_id})
ON CREATE SET s.code = r.code, s.section = r.section,
              s.base_section = r.base_section, s.is_active = r.is_active
MERGE (c)-[i:INTERPRETS]->(s)
SET i.paragraph = r.paragraphs[0], i.paragraphs = r.paragraphs, i.mentions = r.mentions
"""

RESOLVE_KEYS = """
UNWIND $keys AS k
MATCH (c:Case) WHERE k IN c.citation_keys
RETURN k AS key, c.case_id AS case_id
"""

LINK_CITATIONS = """
MATCH (c:Case {case_id: $case_id})
UNWIND $refs AS r
MERGE (t:Case {case_id: r.target_id})
ON CREATE SET t.is_stub = true, t.name = r.name_hint, t.citation = r.citation,
              t.citations = r.citations, t.citation_keys = r.citation_keys
MERGE (c)-[e:CITES]->(t)
SET e.paragraph = r.paragraphs[0], e.paragraphs = r.paragraphs,
    e.type = coalesce(e.type, 'unclassified')
"""

MARK_OVERRULED = """
MATCH (c:Case {case_id: $case_id})-[e:CITES]->(t:Case)
WHERE any(k IN t.citation_keys WHERE k IN $keys)
SET e.type = 'overrules'
MERGE (c)-[o:OVERRULES]->(t)
SET o.paragraph = e.paragraph
"""

LOAD_REPLACEMENTS = """
UNWIND $rows AS m
MERGE (old:Statute {statute_id: m.old_id})
SET old.code = m.old_code, old.section = m.old_section, old.base_section = m.old_base,
    old.title = coalesce(m.heading, old.title),
    old.is_active = false, old.repealed_on = date(m.effective_date)
MERGE (new:Statute {statute_id: m.new_id})
SET new.code = m.new_code, new.section = m.new_section, new.base_section = m.new_base,
    new.is_active = true
MERGE (old)-[r:REPLACED_BY]->(new)
SET r.effective_date = date(m.effective_date), r.relation = m.relation,
    r.change_note = m.change_note, r.source = m.source, r.verified = m.verified
"""

LOAD_OMITTED = """
UNWIND $rows AS m
MERGE (old:Statute {statute_id: m.old_id})
SET old.code = m.old_code, old.section = m.old_section, old.base_section = m.old_base,
    old.title = coalesce(m.heading, old.title),
    old.is_active = false, old.repealed_on = date(m.effective_date),
    old.omitted = true, old.omitted_note = m.change_note
"""

COUNTS = """
MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY label
"""


class Neo4jRunner:
    """Runs Cypher against Neo4j Aura or a local server. Needs the neo4j 5 driver."""

    def __init__(self, uri: str, username: str, password: str, database: str = "neo4j"):
        from neo4j import GraphDatabase

        self._driver = GraphDatabase.driver(uri, auth=(username, password))
        self._database = database

    def __call__(self, query: str, params: dict | None = None) -> list[dict]:
        records, _, _ = self._driver.execute_query(
            query, parameters_=params or {}, database_=self._database
        )
        return [record.data() for record in records]

    def verify(self) -> None:
        self._driver.verify_connectivity()

    def close(self) -> None:
        self._driver.close()


class GraphWriter:
    def __init__(self, run: Runner):
        self.run = run

    def init_schema(self) -> None:
        for statement in SCHEMA:
            self.run(statement, {})

    def upsert_case(self, *, case_id: str, name: str, date: str, court: str, citations: list[str],
                    citation_keys: list[str], outcome: str | None, doc_sha256: str,
                    paragraph_count: int, para_numbering: str) -> None:
        params = dict(
            case_id=case_id, name=name, date=date, court=court,
            citation=citations[0] if citations else None, citations=citations,
            citation_keys=citation_keys, outcome=outcome, doc_sha256=doc_sha256,
            paragraph_count=paragraph_count, para_numbering=para_numbering,
        )
        self.run(UPSERT_CASE, params)
        self.run(ABSORB_STUBS, {"case_id": case_id, "citation_keys": citation_keys})
        self.run(REBUILD_OVERRULES, {"case_id": case_id})

    def set_bench(self, case_id: str, judges: list[dict]) -> None:
        if judges:
            self.run(SET_BENCH, {"case_id": case_id, "judges": judges})

    def set_opinions(self, case_id: str, opinions: list[dict]) -> None:
        if opinions:
            self.run(SET_OPINIONS, {"case_id": case_id, "opinions": opinions})

    def link_statutes(self, case_id: str, refs: list[dict]) -> None:
        if refs:
            self.run(LINK_STATUTES, {"case_id": case_id, "refs": refs})

    def link_citations(self, case_id: str, refs: list[dict]) -> int:
        """Create CITES edges. Each ref has citation_keys, citations, name_hint, paragraphs.

        A cited case already in the graph (full or stub) is reused; otherwise
        a stub node is created, keyed by the citation.
        """
        if not refs:
            return 0
        all_keys = sorted({k for r in refs for k in r["citation_keys"]})
        known = {row["key"]: row["case_id"] for row in self.run(RESOLVE_KEYS, {"keys": all_keys})}

        merged: dict[str, dict] = {}
        for ref in refs:
            target = next((known[k] for k in ref["citation_keys"] if k in known), None)
            target = target or f"cite:{ref['citation_keys'][0]}"
            if target == case_id:
                continue
            entry = merged.setdefault(
                target,
                {"target_id": target, "name_hint": ref.get("name_hint"), "citation": ref["citations"][0],
                 "citations": [], "citation_keys": [], "paragraphs": []},
            )
            entry["name_hint"] = entry["name_hint"] or ref.get("name_hint")
            for field in ("citations", "citation_keys", "paragraphs"):
                for value in ref[field]:
                    if value not in entry[field]:
                        entry[field].append(value)
        for entry in merged.values():
            entry["paragraphs"].sort()
        if merged:
            self.run(LINK_CITATIONS, {"case_id": case_id, "refs": list(merged.values())})
        return len(merged)

    def mark_overruled(self, case_id: str, keys: list[str]) -> None:
        if keys:
            self.run(MARK_OVERRULED, {"case_id": case_id, "keys": keys})

    def load_mapping(self, rows: list[MappingRow]) -> tuple[int, int]:
        replaced = [r.as_params() for r in rows if r.new_id]
        omitted = [r.as_params() for r in rows if not r.new_id]
        if replaced:
            self.run(LOAD_REPLACEMENTS, {"rows": replaced})
        if omitted:
            self.run(LOAD_OMITTED, {"rows": omitted})
        return len(replaced), len(omitted)

    def counts(self) -> dict[str, int]:
        return {row["label"]: row["n"] for row in self.run(COUNTS, {})}
