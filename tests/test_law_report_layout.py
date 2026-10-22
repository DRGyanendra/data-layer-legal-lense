"""Law-report copies as the newer Supreme Court Reports volumes print them:
judge headers in square brackets, paragraph first lines indented differently
on facing pages, margin letters inside the text, and a concurring opinion
whose first paragraph number shares the line with the judge's name."""

import unittest

from legal_lens.paragraphs import Line, split_paragraphs

SENTENCE = "The sentence that follows is ordinary text of the judgment and runs on for a while."


def report(missing=()):
    """A 12-paragraph opinion by Misra, then a 4-paragraph concurrence by Nariman."""
    lines = [Line(1, "JOSEPH SHINE", 250, False), Line(1, "v.", 290, False), Line(1, "UNION OF INDIA", 230, False),
             Line(1, "SEPTEMBER 27, 2018", 250, False),
             Line(1, "[DIPAK MISRA, CJI, R. F. NARIMAN AND D. Y. CHANDRACHUD, JJ.]", 150, False),
             Line(1, "Penal Code, 1860 - s.497 - Adultery - Headnote written by the reporter.", 90, False),
             Line(1, "HELD: the reporter's summary of the holding.", 90, False),
             Line(2, "The Judgments of the Court were delivered by", 170, False),
             Line(2, "DIPAK MISRA, CJI (For himself and A.M. Khanwilkar, J.)", 170, False)]
    page = 2
    for n in range(1, 13):
        if n in missing:
            continue
        if n % 3 == 0:
            page += 1
            header = f"{700 + page} SUPREME COURT REPORTS [2018] 11 S.C.R." if page % 2 == 0 else f"JOSEPH SHINE v. UNION OF INDIA [DIPAK MISRA, CJI] {700 + page}"
            lines.append(Line(page, header, 72, False))
        x0 = 193.3 if page % 2 else 179.2                       # facing pages indent differently
        first = f"{n}. Paragraph {n} of the first opinion begins with its number."
        if n == 7:
            first = "B " + first                                 # a margin letter in front of the number
        lines.append(Line(page, first, x0, False))
        lines.append(Line(page, SENTENCE + (" E" if n == 4 else ""), 151.3, False))   # ...and one at a line end
        lines.append(Line(page, "It ends here.", 151.3, False))
    page += 1
    lines.append(Line(page, f"JOSEPH SHINE v. UNION OF INDIA [R. F. NARIMAN, J.] {700 + page}", 72, False))
    lines.append(Line(page, "R. F. NARIMAN, J. (Concurring) 1. What is before us in this writ petition is the", 193.3, False))
    lines.append(Line(page, "constitutional validity of an archaic provision.", 151.3, False))
    for n in range(2, 5):
        lines.append(Line(page, f"{n}. Paragraph {n} of the concurrence.", 179.2, False))
        lines.append(Line(page, SENTENCE, 151.3, False))
    page += 1
    lines.append(Line(page, f"{700 + page} SUPREME COURT REPORTS [2018] 11 S.C.R.", 72, False))
    lines.append(Line(page, "The writ petition is allowed.", 151.3, False))
    return lines


class NewVolumeLayout(unittest.TestCase):
    def test_square_bracket_headers_split_the_opinions_and_are_not_text(self):
        split = split_paragraphs(report())
        self.assertEqual(split.layout, "law_report")
        self.assertEqual([o.author_line for o in split.opinions],
                         ["DIPAK MISRA, CJI (For himself and A.M. Khanwilkar, J.)", "R. F. NARIMAN, J. (Concurring)"])
        text = " ".join(p.text for p in split.paragraphs)
        self.assertNotIn("S.C.R.", text)
        self.assertNotIn("[R. F. NARIMAN, J.]", text)

    def test_numbers_chain_across_facing_pages_and_margin_letters(self):
        split = split_paragraphs(report())
        self.assertEqual(split.numbering, "original")
        self.assertEqual([o.paragraphs for o in split.opinions], [12, 4])
        seven = next(p for p in split.paragraphs if p.segment == 0 and p.number == 7)
        self.assertTrue(seven.text.startswith("7. Paragraph 7"), seven.text[:40])
        four = next(p for p in split.paragraphs if p.segment == 0 and p.number == 4)
        self.assertNotRegex(four.text, r"\bE$|\bE It ends")
        self.assertNotIn(" B Paragraph", " ".join(p.text for p in split.paragraphs))

    def test_the_concurrence_opens_on_the_judges_line(self):
        split = split_paragraphs(report())
        first = next(p for p in split.paragraphs if p.segment == 1 and p.number == 1)
        self.assertTrue(first.text.startswith("1. What is before us"), first.text[:40])
        self.assertEqual(split.opinions[1].numbering, "original")

    def test_two_lost_numbers_in_a_row_do_not_break_the_chain(self):
        split = split_paragraphs(report(missing=(5, 7)))
        self.assertEqual(split.numbering, "original")
        numbers = [p.number for p in split.paragraphs if p.segment == 0]
        self.assertEqual(numbers, [1, 2, 3, 4, 6, 8, 9, 10, 11, 12])


class OrderCopy(unittest.TestCase):
    def test_the_headnote_of_an_order_is_not_text(self):
        lines = [Line(1, "[2022] 9 S.C.R. 293", 250, False), Line(1, "FUTURE COUPONS", 250, False), Line(1, "v.", 290, False),
                 Line(1, "AMAZON.COM NV INVESTMENT HOLDINGS LLC", 230, False), Line(1, "FEBRUARY 15, 2022", 250, False),
                 Line(1, "[N. V. RAMANA, CJI, A. S. BOPANNA AND HIMA KOHLI, JJ.]", 150, False)]
        for _ in range(12):
            lines.append(Line(1, "Companies Act, 2013 - headnote written by the reporter about the order.", 90, False))
        lines += [Line(2, "294 SUPREME COURT REPORTS [2022] 9 S.C.R.", 72, False),
                  Line(2, "Ms. Manisha Singh, Advs. for the appearing parties.", 90, False),
                  Line(2, "The following Order of the Court was passed:", 150, False), Line(2, "O R D E R", 280, False)]
        page = 2
        for n in range(1, 9):
            if n % 2 == 1 and n > 1:
                page += 1
                header = f"{292 + page} SUPREME COURT REPORTS [2022] 9 S.C.R." if page % 2 == 0 else f"FUTURE COUPONS v. AMAZON [N. V. RAMANA, CJI] {292 + page}"
                lines.append(Line(page, header, 72, False))
            lines.append(Line(page, f"{n}. Paragraph {n} of the order.", 179.2, False))
            lines.append(Line(page, SENTENCE, 151.3, False))
        split = split_paragraphs(lines)
        self.assertEqual((split.layout, split.numbering, split.count), ("law_report", "original", 8))
        text = " ".join(p.text for p in split.paragraphs)
        self.assertNotIn("headnote written", text)
        self.assertNotIn("O R D E R", text)
        self.assertIn("headnote written", split.header)


if __name__ == "__main__":
    unittest.main()
