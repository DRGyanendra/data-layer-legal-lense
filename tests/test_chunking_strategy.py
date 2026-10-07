"""Headings, rhetorical roles, role-pure chunks, the context line, and evaluation."""

import unittest

from legal_lens.chunking import chunk_paragraphs, context_line
from legal_lens.evaluate import Question, score
from legal_lens.paragraphs import Line, Paragraph, is_heading, split_paragraphs
from legal_lens.roles import label_paragraphs, quoted_share
from legal_lens.vector_store import Hit


def para(number, text, heading=None, segment=0):
    return Paragraph(segment, number, f"{number}. {text}", 1, heading)


def roles_of(*texts, heading=None, state=None):
    return label_paragraphs([para(i + 1, t, heading) for i, t in enumerate(texts)], state)


class Headings(unittest.TestCase):
    def test_what_counts_as_a_heading(self):
        for text in ["SUBMISSIONS", "SUBMISSIONS ON BEHALF OF THE APPELLANT", "A N A L Y S I S",
                     "B. Analysis", "II. Submissions of the State"]:
            self.assertTrue(is_heading(text), text)
        for text in ["(2005) 6 SCC 1", "AIR 2005 SC 3180", "The court heard the matter.",
                     "NEW DELHI;", "of the Indian Penal Code", "J."]:
            self.assertFalse(is_heading(text), text)

    def test_headings_are_lifted_out_and_attached_to_what_follows(self):
        doc = """
1. Leave granted.
FACTUAL BACKGROUND
2. The appellant was charged.
3. The trial began.
S U B M I S S I O N S
4. Learned counsel for the appellant submitted that the charge was bad.
5. Per contra, it was urged that the charge was good.
ANALYSIS
6. We have considered the matter.
"""
        result = split_paragraphs([Line(1, l) for l in doc.strip().split("\n")])
        self.assertEqual(result.headings, ["FACTUAL BACKGROUND", "SUBMISSIONS", "ANALYSIS"])
        self.assertEqual([p.heading for p in result.paragraphs],
                         [None, "FACTUAL BACKGROUND", "FACTUAL BACKGROUND", "SUBMISSIONS", "SUBMISSIONS", "ANALYSIS"])
        self.assertEqual(result.paragraphs[0].text, "1. Leave granted.")      # heading removed from the text

    def test_capitals_at_the_end_of_a_wrapped_sentence_are_not_a_heading(self):
        doc = "1. One.\n2. Two.\n3. Three.\n4. Four.\n5. The offence is defined in the\nINDIAN PENAL CODE\n6. Six."
        result = split_paragraphs([Line(1, l) for l in doc.split("\n")])
        self.assertEqual(result.headings, [])
        self.assertIn("INDIAN PENAL CODE", result.paragraphs[4].text)


