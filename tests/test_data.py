"""The shipped mapping CSVs and case manifest must be valid."""

import datetime as dt
import unittest

from legal_lens.config import ROOT
from legal_lens.manifest import ManifestError, _parse_case, check_against_text, load_manifest
from legal_lens.mapping import Mapping, MappingRow, validate


def row(old, new, relation="unreviewed", note=None, code="IPC"):
    return MappingRow(code, old, ("BNS" if new else None), new, relation, "h", note,
                      dt.date(2024, 7, 1), "test", True)


class ShippedMapping(unittest.TestCase):
    mapping = Mapping.from_dir(ROOT / "data" / "mappings")

    def test_no_errors(self):
        errors, _ = validate(self.mapping.rows)
        self.assertEqual(errors, [])

    def test_flagship_row(self):
        rows, how = self.mapping.lookup("IPC", "304A")
        self.assertEqual((how, rows[0].new_id, rows[0].relation), ("exact", "BNS:106(1)", "modified"))
        self.assertIn("five years", rows[0].change_note)

    def test_omitted_provision_has_no_successor(self):
        rows, how = self.mapping.lookup("IPC", "377")
        self.assertEqual((how, rows[0].relation, rows[0].new_id), ("exact", "omitted", None))
        self.assertEqual(self.mapping.successors("IPC", "377"), [])

    def test_split_provision_has_two_successors(self):
        self.assertEqual(self.mapping.successors("IPC", "506"), ["BNS:351(2)", "BNS:351(3)"])

    def test_sub_section_falls_back_to_base_and_says_so(self):
        rows, how = self.mapping.lookup("IPC", "376(2)(g)")
        self.assertEqual((how, rows[0].new_id), ("base", "BNS:64"))
        self.assertEqual(self.mapping.lookup("IPC", "999")[1], "none")

    def test_procedure_code(self):
        self.assertEqual(self.mapping.successors("CrPC", "154"), ["BNSS:173"])


class MappingRules(unittest.TestCase):
    def errors(self, *rows):
        return validate(list(rows))[0]

    def test_modified_needs_a_note(self):
        self.assertTrue(self.errors(row("304A", "106(1)", "modified")))
        self.assertFalse(self.errors(row("304A", "106(1)", "modified", "changed")))

    def test_omitted_must_not_name_a_successor(self):
        self.assertTrue(self.errors(row("377", "99", "omitted", "gone")))
        self.assertFalse(self.errors(row("377", None, "omitted", "gone")))

    def test_two_rows_for_one_section_must_be_split(self):
        self.assertTrue(self.errors(row("506", "351(2)"), row("506", "351(3)")))
        self.assertFalse(self.errors(row("506", "351(2)", "split"), row("506", "351(3)", "split")))

    def test_wrong_target_code(self):
        bad = MappingRow("CrPC", "154", "BNS", "173", "unreviewed", "h", None, dt.date(2024, 7, 1), "t", True)
        self.assertTrue(self.errors(bad))


class Manifest(unittest.TestCase):
    cases = load_manifest(ROOT / "data" / "cases.yaml")

    def test_seed_cases(self):
        self.assertGreaterEqual(len(self.cases), 5)
        jm = self.cases["jacob_mathew_2005"]
        self.assertEqual(jm.date, dt.date(2005, 8, 5))
        self.assertEqual(jm.bench[0], "R.C. Lahoti")
        self.assertEqual(jm.citation_keys, ["2005-6-scc-1", "air-2005-sc-3180"])
        self.assertEqual(len(self.cases["puttaswamy_2017"].bench), 9)
        self.assertEqual(len(self.cases["kesavananda_bharati_1973"].bench), 13)

    def test_opinion_author_must_be_on_the_bench(self):
        raw = dict(case_id="x", name="A v. B", date="2020-01-01", court="Supreme Court",
                   citations=["(2020) 1 SCC 1"], bench=["A.B. Tester"], file="x.pdf",
                   opinions=[{"author": "Someone Else", "type": "majority"}])
        with self.assertRaises(ManifestError):
            _parse_case(raw)

    def test_text_check_catches_wrong_date_and_bench(self):
        jm = self.cases["jacob_mathew_2005"]
        good = ("Jacob Mathew ... Appellant versus State of Punjab. R.C. LAHOTI, CJI. "
                "(G.P. MATHUR) (P.K. BALASUBRAMANYAN) New Delhi; August 5, 2005")
        self.assertEqual(check_against_text(jm, good), [])
        wrong = good.replace("August 5, 2005", "March 1, 2004").replace("LAHOTI", "PASAYAT")
        warnings = check_against_text(jm, wrong)
        self.assertEqual(len(warnings), 2)
        self.assertIn("R.C. Lahoti", warnings[1])


if __name__ == "__main__":
    unittest.main()


class HttpRunnerReplies(unittest.TestCase):
    """The Query API's table shape becomes the driver's list of dicts."""

    def test_rows(self):
        from legal_lens.graph_store import Neo4jHttpRunner, make_runner
        reply = {"data": {"fields": ["label", "n"], "values": [["Case", 7], ["Statute", 66]]}, "bookmarks": ["x"]}
        self.assertEqual(Neo4jHttpRunner.rows(reply), [{"label": "Case", "n": 7}, {"label": "Statute", "n": 66}])
        self.assertEqual(Neo4jHttpRunner.rows({"data": {"fields": [], "values": []}}), [])
        with self.assertRaises(RuntimeError):
            Neo4jHttpRunner.rows({"errors": [{"code": "Neo.ClientError.Database.DatabaseNotFound", "message": "no"}]})
        self.assertIsInstance(make_runner("https://x.databases.neo4j.io", "u", "p", "d"), Neo4jHttpRunner)
