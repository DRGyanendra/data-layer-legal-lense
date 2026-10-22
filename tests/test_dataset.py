"""The validation-sample builder: reading the AWS metadata rows and balancing the pick."""

import unittest
from collections import Counter
from pathlib import Path

from legal_lens.dataset import ERAS, Judgment, _quota, judgment_from_row, select_sample

RAW_HTML = (
    "<select>...</select><button ...><font size='4'> <strong>JACOB MATHEW <span class='fst-italic'> versus "
    "</span>STATE OF PUNJAB AND ANR. </strong>- <span class='escrText'>[2005] SUPP. 2 S.C.R. 307</span>"
    "<span class='ncDisplay'>2005 INSC 384</span></button></font><br><strong>Coram : R.C. LAHOTI"
    "<sup style=\"color: #268e97;\" class=\"tooltip-sup\" data-tooltip=\"Author\">*</sup>, G.P. MATHUR, "
    "P.K. BALASUBRAMANYAN</strong><br> Indian Penal Code, 1860: s. 304A. Criminal negligence by a doctor-"
    "Held, negligence must be gross.<br><strong class='caseDetailsTD' ><span> Decision Date :</span>"
    "<font color='green'> 05-08-2005</font><span> | Case No :</span><font color='green'> "
    "CRIMINAL APPEAL No. 144/2004</font><span> | Disposal Nature :</span><font color='green'> "
    "Disposed off</font>   <span> |  Bench :</span><font color='green'> 3 Judges</font></strong>"
)


def row(**over):
    base = dict(title="JACOB MATHEW versus STATE OF PUNJAB AND ANR.", petitioner="JACOB MATHEW",
                respondent="STATE OF PUNJAB AND ANR.", citation="[2005] SUPP. 2 S.C.R. 307",
                case_id="2005 INSC 384", decision_date="05-08-2005", disposal_nature="Disposed off",
                raw_html=RAW_HTML, path="S_2005_2_307_341", year="2005", available_languages="ENG,HIN")
    base.update(over)
    return base


class ReadRow(unittest.TestCase):
    def test_fields_hidden_in_the_html_are_read(self):
        j = judgment_from_row(row())
        self.assertEqual(j.bench, ["R.C. LAHOTI", "G.P. MATHUR", "P.K. BALASUBRAMANYAN"])
        self.assertEqual(j.author, "R.C. LAHOTI")
        self.assertEqual(j.bench_size, 3)
        self.assertEqual(j.case_number, "CRIMINAL APPEAL No. 144/2004")
        self.assertTrue(j.criminal)
        self.assertIn("negligence must be gross", j.headnote)
        self.assertEqual(j.decision_date, "2005-08-05")
        self.assertEqual(j.pages, 35)                    # SCR pages 307 to 341
        self.assertEqual(j.era, "1990-2009")
        self.assertEqual(j.pdf_url.rsplit("/", 1)[1], "S_2005_2_307_341_EN.pdf")
        self.assertEqual(j.file_stem, "2005_jacob_mathew_v_state_of_punjab_S_2005_2_307_341")

    def test_row_without_coram_has_no_bench(self):
        j = judgment_from_row(row(raw_html="<p>nothing useful</p>"))
        self.assertEqual(j.bench, [])
        self.assertIsNone(j.bench_size)
        self.assertFalse(j.criminal)


