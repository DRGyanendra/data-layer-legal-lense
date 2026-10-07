"""Layouts found in real Supreme Court PDFs: several opinions, different
numbering styles, indexes of headings, sub-paragraphs, footnotes and stamps.

Each case here reproduces something that broke on an actual judgment.
The text is invented.
"""

import tempfile
import unittest
from pathlib import Path

from legal_lens.chunking import chunk_paragraphs
from legal_lens.paragraphs import Line, split_paragraphs
from legal_lens.pdf_extract import _footnotes, _tidy, extract_pdf


def L(text, x0=72.0, page=1):
    return Line(page, text, x0)


def cause_title():
    return [L("REPORTABLE", 400), L("IN THE SUPREME COURT OF INDIA", 170), L("WRIT PETITION (CRIMINAL) NO. 1 OF 2017", 150),
            L("Petitioner Name …Petitioner", 100), L("VERSUS", 280), L("Union of Somewhere …Respondent", 100)]


def body(first, last, dot=True, x0=72.0, page=1):
    out = []
    for n in range(first, last + 1):
        out.append(L(f"{n}{'.' if dot else ''} Paragraph {n} of this opinion states a proposition of law.", x0, page))
        out.append(L("It continues on a second line and ends here.", x0, page))
    return out


def signature(name):
    return [L("……………………..J.", 300), L(f"({name})", 300), L("New Delhi", 72), L("September 27, 2018", 72)]


class SeveralOpinions(unittest.TestCase):
    def setUp(self):
        lines = cause_title() + [L("J U D G M E N T", 250), L("Dipak Misra, CJI (For himself and A.M. Khanwilkar, J.)")]
        # first paragraph unnumbered, then 2., 3., ...
        lines += [L("The opening paragraph carries no number at all."), L("It runs to a second line.")]
        lines += body(2, 9) + signature("DIPAK MISRA")
        # second opinion: numbered from 1
        lines += cause_title() + [L("J U D G M E N T", 250), L("R.F. Nariman, J. (Concurring)")] + body(1, 7, page=2) + signature("R.F. NARIMAN")
        # third opinion: bare numbers, an index of headings, an epigraph after a heading
        lines += cause_title() + [L("J U D G M E N T", 250), L("Index"), L("A Gender and the law"), L("B Relics of the past"),
                                  L("Dr Dhananjaya Y Chandrachud, J"), L("A Gender and the law")]
        lines += body(1, 4, dot=False, page=3)
        lines += [L("B Relics of the past", page=3), L("“An epigraph that precedes the next paragraph”", 144, 3)]
        lines += body(5, 8, dot=False, page=3) + signature("Dr Dhananjaya Y Chandrachud")
        self.result = split_paragraphs(lines)

    def test_each_judgment_marker_opens_an_opinion_with_its_author(self):
        self.assertEqual(self.result.segments, 3)
        self.assertEqual([o.author_line for o in self.result.opinions],
                         ["Dipak Misra, CJI (For himself and A.M. Khanwilkar, J.)", "R.F. Nariman, J. (Concurring)",
                          "Dr Dhananjaya Y Chandrachud, J"])
        self.assertEqual([o.style for o in self.result.opinions], ["dot", "dot", "bare"])

    def test_unnumbered_first_paragraph_becomes_paragraph_one(self):
        first = [p for p in self.result.paragraphs if p.segment == 0]
        self.assertEqual([p.number for p in first], list(range(1, 10)))
        self.assertTrue(first[0].text.startswith("The opening paragraph carries no number"))

    def test_bare_numbers_and_index_headings(self):
        third = [p for p in self.result.paragraphs if p.segment == 2]
        self.assertEqual([p.number for p in third], list(range(1, 9)))
        self.assertEqual([p.heading for p in third], ["A Gender and the law"] * 4 + ["B Relics of the past"] * 4)
        self.assertNotIn("B Relics of the past", third[3].text)          # the heading is lifted out

    def test_cause_title_and_signature_are_not_in_any_paragraph(self):
        text = " ".join(p.text for p in self.result.paragraphs)
        for junk in ["REPORTABLE", "SUPREME COURT OF INDIA", "VERSUS", "New Delhi", "September 27, 2018", "NARIMAN)"]:
            self.assertNotIn(junk, text)

    def test_opinions_are_matched_to_the_manifest_by_name(self):
        import datetime as dt
        from legal_lens.manifest import CaseMeta, Opinion
        from legal_lens.pipeline import _opinion_for_segment
        meta = CaseMeta("x", "A v. Union of Somewhere", dt.date(2018, 9, 27), "Supreme Court", [], [], "f",
                        opinions=[Opinion("D.Y. Chandrachud", "concurring"), Opinion("Dipak Misra", "majority"),
                                  Opinion("R.F. Nariman", "concurring"), Opinion("A.M. Khanwilkar", "majority")])
        warnings = []
        found = _opinion_for_segment(meta, self.result, warnings)
        # listed in the wrong order in the manifest; names still decide. Khanwilkar is named
        # in the first header only as joining, so Misra is the author.
        self.assertEqual(found, {0: ("Dipak Misra", "majority"), 1: ("R.F. Nariman", "concurring"),
                                 2: ("D.Y. Chandrachud", "concurring")})