class Roles(unittest.TestCase):
    def test_counsel_for_each_side(self):
        self.assertEqual(roles_of(
            "Mr. X, learned senior counsel appearing for the appellant, submitted that the negligence was not gross.",
            "It was further submitted that the High Court ignored the medical evidence.",
            "Per contra, learned counsel for the State contended that the charge was rightly framed.",
            "According to him, the trial must proceed.",
            state="respondent",
        ), ["argument_petitioner", "argument_petitioner", "argument_respondent", "argument_respondent"])

    def test_the_state_gets_a_side_only_when_the_case_name_shows_it(self):
        from legal_lens.roles import state_side
        self.assertEqual(state_side("Jacob Mathew v. State of Punjab"), "respondent")
        self.assertEqual(state_side("State of Haryana v. Bhajan Lal"), "petitioner")
        self.assertIsNone(state_side("Vineeta Sharma v. Rakesh Sharma"))
        text = "The learned Additional Solicitor General argued that the statute is valid."
        self.assertEqual(roles_of(text), ["argument"])
        self.assertEqual(roles_of(text, state="respondent"), ["argument_respondent"])
        self.assertEqual(roles_of(text, state="petitioner"), ["argument_petitioner"])

    def test_named_counsel_without_a_party(self):
        self.assertEqual(roles_of("Shri Rakesh Dwivedi, learned senior advocate, appearing in the connected appeal, "
                                  "submitted that the creamy layer test cannot apply."), ["argument"])

    def test_cues_inside_quotations_are_ignored(self):
        text = ("In that decision the Bench said: “Learned counsel for the appellant submitted that the rule is void. "
                "The appeal is dismissed.” Nothing more turns on it.")
        self.assertNotIn(roles_of(text)[0], ["argument_petitioner", "ruling"])

    def test_a_ruling_is_only_recognised_near_the_end(self):
        texts = ["The appeal is dismissed, said the High Court in the earlier round, and we recount that history."] \
            + [f"The court then considered point number {i} at length." for i in range(30)] \
            + ["The appeal is allowed. No order as to costs."]
        roles = roles_of(*texts)
        self.assertNotEqual(roles[0], "ruling")
        self.assertEqual(roles[-1], "ruling")

    def test_hearing_both_sides_is_not_an_argument(self):
        role = roles_of(
            "We have heard learned counsel for the appellant and learned counsel for the respondent and perused the record."
        )[0]
        self.assertNotIn(role, ["argument_petitioner", "argument_respondent", "argument"])

    def test_the_courts_own_acts(self):
        self.assertEqual(roles_of("The short question that arises for consideration is whether negligence must be gross."), ["issue"])
        self.assertEqual(roles_of("The appeal is accordingly allowed. No order as to costs."), ["ruling"])
        self.assertEqual(roles_of("The impugned judgment of the High Court is set aside."), ["ruling"])
        self.assertEqual(roles_of("The High Court dismissed the petition, holding that a prima facie case existed."), ["lower_court"])
        self.assertEqual(roles_of("In our view the test in Bolam applies in India."), ["analysis"])

    def test_no_cue_means_unlabelled_not_a_guess(self):
        self.assertEqual(roles_of("The Act was amended in the year that followed."), ["unlabelled"])

    def test_near_misses(self):
        self.assertNotEqual(roles_of("We dismiss this contention as untenable.")[0], "ruling")
        self.assertNotIn(roles_of("Counsel relied on a passage for the statement of principle and submitted nothing more.")[0],
                         ["argument_respondent"])
        # counsel asking for an outcome is not the court granting it
        self.assertEqual(roles_of("Learned counsel for the appellant submitted that the appeal deserves to be allowed.")[0],
                         "argument_petitioner")

    def test_heading_decides_when_there_is_no_cue(self):
        self.assertEqual(roles_of("The patient was admitted on the fourth day.", heading="FACTUAL BACKGROUND"), ["facts"])
        self.assertEqual(roles_of("The Act does not apply to such cases.", heading="SUBMISSIONS ON BEHALF OF THE RESPONDENT"),
                         ["argument_respondent"])

    def test_roles_do_not_carry_into_the_next_opinion(self):
        paras = [para(9, "Learned counsel for the appellant submitted that the law is otherwise."),
                 para(1, "It was further submitted, in the earlier round, that the claim was barred.", segment=1)]
        self.assertEqual(label_paragraphs(paras), ["argument_petitioner", "argument"])

    def test_quoted_share(self):
        self.assertEqual(quoted_share("no quotation here"), 0.0)
        self.assertGreater(quoted_share('Section 304A reads: "Whoever causes the death of any person by doing any rash or negligent act."'), 0.6)


