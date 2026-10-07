"""End to end on a generated PDF, with in-memory stand-ins for Qdrant and Neo4j."""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from legal_lens import graph_store
from legal_lens.config import ROOT
from legal_lens.embed import HashEmbedder
from legal_lens.graph_store import GraphWriter
from legal_lens.manifest import CaseMeta, Opinion
from legal_lens.mapping import Mapping
from legal_lens.pipeline import DuplicateCaseError, build_case, ingest_case
from legal_lens.vector_store import MemoryStore

from .sample_pdf import build_sample_pdf


class FakeGraph:
    """Records every Cypher call. Answers the one read query from `known`."""

    def __init__(self, known=None):
        self.calls = []
        self.known = known or {}

    def __call__(self, query, params):
        self.calls.append((query, params))
        if query == graph_store.RESOLVE_KEYS:
            return [{"key": k, "case_id": v} for k, v in self.known.items() if k in params["keys"]]
        return []

    def params_for(self, query):
        return [p for q, p in self.calls if q == query]


def sample_meta(case_id="sample_2019"):
    return CaseMeta(
        case_id=case_id, name="Sample Appellant v. State of Example", date=dt.date(2019, 3, 14),
        court="Supreme Court", citations=["(2019) 99 SCC 999"], bench=["A.B. Tester"],
        file="unused", opinions=[Opinion("A.B. Tester", "majority")],
        summary="Invented test appeal about criminal negligence by a surgeon",
    )


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pdf = build_sample_pdf(Path(cls.tmp.name) / "sample.pdf")
        cls.mapping = Mapping.from_dir(ROOT / "data" / "mappings")
        cls.built = build_case(sample_meta(), cls.pdf, cls.mapping, backend="pdfplumber")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_pdf_is_read_and_split_on_its_own_paragraph_numbers(self):
        self.assertEqual(self.built.warnings, [])
        self.assertEqual(self.built.split.numbering, "original")
        self.assertEqual([p.number for p in self.built.split.paragraphs], list(range(1, 12)))
        self.assertGreaterEqual(len(self.built.extraction.pages), 3)

    def test_running_header_and_page_numbers_are_removed(self):
        text = self.built.extraction.text
        self.assertNotIn("Crl. A. No. 999", text)
        self.assertNotIn("Page 2 of", text)

    def test_statutes_are_found_with_their_paragraphs(self):
        by_id = {s["statute_id"]: s for s in self.built.statute_refs}
        self.assertEqual(by_id["IPC:304A"]["paragraphs"], [2, 5, 10])   # para 10 is the OCR-damaged '3O4A'
        self.assertFalse(by_id["IPC:304A"]["is_active"])
        self.assertEqual(by_id["CrPC:482"]["paragraphs"], [3])
        self.assertIn("BNS:106(1)", by_id)
        self.assertEqual(self.built.unmapped, [])

    def test_cited_cases_are_found_once_each(self):
        firsts = sorted(r["citations"][0] for r in self.built.case_refs)
        self.assertEqual(firsts, ["(2004) 6 SCC 422", "(2005) 6 SCC 1", "[1957] 1 WLR 582"])
        jacob = next(r for r in self.built.case_refs if r["citations"][0] == "(2005) 6 SCC 1")
        self.assertEqual(jacob["paragraphs"], [4, 8])
        self.assertEqual(jacob["citation_keys"], ["2005-6-scc-1", "air-2005-sc-3180"])

    def test_payload_has_what_the_backend_needs(self):
        payload = self.built.payloads[0]
        for key in ["case_id", "case_name", "chunk_id", "paragraph_num", "para_end", "court", "date",
                    "judges", "text", "statutes", "statutes_current", "opinion_type", "citation"]:
            self.assertIn(key, payload)
        self.assertEqual(payload["date"], "2019-03-14")
        self.assertEqual(payload["opinion_author"], "A.B. Tester")
        self.assertIn("IPC:304A", payload["statutes"])
        self.assertIn("BNS:106(1)", payload["statutes_current"])

    def test_chunks_follow_headings_and_roles(self):
        got = [(c.para_start, c.para_end, c.role, c.heading) for c in self.built.chunks
               if c.part == 0 and c.role != "case_card"]
        self.assertEqual(got, [
            (1, 2, "facts", "FACTUAL BACKGROUND"),
            (3, 3, "lower_court", "FACTUAL BACKGROUND"),
            (4, 4, "argument_petitioner", "SUBMISSIONS"),
            (5, 5, "analysis", "ANALYSIS"),
            (6, 6, "analysis", "ANALYSIS"),
            (7, 9, "analysis", "ANALYSIS"),
            (10, 11, "ruling", "ORDER"),
        ])
        self.assertEqual(self.built.split.headings, ["FACTUAL BACKGROUND", "SUBMISSIONS", "ANALYSIS", "ORDER"])
        self.assertNotIn("SUBMISSIONS", self.built.chunks[1].text)       # headings are not left in the passage

    def test_context_line_is_embedded_but_not_stored_in_the_passage(self):
        payload = self.built.payloads[2]
        self.assertEqual(
            payload["context"],
            "Sample Appellant v. State of Example, Supreme Court, 14 March 2019, (2019) 99 SCC 999. "
            "Invented test appeal about criminal negligence by a surgeon. Section: SUBMISSIONS.")
        self.assertTrue(payload["text"].startswith("4. Learned counsel"))
        with_context = self.built.embedding_texts()[2]
        self.assertTrue(with_context.startswith(payload["context"]) and with_context.endswith(payload["text"]))
        self.assertEqual(self.built.embedding_texts(with_context=False)[2], payload["text"])
        self.assertEqual((payload["rhetorical_role"], payload["role_source"]), ("argument_petitioner", "heuristic"))

    def test_neighbour_links_form_a_chain(self):
        passages = [p for p in self.built.payloads if p["doc_type"] == "judgment"]
        ids = [p["chunk_id"] for p in passages]
        self.assertIsNone(passages[0]["prev_chunk_id"])
        self.assertIsNone(passages[-1]["next_chunk_id"])
        self.assertEqual([p["next_chunk_id"] for p in passages[:-1]], ids[1:])
        self.assertEqual([p["prev_chunk_id"] for p in passages[1:]], ids[:-1])

    def test_blocks_keep_one_speaker_together(self):
        by_id = {p["chunk_id"]: p for p in self.built.payloads}
        # the four analysis chunks (paras 5 to 9) are one block, in order
        analysis = [by_id[i] for i in ["sample_2019_o0_p5", "sample_2019_o0_p6_0", "sample_2019_o0_p6_1", "sample_2019_o0_p7-9"]]
        self.assertEqual(len({p["block_id"] for p in analysis}), 1)
        self.assertEqual([p["block_index"] for p in analysis], [0, 1, 2, 3])
        # counsel's submission is its own block, so expanding the analysis never pulls it in
        self.assertNotEqual(by_id["sample_2019_o0_p4"]["block_id"], analysis[0]["block_id"])

    def test_parts_of_one_paragraph_share_a_parent_for_deduplication(self):
        by_id = {p["chunk_id"]: p for p in self.built.payloads}
        self.assertEqual(by_id["sample_2019_o0_p6_0"]["parent_id"], "sample_2019_o0_p6")
        self.assertEqual(by_id["sample_2019_o0_p6_1"]["parent_id"], "sample_2019_o0_p6")
        self.assertEqual(by_id["sample_2019_o0_p7-9"]["parent_id"], "sample_2019_o0_p7-9")

    def test_citation_label_and_keywords_for_the_prompt_and_keyword_index(self):
        by_id = {p["chunk_id"]: p for p in self.built.payloads}
        self.assertEqual(by_id["sample_2019_o0_p4"]["cite_as"],
                         "Sample Appellant v. State of Example, (2019) 99 SCC 999, para 4")
        self.assertEqual(by_id["sample_2019_o0_p7-9"]["cite_as"],
                         "Sample Appellant v. State of Example, (2019) 99 SCC 999, paras 7-9")
        first = by_id["sample_2019_o0_p1-2"]
        self.assertEqual(first["keywords"][:4], ["IPC 304A", "CrPC 154", "BNS 106(1)", "BNSS 173"])
        cites = by_id["sample_2019_o0_p4"]
        self.assertIn("2005-6-scc-1", cites["cited_cases"])
        self.assertIn("Jacob Mathew v. State of Punjab", cites["keywords"])
        # every chunk fits a cross-encoder window together with a question
        self.assertTrue(all(p["tokens"] <= 384 for p in self.built.payloads))

    def test_case_card_answers_searches_by_name(self):
        card = self.built.payloads[-1]
        self.assertEqual((card["doc_type"], card["chunk_id"], card["paragraph_num"]),
                         ("case_card", "sample_2019_card", 0))
        self.assertIn("Sample Appellant v. State of Example", card["text"])
        self.assertIn("Bench: A.B. Tester", card["text"])
        self.assertIn("IPC 304A", card["text"])
        self.assertIn("IPC:304A", card["statutes"])
        self.assertEqual(self.built.embedding_texts()[-1], card["text"])     # no context line on the card

        store, embedder = MemoryStore(), HashEmbedder()
        ingest_case(self.built, embedder, store, None)
        hits = store.search(embedder.embed(["Sample Appellant v State of Example citations bench decided"])[0], limit=1)
        self.assertEqual(hits[0].payload["doc_type"], "case_card")

    def test_load_search_reload_and_duplicate_guard(self):
        store, embedder, fake = MemoryStore(), HashEmbedder(), FakeGraph()
        first = ingest_case(self.built, embedder, store, GraphWriter(fake))
        self.assertEqual(first["points"], len(self.built.chunks))

        # A search for the new section number finds the passage that cites the old one.
        query = embedder.embed(["surgeon charged after patient died during operation"])[0]
        hits = store.search(query, limit=3, statute="BNS:106(1)")
        self.assertTrue(hits)
        self.assertIn("IPC:304A", hits[0].payload["statutes"])
        self.assertEqual(hits[0].payload["paragraph_num"], 1)

        # Loading the same case again replaces it; nothing is duplicated.
        ingest_case(self.built, embedder, store, GraphWriter(fake))
        self.assertEqual(len(store.points), len(self.built.chunks))

        # The same PDF under another case_id is refused.
        other = build_case(sample_meta("another_id"), self.pdf, self.mapping, backend="pdfplumber")
        with self.assertRaises(DuplicateCaseError):
            ingest_case(other, embedder, store, None)

    def test_graph_calls(self):
        fake = FakeGraph(known={"2005-6-scc-1": "jacob_mathew_2005"})
        ingest_case(self.built, HashEmbedder(), None, GraphWriter(fake))

        case = fake.params_for(graph_store.UPSERT_CASE)[0]
        self.assertEqual((case["case_id"], case["date"], case["paragraph_count"]), ("sample_2019", "2019-03-14", 11))
        self.assertEqual(fake.params_for(graph_store.SET_BENCH)[0]["judges"],
                         [{"judge_id": "a_b_tester", "name": "A.B. Tester"}])

        targets = {r["target_id"]: r for r in fake.params_for(graph_store.LINK_CITATIONS)[0]["refs"]}
        # Jacob Mathew is already in the graph, so the edge goes to the real node, not a stub.
        self.assertEqual(sorted(targets), ["cite:1957-1-wlr-582", "cite:2004-6-scc-422", "jacob_mathew_2005"])
        self.assertEqual(targets["jacob_mathew_2005"]["paragraphs"], [4, 8])
        self.assertEqual(targets["cite:1957-1-wlr-582"]["name_hint"],
                         "Bolam v. Friern Hospital Management Committee")


    def test_overruled_case_gets_an_edge_even_if_the_text_never_cites_it(self):
        meta = sample_meta()
        meta.overrules = ["(1990) 1 SCC 50"]
        built = build_case(meta, self.pdf, self.mapping, backend="pdfplumber")
        fake = FakeGraph()
        ingest_case(built, HashEmbedder(), None, GraphWriter(fake))
        targets = [r["target_id"] for r in fake.params_for(graph_store.LINK_CITATIONS)[0]["refs"]]
        self.assertIn("cite:1990-1-scc-50", targets)
        self.assertEqual(fake.params_for(graph_store.MARK_OVERRULED)[0]["keys"], ["1990-1-scc-50"])


class MappingLoad(unittest.TestCase):
    def test_replacements_and_omissions_go_to_separate_queries(self):
        fake = FakeGraph()
        mapping = Mapping.from_dir(ROOT / "data" / "mappings")
        replaced, omitted = GraphWriter(fake).load_mapping(mapping.rows)
        self.assertEqual(omitted, 3)                      # IPC 309, 377, 497
        self.assertEqual(replaced + omitted, len(mapping.rows))
        rows = {r["old_id"]: r for r in fake.params_for(graph_store.LOAD_REPLACEMENTS)[0]["rows"]}
        self.assertEqual(rows["IPC:304A"]["new_id"], "BNS:106(1)")
        self.assertEqual(rows["IPC:304A"]["new_base"], "106")
        self.assertEqual(rows["IPC:304A"]["effective_date"], "2024-07-01")
        gone = {r["old_id"] for r in fake.params_for(graph_store.LOAD_OMITTED)[0]["rows"]}
        self.assertEqual(gone, {"IPC:309", "IPC:377", "IPC:497"})


if __name__ == "__main__":
    unittest.main()
