import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

import photography_lib
from photography_lib.cli import main, parser
from photography_lib.fingerprints import fingerprint
from photography_lib.index_profiles import default_profile


class RetiredAnalysisTests(unittest.TestCase):
    def test_model_profile_id_is_unchanged_by_helper_extraction(self):
        self.assertEqual(fingerprint(default_profile()),
                         "b87a6c01508531411c626dcfda0a2059d51133000d37b18200dc2ae2260f924a")
        self.assertEqual(fingerprint({"b": 2, "a": 1}), fingerprint({"a": 1, "b": 2}))

    def test_obsolete_modules_and_package_exports_are_removed(self):
        for name in ("analysis_api", "analysis_execution", "analysis_planner", "analysis_schema",
                     "analysis_settings", "analyze", "codex_vision", "openai_batch",
                     "provider_capabilities", "report", "status", "vision",
                     "workflow_cli", "workflow_report", "workflow_storage"):
            with self.subTest(module=name):
                self.assertIsNone(importlib.util.find_spec("photography_lib." + name))
        for name in ("analyze", "AnalysisConfig", "OpenAIResponsesProvider"):
            self.assertFalse(hasattr(photography_lib, name))

    def test_removed_commands_fail_before_creating_state(self):
        with tempfile.TemporaryDirectory(prefix="smart-albums-retired-") as root:
            state = Path(root) / "must-not-exist"
            for command in ("analyze", "analysis", "analyses", "analysis-plan", "analysis-status",
                            "analysis-config", "analysis-confirm", "analysis-execute", "analysis-resume",
                            "analysis-job", "analysis-collect", "analysis-cancel", "analysis-cleanup",
                            "analysis-recover", "analysis-run", "analysis-report"):
                with self.subTest(command=command), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                    main(["--state-dir", str(state), command])
                self.assertEqual(exc.exception.code, 2)
            self.assertFalse(state.exists())
        self.assertNotIn("analysis", parser().format_help())

    def test_new_ingestion_uses_only_index_status(self):
        with tempfile.TemporaryDirectory(prefix="smart-albums-retired-scan-") as root:
            source = Path(root) / "originals"
            source.mkdir()
            Image.new("RGB", (120, 60), "navy").save(source / "fixture.jpg")
            result = photography_lib.ingestion(source, config=photography_lib.Config(Path(root) / "state"))
            for field in ("analysis_summary", "analysis_scope", "analysis_suggested"):
                self.assertNotIn(field, result)
            self.assertEqual(result["index_summary"]["status"], "not_configured")
            self.assertEqual(result["model_calls"], 0)


if __name__ == "__main__":
    unittest.main()
