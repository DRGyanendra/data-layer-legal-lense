"""Parsing, chunking and ID rules. No database or model needed."""

import unittest

from legal_lens.chunking import chunk_paragraphs, split_sentences
from legal_lens.citations import find_case_refs, find_statute_refs, repair_ocr_digits
from legal_lens.ids import base_section, citation_key, judge_id, normalize_section, point_id, statute_id
from legal_lens.paragraphs import Line, Paragraph, split_paragraphs


def ids(text):
    return [r.statute_id for r in find_statute_refs(text)]


def lines(text):
    return [Line(1, l) for l in text.strip("\n").split("\n")]


class StatuteRefs(unittest.TestCase):
    def test_every_spelling_of_304a_is_one_provision(self):
        for text in [
            "Section 304A IPC", "Section 304-A of the Indian Penal Code, 1860",
            "S. 304A, I.P.C.", "IPC Sec. 304A", "IPC 304A", "u/s 304A IPC",
            "Section 304A of the Penal Code",
        ]:
            self.assertEqual(ids(text), ["IPC:304A"], text)

    def test_lists_and_read_with(self):
        self.assertEqual(ids("Sections 302, 307 and 34 IPC"), ["IPC:302", "IPC:307", "IPC:34"])
        self.assertEqual(ids("Section 302 read with Section 34 of the Penal Code"), ["IPC:302", "IPC:34"])

    def test_procedure_codes_and_new_codes(self):
        self.assertEqual(ids("u/s 154 Cr.P.C."), ["CrPC:154"])
        self.assertEqual(ids("Section 156(3) of the Code of Criminal Procedure, 1973"), ["CrPC:156(3)"])
        self.assertEqual(ids("Section 106(1) of the Bharatiya Nyaya Sanhita, 2023"), ["BNS:106(1)"])
        self.assertEqual(ids("Section 173 BNSS"), ["BNSS:173"])

    def test_other_acts_keep_their_name(self):
        self.assertEqual(ids("Section 6 of the Hindu Succession Act, 1956"), ["Hindu Succession Act, 1956:6"])

    def test_articles_mean_the_constitution_unless_told_otherwise(self):
        self.assertEqual(ids("Articles 14, 19 and 21 of the Constitution"),
                         ["Constitution:14", "Constitution:19", "Constitution:21"])
        self.assertEqual(ids("Article 19(1)(a)"), ["Constitution:19(1)(a)"])
        self.assertEqual(ids("Article 17 of the ICCPR"), [])

    def test_ocr_letter_o_is_read_as_zero(self):
        self.assertEqual(repair_ocr_digits("Section 3O4A")[0], "Section 304A")
        self.assertEqual(ids("Section 3O4A IPC"), ["IPC:304A"])

    def test_things_that_are_not_sections(self):
        self.assertEqual(ids("Section 304A IPC 2 years imprisonment"), ["IPC:304A"])
        self.assertEqual(ids("Section 300 IPC 1860"), ["IPC:300"])
        self.assertEqual(ids("Section 302 I.P.C. was"), ["IPC:302"])
        self.assertEqual(ids("under Section 304A. The court"), [])       # no Act named


class CaseRefs(unittest.TestCase):
    def test_parallel_citations_are_one_case(self):
        refs = find_case_refs("Jacob Mathew v. State of Punjab, (2005) 6 SCC 1 : 2005 SCC (Cri) 1369 : AIR 2005 SC 3180, held")
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].citations, ["(2005) 6 SCC 1", "2005 SCC (Cri) 1369", "AIR 2005 SC 3180"])
        self.assertEqual(refs[0].name_hint, "Jacob Mathew v. State of Punjab")

    def test_two_cases_joined_by_and_stay_separate(self):
        refs = find_case_refs("See Prakash v. Phulavati (2016) 2 SCC 36 and Danamma v. Amar (2018) 3 SCC 343.")
        self.assertEqual([r.name_hint for r in refs], ["Prakash v. Phulavati", "Danamma v. Amar"])

    def test_foreign_reports_and_alternate_scc_style(self):
        self.assertEqual(find_case_refs("Bolam v. Friern Hospital Management Committee [1957] 1 WLR 582")[0].keys,
                         ["1957-1-wlr-582"])
        self.assertEqual(find_case_refs("reported as 2005 (6) SCC 1")[0].citations, ["(2005) 6 SCC 1"])


