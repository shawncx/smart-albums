from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))
from photography_lib import Config, analyze, ingest
from photography_lib.analysis_schema import OUTPUT_SCHEMA, validate_analysis
from photography_lib.config import PhotographyError
from photography_lib.report import analysis_report
from photography_lib.sqlite_storage import SQLiteStorage
from photography_lib.vision import AnalysisConfig, ModelResult, OpenAIResponsesProvider, ProviderError

SAMPLE = {
    "subjects": ["蓝色测试图案"], "scene": "合成测试图",
    "composition": ["横向画幅"], "color": ["蓝色"], "lighting": [], "mood": [],
    "technical_observations": {"visible_issues": [], "assessment_scope": "preview",
                               "limitations": ["缩略图不能代表原图细节。"]},
    "visual_description": "仅用于离线测试的合成图案描述。", "tags": ["测试图"],
}


class FakeProvider:
    """Explicit test double; never used by the production CLI."""
    def __init__(self, callback=None, version="test-v1"):
        self.calls = 0
        self.callback, self.version = callback, version

    def profile(self):
        return {"provider": "unit-test", "version": self.version, "model": "test-model"}

    def check_ready(self):
        pass

    def analyze(self, preview):
        self.calls += 1
        if self.callback:
            return self.callback(preview, self.calls)
        return ModelResult(copy.deepcopy(SAMPLE), "test-model", usage={"total_tokens": 7})


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="photography-analysis-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.photos = self.base / "照片"
        self.photos.mkdir()
        self.config = Config(self.base / "state", thumbnail_size=256)
        for index in range(3):
            Image.new("RGB", (320, 160), (0, 0, 100 + index * 50)).save(self.photos / f"{index}.jpg")
        scan = ingest(self.photos, config=self.config)
        self.library_id = scan["library_id"]
        self.ids = scan["changed_photo_ids"]

    def run_analysis(self, ids=None, **kwargs):
        return analyze(self.ids if ids is None else ids, config=self.config,
                       provider=kwargs.pop("provider", FakeProvider()), **kwargs)

    def test_serial_results_persistence_cache_order_and_read_only_source(self):
        before = [(p.name, p.read_bytes(), p.stat().st_mtime_ns) for p in self.photos.iterdir()]
        provider = FakeProvider()
        result = self.run_analysis([self.ids[1], self.ids[0], self.ids[1]], provider=provider)
        self.assertEqual((result["requested"], result["analyzed"], result["failed"]), (2, 2, 0))
        self.assertEqual([item["photo_id"] for item in result["results"]], [self.ids[1], self.ids[0]])
        self.assertEqual(provider.calls, 2)
        cached = self.run_analysis([self.ids[0], self.ids[1]], provider=provider)
        self.assertEqual((cached["cached"], provider.calls), (2, 2))
        with SQLiteStorage(self.config.state_dir) as store:
            self.assertEqual(store.analysis_run(result["run_id"]), result)
            photo = store.photo(self.ids[0])
            record = store.analysis(cached["results"][0]["analysis_id"])
            self.assertFalse(photo["needs_analysis"])
            self.assertEqual(record["data"], SAMPLE)
            self.assertEqual(record["input_image_hash"], hashlib.sha256(store.thumbnail(photo["photo_id"])["data"]).hexdigest())
            self.assertEqual(record["content_version"], photo["content_version"])
            self.assertEqual(record["model"], "test-model")
        self.assertEqual(before, [(p.name, p.read_bytes(), p.stat().st_mtime_ns) for p in self.photos.iterdir()])

    def test_dry_run_does_not_call_provider_or_insert_runs(self):
        provider = FakeProvider()
        plan = self.run_analysis(provider=provider, dry_run=True)
        self.assertEqual((plan["status"], plan["pending"], provider.calls), ("ready", 3, 0))
        with SQLiteStorage(self.config.state_dir) as store:
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0], 0)
            self.assertTrue(all(photo["needs_analysis"] for photo in store.photos_for_library(self.library_id)))

    def test_missing_credential_blocks_without_fake_results(self):
        provider = OpenAIResponsesProvider(AnalysisConfig(api_key=""))
        plan = self.run_analysis(provider=provider, dry_run=True)
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["pending"], 3)
        with self.assertRaises(PhotographyError) as context:
            self.run_analysis(provider=provider)
        self.assertEqual(context.exception.code, "MODEL_CREDENTIAL_MISSING")
        with SQLiteStorage(self.config.state_dir) as store:
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM analyses").fetchone()[0], 0)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0], 0)

    def test_complete_cache_can_be_read_without_credential(self):
        provider = FakeProvider()
        self.run_analysis(provider=provider)
        provider.check_ready = lambda: (_ for _ in ()).throw(PhotographyError("MISSING", "No key"))
        self.assertEqual(self.run_analysis(provider=provider)["cached"], 3)
        self.assertEqual(provider.calls, 3)

    def test_invalid_output_isolated_from_valid_photos(self):
        def respond(preview, index):
            return ModelResult({"score": 100} if index == 2 else copy.deepcopy(SAMPLE), "test-model")
        result = self.run_analysis(provider=FakeProvider(respond))
        self.assertEqual((result["status"], result["analyzed"], result["failed"]), ("partial", 2, 1))
        self.assertEqual(result["results"][1]["error"]["code"], "INVALID_MODEL_OUTPUT")
        with SQLiteStorage(self.config.state_dir) as store:
            self.assertTrue(store.photo(self.ids[1])["needs_analysis"])
            self.assertEqual(store.analyses(self.ids[1])["items"], [])

    def test_force_keeps_history_and_failed_force_preserves_success(self):
        first = self.run_analysis([self.ids[0]])
        second = self.run_analysis([self.ids[0]], force=True)
        self.assertNotEqual(first["results"][0]["analysis_id"], second["results"][0]["analysis_id"])
        provider = FakeProvider(lambda *_: ModelResult({}, "test"))
        self.assertEqual(self.run_analysis([self.ids[0]], provider=provider, force=True)["status"], "failed")
        cached = self.run_analysis([self.ids[0]])
        self.assertEqual(cached["results"][0]["analysis_id"], second["results"][0]["analysis_id"])
        with SQLiteStorage(self.config.state_dir) as store:
            page = store.analyses(self.ids[0], limit=1)
            self.assertIsNotNone(page["next_cursor"])
            self.assertEqual(len(store.analyses(self.ids[0], after=page["next_cursor"])["items"]), 1)
            self.assertFalse(store.photo(self.ids[0])["needs_analysis"])

    def test_model_profile_and_input_changes_invalidate_cache(self):
        self.run_analysis([self.ids[0]])
        changed_model = self.run_analysis([self.ids[0]], provider=FakeProvider(version="test-v2"))
        self.assertEqual(changed_model["analyzed"], 1)
        with SQLiteStorage(self.config.state_dir) as store:
            photo = store.photo(self.ids[0])
        source = Path(photo["original_path"])
        Image.new("RGB", (310, 150), "yellow").save(source)
        os.utime(source, ns=(source.stat().st_atime_ns, photo["mtime_ns"] + 2_000_000_000))
        ingest(self.photos, config=self.config)
        self.assertEqual(self.run_analysis([self.ids[0]])["analyzed"], 1)
        with SQLiteStorage(self.config.state_dir) as store:
            history = store.analyses(self.ids[0])["items"]
            self.assertEqual([item["matches_indexed_photo"] for item in history], [False, False, True])

    def test_preview_profile_and_preview_bytes_both_invalidate_cache(self):
        self.run_analysis([self.ids[0]])
        config = Config(self.config.state_dir, thumbnail_size=128)
        ingest(self.photos, config=config)
        self.assertEqual(self.run_analysis([self.ids[0]])["analyzed"], 1)
        with SQLiteStorage(self.config.state_dir) as store:
            data = io.BytesIO()
            Image.new("RGB", (128, 64), "red").save(data, "JPEG")
            store.put_thumbnail(store.photo(self.ids[0]), data.getvalue())
        self.assertEqual(self.run_analysis([self.ids[0]])["analyzed"], 1)

    def test_changed_original_requires_rescan(self):
        with SQLiteStorage(self.config.state_dir) as store:
            photo = store.photo(self.ids[0])
        os.utime(photo["original_path"], ns=(photo["mtime_ns"], photo["mtime_ns"] + 2_000_000_000))
        provider = FakeProvider()
        result = self.run_analysis([self.ids[0]], provider=provider)
        self.assertEqual(result["results"][0]["error"]["code"], "PHOTO_CHANGED")
        self.assertEqual(provider.calls, 0)

    def test_concurrent_ingestion_during_model_call_never_saves_stale_analysis(self):
        def modify_source(preview, index):
            with SQLiteStorage(self.config.state_dir) as store:
                photo = store.photo(self.ids[0])
            Image.new("RGB", (400, 200), "orange").save(photo["original_path"])
            ingest(self.photos, config=self.config)  # Succeeds: no transaction spans the provider.
            return ModelResult(copy.deepcopy(SAMPLE), "test")
        result = self.run_analysis([self.ids[0]], provider=FakeProvider(modify_source))
        self.assertEqual(result["results"][0]["error"]["code"], "PHOTO_CHANGED")
        with SQLiteStorage(self.config.state_dir) as store:
            self.assertTrue(store.photo(self.ids[0])["needs_analysis"])
            self.assertEqual(store.analyses(self.ids[0])["items"], [])

    def test_preview_changed_during_model_call_is_rejected(self):
        def mutate_preview(preview, index):
            with SQLiteStorage(self.config.state_dir) as store:
                data = io.BytesIO()
                Image.new("RGB", (256, 128), "red").save(data, "JPEG")
                store.put_thumbnail(store.photo(self.ids[0]), data.getvalue())
            return ModelResult(copy.deepcopy(SAMPLE), "test")
        result = self.run_analysis([self.ids[0]], provider=FakeProvider(mutate_preview))
        self.assertEqual(result["results"][0]["error"]["code"], "PHOTO_CHANGED")

    def test_missing_error_unknown_and_corrupt_previews_are_per_photo_failures(self):
        with SQLiteStorage(self.config.state_dir) as store, store.transaction():
            photo = store.photo(self.ids[0])
            store.put_photo(dict(photo, state="missing"))
            photo = store.photo(self.ids[1])
            store.db.execute("UPDATE thumbnails SET data=? WHERE photo_id=?", (b"not-an-image", photo["photo_id"]))
        provider = FakeProvider()
        result = self.run_analysis([*self.ids, "unknown"], provider=provider)
        self.assertEqual((result["analyzed"], result["failed"], provider.calls), (1, 3, 1))
        self.assertEqual([item.get("error", {}).get("code") for item in result["results"]],
                         ["PHOTO_UNAVAILABLE", "INVALID_PREVIEW", None, "PHOTO_NOT_FOUND"])

    def test_fatal_auth_failure_does_not_repeat_for_whole_library(self):
        def denied(*_):
            raise ProviderError("MODEL_AUTH_FAILED", "Access denied", fatal=True)
        provider = FakeProvider(denied)
        result = self.run_analysis(provider=provider)
        self.assertEqual((result["status"], result["failed"], provider.calls), ("failed", 3, 1))

    def test_interrupted_batch_retains_completed_photos_and_can_resume(self):
        def interrupt(preview, index):
            if index == 2:
                raise KeyboardInterrupt()
            return ModelResult(copy.deepcopy(SAMPLE), "test")
        result = self.run_analysis(provider=FakeProvider(interrupt))
        self.assertEqual((result["analyzed"], result["failed"]), (1, 2))
        self.assertTrue(result["interrupted"])
        resumed = self.run_analysis()
        self.assertEqual((resumed["cached"], resumed["analyzed"]), (1, 2))

    def test_atomic_photo_analysis_and_run_checkpoint_rollback(self):
        with SQLiteStorage(self.config.state_dir) as store:
            original = store.save_analysis_run
            def fail_checkpoint(run):
                if run["analyzed"]:
                    raise sqlite3.OperationalError("simulated disk failure")
                return original(run)
            with patch.object(store, "save_analysis_run", side_effect=fail_checkpoint):
                with self.assertRaises(PhotographyError):
                    self.run_analysis([self.ids[0]], storage=store)
            self.assertEqual(store.analyses(self.ids[0])["items"], [])
            self.assertTrue(store.photo(self.ids[0])["needs_analysis"])

    def test_v1_migration_backups_and_preserves_records(self):
        with SQLiteStorage(self.config.state_dir) as store:
            original = store.photos_for_library(self.library_id)
            store.db.execute("DROP TABLE analyses")
            store.db.execute("DROP TABLE analysis_runs")
            store.db.execute("PRAGMA user_version=1")
        with SQLiteStorage(self.config.state_dir) as store:
            self.assertEqual(store.photos_for_library(self.library_id), original)
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 5)
        backups = list((self.config.state_dir / "backups").glob("*.db"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM photos").fetchone()[0], 3)

    def test_invalid_arguments_schema_and_mismatched_preview_are_rejected(self):
        for ids in ([], "a", [None], [""], {"photo_id": "a"}):
            with self.subTest(ids=ids), self.assertRaises(PhotographyError):
                self.run_analysis(ids)
        for mutate in (lambda x: x.update(score=10), lambda x: x.update(subjects="person"),
                       lambda x: x["technical_observations"].update(assessment_scope="original"),
                       lambda x: x["technical_observations"].update(limitations=[])):
            data = copy.deepcopy(SAMPLE)
            mutate(data)
            with self.assertRaises(PhotographyError):
                validate_analysis(data)
        with SQLiteStorage(self.config.state_dir) as store, store.transaction():
            photo = store.photo(self.ids[0])
            store.db.execute("UPDATE thumbnails SET content_version='old' WHERE photo_id=?", (photo["photo_id"],))
        self.assertEqual(self.run_analysis([self.ids[0]])["results"][0]["error"]["code"], "INVALID_PREVIEW")

    def test_cli_dry_run_sample_all_and_missing_key_exit_codes(self):
        env = {**os.environ, "OPENAI_API_KEY": "", "OPENAI_BASE_URL": "https://api.openai.com/v1"}
        command = [sys.executable, str(PROJECT / "photography/scripts/photography.py"),
                   "--state-dir", str(self.config.state_dir), "analyze", "--library-id", self.library_id]
        for options, count in ((["--limit", "2"], 2), (["--all"], 3)):
            process = subprocess.run([*command, "--dry-run", *options], cwd=self.base,
                                     env=env, capture_output=True, encoding="utf-8")
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual((result["status"], result["pending"]), ("proposed", count))
        process = subprocess.run(command, env=env, capture_output=True, encoding="utf-8")
        self.assertEqual(json.loads(process.stdout)["model_calls"], 0)
        self.assertEqual(json.loads(process.stdout)["status"], "proposed")

    def test_report_uses_only_matching_saved_results_and_escapes_model_text(self):
        data = copy.deepcopy(SAMPLE)
        data["visual_description"] = '<script>alert("test")</script>'
        provider = FakeProvider(lambda *_: ModelResult(data, "test-model"))
        self.run_analysis([self.ids[0]], provider=provider)
        output = self.base / "report.html"
        with SQLiteStorage(self.config.state_dir) as store:
            result = analysis_report(self.library_id, output, config=self.config, store=store, analysis_config=provider)
            self.assertEqual((result["analyzed"], result["pending"]), (1, 2))
            page = output.read_text(encoding="utf-8")
            self.assertNotIn(data["visual_description"], page)
            self.assertIn("&lt;script&gt;", page)
            self.assertEqual(page.count("data:image/jpeg;base64,"), 3)
            self.assertEqual(analysis_report(self.library_id, output, config=self.config, store=store)["analyzed"], 1)
            self.assertEqual(analysis_report(self.library_id, output, config=self.config, store=store,
                                            analysis_config=FakeProvider(version="other"))["needs_update"], 1)
            for forbidden in (self.photos / "report.html", self.config.state_dir / "report.html"):
                with self.assertRaises(PhotographyError):
                    analysis_report(self.library_id, forbidden, config=self.config, store=store)
                self.assertFalse(forbidden.exists())


class OpenAIAdapterTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.responses = []
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                parent.requests.append((self.path, self.headers["Authorization"], json.loads(body)))
                status, data = parent.responses.pop(0)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode("utf-8"))
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.provider = OpenAIResponsesProvider(AnalysisConfig(
            api_key="unit-test-secret", base_url=f"http://127.0.0.1:{server.server_port}/v1", retries=1))

    @staticmethod
    def envelope(**overrides):
        return {"id": "test-response", "model": "test-model-snapshot", "status": "completed",
                "output": [{"type": "reasoning", "summary": []},
                           {"type": "message", "content": [{"type": "output_text", "text": json.dumps(SAMPLE)}]}],
                "usage": {"input_tokens": 13, "output_tokens": 4, "total_tokens": 17, "unexpected": "omit"},
                **overrides}

    def test_real_http_request_contract_and_response_parsing(self):
        self.responses.append((200, self.envelope()))
        output = self.provider.analyze(b"test-preview-bytes")
        path, authorization, payload = self.requests[0]
        self.assertEqual((path, authorization), ("/v1/responses", "Bearer unit-test-secret"))
        self.assertFalse(payload["store"])
        self.assertEqual(payload["text"]["format"]["schema"], OUTPUT_SCHEMA)
        self.assertTrue(payload["text"]["format"]["strict"])
        image = payload["input"][0]["content"][1]
        self.assertEqual(base64.b64decode(image["image_url"].split(",", 1)[1]), b"test-preview-bytes")
        self.assertEqual(image["detail"], "high")
        self.assertEqual(validate_analysis(output.data), SAMPLE)
        self.assertEqual(output.model, "test-model-snapshot")
        self.assertEqual(output.usage, {"input_tokens": 13, "output_tokens": 4, "total_tokens": 17})
        self.assertNotIn("unit-test-secret", repr(self.provider.config))
        self.assertNotIn("unit-test-secret", json.dumps(self.provider.profile()))

    def test_bounded_retry_then_success(self):
        self.responses.extend([(429, {}), (200, self.envelope())])
        with patch("photography_lib.vision.time.sleep") as sleep:
            self.provider.analyze(b"preview")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(sleep.call_count, 1)

    def test_retry_exhaustion_and_sanitized_auth_error(self):
        self.responses.extend([(503, {}), (503, {})])
        with patch("photography_lib.vision.time.sleep"), self.assertRaises(ProviderError) as error:
            self.provider.analyze(b"preview")
        self.assertEqual(error.exception.code, "MODEL_HTTP_ERROR")
        self.responses.append((401, {"error": "unit-test-secret must never escape"}))
        with self.assertRaises(ProviderError) as error:
            self.provider.analyze(b"preview")
        self.assertTrue(error.exception.fatal)
        self.assertNotIn("unit-test-secret", str(error.exception))
        self.assertEqual(len(self.requests), 3)

    def test_refused_incomplete_malformed_and_wrong_schema_responses(self):
        variants = [
            (self.envelope(output=[{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]), "MODEL_REFUSED"),
            (self.envelope(status="incomplete"), "MODEL_INCOMPLETE"),
            (self.envelope(output="bad"), "INVALID_MODEL_OUTPUT"),
            (self.envelope(output=[{"type": "message", "content": [{"type": "output_text", "text": "not json"}]}]), "INVALID_MODEL_OUTPUT"),
        ]
        for response, code in variants:
            with self.subTest(code=code):
                self.responses.append((200, response))
                with self.assertRaises(ProviderError) as error:
                    self.provider.analyze(b"preview")
                self.assertEqual(error.exception.code, code)
        self.assertEqual(len(self.requests), len(variants))

    def test_cli_uses_production_adapter_against_local_fixture_service(self):
        with tempfile.TemporaryDirectory(prefix="photography-cli-analysis-") as temporary:
            base = Path(temporary).resolve()
            photos = base / "photos"
            photos.mkdir()
            Image.new("RGB", (40, 20), "navy").save(photos / "fixture.jpg")
            config = Config(base / "state")
            scan = ingest(photos, config=config)
            self.responses.append((200, self.envelope()))
            env = {**os.environ, "OPENAI_API_KEY": "unit-test-secret", "OPENAI_BASE_URL": self.provider.config.base_url}
            command = [sys.executable, str(PROJECT / "photography/scripts/photography.py"),
                       "--state-dir", str(config.state_dir), "analyze", "--library-id", scan["library_id"]]
            for expected in ("analyzed", "cached"):
                process = subprocess.run(command, cwd=base, env=env, capture_output=True, encoding="utf-8")
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                result = json.loads(process.stdout)
                if expected == "analyzed":
                    self.assertEqual(len(self.requests), 0)
                    execute = [*command[:4], "analysis-execute", result["plan_id"], "--confirm", result["digest"]]
                    process = subprocess.run(execute, cwd=base, env=env, capture_output=True, encoding="utf-8")
                    self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                    result = json.loads(process.stdout)
                    self.assertEqual(result["summary"]["counts"][expected], 1)
                else:
                    self.assertEqual(result[expected], 1)
                self.assertNotIn("unit-test-secret", process.stdout)
            self.assertEqual(len(self.requests), 1)

    def test_config_profile_tracks_output_settings_but_not_secret_or_timeout(self):
        a = AnalysisConfig(api_key="a")
        self.assertEqual(a.profile(), AnalysisConfig(api_key="b", timeout=10).profile())
        for change in ({"model": "different"}, {"language": "en"}, {"detail": "low"}, {"max_output_tokens": 3000}):
            self.assertNotEqual(a.profile(), AnalysisConfig(**change).profile())
        for base_url in ("http://example.com/v1", "https://name:secret@example.com/v1", "https://example.com?key=secret"):
            with self.assertRaises(PhotographyError):
                AnalysisConfig(base_url=base_url)


if __name__ == "__main__":
    unittest.main()