class NumberingTraps(unittest.TestCase):
    def test_quoted_paragraph_numbers_are_ignored_because_they_are_indented(self):
        lines = [L("J U D G M E N T", 250)] + body(1, 3)
        lines += [L("4. This Court said in an earlier case:")] + [L(f"{n}. a quoted paragraph from another judgment.", 144) for n in (5, 6, 7)]
        lines += body(5, 9)
        result = split_paragraphs(lines)
        self.assertEqual([p.number for p in result.paragraphs], list(range(1, 10)))
        self.assertIn("a quoted paragraph", result.paragraphs[3].text)   # stays inside paragraph 4

    def test_a_paragraph_typed_at_a_different_indent_is_recovered(self):
        lines = [L("J U D G M E N T", 250)] + body(1, 4) + body(5, 5, x0=120.0) + body(6, 9)
        self.assertEqual([p.number for p in split_paragraphs(lines).paragraphs], list(range(1, 10)))

    def test_a_short_list_in_an_unnumbered_judgment_is_not_paragraph_numbering(self):
        prose = [L("The court considered the evidence in this matter at length.", 29 if i % 6 else 77) for i in range(120)]
        prose[40:40] = [L("1. the existence of a duty of care,", 29), L("2. a breach of that duty,", 29), L("3. damage resulting from it.", 29)]
        result = split_paragraphs([L("J U D G M E N T", 250), L("R.C. LAHOTI, CJI", 29)] + prose)
        self.assertEqual(result.numbering, "synthetic")
        self.assertEqual(result.opinions[0].author_line, "R.C. LAHOTI, CJI")
        self.assertGreater(result.count, 10)                              # split at the indents
        self.assertTrue(result.paragraphs[0].text.startswith("The court considered"))   # nothing before it is lost

    def test_sub_paragraphs_and_numbered_titles(self):
        lines = [L("J U D G M E N T", 250)] + body(1, 4)
        lines += [L("5. HISTORICAL BACKGROUND"), L("5.1. The first sub-paragraph says one thing."),
                  L("5.2. The second sub-paragraph says another."), L("5.3. The third closes the point.")]
        lines += body(6, 9)
        result = split_paragraphs(lines)
        fifth = [p for p in result.paragraphs if p.number == 5]
        self.assertEqual([p.label for p in fifth], ["5.1", "5.2", "5.3"])
        self.assertEqual({p.heading for p in fifth}, {"HISTORICAL BACKGROUND"})
        chunks = chunk_paragraphs("c", fifth)
        self.assertEqual((chunks[0].chunk_id, chunks[0].span), ("c_o0_p5.1-5.3", "5.1-5.3"))


