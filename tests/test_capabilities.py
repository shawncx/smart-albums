import builtins
from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))
from photography_lib.cli import main, parser
from photography_lib.image_embedding_profiles import default_profile
from photography_lib.sqlite_storage import SQLiteStorage


class FixtureEncoder:
    def __init__(self, profile):
        self.identity = json.loads(json.dumps(profile))
        self.calls = self.ready_checks = 0
        self.load_seconds = 0

    def profile(self):
        return self.identity

    def check_ready(self):
        self.ready_checks += 1

    def encode_image(self, data):
        self.calls += 1
        return SimpleNamespace(vector=[1.] + [0.] * (self.identity["dimensions"] - 1),
                               elapsed_seconds=.001, token_count=None)

    def encode_text(self, text):
        self.calls += 1
        return SimpleNamespace(vector=[1.] + [0.] * (self.identity["dimensions"] - 1),
                               elapsed_seconds=.001, token_count=4)


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="portable-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.database = self.root / "first.sqlite"
        self.cache = self.root / "models"
        self.source = self.root / "photos"
        self.source.mkdir()
        Image.new("RGB", (180, 90), "navy").save(self.source / "wide.jpg")
        Image.new("RGB", (90, 180), "green").save(self.source / "tall.jpg")
        self.assertEqual(self.cli("management", "create")[0], 0)
        code, self.scan = self.cli("ingestion", str(self.source))
        self.assertEqual(code, 0, self.scan)
        self.profile = default_profile()
        self.encoder = FixtureEncoder(self.profile)
        guard = patch("urllib.request.urlopen", side_effect=AssertionError("No real network calls"))
        guard.start()
        self.addCleanup(guard.stop)

    def cli(self, *args, database=None):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--database", str(database or self.database), "--model-cache-dir", str(self.cache), *args])
        return code, json.loads(output.getvalue())

    def configure(self):
        with SQLiteStorage.open(self.database, writable=True) as store:
            pid = store.put_embedding_profile(self.profile)
        self.assertEqual(self.cli("index", "configure", "--default-profile", pid)[0], 0)
        return pid

    def index(self):
        self.configure()
        _, plan = self.cli("index", "plan", "--all")
        code, run = self.cli("index", "execute", plan["run_id"], "--confirm", plan["digest"])
        self.assertEqual(code, 0, run)
        return plan, run

    def test_create_open_are_explicit_and_album_envelopes_are_correct(self):
        code, opened = self.cli("management", "open")
        self.assertEqual(code, 0)
        self.assertEqual(opened["album"]["database_path"], str(self.database))
        self.assertEqual(opened["album"]["name"], "first")
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(self.cli("management", "create")[0], 2)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)
        missing = self.root / "missing.sqlite"
        self.assertEqual(self.cli("management", "open", database=missing)[0], 2)
        self.assertFalse(missing.exists())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["management", "open"])

    def test_old_format_and_commands_are_rejected_unchanged(self):
        old = self.root / "old.db"
        with closing(sqlite3.connect(old)) as db:
            db.execute("CREATE TABLE legacy(value TEXT)")
            db.execute("INSERT INTO legacy VALUES ('keep')")
            db.execute("PRAGMA user_version=7")
            db.commit()
        before = old.read_bytes()
        self.assertEqual(self.cli("management", "open", database=old)[0], 2)
        self.assertEqual(old.read_bytes(), before)
        for args in (["--state-dir", str(self.root), "management", "open"],
                     ["--database", str(self.database), "ingest", str(self.source)],
                     ["--database", str(self.database), "albums"],
                     ["--database", str(self.database), "index", "plan", "--album-id", "old"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(args)

    def test_prompt_explains_both_levels_and_does_not_automatically_index(self):
        prompt = self.scan["index_prompt"]
        self.assertEqual(prompt["photo_ids"], self.scan["successful_photo_ids"])
        self.assertEqual(prompt["photo_count"], 2)
        self.assertTrue(prompt["configuration_required"])
        self.assertIn("filename", " ".join(prompt["without_index"]))
        self.assertIn("Chinese/English semantic", " ".join(prompt["with_index"]))
        with SQLiteStorage.open(self.database) as store:
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM image_embedding_runs").fetchone()[0], 0)

    def test_setup_registers_without_default_and_cache_paths_are_shared(self):
        response = {"status": "ready", "profile": self.profile, "downloaded_files": [], "reused_files": []}
        other = self.root / "second.db"
        self.assertEqual(self.cli("management", "create", database=other)[0], 0)
        with patch("photography_lib.siglip_embedding.setup_model", return_value=response) as setup:
            _, first = self.cli("index", "setup")
            _, second = self.cli("index", "setup", database=other)
        self.assertEqual(first["profile_id"], second["profile_id"])
        self.assertIsNone(first["default_profile_id"])
        self.assertIsNone(second["default_profile_id"])
        self.assertEqual(setup.call_args_list[0].args, setup.call_args_list[1].args)

    def test_index_search_cache_and_partial_import_scope(self):
        with patch("photography_lib.siglip_embedding.SiglipEncoder", return_value=self.encoder):
            plan, run = self.index()
            self.assertEqual(self.encoder.calls, 2)
            self.assertEqual(run["component"], "image_embedding")
            code, found = self.cli("management", "search", "trees", "--mode", "semantic",
                                   "--html", str(self.root / "results.html"))
            self.assertEqual((code, len(found["results"]), found["coverage"]["ready"]), (0, 2, 2))
            self.assertEqual(self.encoder.calls, 3)
            before = self.encoder.ready_checks
            self.assertEqual(self.cli("index", "plan", "--all")[0], 0)
            self.assertEqual(self.encoder.ready_checks, before)
        _, repeated = self.cli("ingestion", str(self.source))
        self.assertIsNone(repeated["index_prompt"])
        Image.new("RGB", (120, 80), "white").save(self.source / "new.jpg")
        (self.source / "bad.jpg").write_bytes(b"invalid")
        with patch("photography_lib.siglip_embedding.SiglipEncoder", side_effect=AssertionError("No automatic encoding")):
            code, result = self.cli("ingestion", str(self.source))
        self.assertEqual((code, result["added"], result["failed"]), (1, 1, 1))
        self.assertEqual(result["index_prompt"]["photo_ids"], result["changed_photo_ids"])
        self.assertEqual(result["index_prompt"]["photo_count"], 1)

    def test_move_preserves_embeddings_and_original_command_repairs_absolute(self):
        with patch("photography_lib.siglip_embedding.SiglipEncoder", return_value=self.encoder):
            self.index()
        with SQLiteStorage.open(self.database) as store:
            album_id = store.album()["id"]
            photo_id = store.photos()[0]["photo_id"]
            vectors = [tuple(row) for row in store.db.execute("SELECT * FROM image_embedding_results ORDER BY result_id")]
        moved = self.root / "moved"
        moved.mkdir()
        database = moved / "first.sqlite"
        shutil.move(self.database, database)
        shutil.move(self.source, moved / "photos")
        code, result = self.cli("management", "original", photo_id, database=database)
        self.assertEqual(code, 0, result)
        with SQLiteStorage.open(database) as store:
            self.assertEqual(store.album()["id"], album_id)
            self.assertTrue(store.photo(photo_id)["original_absolute_path"].startswith(str(moved)))
            self.assertEqual([tuple(row) for row in store.db.execute("SELECT * FROM image_embedding_results ORDER BY result_id")], vectors)
        code, scanned = self.cli("ingestion", str(moved / "photos"), database=database)
        self.assertEqual((code, scanned["added"], scanned["index_summary"]["counts"]["ready"]), (0, 0, 2))
        self.assertIsNone(scanned["index_prompt"])

    def test_readonly_browsing_never_checks_originals_or_imports_runtime(self):
        original_import = builtins.__import__
        def guard(name, *args, **kwargs):
            if name.split(".")[0] in ("torch", "transformers", "numpy", "tokenizers"):
                raise AssertionError("Optional runtime loaded during metadata operation")
            return original_import(name, *args, **kwargs)
        before = self.database.read_bytes()
        with patch("builtins.__import__", side_effect=guard), \
                patch("photography_lib.source_paths._probe", side_effect=AssertionError("Original checked")):
            for args in (("management", "open"), ("management", "photos"),
                         ("management", "search", "wide", "--mode", "metadata"), ("index", "profiles")):
                self.assertEqual(self.cli(*args)[0], 0)
        self.assertEqual(self.database.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
