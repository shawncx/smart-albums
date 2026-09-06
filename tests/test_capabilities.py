from __future__ import annotations

import builtins
import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

from photography_lib import ingestion, ingest
from photography_lib.cli import main, parser
from photography_lib.index_profiles import default_profile
from photography_lib.sqlite_storage import SQLiteStorage


class FixtureEncoder:
    def __init__(self, profile):
        self._profile = profile
        self.calls = self.ready_checks = 0
        self.load_seconds = 0

    def profile(self):
        return copy.deepcopy(self._profile)

    def check_ready(self):
        self.ready_checks += 1

    def encode_image(self, data):
        self.calls += 1
        return SimpleNamespace(vector=[1.] + [0.] * (self._profile["dimensions"] - 1),
                               elapsed_seconds=.001, token_count=None)

    def encode_text(self, text):
        self.calls += 1
        return SimpleNamespace(vector=[1.] + [0.] * (self._profile["dimensions"] - 1),
                               elapsed_seconds=.001, token_count=5)


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="smart-albums-capabilities-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "photos"
        self.source.mkdir()
        self.state = self.base / "state"
        Image.new("RGB", (300, 150), "navy").save(self.source / "wide.jpg")
        Image.new("RGB", (150, 300), "green").save(self.source / "tall.jpg")
        code, self.scan = self.cli("ingestion", str(self.source), "--album-name", "Travel")
        self.assertEqual(code, 0)
        self.album_id = self.scan["album"]["album_id"]
        self.profile = default_profile()
        self.encoder = FixtureEncoder(self.profile)
        guard = patch("urllib.request.urlopen", side_effect=AssertionError("No live network calls"))
        guard.start()
        self.addCleanup(guard.stop)

    def cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--state-dir", str(self.state), *args])
        return code, json.loads(output.getvalue())

    def configure(self):
        with SQLiteStorage(self.state) as store:
            profile_id = store.put_index_profile(self.profile)
        code, configured = self.cli("index", "configure", "--default-profile", profile_id)
        self.assertEqual(code, 0)
        return profile_id

    def test_ingestion_alias_proportions_and_unconfigured_summary(self):
        self.assertIs(ingestion, ingest)
        self.assertEqual(self.scan["index_summary"]["status"], "not_configured")
        self.assertEqual(self.scan["index_summary"]["counts"]["total"], 2)
        self.assertTrue(self.scan["index_suggested"])
        code, scan = self.cli("ingest", str(self.source))
        self.assertEqual((code, scan["unchanged"], scan["model_calls"]), (0, 2, 0))
        with SQLiteStorage(self.state) as store:
            sizes = [(store.thumbnail(pid)["width"], store.thumbnail(pid)["height"])
                     for pid in self.scan["successful_photo_ids"]]
            self.assertCountEqual(sizes, [(300, 150), (150, 300)])
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM image_index_results").fetchone()[0], 0)

    def test_setup_registration_does_not_change_default(self):
        response = {"status": "completed", "profile": self.profile, "downloaded_files": 0, "reused_files": 1}
        with patch("photography_lib.siglip_embedding.setup_model", return_value=response) as setup:
            code, result = self.cli("index", "setup")
        self.assertEqual(code, 0)
        setup.assert_called_once()
        self.assertIsNone(result["default_profile_id"])
        code, profiles = self.cli("index", "profiles")
        self.assertEqual(code, 0)
        self.assertEqual(len(profiles["profiles"]), 1)
        self.assertEqual(profiles["profiles"][0]["profile_id"], result["profile_id"])
        self.assertEqual(self.cli("index", "status")[0], 2)

    def test_setup_rejects_source_and_state_root_before_downloading(self):
        with patch("photography_lib.siglip_embedding.setup_model", side_effect=AssertionError("Must validate first")):
            for directory in (self.source, self.source / "models", self.base, self.state):
                code, result = self.cli("index", "setup", "--model-dir", str(directory))
                self.assertEqual((code, result["error"]["code"]), (2, "INVALID_ARGUMENT"))

    def test_configuration_is_explicit_and_persistent(self):
        code, result = self.cli("index", "configure", "--default-profile", "unknown")
        self.assertEqual(code, 2)
        with SQLiteStorage(self.state) as store:
            self.assertIsNone(store.default_index_profile())
            profile_id = store.put_index_profile(self.profile)
        code, result = self.cli("index", "configure", "--default-profile", profile_id)
        self.assertEqual((code, result["default_profile_id"]), (0, profile_id))
        code, profiles = self.cli("index", "profiles")
        self.assertEqual((code, profiles["default_profile_id"]), (0, profile_id))

    def test_plan_execute_cache_and_ingestion_summary(self):
        profile_id = self.configure()
        with patch("photography_lib.siglip_embedding.SiglipEncoder", return_value=self.encoder):
            code, plan = self.cli("index", "plan", "--album-id", self.album_id)
            self.assertEqual(code, 0)
            self.assertEqual(self.encoder.calls, 0)
            code, rejected = self.cli("index", "execute", plan["run_id"], "--confirm", "wrong")
            self.assertEqual(code, 2)
            self.assertEqual(self.encoder.calls, 0)
            code, executed = self.cli("index", "execute", plan["run_id"], "--confirm", plan["digest"])
            self.assertEqual(code, 0, executed)
            self.assertEqual(self.encoder.calls, 2)
            code, status = self.cli("index", "status", "--album-id", self.album_id, "--limit", "1")
            self.assertEqual((code, status["counts"]["ready"], len(status["items"])), (0, 2, 1))
            self.assertIsNotNone(status["next_cursor"])
            code, job = self.cli("index", "job", plan["run_id"])
            self.assertEqual(code, 0)
            self.assertEqual(job["profile"], self.profile)
            checks = self.encoder.ready_checks
            code, cached = self.cli("index", "plan", "--album-id", self.album_id)
            self.assertEqual(code, 0, cached)
            self.assertEqual((self.encoder.calls, self.encoder.ready_checks), (2, checks))
            output = self.base / "semantic.html"
            code, found = self.cli("management", "search", "trees", "--mode", "semantic",
                                   "--album-id", self.album_id, "--html", str(output))
            self.assertEqual(code, 0, found)
            self.assertEqual(found["profile_id"], profile_id)
            self.assertEqual(found["coverage"]["ready"], 2)
            self.assertEqual(len(found["results"]), 2)
            self.assertEqual(self.encoder.calls, 3)
            self.assertTrue(output.exists())
            for item in found["results"]:
                self.assertIn("result_id", item)
                self.assertIn("input_image_hash", item)
                self.assertNotIn("description", item)
        code, scan = self.cli("ingestion", str(self.source))
        self.assertEqual((code, scan["index_summary"]["counts"]["ready"]), (0, 2))
        self.assertEqual(scan["index_summary"]["profile_id"], profile_id)
        self.assertFalse(scan["index_suggested"])
        with SQLiteStorage(self.state) as store:
            self.assertIsNone(store.db.execute("SELECT name FROM sqlite_master WHERE name='analyses'").fetchone())

    def test_dry_run_and_invalid_scopes_do_not_save_plans(self):
        self.configure()
        with patch("photography_lib.siglip_embedding.SiglipEncoder", return_value=self.encoder):
            code, plan = self.cli("index", "plan", "--album-id", self.album_id, "--dry-run")
            self.assertEqual(code, 0, plan)
            for limit in ("0", "-1"):
                self.assertEqual(self.cli("index", "plan", "--album-id", self.album_id, "--limit", limit)[0], 2)
            invalid = self.base / "invalid.json"
            invalid.write_text('{"photo_ids":[]}', encoding="utf-8")
            self.assertEqual(self.cli("index", "plan", "--ids-file", str(invalid))[0], 2)
        self.assertEqual(self.encoder.calls, 0)
        with SQLiteStorage(self.state) as store:
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM image_index_runs").fetchone()[0], 0)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(["index", "plan"])

    def test_base_capabilities_do_not_import_optional_runtime(self):
        original_import = builtins.__import__

        def guard(name, *args, **kwargs):
            if name.split(".")[0] in ("torch", "transformers", "tokenizers", "numpy", "onnxruntime"):
                raise AssertionError("Optional runtime imported during read-only/ingestion operation")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guard):
            for args in (("ingestion", str(self.source)), ("management", "albums"),
                         ("management", "photos"), ("index", "profiles"),
                         ("management", "search", "wide", "--mode", "metadata", "--target", "photos")):
                self.assertEqual(self.cli(*args)[0], 0)


if __name__ == "__main__":
    unittest.main()
