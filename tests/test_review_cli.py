import builtins
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "photography" / "scripts"))
from photography_lib.cli import main, parser
from tests.test_review_execution import FakeReviewProvider, ReviewFixture


class ReviewCLITests(ReviewFixture):
    def cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--database", str(self.database), "review", *args])
        return code, json.loads(output.getvalue())

    def test_four_capabilities_and_review_arguments(self):
        help_text = parser().format_help()
        for name in ("ingestion", "index", "management", "review"):
            self.assertIn(name, help_text)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(["--database", str(self.database), "review", "execute", "run"])
        code, plan = self.cli("plan", "--photo-id", self.ids[0], "--model", "vision-test")
        self.assertEqual(code, 0)
        self.assertEqual(plan["counts"]["pending"], 1)
        self.assertEqual((plan["max_concurrency"], plan["batch_size"]), (5, 4))
        self.assertEqual(plan["album"]["database_path"], str(self.database))

    def test_configured_concurrency_is_frozen_and_invalid_limits_are_rejected(self):
        code, plan = self.cli("plan", "--photo-id", self.ids[0], "--model", "vision-test",
                              "--max-concurrency", "2", "--batch-size", "3")
        self.assertEqual(code, 0)
        self.assertEqual((plan["max_concurrency"], plan["batch_size"]), (2, 3))
        self.assertEqual(self.cli("job", plan["run_id"])[1]["max_concurrency"], 2)
        for value in ("0", "-1"):
            with self.subTest(value=value):
                code, result = self.cli("plan", "--photo-id", self.ids[0], "--model", "vision-test",
                                        "--max-concurrency", value)
                self.assertEqual(code, 2)
                self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

    def test_models_require_separate_approval_and_local_commands_never_construct_provider(self):
        factory = Mock(return_value=FakeReviewProvider())
        with patch("photography_lib.review._provider", factory):
            code, error = self.cli("models")
            self.assertEqual(code, 2)
            self.assertEqual(error["error"]["code"], "CONFIRMATION_REQUIRED")
            self.assertEqual(self.cli("rubric")[0], 0)
            _, plan = self.cli("plan", "--photo-id", self.ids[0], "--model", "vision-test")
            self.assertEqual(self.cli("job", plan["run_id"])[0], 0)
            self.assertEqual(self.cli("result", self.ids[0])[1]["status"], "missing")
            self.assertEqual(self.cli("history", self.ids[0])[0], 0)
            factory.assert_not_called()
            self.assertEqual(self.cli("models", "--confirm-provider-access")[0], 0)
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(self.cli("execute", plan["run_id"], "--confirm", "wrong")[0], 2)
            self.assertEqual(factory.call_count, 1)

    def test_execute_and_failure_exit_codes_and_fresh_retry(self):
        _, plan = self.cli("plan", "--photo-id", self.ids[0], "--model", "vision-test")
        def malformed(request, value, ordinal):
            value["results"][0]["dimensions"]["technical"]["score"] = "wrong"
        with patch("photography_lib.review._provider", return_value=FakeReviewProvider(malformed)):
            code, failed = self.cli("execute", plan["run_id"], "--confirm", plan["digest"])
        self.assertEqual(code, 1, failed)
        self.assertEqual(failed["status"], "failed")
        with patch("photography_lib.review._provider", return_value=FakeReviewProvider()):
            code, complete = self.cli("resume", plan["run_id"], "--confirm", failed["retry"]["digest"])
        self.assertEqual(code, 0, complete)
        self.assertEqual(complete["status"], "completed")
        result = self.cli("result", self.ids[0])[1]["result"]
        self.assertIsInstance(result["payload"], dict)
        self.assertEqual(result["payload"]["dimensions"]["composition"]["score"], 7)

    def test_optional_sdk_is_not_imported_for_local_surfaces(self):
        original_import = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name == "copilot" or name.startswith("copilot."):
                raise AssertionError("Optional SDK imported by a local-only command")
            return original_import(name, *args, **kwargs)
        before = self.database.read_bytes()
        with patch("builtins.__import__", side_effect=guarded):
            self.assertIn("review", parser().format_help())
            self.assertEqual(self.cli("rubric")[0], 0)
            self.assertEqual(self.cli("plan", "--photo-id", self.ids[0], "--model", "vision-test", "--dry-run")[0], 0)
            self.assertEqual(self.cli("history", self.ids[0])[0], 0)
        self.assertEqual(self.database.read_bytes(), before)

    def test_relative_ids_file_rejected_and_invalid_pagination_is_structured(self):
        code, error = self.cli("plan", "--ids-file", "relative.json", "--model", "vision-test")
        self.assertEqual(code, 2)
        self.assertEqual(error["error"]["code"], "INVALID_ARGUMENT")
        code, error = self.cli("history", self.ids[0], "--limit", "0")
        self.assertEqual(code, 2)

    def test_copied_skill_resolves_bundled_prompt_independently(self):
        source = Path(__file__).resolve().parents[1] / "photography"
        installed = self.root / "installed-skill"
        shutil.copytree(source, installed, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elsewhere = self.root / "unrelated-working-directory"
        elsewhere.mkdir()
        completed = subprocess.run(
            [sys.executable, str(installed / "scripts" / "photography.py"), "--database",
             str(self.database), "review", "rubric"],
            cwd=elsewhere, capture_output=True, text=True, encoding="utf-8", check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["rubric_version"], "photo-review-v2")
        self.assertIn("downsampled JPEG", value["prompt"])
