"""Behavioral tests: fake image observations and HTTP, never production services."""
import copy
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout, closing
from pathlib import Path
from unittest.mock import patch

from test_analyze import SAMPLE, PROJECT
from photography_lib import Config, ingest, analyze
from photography_lib.analysis_settings import resolve_settings, make_provider
from photography_lib.analysis_planner import create_plan, confirm_plan
from photography_lib.analysis_execution import execute_plan, RateGate, summarize
from photography_lib.analysis_api import RequestFailure, parse_results, build_payload, retry_delay
from photography_lib.openai_batch import batch_action
from photography_lib.sqlite_storage import SQLiteStorage
from photography_lib.vision import ModelResult, AnalysisConfig
from photography_lib.cli import main
from photography_lib.config import PhotographyError
from photography_lib.workflow_cli import recover_interrupted


def envelope(data=None):
    return {"id": "response_fixture", "model": AnalysisConfig.model, "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(SAMPLE if data is None else data)}]}],
        "usage": {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130, "input_tokens_details": {"cached_tokens": 20}}}


class FakeBatchHTTP:
    def __init__(self):
        self.calls, self.rows, self.batches = [], [], []
        self.file_rows = {}
        self.fail_create = False
        self.reverse = True
        self.status = "completed"

    def call(self, path, **kwargs):
        self.calls.append((path, kwargs.get("method", "GET")))
        if path == "/files":
            content = kwargs["data"].split(b"Content-Type: application/jsonl\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            self.rows = [json.loads(line) for line in content.splitlines()]
            fid = "file_input_" + str(len(self.file_rows) + 1)
            self.file_rows[fid] = self.rows
            return {"id": fid}
        if path == "/batches":
            metadata = kwargs["data"]["metadata"]
            rid = "batch_remote_" + str(len(self.batches) + 1)
            self.batches.append({"id": rid, "metadata": metadata, "input_file_id": kwargs["data"]["input_file_id"]})
            if self.fail_create:
                raise RequestFailure("TIMEOUT", "Unknown submission", uncertain=True)
            return {"id": rid}
        if path.startswith("/batches?"):
            return {"data": self.batches, "has_more": False}
        if path.startswith("/batches/batch_remote_") and path.endswith("/cancel"):
            self.status = "cancelled"
            return {"status": "cancelled"}
        if path.startswith("/batches/batch_remote_"):
            index = int(path.rsplit("_", 1)[1])
            return {"id": path.split("/")[-1], "status": self.status, "output_file_id": "file_output_" + str(index)}
        if path.startswith("/files/file_output_") and path.endswith("/content"):
            index = int(path.split("/")[-2].rsplit("_", 1)[1])
            rows = self.file_rows[self.batches[index - 1]["input_file_id"]]
            rows = list(reversed(rows)) if self.reverse else rows
            return b"\n".join(json.dumps({"custom_id": r["custom_id"], "response": {"status_code": 200, "body": envelope()}}).encode() for r in rows)
        if path.startswith("/files/") and kwargs.get("method") == "DELETE":
            return {"deleted": True}
        raise AssertionError(path)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.photos = self.base / "photos"
        self.photos.mkdir()
        from PIL import Image
        for i in range(3):
            Image.new("RGB", (320, 160), (i * 70, 0, 120)).save(self.photos / f"{i}.jpg")
        self.config = Config(self.base / "state", thumbnail_size=256)
        scan = ingest(self.photos, config=self.config)
        self.ids, self.library = scan["changed_photo_ids"], scan["library_id"]
        self.store = SQLiteStorage(self.config.state_dir)
        self.addCleanup(self.store.close)
        env = patch.dict(os.environ, {"OPENAI_API_KEY": "test-fixture-key", "OPENAI_BASE_URL": "https://api.openai.com/v1", "PHOTOGRAPHY_MODEL": AnalysisConfig.model})
        env.start()
        self.addCleanup(env.stop)
        for name in ("photography_lib.analysis_api.OpenAIHTTP.call", "photography_lib.codex_vision.CodexCLIProvider.analyze"):
            guard = patch(name, side_effect=AssertionError("Unexpected model/network call"))
            guard.start()
            self.addCleanup(guard.stop)
        self.calls = 0

    def plan(self, ids=None, **settings):
        return create_plan(self.ids if ids is None else ids, store=self.store, config=self.config, settings=settings)

    def confirm(self, plan):
        return confirm_plan(self.store, plan["plan_id"], plan["digest"])

    def call(self, plan, images, provider):
        self.calls += 1
        if len(images) == 1:
            outputs, errors, usage = parse_results(envelope(), [images[0][0]])
        else:
            data = {"photos": [{"photo_id": pid, "analysis": copy.deepcopy(SAMPLE)} for pid, _ in reversed(images)]}
            outputs, errors, usage = parse_results(envelope(data), [pid for pid, _ in images])
        return outputs, errors, usage, .01

    def execute(self, plan, **kwargs):
        return execute_plan(plan["plan_id"], store=self.store, config=self.config, caller=kwargs.pop("caller", self.call), **kwargs)

    def cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--state-dir", str(self.config.state_dir), *args])
        return code, json.loads(output.getvalue())

    def test_plan_no_credentials_no_network_and_confirmation_required(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            plan = self.plan()
        self.assertEqual((plan["pending"], plan["status"], plan["model_calls"]), (3, "proposed", 0))
        with self.assertRaisesRegex(PhotographyError, "confirm"):
            self.execute(plan)
        self.assertEqual(self.calls, 0)

    def test_confirm_execute_and_complete_cache_no_key(self):
        plan = self.plan()
        self.confirm(plan)
        result = self.execute(plan)
        self.assertEqual(result["summary"]["counts"]["analyzed"], 3)
        self.assertEqual(self.execute(plan)["status"], "completed")
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            cached = self.plan()
            self.assertEqual((cached["cached"], cached["pending"]), (3, 0))
            self.assertEqual(self.execute(cached)["status"], "completed")
        self.assertEqual(self.calls, 3)

    def test_preserves_legacy_single_photo_cache(self):
        provider = make_provider(resolve_settings())
        with patch.object(provider, "analyze", return_value=ModelResult(copy.deepcopy(SAMPLE), AnalysisConfig.model)):
            analyze(self.ids[:1], provider=provider, config=self.config, storage=self.store)
        plan = self.plan()
        self.assertEqual((plan["cached"], plan["pending"]), (1, 2))
        changed = self.plan(model="another-model")
        self.assertEqual(changed["snapshots"][0]["reason"], "configuration_changed")

    def test_digest_and_provider_change_rejected(self):
        plan = self.plan()
        with self.assertRaises(PhotographyError):
            confirm_plan(self.store, plan["plan_id"], "wrong")
        self.confirm(plan)
        saved = self.store.plan(plan["plan_id"])
        saved["settings"]["model"] = "changed"
        self.store.save_plan(saved)
        with self.assertRaises(PhotographyError):
            self.execute(plan)
        self.assertEqual(self.calls, 0)

    def test_changed_input_does_not_expand_or_call(self):
        plan = self.plan(self.ids[:1])
        self.confirm(plan)
        photo = self.store.photo(self.ids[0])
        os.utime(photo["original_path"], ns=(photo["mtime_ns"], photo["mtime_ns"] + 2_000_000_000))
        result = self.execute(plan)
        self.assertEqual((self.calls, result["summary"]["counts"]["stale"]), (0, 1))
        self.assertEqual(len(result["results"]), 1)

    def test_changed_during_request_saves_usage_but_not_observation(self):
        plan = self.plan(self.ids[:1])
        self.confirm(plan)
        def call(*args):
            photo = self.store.photo(self.ids[0])
            os.utime(photo["original_path"], ns=(photo["mtime_ns"], photo["mtime_ns"] + 1_000_000_000))
            return self.call(*args)
        # Worker uses its own source-path snapshot; do not share sqlite across threads.
        path = self.store.photo(self.ids[0])["original_path"]
        def safe_call(*args):
            os.utime(path, None)
            return self.call(*args)
        result = self.execute(plan, caller=safe_call)
        self.assertEqual(result["summary"]["counts"]["stale"], 1)
        self.assertEqual(result["summary"]["known_usage"]["input_tokens"], 100)

    def test_rate_limit_retry_and_unknown_usage(self):
        plan = self.plan(self.ids[:1])
        self.confirm(plan)
        count = [0]
        def limited(*args):
            count[0] += 1
            if count[0] == 1:
                raise RequestFailure("RATE_LIMITED", "Wait", retryable=True)
            return self.call(*args)
        result = self.execute(plan, caller=limited)
        self.assertEqual((result["summary"]["attempt_count"], result["summary"]["retry_count"]), (2, 1))
        self.assertEqual(result["summary"]["unknown_usage_attempts"], 1)
        self.assertEqual(result["summary"]["known_usage"]["total_tokens"], 130)

    def test_long_retry_after_pauses_and_resume_keeps_cooldown(self):
        plan = self.plan(self.ids[:1], max_wait_seconds=0)
        self.confirm(plan)
        result = self.execute(plan, caller=lambda *_: (_ for _ in ()).throw(RequestFailure("RATE_LIMITED", "Wait", retryable=True, wait=120)))
        self.assertEqual(result["status"], "paused")
        resumed = self.execute(plan, resume=True)
        self.assertEqual((resumed["status"], self.calls), ("paused", 0))

    def test_uncertain_call_not_retried_and_recovery_keeps_unknown_usage(self):
        plan = self.plan(self.ids[:1])
        self.confirm(plan)
        result = self.execute(plan, caller=lambda *_: (_ for _ in ()).throw(RequestFailure("TIMEOUT", "Unknown", uncertain=True)))
        self.assertEqual((result["status"], result["summary"]["attempt_count"]), ("attention", 1))
        with self.assertRaises(PhotographyError):
            self.execute(plan, resume=True)
        result = recover_interrupted(plan["plan_id"], self.store)
        self.assertEqual((result["status"], result["summary"]["unknown_usage_attempts"]), ("partial", 1))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM analysis_claims").fetchone()[0], 0)

    def test_cross_plan_claim_prevents_duplicate_network(self):
        plan1, plan2 = self.plan(self.ids[:1]), self.plan(self.ids[:1])
        self.confirm(plan1)
        self.confirm(plan2)
        self.execute(plan1, caller=lambda *_: (_ for _ in ()).throw(RequestFailure("TIMEOUT", "Unknown", uncertain=True)))
        result = self.execute(plan2)
        self.assertEqual(self.calls, 0)
        self.assertEqual(result["summary"]["counts"]["failed"], 1)

    def test_multi_image_mapping_usage_and_group_cache(self):
        plan = self.plan(images_per_request=2)
        self.confirm(plan)
        result = self.execute(plan)
        self.assertEqual((self.calls, result["summary"]["counts"]["analyzed"]), (2, 3))
        self.assertEqual(result["summary"]["known_usage"]["input_tokens"], 200)
        record = self.store.analysis_records(self.ids[0])[-1]
        self.assertIsNone(record["usage"])
        again = self.plan(images_per_request=2)
        self.assertEqual((again["cached"], again["pending"]), (3, 0))
        regroup = self.plan(ids=list(reversed(self.ids)), images_per_request=2)
        self.assertGreater(regroup["pending"], 0)

    def test_multi_duplicate_and_missing_are_not_mapped_by_position(self):
        data = {"photos": [{"photo_id": self.ids[0], "analysis": SAMPLE}] * 2}
        output, errors, usage = parse_results(envelope(data), self.ids[:2])
        self.assertEqual(output, {})
        self.assertEqual({e["code"] for e in errors.values()}, {"DUPLICATE_RESULT", "MISSING_RESULT"})
        data["photos"] = [{"photo_id": "unknown", "analysis": SAMPLE}]
        with self.assertRaises(RequestFailure):
            parse_results(envelope(data), self.ids[:2])

    def test_batch_out_of_order_collection_and_idempotent_recollection(self):
        plan = self.plan(mode="batch")
        self.confirm(plan)
        http = FakeBatchHTTP()
        result = self.execute(plan, batch_http=http)
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(len(http.rows), 3)
        collected = batch_action(plan["plan_id"], store=self.store, config=self.config, action="collect", http=http)
        self.assertEqual(collected["summary"]["counts"]["analyzed"], 3)
        batch_action(plan["plan_id"], store=self.store, config=self.config, action="collect", http=http)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM analyses").fetchone()[0], 3)
        batch_action(plan["plan_id"], store=self.store, config=self.config, action="cleanup", http=http)
        self.assertEqual(sum(method == "DELETE" for _, method in http.calls), 2)

    def test_batch_unknown_submission_reconciled_without_second_post(self):
        plan = self.plan(mode="batch")
        self.confirm(plan)
        http = FakeBatchHTTP()
        http.fail_create = True
        result = self.execute(plan, batch_http=http)
        self.assertEqual(result["status"], "attention")
        with self.assertRaises(PhotographyError):
            recover_interrupted(plan["plan_id"], self.store)
        result = batch_action(plan["plan_id"], store=self.store, config=self.config, action="collect", http=http)
        self.assertEqual(result["summary"]["counts"]["analyzed"], 3)
        self.assertEqual(http.calls.count(("/batches", "POST")), 1)

    def test_batch_duplicate_output_no_arbitrary_selection(self):
        plan = self.plan(mode="batch")
        self.confirm(plan)
        http = FakeBatchHTTP()
        self.execute(plan, batch_http=http)
        http.rows.append(http.rows[0])
        result = batch_action(plan["plan_id"], store=self.store, config=self.config, action="collect", http=http)
        self.assertEqual((result["summary"]["counts"]["failed"], result["summary"]["counts"]["analyzed"]), (1, 2))

    def test_settings_saved_override_and_channel_reset(self):
        saved = resolve_settings(overrides={"provider": "codex", "model": "gpt-6-astra"})
        self.store.save_settings(saved)
        self.assertEqual(self.plan()["settings"]["provider"], "codex")
        switched = self.plan(provider="openai")
        self.assertEqual(switched["settings"]["model"], AnalysisConfig.model)
        self.assertEqual(resolve_settings(saved, {"model": "my-model"})["model"], "my-model")
        with self.assertRaises(PhotographyError):
            self.plan(provider="codex", mode="batch")

    def test_readonly_dry_run_no_plan_record_and_no_credential_probe(self):
        before = self.store.db.execute("SELECT COUNT(*) FROM analysis_plans").fetchone()[0]
        code, result = self.cli("analyze", "--library-id", self.library, "--all", "--dry-run")
        self.assertEqual((code, result["pending"]), (0, 3))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM analysis_plans").fetchone()[0], before)
        self.assertEqual(self.cli("analysis-execute", "missing")[0], 2)

    def test_rate_gate_dual_budget_and_oversized_request(self):
        settings = resolve_settings(overrides={"rpm": 2, "tpm": 10000, "input_tokens_per_photo": 1000})
        gate = RateGate(settings)
        gate.reserve(6000)
        self.assertGreater(gate.delay(6000), 0)
        self.assertEqual(gate.delay(1000), 0)
        with self.assertRaises(PhotographyError):
            gate.delay(10001)
        self.assertEqual(retry_delay({"Retry-After": "120"}), 120)

    def test_cost_unknown_and_user_estimate_discount(self):
        self.assertIsNone(self.plan()["execution"]["estimates"]["cost"])
        opts = dict(input_tokens_per_photo=1000, input_price=1, output_price=4)
        immediate = self.plan(**opts)
        batch = self.plan(mode="batch", **opts)
        self.assertAlmostEqual(batch["execution"]["estimates"]["cost"]["range"][1] * 2,
                               immediate["execution"]["estimates"]["cost"]["range"][1], places=6)

    def test_migration_v4_backup_and_rollback(self):
        self.store.db.execute("PRAGMA user_version=4")
        self.store.close()
        with patch("photography_lib.sqlite_storage.WORKFLOW_SCHEMA", ("THIS IS INVALID SQL",)):
            with self.assertRaises(PhotographyError):
                SQLiteStorage(self.config.state_dir)
        with closing(sqlite3.connect(self.config.state_dir / "photography.db")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.store = SQLiteStorage(self.config.state_dir)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertTrue(list((self.config.state_dir / "backups").glob("schema-v4-*.db")))
        self.assertEqual(len(self.store.photos_for_library(self.library)), 3)

    def test_batch_queue_partition_collect_then_resume(self):
        plan = self.plan(mode="batch", queue_tokens=2000, input_tokens_per_photo=1000)
        self.confirm(plan)
        http = FakeBatchHTTP()
        result = self.execute(plan, batch_http=http)
        self.assertEqual(result["summary"]["counts"]["pending"], 1)
        result = batch_action(plan["plan_id"], store=self.store, config=self.config, action="collect", http=http)
        self.assertEqual((result["status"], result["summary"]["counts"]["analyzed"]), ("paused", 2))
        self.execute(plan, resume=True, batch_http=http)
        result = batch_action(plan["plan_id"], store=self.store, config=self.config, action="collect", http=http)
        self.assertEqual((result["status"], result["summary"]["counts"]["analyzed"]), ("completed", 3))

    def test_batch_cancel_still_collects_completed_work(self):
        plan = self.plan(mode="batch")
        self.confirm(plan)
        http = FakeBatchHTTP()
        http.status = "in_progress"
        self.execute(plan, batch_http=http)
        result = batch_action(plan["plan_id"], store=self.store, config=self.config, action="cancel", http=http)
        self.assertNotEqual(result["status"], "completed")
        result = batch_action(plan["plan_id"], store=self.store, config=self.config, action="collect", http=http)
        self.assertEqual(result["summary"]["counts"]["analyzed"], 3)

    def test_batch_changed_input_and_missing_result_do_not_replace_analysis(self):
        plan = self.plan(mode="batch")
        self.confirm(plan)
        http = FakeBatchHTTP()
        self.execute(plan, batch_http=http)
        http.rows.pop()
        photo = self.store.photo(self.ids[0])
        os.utime(photo["original_path"], ns=(photo["mtime_ns"], photo["mtime_ns"] + 1_000_000_000))
        result = batch_action(plan["plan_id"], store=self.store, config=self.config, action="collect", http=http)
        self.assertEqual((result["summary"]["counts"]["stale"], result["summary"]["counts"]["failed"], result["summary"]["counts"]["analyzed"]), (1, 1, 1))

    def test_cached_input_changed_after_proposal_is_stale_on_execute(self):
        plan = self.plan(self.ids[:1])
        self.confirm(plan)
        self.execute(plan)
        cached = self.plan(self.ids[:1])
        photo = self.store.photo(self.ids[0])
        os.utime(photo["original_path"], ns=(photo["mtime_ns"], photo["mtime_ns"] + 1_000_000_000))
        result = self.execute(cached)
        self.assertEqual(result["summary"]["counts"]["stale"], 1)
        self.assertEqual(self.calls, 1)

    def test_rate_headers_reduce_budget_and_pause_dispatch(self):
        gate = RateGate(resolve_settings())
        gate.observe({"x-ratelimit-limit-requests": "2", "x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "1m2s"})
        self.assertEqual(gate.settings["rpm"], 2)
        self.assertGreater(gate.delay(0), 60)

    def test_bad_output_usage_is_preserved(self):
        bad = envelope()
        bad["status"] = "incomplete"
        with self.assertRaises(RequestFailure) as exc:
            parse_results(bad, self.ids[:1])
        self.assertEqual(exc.exception.usage["total_tokens"], 130)

    def test_readonly_html_report_escapes_and_does_not_execute(self):
        output = self.base / "proposal.html"
        code, result = self.cli("analysis-plan", "--library-id", self.library, "--html", str(output))
        self.assertEqual(code, 0)
        self.assertTrue(output.exists())
        self.assertIn("等待确认", output.read_text(encoding="utf-8"))
        self.assertEqual(result["model_calls"], 0)