class Balance(unittest.TestCase):
    def test_quota_adds_up(self):
        self.assertEqual(sum(_quota(100, {"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}).values()), 100)
        self.assertEqual(_quota(10, {"a": 0.2, "b": 0.8}), {"a": 2, "b": 8})
        self.assertEqual(_quota(10, {"a": 0.26, "b": 0.74}), {"a": 3, "b": 7})   # larger remainder wins

    def make(self, n, year, pages, criminal, bench=2):
        out = []
        for i in range(n):
            out.append(Judgment(
                path=f"{year}_{pages}_{criminal}_{bench}_{i}", year=year, title="A versus B", petitioner="A",
                respondent="B", citation="x", neutral_citation="y", decision_date=f"{year}-01-01",
                disposal="", bench=["J"] * bench, author="J", bench_size=bench,
                case_number="CRIMINAL APPEAL 1/1" if criminal else "CIVIL APPEAL 1/1",
                headnote="", pages=pages))
        return out

    def test_sample_is_spread_over_eras_lengths_and_subjects(self):
        pool = []
        for year in (1960, 1975, 1995, 2005, 2015, 2022):
            for pages, crim in ((3, True), (3, False), (20, True), (20, False), (60, True), (60, False)):
                pool += self.make(15, year, pages, crim)
            pool += self.make(4, year, 200, False, bench=7)
        sample = select_sample(pool, size=60, seed=1)
        self.assertEqual(len(sample), 60)
        self.assertEqual(len({j.path for j in sample}), 60)
        eras = Counter(j.era for j in sample)
        self.assertEqual(set(eras), set(ERAS))
        self.assertTrue(all(c == 20 for c in eras.values()), eras)
        self.assertEqual(Counter(j.length for j in sample).keys(), {"short", "medium", "long"})
        criminal = sum(j.criminal for j in sample)
        self.assertTrue(18 <= criminal <= 30, criminal)
        self.assertGreaterEqual(sum((j.bench_size or 0) >= 5 for j in sample), 6)

    def test_same_seed_same_sample_and_exclusions_hold(self):
        pool = self.make(50, 1960, 20, True) + self.make(50, 2000, 20, False) + self.make(50, 2020, 20, True)
        a = select_sample(pool, size=30, seed=7)
        b = select_sample(pool, size=30, seed=7)
        self.assertEqual([j.path for j in a], [j.path for j in b])
        excluded = {a[0].path}
        c = select_sample(pool, size=30, seed=7, exclude_paths=excluded)
        self.assertNotIn(a[0].path, {j.path for j in c})

    def test_rows_without_a_bench_are_not_picked(self):
        pool = self.make(30, 2020, 20, True)
        for j in pool[:10]:
            j.bench = []
        sample = select_sample(pool, size=20, seed=3)
        self.assertTrue(all(j.bench for j in sample))


class RecordAsCaseDetails(unittest.TestCase):
    """A PDF that `sample` downloaded has a record in sample.csv beside it."""

    def setUp(self):
        import csv, tempfile, datetime as dt
        from legal_lens.manifest import CaseMeta, Opinion
        from legal_lens.dataset import Judgment, write_sample_csv
        self.folder = Path(tempfile.mkdtemp())
        j = judgment_from_row(row())
        write_sample_csv([j], self.folder / "sample.csv")
        self.pdf = self.folder / (j.file_stem + ".pdf")
        self.pdf.write_bytes(b"%PDF-1.4")
        self.auto = CaseMeta(case_id="x", name="Jacob Mathew v. State of Punjab", date=dt.date(2005, 8, 5),
                             court="Supreme Court", citations=[], bench=["R.C. Lahoti"], file=str(self.pdf),
                             opinions=[Opinion("R.C. Lahoti", "majority")], source="auto", missing=["citation"],
                             layout="law_report")

    def test_the_record_supplies_what_the_pdf_could_not(self):
        from legal_lens.dataset import dataset_meta
        meta = dataset_meta(self.pdf, self.auto)
        self.assertEqual(meta.source, "dataset")
        self.assertEqual(meta.name, "Jacob Mathew v. State of Punjab")
        self.assertEqual(meta.citations, ["[2005] SUPP. 2 S.C.R. 307", "2005 INSC 384"])
        self.assertEqual(meta.bench, ["R.C. Lahoti", "G.P. Mathur", "P.K. Balasubramanyan"])
        self.assertEqual([(o.author, o.type) for o in meta.opinions], [("R.C. Lahoti", "majority")])
        self.assertEqual((meta.case_id, meta.case_number), ("jacob_mathew_2005", "CRIMINAL APPEAL No. 144/2004"))
        self.assertEqual((meta.missing, meta.conflicts), ([], []))
        self.assertEqual(meta.field_sources, {
            "parties": "pdf+dataset", "date": "pdf+dataset", "bench": "pdf+dataset",
            "citation": "dataset", "author": "pdf+dataset"})

    def test_a_disagreement_keeps_the_pdf_and_is_recorded(self):
        import datetime as dt
        from legal_lens.dataset import dataset_meta
        self.auto.date = dt.date(2005, 8, 15)
        self.auto.bench = ["R.C. Lahoti", "K. Roy"]
        self.auto.name = "Something Else v. Union of India"
        meta = dataset_meta(self.pdf, self.auto)
        self.assertEqual(len(meta.conflicts), 3, meta.conflicts)
        self.assertEqual(meta.date, dt.date(2005, 8, 15))           # the PDF's value is kept
        self.assertEqual(meta.name, "Something Else v. Union of India")
        self.assertEqual(meta.bench, ["R.C. Lahoti", "K. Roy"])
        for key in ("parties", "date", "bench"):
            self.assertEqual(meta.field_sources[key], "pdf, dataset differs")
        self.assertEqual(meta.case_id, "jacob_mathew_2005")         # the identifier stays the record's

    def test_the_record_fills_only_what_the_pdf_lacks(self):
        from legal_lens.dataset import dataset_meta
        self.auto.date, self.auto.name = None, "jacob_mathew_file"
        self.auto.missing = ["parties", "date", "citation"]
        meta = dataset_meta(self.pdf, self.auto)
        self.assertEqual(meta.name, "Jacob Mathew v. State of Punjab")
        self.assertEqual(meta.date.isoformat(), "2005-08-05")
        self.assertEqual((meta.field_sources["parties"], meta.field_sources["date"]), ("dataset", "dataset"))
        self.assertEqual((meta.missing, meta.conflicts), ([], []))

    def test_an_unreadable_pdf_bench_falls_back_to_the_record(self):
        from legal_lens.dataset import dataset_meta
        self.auto.bench = ["R.C. Lahoti", "il ,J"]                   # OCR debris taken for a judge
        meta = dataset_meta(self.pdf, self.auto)
        self.assertEqual(meta.bench, ["R.C. Lahoti", "G.P. Mathur", "P.K. Balasubramanyan"])
        self.assertEqual(meta.field_sources["bench"], "dataset")
        self.assertEqual(meta.conflicts, [])

    def test_the_pdf_citation_comes_first_and_the_record_adds_the_rest(self):
        from legal_lens.dataset import dataset_meta
        self.auto.citations, self.auto.missing = ["(2005) 6 SCC 1"], []
        meta = dataset_meta(self.pdf, self.auto)
        self.assertEqual(meta.citations, ["(2005) 6 SCC 1", "[2005] SUPP. 2 S.C.R. 307", "2005 INSC 384"])
        self.assertEqual(meta.field_sources["citation"], "pdf+dataset")

    def test_a_different_author_keeps_the_pdf_and_is_recorded(self):
        from legal_lens.dataset import dataset_meta
        from legal_lens.manifest import Opinion
        self.auto.bench = ["R.C. Lahoti", "G.P. Mathur", "P.K. Balasubramanyan"]
        self.auto.opinions = [Opinion("G.P. Mathur", "majority")]
        meta = dataset_meta(self.pdf, self.auto)
        self.assertEqual(meta.opinions[0].author, "G.P. Mathur")
        self.assertEqual(meta.field_sources["author"], "pdf, dataset differs")
        self.assertEqual(len(meta.conflicts), 1, meta.conflicts)

    def test_a_missing_author_is_taken_from_the_record(self):
        from legal_lens.dataset import dataset_meta
        from legal_lens.manifest import Opinion
        self.auto.opinions = [Opinion(None, "unknown")]
        meta = dataset_meta(self.pdf, self.auto)
        self.assertEqual((meta.opinions[0].author, meta.opinions[0].type), ("R.C. Lahoti", "majority"))
        self.assertEqual(meta.field_sources["author"], "dataset")

    def test_a_pdf_outside_a_sample_folder_has_no_record(self):
        from legal_lens.dataset import dataset_meta
        self.assertIsNone(dataset_meta(self.folder.parent / "elsewhere.pdf", self.auto))



class MatchIndicLegalQA(unittest.TestCase):
    """The three false matches found in the first 60-judgment set (7 Oct 2026):
    same decision date, one shared word that names nothing in particular."""

    def judgments(self, *titles_dates):
        return [judgment_from_row(row(title=t, petitioner=t.split(" versus ")[0], respondent=t.split(" versus ")[1],
                                      decision_date=d, path=f"P_{i}")) for i, (t, d) in enumerate(titles_dates)]

    def qa(self, name, date):
        return [{"case_name": name, "judgement_date": date, "question": "q", "answer": "a"}]

    def test_a_shared_state_name_is_not_a_match(self):
        from legal_lens.dataset import match_indiclegalqa
        js = self.judgments(("DILAWAR versus THE STATE OF HARYANA & ANR.", "01-05-2018"))
        matched, unmatched = match_indiclegalqa(self.qa("Satpal vs. State of Haryana", "1st May 2018"), js)
        self.assertEqual((matched, len(unmatched)), ({}, 1))

    def test_a_shared_office_is_not_a_match(self):
        from legal_lens.dataset import match_indiclegalqa
        js = self.judgments(("PR. COMMISSIONER OF INCOME TAX SHIMLA versus M/S AARHAM SOFTRONICS", "20-02-2019"))
        matched, _ = match_indiclegalqa(
            self.qa("Western Coalfields Ltd. vs. Commissioner of Central Excise, Trichy/Madurai", "20th February 2019"), js)
        self.assertEqual(matched, {})

    def test_one_side_matching_is_not_enough(self):
        from legal_lens.dataset import match_indiclegalqa
        js = self.judgments(("THE GREATER BOMBAY CO-OPERATIVE BANK LIMITED versus MR. NAGRAJ GANESHMAL JAIN & ORS.",
                             "26-07-2017"))
        matched, _ = match_indiclegalqa(self.qa("M.D. Jain vs Bhagyavathi & Ors.", "26th July 2017"), js)
        self.assertEqual(matched, {})

    def test_the_right_case_still_matches(self):
        from legal_lens.dataset import match_indiclegalqa
        js = self.judgments(("DILAWAR versus THE STATE OF HARYANA & ANR.", "01-05-2018"),
                            ("SATPAL versus THE STATE OF HARYANA", "01-05-2018"))
        pairs = self.qa("Dilawar vs. The State of Haryana", "1st May 2018") + self.qa("Satpal vs. State of Haryana", "1st May 2018")
        matched, unmatched = match_indiclegalqa(pairs, js)
        self.assertEqual({k[0]: j.title for k, j in matched.items()}, {
            "Dilawar vs. The State of Haryana": "DILAWAR versus THE STATE OF HARYANA & ANR.",
            "Satpal vs. State of Haryana": "SATPAL versus THE STATE OF HARYANA"})
        self.assertEqual(unmatched, [])

    def test_sides_may_be_swapped_in_an_appeal(self):
        from legal_lens.dataset import _sides_agree
        self.assertTrue(_sides_agree("Menoka Malik and others vs. The State of West Bengal and others",
                                     "MENOKA MALIK AND OTHERS versus THE STATE OF WEST BENGAL AND OTHERS"))
        self.assertTrue(_sides_agree("Union of India vs. Apollo Institute", "APOLLO INSTITUTE versus UNION OF INDIA"))

if __name__ == "__main__":
    unittest.main()
