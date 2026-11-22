from pathlib import Path
import unittest

from registration_parser import parse_registration_tables


ROOT = Path(__file__).resolve().parents[1]


def _table_row(course: str, section: str, add_cell: str, allowed: str = "ALL") -> str:
    cells = [
        course,
        section,
        "TTh",
        "8:00 AM-9:00 AM",
        "25",
        "0",
        "0",
        allowed,
        add_cell,
    ]
    return "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"


class RegistrationParserTests(unittest.TestCase):
    def test_extracts_live_action_args_and_keeps_disallowed_row(self) -> None:
        headers = (
            "<tr>"
            "<th>Course Number</th><th>Section</th><th>Day</th><th>Time</th>"
            "<th>Max Size</th><th>Count</th><th>Wait List</th>"
            "<th>Allowed</th><th>Add</th>"
            "</tr>"
        )
        allowed = _table_row(
            "PE 2 Dance",
            "AB",
            '<img onclick="javascript:enlist(\'student-demo\',\'course-17\',\'PE II\')">',
        )
        denied = _table_row(
            "PE 2 Dance",
            "CD",
            '<label>NOT ALLOWED</label>',
            allowed="NOT ALLOWED",
        )
        html = (
            '<div class="panel"><label>Physical Education</label>'
            f"<table>{headers}{allowed}{denied}</table></div>"
            "<p>Example outside table: "
            "<img onclick=\"javascript:enlist('ignored','ignored','ignored')\"></p>"
        )

        tables = parse_registration_tables(html)

        self.assertEqual(len(tables), 1)
        table = tables[0]
        self.assertEqual(table["category"], "PE")
        self.assertEqual(len(table["headers"]), 9)
        self.assertEqual(len(table["subjects"]), 2)
        self.assertTrue(table["subjects"][0]["selectable"])
        self.assertEqual(
            table["subjects"][0]["enlist_args"],
            ("student-demo", "course-17", "PE II"),
        )
        self.assertFalse(table["subjects"][1]["selectable"])
        self.assertEqual(table["subjects"][1]["reason"], "Not allowed")

    def test_parses_the_markdown_snapshots_without_trailing_examples(self) -> None:
        expected = {
            "PE.md": ("PE", 33, 32),
            "NSTP.md": ("NSTP", 19, 3),
        }
        for filename, (category, row_count, selectable_count) in expected.items():
            with self.subTest(filename=filename):
                html = (ROOT / filename).read_text(encoding="utf-8")
                tables = parse_registration_tables(html)
                self.assertEqual(len(tables), 1)
                self.assertEqual(tables[0]["category"], category)
                self.assertEqual(len(tables[0]["subjects"]), row_count)
                self.assertEqual(
                    sum(row["selectable"] for row in tables[0]["subjects"]),
                    selectable_count,
                )

    def test_invalid_or_multiple_enlist_handlers_are_not_selectable(self) -> None:
        headers = (
            "<tr><th>Course Number</th><th>Section</th><th>Day</th><th>Time</th>"
            "<th>Max Size</th><th>Count</th><th>Wait List</th>"
            "<th>Allowed</th><th>Add</th></tr>"
        )
        invalid = _table_row(
            "PE 2 Dance",
            "AB",
            '<img onclick="javascript:enlist(untrusted,\'course-17\',\'PE II\')">',
        )
        html = (
            '<div class="panel"><label>Physical Education</label>'
            f"<table>{headers}{invalid}</table></div>"
        )

        tables = parse_registration_tables(html)

        self.assertFalse(tables[0]["subjects"][0]["selectable"])
        self.assertIsNone(tables[0]["subjects"][0]["enlist_args"])

    def test_keeps_a_colspan_table_row_in_the_original_column_count(self) -> None:
        headers = (
            "<tr><th>Course Number</th><th>Section</th><th>Day</th><th>Time</th>"
            "<th>Max Size</th><th>Count</th><th>Wait List</th>"
            "<th>Allowed</th><th>Add</th></tr>"
        )
        html = (
            '<div class="panel"><label>National Service Training Program</label>'
            f"<table>{headers}<tr><td colspan='9'>No current options</td></tr></table>"
            "</div>"
        )

        tables = parse_registration_tables(html)

        row = tables[0]["subjects"][0]
        self.assertEqual(len(row["cells"]), len(tables[0]["headers"]))
        self.assertEqual(row["cells"][0], "No current options")
        self.assertFalse(row["selectable"])


if __name__ == "__main__":
    unittest.main()
