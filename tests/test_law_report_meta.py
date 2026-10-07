"""Case details from the head of a Supreme Court Reports copy, as the AWS Open
Data scans actually print it: brackets of either kind, margin letters, OCR
specks, a 'v.' that came out as 't.', and extracts that open mid-way through
the previous case."""

import datetime as dt
import unittest

from legal_lens.autometa import _law_report_title


class ModernReport(unittest.TestCase):
    HEAD = ["(2008] 15 S.C.R. 735", "HARDEEP SINGH A", "v.", "~", "STATE OF PUNJAB & ORS.", "l",
            "(Criminal Appeal No. 1750 of 2008)", "NOVEMBER 7, 2008", ".8",
            "[C.K. THAKKER AND D.K. JAIN, JJ.]", "Code of Criminal Procedure, 1973:", "c",
            "s. 319 - Power under - Scope of - Application under,"]

    def test_everything_is_read_despite_specks_and_margin_letters(self):
        t = _law_report_title(self.HEAD)
        self.assertEqual((t["petitioner"], t["respondent"]), ("HARDEEP SINGH", "STATE OF PUNJAB & ORS"))
        self.assertEqual(t["date"], dt.date(2008, 11, 7))
        self.assertEqual(t["bench"], ["C.K. Thakker", "D.K. Jain"])
        self.assertEqual(t["case_number"], "Criminal Appeal No. 1750 of 2008")
        self.assertEqual(t["citation"], "(2008) 15 SCR 735")

    def test_margin_letter_before_the_name_and_a_bracket_that_wraps(self):
        head = ["[2008] 16 S.C.R. 540", "~", "A BALDEV SINGH MANN", "~·", "v.", ". ' SURJIT SINGH DHIMAN",
                "(Civil Appeal No. 3700 of 2007)", "NOVEMBER 21, 2008", "B",
                "[DALVEER BHANDARI AND", "HARJIT SINGH BEDI, JJ.]", "t", "Representation of People Act, 1951:"]
        t = _law_report_title(head)
        self.assertEqual((t["petitioner"], t["respondent"]), ("BALDEV SINGH MANN", "SURJIT SINGH DHIMAN"))
        self.assertEqual(t["bench"], ["Dalveer Bhandari", "Harjit Singh Bedi"])
        self.assertEqual(t["date"], dt.date(2008, 11, 21))

    def test_a_reference_has_a_title_and_no_parties(self):
        head = ["[2016j 11S.C.R.15", '"IN RE: THE PUNJAB TERMINATION OF AGREEMENT ACT, A', '2004"',
                "(Special Reference No. I of 2004)", "NOVEMBER 10, 2016",
                "[ANIL R. DAVE, PINAKI CHANDRA GHOSE, SHIVA KIRTI B", "SINGH, ADARSH KUMAR GOEL AND AMITAVA ROY, JJ.)",
                "Constitution of India:"]
        t = _law_report_title(head)
        self.assertEqual(t["petitioner"], "")
        self.assertEqual(t["title"], "IN RE: THE PUNJAB TERMINATION OF AGREEMENT ACT")
        self.assertEqual(t["citation"], "(2016) 11 SCR 15")
        self.assertEqual(len(t["bench"]), 5)
        self.assertEqual(t["case_number"], "Special Reference No. I of 2004")