class ContentsTables(unittest.TestCase):
    def test_contents_table_with_a_header_row_and_wrapped_entries(self):
        lines = cause_title() + [L("JUDGMENT", 250), L("Dipak Misra, CJI (for himself and A.M. Khanwilkar, J.)"),
                                 L("C O N T E N T S", 270), L("S. No(s). Heading Page No(s)", 93),
                                 L("A. Introduction………………………… 3-11", 111),
                                 L("B. Submissions on behalf of the respondents", 111), L("and other intervenors.………… 31-44", 163),
                                 L("A. Introduction", 108),
                                 L("Not for nothing did the thinker say what he said.", 144), L("It bears repeating here.")]
        lines += body(2, 5)
        lines += [L("B. Submissions on behalf of the respondents and", 108), L("other intervenors", 108)]
        lines += body(6, 9)
        result = split_paragraphs(lines)
        paras = result.paragraphs
        self.assertEqual([p.number for p in paras], list(range(1, 10)))
        self.assertTrue(paras[0].text.startswith("Not for nothing"))          # the contents table is not paragraph 1
        self.assertEqual(paras[0].heading, "A. Introduction")
        self.assertEqual(paras[5].heading, "B. Submissions on behalf of the respondents and other intervenors")
        self.assertNotIn("intervenors", paras[4].text)


class LawReports(unittest.TestCase):
    def test_an_order_at_the_very_end_does_not_swallow_the_judgment(self):
        prose = [L(f"The court considered point {i} of the argument at length.", 60 if i % 5 else 80) for i in range(600)]
        lines = prose + [L("ORDER", 250), L("The view of the majority is as follows."), L("S. M. Sikri C.J.")]
        result = split_paragraphs(lines)
        self.assertEqual(result.segments, 2)
        self.assertGreater(sum(1 for p in result.paragraphs if p.segment == 0), 50)
        self.assertIn("point 0 of the argument", result.paragraphs[0].text)

    def test_two_list_items_far_apart_are_not_paragraph_numbering(self):
        prose = [L(f"Sentence {i} of the report.", 60) for i in range(900)]
        for at, n in ((300, 1), (302, 2), (304, 3), (880, 4), (882, 5), (884, 6)):
            prose[at] = L(f"{n}. A conclusion drawn by one of the judges.", 60)
        self.assertEqual(split_paragraphs(prose).numbering, "synthetic")


class Cache(unittest.TestCase):
    def test_second_read_comes_from_the_cache_and_matches(self):
        from tests.sample_pdf import build_sample_pdf
        with tempfile.TemporaryDirectory() as tmp:
            pdf = build_sample_pdf(Path(tmp) / "s.pdf")
            first = extract_pdf(pdf, backend="pdfplumber", cache_dir=Path(tmp) / "cache")
            self.assertEqual(len(list((Path(tmp) / "cache").iterdir())), 1)
            second = extract_pdf(pdf, backend="pdfplumber", cache_dir=Path(tmp) / "cache")
            self.assertEqual([(p.lines, p.x0, p.footnotes) for p in first.pages],
                             [(p.lines, p.x0, p.footnotes) for p in second.pages])
            self.assertEqual(first.sha256, second.sha256)


