"""Review persistence contracts using only disposable synthetic albums and previews."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

from PIL import Image
from photography_lib.config import PhotographyError
from photography_lib.fingerprints import fingerprint
from photography_lib.image_embedding_storage import IMAGE_EMBEDDING_SCHEMA
from photography_lib.image_feature_storage import IMAGE_FEATURE_SCHEMA
from photography_lib.review_schema_v1 import DIMENSIONS, overall_score, review_profile
from photography_lib.review_storage import REVIEW_TABLES, review_schema_registry
from photography_lib.sqlite_storage import APPLICATION_ID, REQUIRED_COLUMNS, SCHEMA, SQLiteStorage
from photography_lib.virtual_folder_storage import VIRTUAL_FOLDER_SCHEMA


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ReviewStorageTests(unittest.TestCase):
    def setUp(self):
        self.base = PROJECT / (".review-storage-tests-" + uuid4().hex)
        self.base.mkdir()
        self.addCleanup(shutil.rmtree, self.base)
        self.path = self.base / "reviews.sqlite"
        self.store = SQLiteStorage.create(self.path)
        self.addCleanup(self.store.close)
        self.profile = review_profile("synthetic-vision", "zh-CN")
        output = io.BytesIO()
        Image.new("RGB", (32, 16), "navy").save(output, format="JPEG")
        self.preview = output.getvalue()
        for photo_id in ("a", "b", "c"):
            photo = {"photo_id": photo_id, "original_absolute_path": str(self.base / "missing" / (photo_id + ".jpg")),
                     "content_version": hashlib.sha256(b"synthetic original").hexdigest(),
                     "thumbnail_profile": "preview-v1-srgb-128-q85", "size_bytes": 123, "mtime_ns": 1,
                     "metadata": {}, "ingest_state": "available", "original_status": "not_checked",
                     "created_at": "2026-09-07T00:00:00+00:00", "updated_at": "2026-09-07T00:00:00+00:00"}
            self.store.put_photo(photo)
            self.store.put_thumbnail(photo, self.preview)

    def manifest(self, photo_id):
        thumb = self.store.thumbnail(photo_id, include_data=False)
        return {"input_scope": "stored_thumbnail", "content_version": thumb["content_version"],
                "thumbnail_profile": thumb["profile"], "input_image_hash": thumb["image_hash"],
                "width": thumb["width"], "height": thumb["height"], "size_bytes": thumb["size_bytes"]}

    @staticmethod
    def payload(offset=0):
        scores = {key: {"score": 6 + index / 2 + offset, "reason": "清晰的视觉选择 " + key}
                  for index, key in enumerate(DIMENSIONS)}
        return {"schema_version": "photo-review-v1", "description": "蓝色背景中的平衡构图。",
                "strengths": ["层次清楚"], "improvements": ["调整边缘留白"],
                "limitations": ["仅评估缩略图，不能判断原图细节。"],
                "scores": scores, "overall_score": overall_score(scores)}

    def plan(self, photo_ids=("a",), *, batch_size=4, force=False, cached=None, save=True, profile=None):
        profile = profile or self.profile
        items = []
        for photo_id in photo_ids:
            manifest = self.manifest(photo_id)
            result_id = (cached or {}).get(photo_id)
            items.append({"photo_id": photo_id, "input_manifest": manifest,
                          "input_fingerprint": fingerprint(manifest), "action": "reuse" if result_id else "review",
                          "result_id": result_id})
        pending = [item for item in items if item["action"] == "review"]
        batches = [{"batch_id": "review_batch_" + uuid4().hex, "ordinal": index // batch_size,
                    "items": pending[index:index + batch_size]} for index in range(0, len(pending), batch_size)]
        plan = {"version": "ai-review-plan-v1", "run_id": "review_" + uuid4().hex,
                "album_id": self.store.album()["id"], "created_at": "2026-09-07T00:00:00+00:00",
                "profile": profile, "profile_id": fingerprint(profile), "batch_size": batch_size,
                "force": force, "items": items, "batches": batches,
                "counts": {"total": len(items), "cached": len(items) - len(pending),
                           "pending": len(pending), "batches": len(batches)},
                "max_image_bytes": 20 * 1024 * 1024, "disclosure": {"input": "stored previews only"}}
        plan["digest"] = fingerprint(plan)
        if save:
            self.store.create_review_run(plan)
        return plan

    def put(self, plan, *, photo_id="a", payload=None, metadata=None):
        batch = next(batch for batch in plan["batches"] if any(item["photo_id"] == photo_id for item in batch["items"]))
        item = next(item for item in batch["items"] if item["photo_id"] == photo_id)
        if next(saved for saved in self.store.review_batches(plan["run_id"])
                if saved["batch_id"] == batch["batch_id"])["status"] != "completed":
            self.store.update_review_run(plan["run_id"], status="running", confirmed_digest=plan["digest"])
            self.store.update_review_batch(batch["batch_id"], status="running")
        return self.store.put_review_result(photo_id, plan["run_id"], batch["batch_id"], plan["profile"],
                                            item["input_manifest"], payload or self.payload(),
                                            metadata if metadata is not None else {"provider": "github-copilot", "output_tokens": 42})

    def complete(self, plan, *, payload=None):
        results = []
        for batch in plan["batches"]:
            with self.store.transaction():
                for item in batch["items"]:
                    results.append(self.put(plan, photo_id=item["photo_id"], payload=payload))
                self.store.update_review_batch(batch["batch_id"], status="completed")
        self.store.update_review_run(plan["run_id"], status="completed")
        return results

    def test_exact_schema_and_typed_score_index_registration(self):
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 12)
        tables = {row[0] for row in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
        self.assertEqual(tables, set(REQUIRED_COLUMNS))
        self.assertEqual(len(tables), 40)
        self.assertEqual(set(REVIEW_TABLES), {"ai_review_results", "ai_review_runs", "ai_review_batches"})
        columns = {row["name"]: row for row in self.store.db.execute("PRAGMA table_info(ai_review_results)")}
        for key in ("overall", *DIMENSIONS):
            self.assertEqual(columns[key + "_score"]["type"], "REAL")
            index = self.store.db.execute("SELECT sql FROM sqlite_master WHERE name=?", ("ai_review_" + key + "_score",)).fetchone()
            self.assertIsNotNone(index)
        self.assertEqual(columns["description"]["type"], "TEXT")
        self.store.validate_review_schema()

    def test_run_batch_initial_state_and_frozen_plan_round_trip(self):
        plan = self.plan(("a", "b", "c"), batch_size=2)
        run = self.store.review_run(plan["run_id"])
        self.assertEqual(run["plan"], plan)
        self.assertEqual((run["status"], run["revision"], run["request_attempts"], run["confirmed_digest"], run["approval_history"]),
                         ("planned", 0, 0, None, []))
        self.assertEqual(run["digest"], plan["digest"])
        batches = self.store.review_batches(plan["run_id"])
        self.assertEqual([batch["items"] for batch in batches], [batch["items"] for batch in plan["batches"]])
        self.assertEqual([batch["ordinal"] for batch in batches], [0, 1])
        self.assertTrue(all(batch["status"] == "pending" and batch["attempts"] == 0 and batch["error"] is None for batch in batches))
        self.assertTrue(run["created_at"] and run["updated_at"] and batches[0]["created_at"] and batches[0]["updated_at"])
        run["plan"]["profile"]["model"] = "changed"
        self.assertEqual(self.store.review_run(plan["run_id"])["plan"], plan)

    def test_sql_queries_survive_reopen_backup_and_missing_originals(self):
        plan = self.plan()
        result_id = self.complete(plan)[0]
        self.assertFalse((self.base / "missing").exists())
        self.store.close()
        backup = self.base / "backup.sqlite"
        with SQLiteStorage.open(self.path) as reopened:
            with patch.object(Path, "open", side_effect=AssertionError("Original files must not be read")):
                result = reopened.review_result(result_id)
                self.assertEqual(result["payload"], self.payload())
                self.assertEqual(result["profile"], self.profile)
                self.assertEqual(result["input_manifest"], plan["items"][0]["input_manifest"])
                self.assertEqual(result["metadata"]["output_tokens"], 42)
                self.assertEqual(reopened.review_history("a"), [result])
                self.assertEqual(reopened.find_review_result("a", plan["profile_id"], result["input_fingerprint"]), result)
            reopened.backup(backup)
        for path in (self.path, backup):
            with self.subTest(path=path), SQLiteStorage.open(path) as reopened:
                self.assertEqual(reopened.review_result(result_id), result)
            with closing(sqlite3.connect(path)) as connection:
                row = connection.execute("""SELECT description,overall_score,composition_score,technical_score,
                    json_extract(payload_json,'$.scores.composition.reason'),
                    json_extract(payload_json,'$.strengths[0]'),json_extract(payload_json,'$.improvements[0]'),
                    json_extract(payload_json,'$.limitations[0]'),provider,model,language,schema_version
                    FROM ai_review_results WHERE overall_score>=7 AND composition_score>=6""").fetchone()
                self.assertEqual(row[:4], (self.payload()["description"], 7.25, 6.0, 8.5))
                self.assertEqual(row[4:8], ("清晰的视觉选择 composition", "层次清楚", "调整边缘留白", "仅评估缩略图，不能判断原图细节。"))
                self.assertEqual(row[8:], ("github-copilot", "synthetic-vision", "zh-CN", "photo-review-v1"))
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_force_appends_history_and_cache_uses_latest_with_stable_cursor(self):
        ids = []
        with patch("photography_lib.review_storage._timestamp", return_value="2026-09-07T12:00:00.000000+00:00"):
            for index in range(3):
                plan = self.plan(force=index > 0)
                ids.extend(self.complete(plan, payload=self.payload(index / 10)))
        self.assertEqual(ids, sorted(ids))
        ordered = [row[0] for row in self.store.db.execute(
            "SELECT result_id FROM ai_review_results ORDER BY created_at DESC,result_id DESC")]
        self.assertEqual(ordered, list(reversed(ids)))
        newest = self.store.find_review_result("a", plan["profile_id"], plan["items"][0]["input_fingerprint"])
        self.assertEqual(newest["result_id"], ids[-1])
        page = self.store.review_history("a", limit=2)
        self.assertEqual([item["result_id"] for item in page], [ids[2], ids[1]])
        self.assertEqual([item["result_id"] for item in self.store.review_history("a", after=page[-1]["result_id"])], [ids[0]])
        self.assertEqual(self.store.review_history("a", after=ids[0]), [])
        self.assertEqual(self.store.review_history("b"), [])
        self.assertIsNone(self.store.find_review_result("b", plan["profile_id"], plan["items"][0]["input_fingerprint"]))

    def test_language_and_input_identity_isolate_cache(self):
        plan = self.plan()
        old_id = self.complete(plan)[0]
        english = review_profile("synthetic-vision", "en")
        self.assertIsNone(self.store.find_review_result("a", fingerprint(english), plan["items"][0]["input_fingerprint"]))
        self.assertIsNone(self.store.find_review_result("a", plan["profile_id"], "0" * 64))
        other = self.plan(profile=english)
        self.complete(other)
        self.assertEqual(self.store.find_review_result("a", plan["profile_id"], plan["items"][0]["input_fingerprint"])["result_id"], old_id)

    def test_decimal_half_up_payloads_survive_sqlite_binary_rounding_boundaries(self):
        values = ([6.996, 8.966, 5.27, 8.855, 7.261, 8.222],
                  [7.594999999999999] * 6, [7.595] * 6, [0.0] * 6, [10.0] * 6)
        for scores in values:
            payload = self.payload()
            for key, score in zip(DIMENSIONS, scores):
                payload["scores"][key]["score"] = score
            payload["overall_score"] = overall_score(payload["scores"])
            with self.subTest(scores=scores):
                result_id = self.complete(self.plan(force=True), payload=payload)[0]
                self.assertEqual(self.store.review_result(result_id)["payload"], payload)

    def test_cached_selection_keeps_result_references_and_needs_no_batches(self):
        first = self.plan()
        result_id = self.complete(first)[0]
        cached = self.plan(cached={"a": result_id}, save=False)
        cached["max_image_bytes"] = 0
        cached["digest"] = fingerprint({key: value for key, value in cached.items() if key != "digest"})
        self.store.create_review_run(cached)
        self.assertEqual(cached["counts"], {"total": 1, "cached": 1, "pending": 0, "batches": 0})
        self.assertEqual(self.store.review_batches(cached["run_id"]), [])
        self.assertEqual(self.store.review_run_results(cached["run_id"]), [])
        self.store.update_review_run(cached["run_id"], status="completed")
        self.assertEqual(self.store.review_run(cached["run_id"])["plan"]["items"][0]["result_id"], result_id)
        wrong = self.plan(("b",), cached={"b": result_id}, save=False)
        with self.assertRaises(PhotographyError):
            self.store.create_review_run(wrong)

    def test_invalid_plans_never_leave_partial_runs_or_batches(self):
        valid = self.plan(("a", "b"), batch_size=1, save=False)
        mutations = [
            lambda p: p.update(album_id=str(uuid4())),
            lambda p: p.update(profile_id="0" * 64),
            lambda p: p.update(force="yes"),
            lambda p: p.update(batch_size=True),
            lambda p: p.update(counts={"total": 2, "cached": 0, "pending": True, "batches": 2}),
            lambda p: p["items"][0].update(input_fingerprint="0" * 64),
            lambda p: p["items"][0]["input_manifest"].update(width=0),
            lambda p: p["items"][0]["input_manifest"].update(original_absolute_path="not allowed"),
            lambda p: p["batches"].reverse(),
            lambda p: p["batches"].pop(),
            lambda p: p["batches"][0].update(items=[copy.deepcopy(p["items"][1])]),
            lambda p: p["items"].append(copy.deepcopy(p["items"][0])),
            lambda p: p["profile"].update(model="auto"),
        ]
        for change in mutations:
            candidate = copy.deepcopy(valid)
            change(candidate)
            candidate["digest"] = fingerprint({key: value for key, value in candidate.items() if key != "digest"})
            with self.subTest(change=change), self.assertRaises(PhotographyError):
                self.store.create_review_run(candidate)
        with self.assertRaises(PhotographyError):
            self.store.create_review_run({**valid, "digest": "0" * 64})
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM ai_review_runs").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM ai_review_batches").fetchone()[0], 0)

    def test_payload_validation_and_batch_provenance_reject_invalid_api_writes(self):
        plan = self.plan()
        other = self.plan(("b",))
        invalid_payloads = []
        for key, value in (("photo_id", "a"), ("overall_score", 9), ("description", " "),
                           ("strengths", []), ("limitations", [False]), ("schema_version", "unknown")):
            invalid_payloads.append({**self.payload(), key: value})
        for value in (True, None, float("nan"), float("inf"), -1, 11, "8"):
            payload = self.payload()
            payload["scores"]["technical"]["score"] = value
            invalid_payloads.append(payload)
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(PhotographyError):
                self.put(plan, payload=payload)
        calls = [
            ("b", plan["run_id"], plan["batches"][0]["batch_id"], self.profile, self.manifest("b")),
            ("a", plan["run_id"], other["batches"][0]["batch_id"], self.profile, self.manifest("a")),
            ("a", plan["run_id"], plan["batches"][0]["batch_id"], review_profile("other-model"), self.manifest("a")),
            ("a", plan["run_id"], plan["batches"][0]["batch_id"], self.profile, {**self.manifest("a"), "width": 31}),
        ]
        for args in calls:
            with self.subTest(args=args), self.assertRaises(PhotographyError):
                self.store.put_review_result(*args, self.payload(), {})
        for metadata in ([], {"model": "different-model"}, {"credits": float("nan")}):
            with self.subTest(metadata=metadata), self.assertRaises(PhotographyError):
                self.put(plan, metadata=metadata)
        self.assertEqual(self.store.review_history("a"), [])

    def test_direct_sql_checks_reject_invalid_payloads_and_projection_mismatches(self):
        plan = self.plan(("a", "b"))
        result_id = self.put(plan)
        template = dict(self.store.db.execute("SELECT * FROM ai_review_results WHERE result_id=?", (result_id,)).fetchone())
        template["photo_id"] = "b"
        template["result_id"] = template["result_id"][:-32] + uuid4().hex
        mutations = [
            {"description": "disagrees"}, {"composition_score": 5.5}, {"overall_score": 9},
            {"payload_json": "not JSON"}, {"payload_json": "null"},
            {"payload_json": encoded({**self.payload(), "unexpected": "field"})},
            {"payload_json": encoded({**self.payload(), "strengths": [True]})},
            {"payload_json": encoded({**self.payload(), "limitations": ["\u2003"]})},
            {"payload_json": encoded({**self.payload(), "improvements": ["x" * 4001]})},
            {"payload_json": encoded({**self.payload(), "description": None})},
            {"payload_json": encoded(self.payload())[:-1] + ',"description":"duplicate"}'},
            {"profile_id": "0" * 64}, {"input_fingerprint": "0" * 64},
            {"model": "different-model"}, {"language": "en"}, {"width": 31},
            {"input_manifest_json": encoded({**self.manifest("b"), "width": 31}), "width": 31},
            {"metadata_json": "[]"}, {"metadata_json": encoded({"model": "wrong"})},
            {"batch_id": "missing"}, {"run_id": "missing"}, {"photo_id": "c"},
            {"result_id": "unsortable"}, {"created_at": "not-a-timestamp"},
        ]
        payload = self.payload()
        payload["scores"]["composition"]["score"] = True
        mutations.append({"payload_json": encoded(payload), "composition_score": 1.0})
        payload = self.payload()
        payload["scores"]["composition"].pop("reason")
        mutations.append({"payload_json": encoded(payload)})
        for mutation in mutations:
            row = {**template, **mutation}
            with self.subTest(mutation=mutation), self.assertRaises((sqlite3.IntegrityError, sqlite3.OperationalError)):
                self.store.db.execute("INSERT INTO ai_review_results (" + ",".join(row) + ") VALUES ("
                                      + ",".join("?" for _ in row) + ")", tuple(row.values()))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM ai_review_results").fetchone()[0], 1)
        self.store.db.execute("INSERT INTO ai_review_results (" + ",".join(template) + ") VALUES ("
                              + ",".join("?" for _ in template) + ")", tuple(template.values()))
        self.assertEqual(self.store.review_result(template["result_id"])["photo_id"], "b")

    def test_immutable_results_frozen_plans_batches_and_unique_batch_photo(self):
        plan = self.plan()
        result_id = self.put(plan)
        mutations = [
            ("UPDATE ai_review_results SET description=description WHERE result_id=?", result_id),
            ("DELETE FROM ai_review_results WHERE result_id=?", result_id),
            ("UPDATE ai_review_runs SET plan_json=plan_json WHERE run_id=?", plan["run_id"]),
            ("DELETE FROM ai_review_runs WHERE run_id=?", plan["run_id"]),
            ("UPDATE ai_review_batches SET items_json=items_json WHERE batch_id=?", plan["batches"][0]["batch_id"]),
            ("DELETE FROM ai_review_batches WHERE batch_id=?", plan["batches"][0]["batch_id"]),
        ]
        for sql, identity in mutations:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.store.db.execute(sql, (identity,))
        with self.assertRaises(PhotographyError):
            self.put(plan)
        self.store.update_review_batch(plan["batches"][0]["batch_id"], status="completed")
        with self.assertRaises(PhotographyError):
            self.store.update_review_batch(plan["batches"][0]["batch_id"], status="pending")

    def test_successful_batch_and_results_are_atomic_with_caller_transaction(self):
        plan = self.plan(("a", "b"))
        batch_id = plan["batches"][0]["batch_id"]
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.store.transaction():
                self.put(plan, photo_id="a")
                self.put(plan, photo_id="b")
                self.store.update_review_batch(batch_id, status="completed")
                raise RuntimeError("abort whole batch")
        self.assertEqual(self.store.review_history("a"), [])
        self.assertEqual(self.store.review_history("b"), [])
        self.assertEqual(self.store.review_batches(plan["run_id"])[0]["status"], "pending")
        self.assertEqual(len(self.complete(plan)), 2)
        self.assertEqual(self.store.review_batches(plan["run_id"])[0]["status"], "completed")

    def test_failed_batch_preserves_previous_complete_batch_without_partial_result(self):
        plan = self.plan(("a", "b", "c"), batch_size=1)
        first, second, third = plan["batches"]
        with self.store.transaction():
            first_result = self.put(plan)
            self.store.update_review_batch(first["batch_id"], status="completed", attempts=1)
        with self.assertRaises(PhotographyError):
            with self.store.transaction():
                self.put(plan, photo_id="b")
                self.put(plan, photo_id="c", payload={**self.payload(), "overall_score": 0})
                self.store.update_review_batch(second["batch_id"], status="completed")
        self.store.update_review_batch(second["batch_id"], status="failed", attempts=1, error={"code": "REVIEW_RESPONSE_INVALID"})
        self.store.update_review_run(plan["run_id"], status="partial", revision=2, request_attempts=2)
        self.assertEqual(self.store.review_result(first_result)["photo_id"], "a")
        self.assertEqual(self.store.review_history("b"), [])
        self.assertEqual(self.store.review_history("c"), [])
        batches = self.store.review_batches(plan["run_id"])
        self.assertEqual([batch["status"] for batch in batches], ["completed", "failed", "pending"])
        self.assertEqual(batches[2]["batch_id"], third["batch_id"])

    def test_progress_validation_and_completion_guards(self):
        plan = self.plan()
        run_id, batch_id = plan["run_id"], plan["batches"][0]["batch_id"]
        approval = [{"digest": plan["digest"], "revision": 1}]
        self.store.update_review_run(run_id, status="running", revision=1, request_attempts=1, elapsed_seconds=0.5,
                                     confirmed_digest=plan["digest"], approval_history=approval)
        self.store.update_review_batch(batch_id, status="running", attempts=1, error=None)
        invalid_calls = [
            lambda: self.store.update_review_run(run_id, status="unknown"),
            lambda: self.store.update_review_run(run_id, revision=True),
            lambda: self.store.update_review_run(run_id, revision=0),
            lambda: self.store.update_review_run(run_id, request_attempts=0),
            lambda: self.store.update_review_run(run_id, elapsed_seconds=0),
            lambda: self.store.update_review_run(run_id, elapsed_seconds=float("inf")),
            lambda: self.store.update_review_run(run_id, elapsed_seconds=True),
            lambda: self.store.update_review_run(run_id, approval_history=[]),
            lambda: self.store.update_review_run(run_id, confirmed_digest="invalid"),
            lambda: self.store.update_review_run(run_id, confirmed_digest="0" * 64),
            lambda: self.store.update_review_run(run_id, digest="0" * 64),
            lambda: self.store.update_review_run(run_id, status="completed"),
            lambda: self.store.update_review_batch(batch_id, attempts=0),
            lambda: self.store.update_review_batch(batch_id, attempts=False),
            lambda: self.store.update_review_batch(batch_id, status="unknown"),
            lambda: self.store.update_review_batch(batch_id, error="raw transcript"),
            lambda: self.store.update_review_batch(batch_id, metadata=[]),
            lambda: self.store.update_review_batch(batch_id, items=[]),
            lambda: self.store.update_review_batch(batch_id, status="completed"),
        ]
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(PhotographyError):
                call()
        retry_digest = fingerprint({"retry": run_id, "revision": 1})
        self.store.update_review_run(run_id, confirmed_digest=plan["digest"], revision=2,
                                     approval_history=[*approval, {"digest": retry_digest, "revision": 2}])
        self.assertEqual(self.store.review_run(run_id)["confirmed_digest"], plan["digest"])
        self.assertEqual(self.store.review_run(run_id)["elapsed_seconds"], 0.5)
        for sql in ("UPDATE ai_review_runs SET revision=-1", "UPDATE ai_review_runs SET request_attempts=0",
                    "UPDATE ai_review_runs SET approval_history_json='[]'", "UPDATE ai_review_batches SET attempts=0",
                    "UPDATE ai_review_batches SET status='completed'"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.store.db.execute(sql)

    def test_batch_metadata_and_run_results_follow_batch_then_photo_order(self):
        plan = self.plan(("c", "a", "b"), batch_size=2)
        results = []
        for batch in plan["batches"]:
            with self.store.transaction():
                for item in reversed(batch["items"]):
                    results.append(self.put(plan, photo_id=item["photo_id"]))
                self.store.update_review_batch(batch["batch_id"], status="completed", metadata={"elapsed_seconds": 1.25})
        saved = self.store.review_run_results(plan["run_id"])
        self.assertEqual([result["photo_id"] for result in saved], ["a", "c", "b"])
        self.assertEqual({result["result_id"] for result in saved}, set(results))
        self.assertEqual(self.store.review_batches(plan["run_id"])[0]["metadata"], {"elapsed_seconds": 1.25})

    def test_actual_usage_and_elapsed_measurements_preserve_only_supplied_fields(self):
        plan = self.plan(("a", "b"), batch_size=1)
        usage = {"elapsed_seconds": 1.25, "request_attempts": 1, "input_tokens": 112, "output_tokens": 57, "reasoning_tokens": 7,
                 "cache_read_tokens": 9, "cache_write_tokens": 0, "credits": 0.025,
                 "model": self.profile["model"], "sdk_version": self.profile["sdk_version"],
                 "runtime_version": self.profile["runtime_version"], "provider": self.profile["provider"],
                 "usage_source": "assistant.usage", "credits_unit": "premium requests"}
        batch_id = plan["batches"][0]["batch_id"]
        with self.store.transaction():
            result_id = self.put(plan, metadata={**usage, "raw_response": "must not persist"})
            self.store.update_review_batch(batch_id, status="completed", metadata={**usage, "unknown": "discarded"})
            self.store.update_review_run(plan["run_id"], elapsed_seconds=1.5)
        with self.store.transaction():
            missing = self.put(plan, photo_id="b", metadata={"elapsed_seconds": 2.5, "credits": None, "input_tokens": None})
            self.store.update_review_batch(plan["batches"][1]["batch_id"], status="completed", metadata={"elapsed_seconds": 2.5})
            self.store.update_review_run(plan["run_id"], elapsed_seconds=4.5, status="completed")
        self.assertEqual(self.store.review_result(result_id)["metadata"], usage)
        self.assertEqual(self.store.review_result(missing)["metadata"], {"elapsed_seconds": 2.5})
        self.assertEqual(self.store.review_batches(plan["run_id"])[0]["metadata"], usage)
        self.assertEqual(self.store.review_run(plan["run_id"])["elapsed_seconds"], 4.5)
        backup = self.base / "usage-backup.sqlite"
        self.store.backup(backup)
        with SQLiteStorage.open(backup) as reopened:
            self.assertEqual(reopened.review_result(result_id)["metadata"], usage)
            self.assertEqual(reopened.review_result(missing)["metadata"], {"elapsed_seconds": 2.5})
            self.assertEqual(reopened.review_batches(plan["run_id"])[0]["metadata"], usage)
            self.assertEqual(reopened.review_run(plan["run_id"])["elapsed_seconds"], 4.5)
            row = reopened.db.execute("""SELECT json_extract(metadata_json,'$.input_tokens'),
                json_extract(metadata_json,'$.credits') FROM ai_review_results WHERE result_id=?""", (result_id,)).fetchone()
            self.assertEqual(tuple(row), (112, 0.025))

    def test_usage_metadata_rejects_invalid_known_values_at_api_and_sql_boundaries(self):
        plan = self.plan(("a", "b"))
        result_id = self.put(plan)
        batch_id = plan["batches"][0]["batch_id"]
        template = dict(self.store.db.execute("SELECT * FROM ai_review_results WHERE result_id=?", (result_id,)).fetchone())
        template.update(photo_id="b", result_id=template["result_id"][:-32] + uuid4().hex)
        statement = "INSERT INTO ai_review_results (" + ",".join(template) + ") VALUES (" + ",".join("?" for _ in template) + ")"
        invalid = [{"input_tokens": item} for item in (True, -1, "12", float("inf"), float("nan"))]
        invalid.extend({"reasoning_tokens": item} for item in (True, -1, float("inf")))
        invalid.extend({"request_attempts": item} for item in (True, 1.5))
        invalid.extend({"usage_source": item} for item in (False, "", "\u2003", "x" * 257))
        for metadata in invalid:
            with self.subTest(metadata=metadata):
                with self.assertRaises(PhotographyError):
                    self.put(plan, photo_id="b", metadata=metadata)
                with self.assertRaises(PhotographyError):
                    self.store.update_review_batch(batch_id, metadata=metadata)
                row = {**template, "metadata_json": encoded(metadata)}
                with self.assertRaises((sqlite3.IntegrityError, sqlite3.OperationalError)):
                    self.store.db.execute(statement, tuple(row.values()))
                with self.assertRaises((sqlite3.IntegrityError, sqlite3.OperationalError)):
                    self.store.db.execute("UPDATE ai_review_batches SET metadata_json=? WHERE batch_id=?",
                                          (encoded(metadata), batch_id))
        for metadata in ({"input_tokens": None}, {"raw_response": "not registered"}):
            row = {**template, "metadata_json": encoded(metadata)}
            with self.subTest(metadata=metadata), self.assertRaises(sqlite3.IntegrityError):
                self.store.db.execute(statement, tuple(row.values()))
        self.store.update_review_batch(batch_id, status="failed",
                                       metadata={"elapsed_seconds": 1.25, "input_tokens": None, "unknown": "discarded"})
        self.assertEqual(self.store.review_batches(plan["run_id"])[0]["metadata"], {"elapsed_seconds": 1.25})

    def test_result_writes_require_only_confirmed_running_run_and_batch(self):
        plan = self.plan()
        run_id, batch_id = plan["run_id"], plan["batches"][0]["batch_id"]
        for status, batch_status, confirmed in (
            ("planned", "pending", None),
            ("running", "pending", plan["digest"]),
            ("running", "running", None),
            ("failed", "running", plan["digest"]),
            ("running", "failed", plan["digest"]),
        ):
            self.store.update_review_run(run_id, status=status, confirmed_digest=confirmed)
            self.store.update_review_batch(batch_id, status=batch_status)
            with self.subTest(status=status, batch_status=batch_status, confirmed=confirmed), self.assertRaises(PhotographyError):
                self.store.put_review_result("a", run_id, batch_id, self.profile, self.manifest("a"), self.payload(), {})
        with self.store.transaction():
            self.store.update_review_run(run_id, status="running", confirmed_digest=plan["digest"])
            self.store.update_review_batch(batch_id, status="running")
            result_id = self.store.put_review_result("a", run_id, batch_id, self.profile, self.manifest("a"), self.payload(), {})
            self.store.update_review_batch(batch_id, status="completed")
        run = self.store.review_run(run_id)
        self.assertEqual((run["revision"], run["request_attempts"], run["approval_history"]), (0, 0, []))
        self.assertEqual(self.store.review_batches(run_id)[0]["attempts"], 0)
        self.assertEqual(self.store.review_run_results(run_id)[0]["result_id"], result_id)

    def test_direct_sql_results_require_confirmed_running_states(self):
        plan = self.plan(("a", "b"))
        result_id = self.put(plan)
        template = dict(self.store.db.execute("SELECT * FROM ai_review_results WHERE result_id=?", (result_id,)).fetchone())
        template.update(photo_id="b", result_id=template["result_id"][:-32] + uuid4().hex)
        statement = "INSERT INTO ai_review_results (" + ",".join(template) + ") VALUES (" + ",".join("?" for _ in template) + ")"
        run_id, batch_id = plan["run_id"], plan["batches"][0]["batch_id"]
        for status, batch_status, confirmed in (
            ("planned", "pending", None),
            ("running", "pending", plan["digest"]),
            ("running", "running", None),
            ("failed", "running", plan["digest"]),
            ("running", "failed", plan["digest"]),
        ):
            self.store.update_review_run(run_id, status=status, confirmed_digest=confirmed)
            self.store.update_review_batch(batch_id, status=batch_status)
            with self.subTest(status=status, batch_status=batch_status, confirmed=confirmed), self.assertRaises(sqlite3.IntegrityError):
                self.store.db.execute(statement, tuple(template.values()))
        self.store.update_review_run(run_id, status="running", confirmed_digest=plan["digest"])
        self.store.update_review_batch(batch_id, status="running")
        self.store.db.execute(statement, tuple(template.values()))
        self.assertEqual(len(self.store.review_run_results(run_id)), 2)

    def test_run_results_are_complete_without_history_and_exclude_other_runs(self):
        first = self.plan(("c", "a", "b"), batch_size=2)
        first_ids = self.complete(first)
        second = self.plan(("a", "b"), force=True)
        second_ids = self.complete(second)
        with patch.object(self.store, "review_history", side_effect=AssertionError("Do not search bounded history")):
            saved = self.store.review_run_results(first["run_id"])
            other = self.store.review_run_results(second["run_id"])
        self.assertEqual([result["photo_id"] for result in saved], ["a", "c", "b"])
        self.assertEqual({result["result_id"] for result in saved}, set(first_ids))
        self.assertEqual({result["result_id"] for result in other}, set(second_ids))
        self.assertTrue(all(result["run_id"] == first["run_id"] for result in saved))

    def test_changed_input_blocks_new_writes_but_does_not_hide_historical_reviews(self):
        plan = self.plan()
        first_id = self.complete(plan)[0]
        second = self.plan(force=True)
        self.store.db.execute("UPDATE photos SET content_version=?", ("0" * 64,))
        with self.assertRaises(PhotographyError):
            self.put(second)
        self.assertEqual(self.store.review_result(first_id)["input_manifest"], plan["items"][0]["input_manifest"])
        self.assertEqual(len(self.store.review_history("a")), 1)
        self.store.close()
        with SQLiteStorage.open(self.path) as reopened:
            self.assertEqual(reopened.review_result(first_id)["result_id"], first_id)

    def test_corrupt_preview_bytes_block_plans_and_result_writes(self):
        plan = self.plan()
        self.store.db.execute("UPDATE thumbnails SET data=? WHERE photo_id='a'", (b"bad image",))
        with self.assertRaises(PhotographyError):
            self.put(plan)
        with self.assertRaises(PhotographyError):
            self.plan()
        self.assertEqual(self.store.review_history("a"), [])

    def test_saved_payload_is_revalidated_at_read_boundary(self):
        plan = self.plan()
        result_id = self.put(plan)
        trigger = review_schema_registry()[0][("trigger", "ai_review_result_immutable")]
        self.store.db.execute("DROP TRIGGER ai_review_result_immutable")
        self.store.db.execute("PRAGMA ignore_check_constraints=ON")
        self.store.db.execute("UPDATE ai_review_results SET payload_json=?", (encoded({**self.payload(), "description": "changed"}),))
        self.store.db.execute("PRAGMA ignore_check_constraints=OFF")
        self.store.db.execute(trigger)
        with self.assertRaises(PhotographyError):
            self.store.review_result(result_id)
        with self.assertRaises(PhotographyError):
            self.store.review_history("a")

    def test_read_only_and_invalid_lookup_arguments_are_nonmutating(self):
        plan = self.plan()
        result_id = self.complete(plan)[0]
        self.store.close()
        before = (self.path.read_bytes(), self.path.stat().st_mtime_ns)
        with SQLiteStorage.open(self.path) as reopened:
            for call in (
                lambda: reopened.create_review_run(plan),
                lambda: reopened.update_review_run(plan["run_id"], status="completed"),
                lambda: reopened.update_review_batch(plan["batches"][0]["batch_id"], status="completed"),
                lambda: reopened.put_review_result("a", plan["run_id"], plan["batches"][0]["batch_id"],
                                                   self.profile, plan["items"][0]["input_manifest"], self.payload(), {}),
            ):
                with self.subTest(call=call), self.assertRaises(PhotographyError) as failure:
                    call()
                self.assertEqual(failure.exception.code, "STORAGE_READ_ONLY")
            for limit in (True, 0, 1001, "1"):
                with self.subTest(limit=limit), self.assertRaises(PhotographyError):
                    reopened.review_history("a", limit=limit)
            with self.assertRaises(PhotographyError):
                reopened.review_history("a", after=1)
            for call in (lambda: reopened.review_result("missing"), lambda: reopened.review_run("missing"),
                         lambda: reopened.review_batches("missing"), lambda: reopened.review_history("missing")):
                with self.assertRaises(PhotographyError):
                    call()
            self.assertEqual(reopened.review_result(result_id)["result_id"], result_id)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)

    def test_review_schema_changes_are_rejected_without_repair(self):
        changes = (
            "DROP INDEX ai_review_overall_score",
            "DROP TRIGGER ai_review_result_lists",
            "DROP TRIGGER image_feature_plan_immutable",
            "CREATE TRIGGER unauthorized_photo_trigger AFTER UPDATE ON photos BEGIN SELECT 1; END",
            "CREATE INDEX unauthorized_review_index ON ai_review_results(description)",
            "ALTER TABLE ai_review_results ADD COLUMN unauthorized TEXT",
            """PRAGMA writable_schema=ON;
                UPDATE sqlite_master SET sql=replace(sql,'overall_score BETWEEN 0 AND 10','overall_score BETWEEN 0 AND 11')
                WHERE type='table' AND name='ai_review_results';
                PRAGMA writable_schema=OFF""",
        )
        for index, sql in enumerate(changes):
            path = self.base / f"invalid-{index}.sqlite"
            with SQLiteStorage.create(path) as store:
                store.db.executescript(sql)
            before = path.read_bytes(), path.stat().st_mtime_ns
            for writable in (False, True):
                with self.subTest(sql=sql, writable=writable), self.assertRaises(PhotographyError) as failure:
                    SQLiteStorage.open(path, writable=writable)
                self.assertEqual(failure.exception.code, "SCHEMA_INVALID")
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_real_v10_album_is_rejected_byte_for_byte_without_migration(self):
        path = self.base / "actual-v10.sqlite"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute("PRAGMA user_version=10")
            for statement in (*SCHEMA, *IMAGE_EMBEDDING_SCHEMA, *VIRTUAL_FOLDER_SCHEMA, *IMAGE_FEATURE_SCHEMA):
                connection.execute(statement)
            connection.execute("INSERT INTO album_metadata VALUES (1,?,?)", (str(uuid4()), "2026-09-07T00:00:00+00:00"))
            connection.commit()
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'").fetchone()[0], 37)
        before = path.read_bytes(), path.stat().st_mtime_ns
        for writable in (False, True):
            with self.subTest(writable=writable), self.assertRaises(PhotographyError) as failure:
                SQLiteStorage.open(path, writable=writable)
            self.assertEqual(failure.exception.code, "SCHEMA_UNSUPPORTED")
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)


if __name__ == "__main__":
    unittest.main()
