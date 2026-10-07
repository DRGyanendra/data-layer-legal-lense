"""Layouts beyond the court's own modern PDF: other numbering styles, opinions
with no 'JUDGMENT' heading, law reports, two columns, and judgments that have
no entry in data/cases.yaml. The text is invented.
"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from legal_lens.autometa import clean_judge, read_meta, same_judge, title_case
from legal_lens.mapping import Mapping
from legal_lens.paragraphs import Line, opinion_breaks, split_paragraphs
from legal_lens.pipeline import build_case

ROOT = Path(__file__).resolve().parent.parent
SENTENCE = "The court below recorded a finding on this point and we see no reason to differ from it."


def L(text, x0=72.0, page=1, gap=False):
    return Line(page, text, x0, gap)


def numbered(first, last, style="{n}.", page=1):
    out = []
    for n in range(first, last + 1):
        out.append(L(f"{style.format(n=n)} Paragraph {n} states a proposition of law in one sentence.", page=page))
        out.append(L("It runs on to a second line and ends here.", page=page))
    return out


def title():
    return [L("IN THE SUPREME COURT OF INDIA", 170), L("CIVIL APPEAL NO. 77 OF 2010", 150),
            L("RAM LAL & ORS. ... Appellants", 100), L("VERSUS", 280), L("STATE OF U.P. ... Respondent", 100),
            L("J U D G M E N T", 250)]


def signed(*names):
    out = []
    for name in names:
        out += [L("……………………..J.", 300), L(f"({name})", 300)]
    return out + [L("New Delhi;", 72), L("March 14, 2019.", 72)]


class NumberingStyles(unittest.TestCase):
    def test_square_brackets_and_round_brackets(self):
        for style in ("[{n}]", "({n})", "Para {n}."):
            split = split_paragraphs(title() + [L("A. SEN, J.")] + numbered(1, 12, style))
            self.assertEqual((split.count, split.numbering), (12, "original"), style)
            self.assertEqual([p.number for p in split.paragraphs], list(range(1, 13)), style)

    def test_a_bracketed_list_in_an_unnumbered_judgment_is_not_paragraph_numbering(self):
        # sub-sections (1) to (4) quoted in the middle of 300 lines of unnumbered text
        lines = title() + [L("A. SEN, J.")]
        for block in range(30):
            lines.append(L("An unnumbered paragraph begins here and carries on.", 108))
            lines += [L("It continues at the margin for several lines of text.") for _ in range(8)]
            lines.append(L("It ends with a full stop."))
            if block in (5, 12, 19, 26):
                lines.append(L(f"({(5, 12, 19, 26).index(block) + 1}) a sub-section quoted from the statute;"))
        split = split_paragraphs(lines)
        self.assertEqual(split.numbering, "synthetic")
        self.assertGreaterEqual(split.count, 25)


class OpinionsWithoutAHeading(unittest.TestCase):
    def test_a_dissent_that_carries_on_the_numbering_is_its_own_opinion(self):
        lines = title() + [L("A. SEN, J.")] + numbered(1, 15) + [L("M. DAS, J. (dissenting)")] + numbered(16, 27)
        split = split_paragraphs(lines)
        self.assertEqual(split.segments, 2)
        self.assertEqual([o.author_line for o in split.opinions], ["A. SEN, J.", "M. DAS, J. (dissenting)"])
        self.assertEqual([o.paragraphs for o in split.opinions], [15, 12])
        second = [p for p in split.paragraphs if p.segment == 1]
        self.assertEqual((second[0].number, second[-1].number), (16, 27))     # the court's numbers are kept
        self.assertNotIn("M. DAS", " ".join(p.text for p in split.paragraphs))

    def test_a_judges_name_in_running_text_is_not_a_new_opinion(self):
        lines = title() + [L("A. SEN, J.")] + numbered(1, 6)
        lines += [L("7. The point was put shortly by the learned judge in that case, namely"),
                  L("Bachawat, J."),                      # a wrapped line, mid-sentence
                  L("who held that the power is plenary. We agree.")] + numbered(8, 14)
        self.assertEqual(opinion_breaks(lines), [])
        self.assertEqual(split_paragraphs(lines).segments, 1)

    def test_the_closing_signature_is_not_a_new_opinion(self):
        lines = title() + [L("A. SEN, J.")] + numbered(1, 12) + [L("A. SEN, J."), L("New Delhi;"), L("March 14, 2019.")]
        self.assertEqual(split_paragraphs(lines).segments, 1)

    def test_law_report_opening_names_the_judge(self):
        lines = [L("PETITIONER:"), L("RAM LAL"), L("RESPONDENT:"), L("STATE OF U.P."), L("DATE OF JUDGMENT: 12/03/1979"),
                 L("BENCH:"), L("KRISHNAIYER, V.R."), L("BHAGWATI, P.N."), L("JUDGMENT:"),
                 L("The Judgment of the Court was delivered by"),
                 L("KRISHNA IYER, J.-This appeal by special leave raises a short point.", 108)]
        for _ in range(9):
            lines += [L(SENTENCE), L("A second sentence closes the paragraph."), L("A new paragraph starts with an indent.", 108)]
        lines.append(L("The appeal is dismissed."))
        lines += [L("BHAGWATI, J. (dissenting)-I regret my inability to agree with my learned brother.", 108)]
        for _ in range(6):
            lines += [L(SENTENCE), L("A second sentence closes the paragraph."), L("A new paragraph starts with an indent.", 108)]
        lines.append(L("I would allow the appeal."))
        split = split_paragraphs(lines)
        self.assertEqual([o.author_line for o in split.opinions], ["KRISHNA IYER, J.", "BHAGWATI, J. (dissenting)"])
        text = " ".join(p.text for p in split.paragraphs)
        self.assertNotIn("delivered by", text)
        self.assertTrue(split.paragraphs[0].text.startswith("This appeal by special leave"))
        first_of_dissent = next(p for p in split.paragraphs if p.segment == 1)
        self.assertTrue(first_of_dissent.text.startswith("I regret my inability"))

        meta = read_meta("ram_lal.pdf", lines, split)
        self.assertEqual((meta.name, meta.date, meta.layout), ("Ram Lal v. State of U.P.", dt.date(1979, 3, 12), "judis"))
        self.assertEqual(meta.bench, ["V.R. Krishnaiyer", "P.N. Bhagwati"])
        self.assertEqual([(o.author, o.type) for o in meta.opinions],
                         [("V.R. Krishnaiyer", "unknown"), ("P.N. Bhagwati", "dissenting")])


class UnnumberedParagraphs(unittest.TestCase):
    def test_blank_space_between_paragraphs_is_used_when_there_are_no_indents(self):
        lines = title() + [L("A. SEN, J.")]
        for n in range(10):
            lines.append(L(f"Block paragraph {n} opens at the margin.", gap=True))
            lines += [L("It continues at the same margin."), L("It ends with a full stop.")]
        split = split_paragraphs(lines)
        self.assertEqual((split.count, split.numbering), (10, "synthetic"))


class LawReports(unittest.TestCase):
    def setUp(self):
        lines = [L("RAM LAL", 250), L("v.", 290), L("STATE OF PUNJAB", 230), L("March 14, 1962", 250),
                 L("(B. P. SINHA, C.J., K. SUBBA RAO AND J. C. SHAH, JJ.)", 150),
                 L("Criminal law-Negligence-Standard of care-Headnote written by the reporter.", 90),
                 L("HELD: the reporter's summary of the holding, which the court did not write.", 90)]
        lines += [L("CRIMINAL APPELLATE JURISDICTION : Criminal Appeal No. 12 of 1960."),
                  L("Appeal by special leave from the judgment of the Punjab High Court."),
                  L("The Judgment of Sinha, C.J. and Subba Rao, J. was delivered by Subba Rao, J. Shah, J. delivered a separate judgment."),
                  L("SUBBA RAO, J.-This appeal raises a question of negligence.", 90)]
        for page in range(2, 8):
            header = f"{300 + page} SUPREME COURT REPORTS [1962] 3 S.C.R." if page % 2 == 0 else f"RAM LAL v. STATE (Subba Rao, J.) {300 + page}"
            lines.append(L(header, 72, page))
            for _ in range(5):
                lines += [L("A paragraph of the first opinion starts here.", 90, page), L(SENTENCE, 72, page)]
            lines.append(L("B", 520, page))                       # a margin letter
        lines.append(L("Appeal dismissed.", 90, 7))
        lines.append(L("SHAH, J.-I have the misfortune to differ.", 90, 7))
        for page in range(8, 12):
            header = f"{300 + page} SUPREME COURT REPORTS [1962] 3 S.C.R." if page % 2 == 0 else f"RAM LAL v. STATE (Shah, J.) {300 + page}"
            lines.append(L(header, 72, page))
            for _ in range(5):
                lines += [L("A paragraph of the second opinion starts here.", 90, page), L(SENTENCE, 72, page)]
        self.lines = lines
        self.split = split_paragraphs(lines)
        self.text = " ".join(p.text for p in self.split.paragraphs)

    def test_recognised_and_the_headnote_is_left_out(self):
        self.assertEqual(self.split.layout, "law_report")
        self.assertNotIn("Headnote written by the reporter", self.text)
        self.assertNotIn("HELD:", self.text)
        self.assertIn("Headnote written by the reporter", self.split.header)

    def test_page_headers_and_margin_letters_are_removed(self):
        self.assertNotIn("SUPREME COURT REPORTS", self.text)
        self.assertNotIn("RAM LAL v. STATE", self.text)
        self.assertNotRegex(self.text, r" B A paragraph")

    def test_each_judge_gets_an_opinion(self):
        self.assertEqual([o.author_line for o in self.split.opinions], ["SUBBA RAO, J.", "Shah, J."])
        second = [p for p in self.split.paragraphs if p.segment == 1]
        self.assertTrue(second[0].text.startswith("I have the misfortune to differ"))
        self.assertTrue(all("second opinion" in p.text or "misfortune" in p.text for p in second))

    def test_details_come_from_the_report_header(self):
        meta = read_meta("ram_lal.pdf", self.lines, self.split)
        self.assertEqual((meta.name, meta.date, meta.court), ("Ram Lal v. State of Punjab", dt.date(1962, 3, 14), "Supreme Court"))
        self.assertEqual(meta.bench, ["B. P. Sinha", "K. Subba Rao", "J. C. Shah"])
        self.assertEqual([o.author for o in meta.opinions], ["K. Subba Rao", "J. C. Shah"])

    def test_an_ordinary_judgment_is_not_taken_for_a_report(self):
        split = split_paragraphs(title() + [L("A. SEN, J.")] + numbered(1, 12))
        self.assertEqual(split.layout, "judgment")


class DetailsFromThePdf(unittest.TestCase):
    def test_the_courts_own_layout(self):
        lines = [L("REPORTABLE", 400), L("2018 INSC 790", 400), L("IN THE SUPREME COURT OF INDIA", 170),
                 L("CRIMINAL ORIGINAL JURISDICTION", 170), L("WRIT PETITION (CRIMINAL) NO. 76 OF 2016", 150),
                 L("NAVTEJ SINGH JOHAR & ORS. …Petitioner(s)", 100), L("VERSUS", 280), L("UNION OF INDIA", 100),
                 L("THR. SECRETARY", 100), L("MINISTRY OF LAW AND JUSTICE …Respondent(s)", 100), L("WITH", 280),
                 L("WRIT PETITION (CIVIL) NO. 572 OF 2016", 150), L("J U D G M E N T", 250),
                 L("Dipak Misra, CJI (for himself and A.M. Khanwilkar, J.)")] + numbered(1, 8) + signed("Dipak Misra", "A.M. Khanwilkar")
        lines += [L("IN THE SUPREME COURT OF INDIA", 170), L("J U D G M E N T", 250), L("R.F. Nariman, J. (Concurring)")]
        lines += numbered(1, 6) + signed("R.F. Nariman")
        lines += [L("IN THE SUPREME COURT OF INDIA", 170), L("J U D G M E N T", 250), L("INDU MALHOTRA, J.")]
        lines += numbered(1, 6) + signed("INDU MALHOTRA")
        split = split_paragraphs(lines)
        meta = read_meta("some_file.pdf", lines, split)
        self.assertEqual(meta.name, "Navtej Singh Johar v. Union of India")
        self.assertEqual((meta.case_id, meta.date, meta.court), ("navtej_singh_johar_2019", dt.date(2019, 3, 14), "Supreme Court"))
        self.assertEqual(meta.citations, ["2018 INSC 790"])
        self.assertEqual(meta.case_number, "WRIT PETITION (CRIMINAL) NO. 76 OF 2016")
        self.assertEqual(meta.bench, ["Dipak Misra", "A.M. Khanwilkar", "R.F. Nariman", "Indu Malhotra"])
        self.assertEqual([(o.author, o.type, o.joined_by) for o in meta.opinions],
                         [("Dipak Misra", "unknown", ["A.M. Khanwilkar"]), ("R.F. Nariman", "concurring", []),
                          ("Indu Malhotra", "unknown", [])])
        self.assertEqual((meta.source, meta.missing), ("auto", []))

    def test_one_opinion_is_the_judgment_of_the_court(self):
        lines = title() + [L("A. SEN, J.")] + numbered(1, 8) + signed("A. Sen", "M. Das", "R. Pal")
        meta = read_meta("x.pdf", lines, split_paragraphs(lines))
        self.assertEqual([(o.author, o.type) for o in meta.opinions], [("A. Sen", "majority")])
        self.assertEqual(meta.name, "Ram Lal v. State of U.P.")

    def test_an_opinion_joined_by_most_of_the_bench_is_the_majority(self):
        lines = title() + [L("A. SEN, J. (for himself, M. DAS and R. PAL, JJ.)")] + numbered(1, 8) + signed("A. Sen", "M. Das", "R. Pal")
        lines += [L("K. ROY, J. (dissenting)")] + numbered(9, 16) + signed("K. Roy")
        meta = read_meta("x.pdf", lines, split_paragraphs(lines))
        self.assertEqual([(o.author, o.type) for o in meta.opinions], [("A. Sen", "majority"), ("K. Roy", "dissenting")])

    def test_a_date_in_the_story_is_not_the_date_of_the_judgment(self):
        lines = title() + [L("A. SEN, J.")] + numbered(1, 4)
        for _ in range(3):                                # a cut-off date quoted on a line of its own, three times
            lines += [L("5. The amendment applies to partitions effected before"), L("20.12.2004"), L("and to none after it.")]
        lines += numbered(6, 9) + signed("A. Sen")
        meta = read_meta("x.pdf", lines, split_paragraphs(lines))
        self.assertEqual(meta.date, dt.date(2019, 3, 14))

    def test_indian_kanoon_printout(self):
        lines = [L("Jacob Mathew vs State Of Punjab & Anr on 5 August, 2005"),
                 L("Equivalent citations: AIR 2005 SC 3180, (2005) 6 SCC 1"), L("Author: R Lahoti"),
                 L("Bench: R.C. Lahoti, G.P. Mathur, P.K. Balasubramanyan"), L("JUDGMENT"), L("R.C. Lahoti, CJI")] + numbered(1, 8)
        meta = read_meta("doc.pdf", lines, split_paragraphs(lines))
        self.assertEqual((meta.name, meta.date, meta.layout), ("Jacob Mathew v. State of Punjab", dt.date(2005, 8, 5), "indian_kanoon"))
        self.assertEqual(meta.citations, ["AIR 2005 SC 3180", "(2005) 6 SCC 1"])
        self.assertEqual(meta.bench, ["R.C. Lahoti", "G.P. Mathur", "P.K. Balasubramanyan"])

    def test_names(self):
        self.assertEqual(title_case("JUSTICE K.S. PUTTASWAMY (RETD.)"), "Justice K.S. Puttaswamy (Retd.)")
        self.assertEqual(clean_judge("HON'BLE MR. JUSTICE A.K. VERMA"), "A.K. Verma")
        self.assertEqual(clean_judge("Dr Dhananjaya Y Chandrachud, J"), "Dhananjaya Y Chandrachud")
        self.assertEqual(clean_judge("Dipak Misra, CJI (For himself and A.M. Khanwilkar, J.)"), "Dipak Misra")
        self.assertTrue(same_judge("Palekar", "D. G. Palek. Ar"))
        self.assertTrue(same_judge("S.K. Kaul", "Sanjay Kishan Kaul"))
        self.assertFalse(same_judge("A.N. Ray", "A.N. Grover"))


def _pdf(path, header, paragraphs, *, columns=1, footer=None):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    width, height = A4
    margin = 60
    col_width = (width - 2 * margin - (24 if columns == 2 else 0)) / columns
    c = canvas.Canvas(str(path), pagesize=A4)
    state = {"y": height - 80, "col": 0, "page": 1}

    def furniture():
        if footer:
            c.setFont("Helvetica", 9)
            c.drawString(margin, 40, footer(state["page"]))
        c.setFont("Helvetica", 11)

    def put(text):
        c.drawString(margin + state["col"] * (col_width + 24), state["y"], text)
        state["y"] -= 15
        if state["y"] < 80:
            if state["col"] + 1 < columns:
                state["col"] += 1
            else:
                c.showPage()
                state["page"] += 1
                state["col"] = 0
                furniture()
            state["y"] = height - 80

    furniture()
    for line in header:
        put(line)
    for paragraph in paragraphs:
        current = ""
        for word in paragraph.split():
            if stringWidth((current + " " + word).strip(), "Helvetica", 11) <= col_width:
                current = (current + " " + word).strip()
            else:
                put(current)
                current = word
        put(current)
        put("")
    c.save()
    return path


class WholePdf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.mapping = Mapping.from_dir(ROOT / "data" / "mappings")
        cls.header = ["IN THE SUPREME COURT OF INDIA", "CRIMINAL APPEAL NO. 12 OF 2019", "RAM LAL ... Appellant", "VERSUS",
                      "STATE OF PUNJAB ... Respondent", "J U D G M E N T", "A. SEN, J."]
        cls.paragraphs = [f"{n}. " + " ".join([f"Sentence {s} of paragraph {n} says that the finding of the court below was right."
                                               for s in range(1, 6)]) for n in range(1, 31)]
        cls.paragraphs.append("New Delhi; March 14, 2019.")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_two_columns_are_read_one_after_the_other(self):
        pdf = _pdf(Path(self.tmp.name) / "columns.pdf", self.header, self.paragraphs, columns=2)
        built = build_case(None, pdf, self.mapping)
        self.assertEqual(built.split.count, 30)
        self.assertEqual([p.number for p in built.split.paragraphs], list(range(1, 31)))
        self.assertTrue(all(p.columns == 2 for p in built.extraction.pages[:1]))
        for p in built.split.paragraphs:                      # no line from the other column inside a paragraph
            self.assertEqual(set(int(n) for n in __import__("re").findall(r"of paragraph (\d+)", p.text)), {p.number})
        self.assertIn("two columns", " ".join(reason for _, reason in built.verdict.reasons))

    def test_a_judgment_with_no_manifest_entry_is_built_and_passes(self):
        pdf = _pdf(Path(self.tmp.name) / "plain.pdf", self.header, self.paragraphs,
                   footer=lambda n: f"Indian Kanoon - http://indiankanoon.org/doc/871062/ {n}")
        built = build_case(None, pdf, self.mapping)
        self.assertEqual((built.meta.name, built.meta.case_id, built.meta.source),
                         ("Ram Lal v. State of Punjab", "ram_lal_2019", "auto"))
        self.assertEqual(built.verdict.status, "ok", built.verdict.lines())
        payload = built.payloads[0]
        self.assertTrue(payload["cite_as"].startswith("Ram Lal v. State of Punjab, decided 14 March 2019, para"), payload["cite_as"])
        self.assertEqual((payload["parse_quality"], payload["meta_source"], payload["opinion_type"]), ("ok", "auto", "majority"))
        self.assertNotIn("indiankanoon", " ".join(p["text"] for p in built.payloads))

    def test_a_document_that_is_not_a_judgment_is_rejected(self):
        pdf = _pdf(Path(self.tmp.name) / "article.pdf", ["Access to Justice in Rural Districts"],
                   ["A report paragraph with several sentences in it. " * 6 for _ in range(12)])
        built = build_case(None, pdf, self.mapping)
        self.assertEqual(built.verdict.status, "reject")
        self.assertIn("does not look like a judgment", " ".join(reason for _, reason in built.verdict.reasons))

    def test_scan_reports_every_file_in_a_folder(self):
        import contextlib
        import csv
        import io

        from legal_lens.cli import main

        folder = Path(self.tmp.name) / "scan"
        folder.mkdir()
        _pdf(folder / "good.pdf", self.header, self.paragraphs)
        _pdf(folder / "article.pdf", ["A Report"], ["A report paragraph with several sentences in it. " * 6 for _ in range(12)])
        out = folder / "report.csv"
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["scan", str(folder), "--out", str(out), "--no-cache"])
        rows = {r["file"]: r for r in csv.DictReader(out.open())}
        self.assertEqual((rows["good.pdf"]["verdict"], rows["article.pdf"]["verdict"], code), ("ok", "reject", 1))
        self.assertEqual(rows["good.pdf"]["name"], "Ram Lal v. State of Punjab")
        self.assertTrue((folder / "cases.draft.yaml").exists())


if __name__ == "__main__":
    unittest.main()