class Paragraphs(unittest.TestCase):
    DOC = """
IN THE SUPREME COURT OF INDIA
1. Leave granted.
2. The facts are these.
3. The directions are:
1. first
2. second
3. third
4. After the list.
5. Five.
6. Six, with a longer list:
1. a
2. b
3. c
4. d
5. e
7. Seven.
8. Eight.
10. Ten, nine was lost to OCR.
11. Eleven.
2005. a wrapped line that begins with a year
12. Twelve.
"""

    def test_lists_inside_a_paragraph_are_not_paragraphs(self):
        result = split_paragraphs(lines(self.DOC))
        self.assertEqual(result.numbering, "original")
        self.assertEqual([p.number for p in result.paragraphs], [1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12])
        self.assertIn("1. first 2. second 3. third", result.paragraphs[2].text)
        self.assertEqual(result.header, "IN THE SUPREME COURT OF INDIA")

    def test_a_second_opinion_restarts_numbering_as_a_new_segment(self):
        second = "\n".join(f"{i}. opinion two, paragraph {i}" for i in range(1, 16))
        result = split_paragraphs(lines(self.DOC + second))
        self.assertEqual(result.segments, 2)
        self.assertEqual([p.number for p in result.paragraphs if p.segment == 1], list(range(1, 16)))

    def test_unnumbered_text_falls_back_and_says_so(self):
        result = split_paragraphs(lines("The court heard the matter at length.\n" * 200))
        self.assertEqual(result.numbering, "synthetic")
        self.assertGreater(result.count, 1)


class Chunking(unittest.TestCase):
    def test_short_paragraphs_merge_and_keep_their_range(self):
        paras = [Paragraph(0, n, f"{n}. " + "word " * 30, 1) for n in range(1, 7)]
        chunks = chunk_paragraphs("c", paras, target_tokens=100, max_tokens=200)
        self.assertEqual([(c.para_start, c.para_end) for c in chunks], [(1, 2), (3, 4), (5, 6)])
        self.assertEqual(chunks[0].chunk_id, "c_o0_p1-2")

    def test_long_paragraph_splits_under_the_limit_with_one_number(self):
        text = "24. " + "The standard of care was examined in detail by the court. " * 60
        chunks = chunk_paragraphs("c", [Paragraph(0, 24, text, 3)], max_tokens=200)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c.tokens <= 200 for c in chunks))
        self.assertTrue(all(c.para_start == c.para_end == 24 for c in chunks))
        self.assertEqual(chunks[1].chunk_id, "c_o0_p24_1")

    def test_opinions_are_never_merged_together(self):
        paras = [Paragraph(0, 9, "9. End of the first opinion.", 1), Paragraph(1, 1, "1. Start of the second.", 2)]
        self.assertEqual(len(chunk_paragraphs("c", paras)), 2)

    def test_sentences_do_not_break_on_v_or_initials(self):
        text = "In Jacob Mathew v. State of Punjab the Court spoke. Dr. R. Rao appeared. It was held so."
        self.assertEqual(len(split_sentences(text)), 3)


class Ids(unittest.TestCase):
    def test_section_forms(self):
        self.assertEqual(normalize_section("304-A"), "304A")
        self.assertEqual(normalize_section("156 (3)"), "156(3)")
        self.assertEqual(base_section("106(1)"), "106")
        self.assertEqual(statute_id("BNS", "106 (1)"), "BNS:106(1)")

    def test_point_ids_are_stable_uuids(self):
        import uuid
        self.assertEqual(point_id("a_o0_p1"), point_id("a_o0_p1"))
        self.assertNotEqual(point_id("a_o0_p1"), point_id("a_o0_p2"))
        uuid.UUID(point_id("a_o0_p1"))

    def test_keys(self):
        self.assertEqual(citation_key("(2005) 6 SCC 1"), "2005-6-scc-1")
        self.assertEqual(judge_id("Justice R.C. Lahoti"), judge_id("R. C. LAHOTI, CJI"))


if __name__ == "__main__":
    unittest.main()