class OldReport(unittest.TestCase):
    def test_round_brackets_over_two_lines_and_no_date(self):
        head = ["426 SUPREME COURT REPORTS [1960(1}]", "V. V. GIRI", "SHRI", "1959", "v.",
                "DIPPALA SURI DORA AND OTHERS", "(B. P. SINHA, JAFER IMAM, J. L. KAPUR,",
                "P. B. GAJENDRAGADKAR and K. N. WANCHOO, JJ.)", "Representation of the People Act-Election."]
        t = _law_report_title(head)
        self.assertEqual(t["respondent"], "DIPPALA SURI DORA AND OTHERS")
        self.assertIn("GIRI", t["petitioner"])
        self.assertEqual(len(t["bench"]), 5)
        self.assertIsNone(t["date"])
        self.assertIsNone(t["citation"])     # the old volumes print it only in the running header

    def test_versus_read_as_t_and_a_date_a_year_before_the_volume(self):
        head = ["A CHOTKA HEMBRAM", "t.", "STATE OF WEST BENGAL AND ORS.", "August 29, 1973",
                "[H. R. KHANNA AND A. ALAGIRISWAMI, JJ.]", "B", "Maintenance of Internal Security Act, 1971"]
        t = _law_report_title(head)
        self.assertEqual((t["petitioner"], t["respondent"]), ("CHOTKA HEMBRAM", "STATE OF WEST BENGAL AND ORS"))
        self.assertEqual(t["date"], dt.date(1973, 8, 29))

    def test_mixed_brackets_and_a_chief_justice(self):
        head = ["KHAZAN SINGH ETC. ETC.", "Y.", "STATE OF U.P. & ORS.", "December 3, 1973",
                "(A. N. RAY, C. J., H. R. KHANNA, K. K. MATHEW,", "A. ALAGIRISWAMI AND P. N. BHAGWATI, JJ.]", "8",
                "Constitution of India, Motor Vehicles Act, s. 68"]
        t = _law_report_title(head)
        self.assertEqual(t["petitioner"], "KHAZAN SINGH ETC. ETC")
        self.assertEqual(t["bench"], ["A. N. Ray", "H. R. Khanna", "K. K. Mathew", "A. Alagiriswami", "P. N. Bhagwati"])

    def test_bench_without_a_rank_right_under_the_parties(self):
        head = ["294 SUPREME COURT REPORTS", "1'64 K. KANKARATHNAMMA AND OTHERS", ",.,._,, Zl", '"·',
                "STATE OF ANDHRA PRADESH AND OTHERS", "(K. SUBBA RAO AND J. R. MUDHOLKAR)",
                "Land Acquisition Act. 1894 (1 of 1894), s. 18(1)(2)-No reference to"]
        t = _law_report_title(head)
        self.assertEqual(t["respondent"], "STATE OF ANDHRA PRADESH AND OTHERS")
        self.assertEqual(t["bench"], ["K. Subba Rao", "J. R. Mudholkar"])

    def test_the_tail_of_the_previous_case_is_not_a_party(self):
        head = ["752 SUPREME COURT REPORTS [1964) VOL.", "from the payment of land revenue. The futility",
                "of the argument that the expression person includes the estate-holder.", "Appeal dismissed.",
                "RAJA RAM JAISWAL", "v.", "STATE OF BIHAR", "April 2, 1964",
                "(P. B. GAJENDRAGADKAR, C.J., K. N. WANCHOO AND J. C. SHAH, JJ.)", "Bihar Land Reforms Act, 1950."]
        t = _law_report_title(head)
        self.assertEqual((t["petitioner"], t["respondent"]), ("RAJA RAM JAISWAL", "STATE OF BIHAR"))
        self.assertEqual(t["date"], dt.date(1964, 4, 2))

    def test_margin_letter_before_the_bench_and_two_dates(self):
        head = ["145 A", "BACHAN SINGH ETC. ETC•", ". v. l", "STATE OF PUNJAB ETC. ETC. B", "May 9, 1980/August 16, i982·",
                "[Y.V. CHANDRACHUD, C.J., P.N. BHAGWATI, R.S. SARKARIA,", "A.C. GUPTA AND N.L. UNTWALIA, JJ.] C.",
                "(A) Death Penalty, whether constitutionally valid?"]
        t = _law_report_title(head)
        self.assertEqual((t["petitioner"], t["respondent"]), ("BACHAN SINGH ETC. ETC", "STATE OF PUNJAB ETC. ETC"))
        self.assertEqual(t["date"], dt.date(1982, 8, 16))
        self.assertEqual(len(t["bench"]), 5)

    def test_short_extract_with_the_bench_after_a_margin_letter(self):
        head = ["A JIBONTARA GHATOWAR", "v.", "SARBANANDA SONOWAL AND ORS.", "MAY 9, 2003",
                "B [R.C. LAHOTI AND B.N. AGRA WAL, JJ.]", "Election laws:"]
        t = _law_report_title(head)
        self.assertEqual(t["bench"], ["R.C. Lahoti", "B.N. Agra Wal"])
        self.assertEqual(t["petitioner"], "JIBONTARA GHATOWAR")


class NewVolumes(unittest.TestCase):
    def test_mixed_case_names_and_the_author_star(self):
        head = ["[2024] 12 S.C.R. 777 : 2024 INSC 979", "Om Prakash Yadav", "v.", "Niranjan Kumar Upadhyay & Ors.",
                "(Criminal Appeal No(s). 5267-5268 of 2024)", "13 December 2024",
                "[J.B. Pardiwala* and Manoj Misra, JJ.]", "Issue for Consideration",
                "Issue arose as regards whether in the absence of the grant"]
        t = _law_report_title(head)
        self.assertEqual((t["petitioner"], t["respondent"]), ("Om Prakash Yadav", "Niranjan Kumar Upadhyay & Ors"))
        self.assertEqual(t["bench"], ["J.B. Pardiwala", "Manoj Misra"])
        self.assertEqual(t["date"], dt.date(2024, 12, 13))
        self.assertEqual(t["citation"], "(2024) 12 SCR 777")

    def test_case_number_without_brackets_date_without_a_space_and_header_citation(self):
        head = ["916 SUPREME [2019]COURT2 REPORTSS.C.R. 916 [2019] 2 S.C.R.", "A MANIK KUTUM", "v.", "JULIE KUTUM",
                "Criminal Appeal No.448 of 2019", "B MARCH 07, 2019", "[ABHAY MANOHAR SAPRE AND",
                "DINESH MAHESHWARI , JJ.]", "Code of Criminal Procedure, 1973:"]
        t = _law_report_title(head)
        self.assertEqual((t["petitioner"], t["respondent"]), ("MANIK KUTUM", "JULIE KUTUM"))
        self.assertEqual(t["case_number"], "Criminal Appeal No.448 of 2019")
        self.assertEqual(t["date"], dt.date(2019, 3, 7))
        self.assertEqual(t["citation"], "(2019) 2 SCR 916")
        head[5] = "B SEPTEMBER27, 2021"
        self.assertEqual(_law_report_title(head)["date"], dt.date(2021, 9, 27))

    def test_backslash_versus(self):
        head = ["[2008] 13 S.C.R. 604", "A NATIONAL INSURANCE CO. LTD.", "\\I.", "VIDHYADHAR MAHARIWALA & ORS.",
                "(Civil Appeal No. 5721 of 2008)", "B SEPTEMBER 17, 2008", "(DR. ARIJIT PASAYAT AND HARJIT SINGH BEDI, JJ.)"]
        t = _law_report_title(head)
        self.assertEqual(t["petitioner"], "NATIONAL INSURANCE CO. LTD")
        self.assertEqual(t["bench"], ["Arijit Pasayat", "Harjit Singh Bedi"])

    def test_nothing_recognisable_gives_nothing(self):
        t = _law_report_title(["COTTR't REPORTS [l!l63]", "l!<'ncral rnlc.", "the Mysore High Court", "R'fl1hulhi"])
        self.assertEqual((t["petitioner"], t["respondent"], t["bench"], t["date"]), ("", "", [], None))


if __name__ == "__main__":
    unittest.main()
