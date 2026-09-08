from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "photography" / "scripts"))
from photography_lib import review
from photography_lib.config import PhotographyError
from photography_lib.fingerprints import fingerprint
from photography_lib.index_lock import execution_lock
from photography_lib.sqlite_storage import SQLiteStorage
from tests.test_review_schema import response


class FakeReviewProvider:
    def __init__(self, on_call=None):
        self.requests = []
        self.on_call = on_call

    def review(self, request):
        self.requests.append(request)
        value = response([image.image_id for image in request.images])
        if self.on_call:
            self.on_call(request, value, len(self.requests))
        return SimpleNamespace(text=json.dumps(value), metadata={})

    def models(self):
        return [{"id": "vision-test", "vision": True}]


class ReviewFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="smart-albums-review-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "album.sqlite"
        self.store = SQLiteStorage.create(self.database)
        self.addCleanup(self.store.close)
        self.ids = []
        self.preview_bytes = {}
        with self.store.transaction():
            for index in range(9):
                photo_id = f"photo_{index + 1}"
                photo = {
                    "photo_id": photo_id, "original_absolute_path": str(self.root / "offline" / (photo_id + ".jpg")),
                    "content_version": hashlib.sha256(photo_id.encode()).hexdigest(),
                    "thumbnail_profile": "preview-v1-srgb-64-q85", "size_bytes": 123,
                    "mtime_ns": 1000, "metadata": {"width": 48, "height": 24},
                    "ingest_state": "available", "original_status": "not_checked",
                    "created_at": "2026-09-01T00:00:00+00:00", "updated_at": "2026-09-01T00:00:00+00:00",
                }
                buffer = io.BytesIO()
                Image.new("RGB", (48, 24), (index * 20, 60, 130)).save(buffer, format="JPEG")
                data = buffer.getvalue()
                self.store.put_photo(photo)
                self.store.put_thumbnail(photo, data)
                self.ids.append(photo_id)
                self.preview_bytes[photo_id] = data

    def plan(self, ids=None, **options):
        return review.create_plan(self.ids[:1] if ids is None else ids, store=self.store, model="vision-test", **options)

    def execute(self, plan, provider=None, **options):
        return review.execute_plan(plan["run_id"], store=self.store, confirm=plan["digest"],
                                   provider_factory=lambda: provider or FakeReviewProvider(), **options)