class RolePureChunks(unittest.TestCase):
    def test_argument_and_finding_are_never_in_one_chunk(self):
        paras = [para(8, "Learned counsel for the appellant submitted that negligence need not be gross."),
                 para(9, "We are unable to agree. Negligence must be gross.")]
        chunks = chunk_paragraphs("c", paras, label_paragraphs(paras))
        self.assertEqual([(c.para_start, c.role) for c in chunks], [(8, "argument_petitioner"), (9, "analysis")])

    def test_same_role_merges_and_heading_change_splits(self):
        paras = [para(2, "The patient was admitted.", "FACTS"), para(3, "The operation followed.", "FACTS"),
                 para(4, "The statute was enacted earlier.", "ANALYSIS")]
        chunks = chunk_paragraphs("c", paras, label_paragraphs(paras))
        self.assertEqual([(c.para_start, c.para_end, c.heading) for c in chunks], [(2, 3, "FACTS"), (4, 4, "ANALYSIS")])

    def test_a_tiny_opening_paragraph_rides_with_the_next(self):
        paras = [para(1, "Leave granted."), para(2, "The patient was admitted.", "FACTS")]
        chunks = chunk_paragraphs("c", paras, label_paragraphs(paras))
        self.assertEqual([(c.para_start, c.para_end, c.role) for c in chunks], [(1, 2, "facts")])

    def test_long_paragraph_parts_overlap_by_two_sentences(self):
        sentences = [f"Sentence number {i} says something about the standard of care." for i in range(40)]
        chunks = chunk_paragraphs("c", [para(24, " ".join(sentences))], max_tokens=200)
        self.assertGreater(len(chunks), 1)
        first, second = chunks[0].text, chunks[1].text
        self.assertTrue(second.startswith(" ".join(first.split(". ")[-2:])[:40]))
        self.assertTrue(all(c.tokens <= 200 for c in chunks))


class Substantive(unittest.TestCase):
    def check(self, text, role="unlabelled", refs=False):
        from legal_lens.chunking import Chunk, approx_tokens
        from legal_lens.pipeline import _is_substantive
        return _is_substantive(Chunk("c", 0, 1, 1, 0, 1, text, approx_tokens(text), role), refs)

    def test_boilerplate_is_kept_out_of_search(self):
        self.assertFalse(self.check("1. Leave granted."))
        self.assertFalse(self.check("2. Heard learned counsel for the parties."))
        self.assertFalse(self.check("3. Delay condoned."))

    def test_short_but_important_passages_stay_searchable(self):
        self.assertTrue(self.check("40. The appeal is allowed.", role="ruling"))
        self.assertTrue(self.check("7. See Section 304A IPC.", refs=True))
        self.assertTrue(self.check("9. " + "The court examined the standard of care owed by a surgeon. " * 3))

    def test_search_skips_non_substantive_unless_asked(self):
        from legal_lens.vector_store import MemoryStore, Point
        store = MemoryStore()
        store.replace_case("c", [Point("1", [1.0, 0.0], {"case_id": "c", "substantive": False}),
                                 Point("2", [0.9, 0.1], {"case_id": "c", "substantive": True})])
        self.assertEqual(len(store.search([1.0, 0.0])), 1)
        self.assertEqual(len(store.search([1.0, 0.0], substantive_only=False)), 2)


class ContextLine(unittest.TestCase):
    def test_format(self):
        line = context_line(case_name="Jacob Mathew v. State of Punjab", court="Supreme Court",
                            date="5 August 2005", citation="(2005) 6 SCC 1",
                            summary="Criminal negligence by doctors must be gross", heading="ANALYSIS")
        self.assertEqual(line, "Jacob Mathew v. State of Punjab, Supreme Court, 5 August 2005, (2005) 6 SCC 1. "
                               "Criminal negligence by doctors must be gross. Section: ANALYSIS.")
        self.assertLess(len(line) / 4, 100)        # stays a short line, not a second document


class Evaluation(unittest.TestCase):
    def hits(self, *rows):
        return [Hit(1.0 - i / 10, {"case_id": c, "paragraph_num": a, "para_end": b, "opinion_index": 0})
                for i, (c, a, b) in enumerate(rows)]

    def test_hit_rank_and_wrong_document_rate(self):
        results = {
            "q1": self.hits(("other", 3, 3), ("jacob", 40, 48), ("jacob", 2, 2)),     # found at rank 2
            "q2": self.hits(("other", 1, 1), ("other", 2, 2), ("other", 9, 9)),       # wrong case throughout
        }
        questions = [Question("q1", "jacob", [48]), Question("q2", "jacob", [12])]
        report = score(questions, lambda q, k: results[q][:k], k=3)
        self.assertEqual(report.hit_at_k, 0.5)
        self.assertEqual(report.mrr, 0.25)
        self.assertAlmostEqual(report.drm_at_k, 4 / 6)
        self.assertEqual(len(report.misses), 1)


if __name__ == "__main__":
    unittest.main()
