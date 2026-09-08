from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from tests.duplicate_fixtures import DuplicateFixture
from photography_lib import cli, duplicates
from photography_lib.config import PhotographyError
from photography_lib.sqlite_storage import SQLiteStorage


class DuplicateCliTests(DuplicateFixture, unittest.TestCase):
    def command(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(["--database", str(self.config.database_path), "management", "duplicates",
                             *(str(arg) for arg in args)])
        return code, json.loads(output.getvalue())

    def test_scan_pages_and_report_through_read_only_main(self):
        self.photo("a", content="copy")
        self.photo("b", content="copy")
        output, report = self.root / "snapshot.json", self.root / "report.html"
        before = self.config.database_path.read_bytes()
        with patch.object(SQLiteStorage, "assert_writable", side_effect=AssertionError("No album writes")), \
             patch("photography_lib.feature_inputs.load_input", side_effect=AssertionError("No inference")), \
             patch("photography_lib.source_paths.resolve_original", side_effect=AssertionError("No original access")):
            code, result = self.command("scan", "--all", "--mode", "exact", "--output", output, "--html", report)
            self.assertEqual(code, 0)
            self.assertEqual(result["summary"]["matched_photo_count"], 2)
            self.assertNotIn("inputs", result)
            self.assertNotIn("groups", result)
            code, listing = self.command("groups", output, "--limit", "all")
            group_id = listing["items"][0]["group_id"]
            code, members = self.command("group", output, "--group-id", group_id, "--limit", 1)
            self.assertEqual(members["total"], 2)
            self.assertIsNotNone(members["next_cursor"])
            code, pairs = self.command("pairs", output, "--group-id", group_id)
            self.assertEqual(pairs["total"], 1)
            code, rendered = self.command("report", output, "--output", self.root / "second.html")
            self.assertEqual(code, 0)
            self.assertTrue(report.is_file())
        self.assertEqual(self.config.database_path.read_bytes(), before)

    def test_partial_and_empty_id_array_exit_codes(self):
        self.photo("a")
        output = self.root / "snapshot.json"
        code, result = self.command("scan", "--all", "--output", output)
        self.assertEqual(code, 1)
        self.assertFalse(result["complete"])
        self.assertTrue(json.loads(output.read_text())["scan_finished"])
        ids = self.root / "ids.json"
        ids.write_text("[]")
        code, result = self.command("scan", "--ids-file", ids, "--output", output)
        self.assertEqual(code, 0)
        self.assertEqual(result["summary"]["group_count"], 0)
        ids.write_text("null")
        code, result = self.command("scan", "--ids-file", ids, "--output", output)
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

    def test_unsafe_exports_rejected_before_scan(self):
        self.photo("a")
        ids = self.root / "ids.json"
        ids.write_text('["a"]')
        with patch("photography_lib.duplicates.scan", side_effect=AssertionError("Must preflight exports")):
            code, result = self.command("scan", "--ids-file", ids, "--output", ids)
            self.assertEqual(code, 2)
            code, result = self.command("scan", "--all", "--output", self.config.database_path)
            self.assertEqual(code, 2)
            code, result = self.command("scan", "--all", "--output", self.root / "a.json", "--html", self.root / "wrong.json")
            self.assertEqual(code, 2)
        self.assertEqual(ids.read_text(), '["a"]')

    def test_interruption_and_export_failure_preserve_existing_snapshot(self):
        self.photo("a", content="same")
        self.photo("b", content="same")
        output = self.root / "snapshot.json"
        output.write_text("existing")
        with patch("photography_lib.duplicates._build_groups", side_effect=KeyboardInterrupt):
            code, result = self.command("scan", "--all", "--mode", "exact", "--output", output)
        self.assertEqual(code, 130)
        self.assertEqual(output.read_text(), "existing")
        with patch("photography_lib.exports.os.replace", side_effect=OSError("fixture publication failure")):
            code, result = self.command("scan", "--all", "--mode", "exact", "--output", output)
        self.assertEqual(code, 2)
        self.assertEqual(output.read_text(), "existing")
        self.assertEqual(list(self.root.glob(".smart-albums-export-*.tmp")), [])

    def test_report_failure_keeps_completed_json_for_retry(self):
        self.photo("a", content="same")
        self.photo("b", content="same")
        output, report = self.root / "snapshot.json", self.root / "report.html"
        with patch("photography_lib.duplicates_report.duplicate_report", side_effect=PhotographyError("EXPORT_FAILED", "fixture")):
            code, result = self.command("scan", "--all", "--mode", "exact", "--output", output, "--html", report)
        self.assertEqual(code, 2)
        self.assertEqual(result["error"]["details"]["output"], str(output))
        snapshot = json.loads(output.read_text())
        duplicates.validate(snapshot, store=self.store)
        self.assertEqual(self.command("report", output, "--output", report)[0], 0)

    def test_scope_required_and_bad_pagination_parameters(self):
        with redirect_stdout(io.StringIO()), patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            self.command("scan", "--output", self.root / "a.json")
        self.photo("a")
        for options in (("--mode", "exact", "--max-distance", "1"), ("--max-distance", "65"),
                        ("--folder-match", "union")):
            code, result = self.command("scan", "--all", "--output", self.root / "a.json", *options)
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