class PageModel(unittest.TestCase):
    """A PDF with a signature stamp, a footnote and a superscript marker."""

    @classmethod
    def setUpClass(cls):
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name) / "layout.pdf"
        c = canvas.Canvas(str(path), pagesize=A4)
        for page in (1, 2, 3):
            y = 760
            c.setFont("Helvetica", 11); c.drawCentredString(297, 800, str(page))
            for n in range(1, 9):
                number = (page - 1) * 8 + n
                c.setFont("Helvetica", 14)
                text = f"{number}. The court held that the rule applies in this case"
                c.drawString(72, y, text)
                if n == 2:                                   # superscript footnote marker
                    c.setFont("Helvetica", 9); c.drawString(72 + c.stringWidth(text, "Helvetica", 14) + 1, y + 5, "1")
                c.setFont("Helvetica", 14); c.drawString(72, y - 20, f"and gave reason {number} at some length for doing so.")
                y -= 60
            c.setFont("Helvetica", 6.5); c.drawString(72, 62, "1")
            c.setFont("Helvetica", 10); c.drawString(78, 58, "(2005) 6 SCC 1 : AIR 2005 SC 3180")
            if page == 1:
                c.setFont("Helvetica", 4.7)
                for i, stamp in enumerate(["Signature Not Verified", "Digitally signed by", "A CLERK", "Date: 2018.09.27", "Reason:"]):
                    c.drawString(40, 140 - 7 * i, stamp)
            c.showPage()
        c.save()
        cls.extraction = extract_pdf(path, backend="pdfplumber")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_stamp_page_numbers_and_markers_are_gone_from_the_body(self):
        text = self.extraction.text
        for junk in ["Signature", "Digitally", "CLERK", "Reason"]:
            self.assertNotIn(junk, text)
        self.assertIn("2. The court held that the rule applies in this case", self.extraction.pages[0].lines)
        self.assertNotIn("case1", text.replace(" ", ""))

    def test_footnotes_are_kept_apart_with_their_citations(self):
        self.assertEqual(self.extraction.pages[0].footnotes, ["1 (2005) 6 SCC 1 : AIR 2005 SC 3180"])
        self.assertNotIn("SCC", self.extraction.text)

    def test_lines_carry_their_left_edge(self):
        self.assertTrue(all(l.x0 is not None and abs(l.x0 - 72) < 2 for l in self.extraction.lines))

    def test_footnote_citations_reach_the_paragraph_and_its_chunk(self):
        import datetime as dt
        from legal_lens.config import ROOT
        from legal_lens.manifest import CaseMeta
        from legal_lens.mapping import Mapping
        from legal_lens.pipeline import build_case
        meta = CaseMeta("layout", "A v. B", dt.date(2018, 9, 27), "Supreme Court", ["(2018) 1 SCC 1"], [], "f", summary="x")
        path = Path(self.tmp.name) / "layout.pdf"
        built = build_case(meta, path, Mapping.from_dir(ROOT / "data" / "mappings"), backend="pdfplumber")
        self.assertEqual(built.split.count, 24)
        cited = {k for r in built.case_refs for k in r["citation_keys"]}
        self.assertIn("2005-6-scc-1", cited)
        with_note = [p for p in built.payloads if p["footnotes"]]
        self.assertTrue(with_note)
        self.assertIn("2005-6-scc-1", with_note[0]["cited_cases"])
        self.assertNotIn("SCC", with_note[0]["text"])                    # the passage itself stays clean


class Tidying(unittest.TestCase):
    def test_glyphs_and_judis_escapes(self):
        self.assertEqual(_tidy("―quoted‖ and ‗single‘"), "“quoted” and ‘single‘")
        self.assertEqual(_tidy("complainant):\\026 \\005\\005\\005the death"), "complainant):– …the death")

    def test_footnote_lines_are_joined_to_their_marker(self):
        self.assertEqual(_footnotes(["2", "1954 SCR 930 : AIR 1954 SC 321", "3", "(1988)2 SCC 72", "continued here"]),
                         ["2 1954 SCR 930 : AIR 1954 SC 321", "3 (1988)2 SCC 72 continued here"])


class LongSentences(unittest.TestCase):
    def test_a_sentence_longer_than_the_limit_is_still_cut(self):
        from legal_lens.paragraphs import Paragraph
        clause = "whoever does any act with the intention of causing such harm to any person, "
        text = "7. " + clause * 60                               # one 'sentence', far over the limit
        chunks = chunk_paragraphs("c", [Paragraph(0, 7, text, 1)], max_tokens=200)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(c.tokens <= 200 for c in chunks))


class StatuteAttribution(unittest.TestCase):
    """Both of these were found in Joseph Shine v. Union of India."""

    def test_number_after_a_full_stop_belongs_to_the_act_after_it(self):
        from legal_lens.citations import find_statute_refs
        text = ("so as to invite the culpability of Section 497 IPC. Section 198 CrPC deals "
                "with a person aggrieved. Sub-section (2) of Section 198 treats the husband "
                "as aggrieved by an offence under Section 497 IPC.")
        ids = [r.statute_id for r in find_statute_refs(text)]
        self.assertEqual(ids, ["IPC:497", "CrPC:198", "IPC:497"])

    def test_a_foreign_penal_code_is_not_the_ipc(self):
        from legal_lens.citations import find_statute_refs
        foreign = ("was called upon to rule on the constitutionality of Section 154 of the "
                   "Penal Code, on the grounds that it violated the Ugandan Constitution")
        self.assertEqual(find_statute_refs(foreign), [])
        home = "convicted under Section 302 of the Penal Code, 1860 and IPC Section 34"
        self.assertEqual([r.statute_id for r in find_statute_refs(home)], ["IPC:302", "IPC:34"])


if __name__ == "__main__":
    unittest.main()