class ReviewExecutionTests(ReviewFixture):
    def test_exact_batching_and_structured_results(self):
        for count, sizes in ((1, [1]), (4, [4]), (5, [4, 1]), (9, [4, 4, 1])):
            with self.subTest(count=count):
                plan = self.plan(self.ids[:count], force=True)
                provider = FakeReviewProvider()
                completed = self.execute(plan, provider)
                self.assertEqual(completed["status"], "completed", completed)
                self.assertEqual([len(request.images) for request in provider.requests], sizes)
                self.assertEqual(completed["provider_calls_this_operation"], len(sizes))
                self.assertEqual(completed["summary"]["reviewed"], count)
                self.assertTrue(all(type(item["payload"]) is dict for item in completed["results"]))
                for request in provider.requests:
                    self.assertNotIn(str(self.root), request.prompt)
                    self.assertNotIn("original_absolute_path", request.prompt)
                    self.assertTrue(all(image.data in self.preview_bytes.values() for image in request.images))

    def test_no_provider_before_confirmation_and_dry_run_no_writes(self):
        before = self.database.read_bytes()
        dry = self.plan(persist=False)
        self.assertEqual(dry["status"], "dry_run")
        self.assertEqual(self.database.read_bytes(), before)
        plan = self.plan()
        factory = Mock(side_effect=AssertionError("Provider construction needs approval"))
        for confirm in (None, "", "wrong"):
            with self.subTest(confirm=confirm), self.assertRaises(PhotographyError):
                review.execute_plan(plan["run_id"], store=self.store, confirm=confirm, provider_factory=factory)
        factory.assert_not_called()
        self.assertEqual(review.job(self.store, plan["run_id"])["request_attempts"], 0)

    def test_cache_reuse_force_history_and_language(self):
        plan = self.plan(self.ids[:4])
        self.execute(plan)
        cached = self.plan(self.ids[:4])
        self.assertEqual(cached["counts"]["cached"], 4)
        factory = Mock(side_effect=AssertionError("Cached reviews must be local"))
        result = review.execute_plan(cached["run_id"], store=self.store, confirm=cached["digest"],
                                     provider_factory=factory)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["provider_calls_this_operation"], 0)
        factory.assert_not_called()
        self.assertEqual(self.plan(self.ids[:4], language="en")["counts"]["pending"], 4)
        self.execute(self.plan(force=True))
        self.assertEqual(len(review.history(self.store, self.ids[0])["results"]), 2)

    def test_scope_deduplication_rejects_invalid_or_corrupt_inputs(self):
        plan = self.plan([self.ids[2], self.ids[0], self.ids[2]])
        self.assertEqual([item["photo_id"] for item in plan["items"]], [self.ids[2], self.ids[0]])
        self.assertEqual(plan["duplicates_removed"], 1)
        for ids in ([], [None], ["missing"], [""]):
            with self.subTest(ids=ids), self.assertRaises(PhotographyError):
                self.plan(ids)
        self.store.db.execute("UPDATE thumbnails SET data=? WHERE photo_id=?", (b"bad", self.ids[0]))
        with self.assertRaises(PhotographyError):
            self.plan()

    def test_failure_stops_later_batches_and_retry_needs_new_approval(self):
        def fail_second(request, value, ordinal):
            if ordinal == 2:
                raise PhotographyError("REVIEW_TIMEOUT", "Synthetic timeout.")
        plan = self.plan(self.ids)
        provider = FakeReviewProvider(fail_second)
        failed = self.execute(plan, provider)
        self.assertEqual(failed["status"], "partial")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(failed["summary"]["reviewed"], 4)
        self.assertEqual([batch["status"] for batch in failed["batches"]], ["completed", "failed", "pending"])
        retry = failed["retry"]["digest"]
        with self.assertRaises(PhotographyError):
            review.execute_plan(plan["run_id"], store=self.store, confirm=plan["digest"], resume=True)
        resumed_provider = FakeReviewProvider()
        complete = review.execute_plan(plan["run_id"], store=self.store, confirm=retry, resume=True,
                                       provider_factory=lambda: resumed_provider)
        self.assertEqual(complete["status"], "completed")
        self.assertEqual([len(request.images) for request in resumed_provider.requests], [4, 1])
        self.assertEqual(len(complete["approval_history"]), 2)
        with self.assertRaises(PhotographyError):
            review.execute_plan(plan["run_id"], store=self.store, confirm=retry, resume=True)
        factory = Mock(side_effect=AssertionError("Completed task must not be resent"))
        repeated = review.execute_plan(plan["run_id"], store=self.store, confirm=plan["digest"], provider_factory=factory)
        self.assertEqual(repeated["status"], "completed")
        factory.assert_not_called()

    def test_invalid_last_review_rolls_back_entire_batch(self):
        def invalid(request, value, ordinal):
            value["reviews"][-1]["scores"]["technical"]["score"] = True
        plan = self.plan(self.ids[:4])
        failed = self.execute(plan, FakeReviewProvider(invalid))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "REVIEW_RESPONSE_INVALID")
        self.assertEqual(self.store.review_run_results(plan["run_id"]), [])

    def test_changed_input_before_or_during_call_is_not_rebound(self):
        plan = self.plan()
        original = self.store.photo(self.ids[0])
        changed = {**original, "content_version": hashlib.sha256(b"changed").hexdigest()}
        with self.store.transaction():
            self.store.put_photo(changed)
            self.store.put_thumbnail(changed, self.preview_bytes[self.ids[0]])
        factory = Mock(side_effect=AssertionError("Stale scope must not call provider"))
        with self.assertRaises(PhotographyError):
            review.execute_plan(plan["run_id"], store=self.store, confirm=plan["digest"], provider_factory=factory)
        factory.assert_not_called()
        fresh = self.plan()
        def change_during(request, value, ordinal):
            with self.store.transaction():
                self.store.put_photo(original)
                self.store.put_thumbnail(original, self.preview_bytes[self.ids[0]])
        failed = self.execute(fresh, FakeReviewProvider(change_during))
        self.assertEqual(failed["error"]["code"], "REVIEW_INPUT_CHANGED")
        self.assertFalse(failed["results"])

    def test_interrupt_running_recovery_and_confirmation_consumption(self):
        def stop(request, value, ordinal):
            raise KeyboardInterrupt()
        plan = self.plan(self.ids[:5])
        interrupted = self.execute(plan, FakeReviewProvider(stop))
        self.assertTrue(interrupted["interrupted"])
        self.assertEqual(interrupted["summary"]["reviewed"], 0)
        old_retry = interrupted["retry"]["digest"]
        again = review.execute_plan(plan["run_id"], store=self.store, confirm=old_retry, resume=True,
                                    provider_factory=lambda: FakeReviewProvider(stop))
        self.assertNotEqual(old_retry, again["retry"]["digest"])
        with self.assertRaises(PhotographyError):
            review.execute_plan(plan["run_id"], store=self.store, confirm=old_retry, resume=True)
        self.store.update_review_run(plan["run_id"], status="running")
        running = review.job(self.store, plan["run_id"])
        with self.assertRaises(PhotographyError):
            review.execute_plan(plan["run_id"], store=self.store, confirm=running["retry"]["digest"], resume=True)
        done = review.execute_plan(plan["run_id"], store=self.store, confirm=running["retry"]["digest"],
                                   resume=True, confirm_stopped=True, provider_factory=FakeReviewProvider)
        self.assertEqual(done["status"], "completed")

    def test_crash_after_final_batch_can_finalize_without_provider(self):
        plan = self.plan()
        self.execute(plan)
        self.store.update_review_run(plan["run_id"], status="running")
        recovery = review.job(self.store, plan["run_id"])
        factory = Mock(side_effect=AssertionError("Committed batches need no resend"))
        completed = review.execute_plan(plan["run_id"], store=self.store, confirm=recovery["retry"]["digest"],
                                        resume=True, confirm_stopped=True, provider_factory=factory)
        self.assertEqual(completed["status"], "completed")
        factory.assert_not_called()

    def test_parallel_executor_is_rejected_and_history_is_local(self):
        plan = self.plan()
        lock = self.database.with_name("." + self.database.name + ".review.lock")
        with execution_lock(lock, error_prefix="REVIEW", component="photo-review"):
            with self.assertRaises(PhotographyError) as failed:
                self.execute(plan)
        self.assertEqual(failed.exception.code, "REVIEW_IN_PROGRESS")
        self.execute(plan)
        before = self.database.read_bytes()
        with patch("photography_lib.review._provider", side_effect=AssertionError("Reads are local")):
            latest = review.result(self.store, self.ids[0])
            history = review.history(self.store, self.ids[0], limit=1)
            self.assertEqual(latest["status"], "ready")
            self.assertEqual(history["results"][0]["payload"]["overall_score"], 7)
        self.assertEqual(self.database.read_bytes(), before)

    def test_plan_corruption_or_rubric_change_cannot_gain_approval(self):
        plan = self.plan()
        profile = deepcopy(plan["profile"])
        profile["prompt_text"] += "Different prompt"
        with patch("photography_lib.review.review_profile", return_value=profile):
            with self.assertRaises(PhotographyError) as failed:
                self.execute(plan)
        self.assertEqual(failed.exception.code, "REVIEW_PROFILE_CHANGED")
        changed = self.store.review_run(plan["run_id"])["plan"]
        changed["batch_size"] = 2
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE ai_review_runs SET plan_json=? WHERE run_id=?",
                                  (json.dumps(changed), plan["run_id"]))
        self.assertEqual(review.job(self.store, plan["run_id"])["digest"], plan["digest"])

    def test_same_length_corrupt_preview_is_not_reported_as_current(self):
        self.execute(self.plan())
        data = self.preview_bytes[self.ids[0]]
        self.store.db.execute("UPDATE thumbnails SET data=? WHERE photo_id=?", (b"x" * len(data), self.ids[0]))
        latest = review.result(self.store, self.ids[0])
        historical = review.history(self.store, self.ids[0])["results"][0]
        self.assertEqual(latest["status"], "invalid_input")
        self.assertFalse(latest["result"]["current"])
        self.assertEqual(historical["status"], "invalid_input")
        self.assertFalse(historical["current"])
        self.assertEqual(historical["payload"]["overall_score"], 7)


if __name__ == "__main__":
    unittest.main()
