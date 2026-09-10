from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
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
        self.lock = threading.Lock()

    def review(self, request):
        with self.lock:
            self.requests.append(request)
            ordinal = len(self.requests)
        value = response([image.image_id for image in request.images])
        if self.on_call:
            self.on_call(request, value, ordinal)
        return SimpleNamespace(text=json.dumps(value), metadata={})

    def models(self):
        return [{"id": "vision-test", "vision": True}]


class ReviewFixture(unittest.TestCase):
    photo_count = 9

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
            for index in range(self.photo_count):
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
                Image.new("RGB", (48, 24), ((index * 20) % 256, 60, 130)).save(buffer, format="JPEG")
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
                self.assertCountEqual([len(request.images) for request in provider.requests], sizes)
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
        plan = self.plan(self.ids, max_concurrency=1)
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
            value["results"][-1]["dimensions"]["technical"]["score"] = True
        plan = self.plan(self.ids[:4])
        failed = self.execute(plan, FakeReviewProvider(invalid))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "REVIEW_RESPONSE_INVALID")
        self.assertEqual(self.store.review_run_results(plan["run_id"]), [])

    def test_fenced_reply_commits_batches_and_preserves_usage(self):
        class WrappedProvider(FakeReviewProvider):
            def review(self, request):
                reply = super().review(request)
                return SimpleNamespace(text="```json\n" + reply.text + "\n```",
                                       metadata={"input_tokens": 123, "output_tokens": 45})
        plan = self.plan(self.ids[:5])
        provider = WrappedProvider()
        done = self.execute(plan, provider)
        self.assertEqual(done["status"], "completed", done.get("error"))
        self.assertCountEqual([len(request.images) for request in provider.requests], [4, 1])
        with SQLiteStorage.open(self.database) as reopened:
            saved = review.job(reopened, plan["run_id"])
            self.assertEqual(saved["summary"]["reviewed"], 5)
            for batch in saved["batches"]:
                metadata = batch["metadata"]
                self.assertEqual((metadata["input_tokens"], metadata["output_tokens"]), (123, 45))
            for photo_id in self.ids[:5]:
                self.assertEqual(review.result(reopened, photo_id)["status"], "ready")

    def test_fenced_invalid_result_still_rejects_entire_batch(self):
        class WrappedProvider(FakeReviewProvider):
            def review(self, request):
                reply = super().review(request)
                return SimpleNamespace(text="```json\n" + reply.text + "\n```", metadata={})
        def invalid(request, value, ordinal):
            value["results"][-1]["dimensions"]["technical"]["score"] = True
        plan = self.plan(self.ids[:5], max_concurrency=1)
        provider = WrappedProvider(invalid)
        failed = self.execute(plan, provider)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["results"], [])
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(failed["batches"][1]["status"], "pending")
        self.assertEqual(failed["batches"][0]["error"]["details"]["response_format"], "json_code_block")

    def test_failed_response_diagnostics_and_usage_survive_an_approved_retry(self):
        text = "Private model text instead of JSON."
        provider = Mock()
        provider.review.return_value = SimpleNamespace(text=text, metadata={"input_tokens": 123, "output_tokens": 45})
        plan = self.plan(self.ids[:5], max_concurrency=1)
        failed = self.execute(plan, provider)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(provider.review.call_count, 1)
        with SQLiteStorage.open(self.database) as reopened:
            saved = review.job(reopened, plan["run_id"])
        diagnostic = saved["batches"][0]["error"]
        self.assertEqual(diagnostic["details"]["response_format"], "other")
        self.assertEqual(diagnostic["details"]["response_sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertNotIn(text, json.dumps(saved))
        done = review.execute_plan(plan["run_id"], store=self.store, confirm=saved["retry"]["digest"],
                                   resume=True, provider_factory=FakeReviewProvider)
        previous = done["approval_history"][-1]["previous_attempts"][0]
        self.assertEqual(previous["error"], saved["batches"][0]["error"])
        self.assertEqual(previous["metadata"]["input_tokens"], 123)
        self.assertEqual(previous["error"]["details"]["response_format"], "other")
        self.assertEqual(done["status"], "completed")

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
            with SQLiteStorage.open(self.database, writable=True) as other, other.transaction():
                other.put_photo(original)
                other.put_thumbnail(original, self.preview_bytes[self.ids[0]])
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


class ReviewConcurrencyTests(ReviewFixture):
    photo_count = 25

    def test_default_five_requests_four_images_and_refill_without_waiting_for_slowest(self):
        first_wave = threading.Barrier(5, timeout=10)
        sixth_started = threading.Event()
        lock = threading.Lock()
        active = peak = 0
        caller = threading.get_ident()
        owners = []

        def concurrent(request, value, ordinal):
            nonlocal active, peak
            self.assertNotEqual(threading.get_ident(), caller)
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                if ordinal <= 5:
                    first_wave.wait()
                if ordinal == 1:
                    self.assertTrue(sixth_started.wait(10), "A slow batch blocked refilling other slots")
                if ordinal == 6:
                    sixth_started.set()
                for item, image in zip(value["results"], request.images):
                    item["description"] = hashlib.sha256(image.data).hexdigest()
            finally:
                with lock:
                    active -= 1

        original_put = self.store.put_review_result
        def put(*args, **kwargs):
            owners.append(threading.get_ident())
            return original_put(*args, **kwargs)

        plan = self.plan(self.ids)
        self.assertEqual((plan["max_concurrency"], plan["batch_size"]), (5, 4))
        provider = FakeReviewProvider(concurrent)
        with patch.object(self.store, "put_review_result", side_effect=put):
            done = self.execute(plan, provider)
        self.assertEqual(done["status"], "completed", done.get("error"))
        self.assertEqual(peak, 5)
        self.assertEqual(active, 0)
        self.assertCountEqual([len(r.images) for r in provider.requests], [4] * 6 + [1])
        self.assertEqual(done["provider_calls_this_operation"], 7)
        self.assertEqual(done["request_attempts"], 7)
        self.assertEqual(owners, [caller] * 25)
        with SQLiteStorage.open(self.database) as reopened:
            saved = review.job(reopened, plan["run_id"])
            self.assertEqual(saved["summary"]["reviewed"], 25)
            self.assertEqual([b["ordinal"] for b in saved["batches"]], list(range(7)))
            for record in saved["results"]:
                self.assertEqual(record["payload"]["description"],
                                 hashlib.sha256(self.preview_bytes[record["photo_id"]]).hexdigest())

    def test_failure_drains_other_requests_stops_new_sends_and_resume_skips_successes(self):
        for mode in ("rate_limit", "invalid_response", "unexpected", "interrupt"):
            with self.subTest(mode=mode):
                first_wave = threading.Barrier(3, timeout=10)
                failure_saved = threading.Event()
                plan = self.plan(self.ids[:9], batch_size=1, max_concurrency=3, force=True)
                def concurrent(request, value, ordinal):
                    first_wave.wait()
                    if request.images[0].data == self.preview_bytes[self.ids[1]]:
                        if mode == "rate_limit":
                            raise PhotographyError("REVIEW_RATE_LIMITED", "Synthetic rate limit.",
                                                   details={"usage": {"input_tokens": 17}})
                        if mode == "unexpected":
                            raise RuntimeError("Private provider diagnostic")
                        if mode == "interrupt":
                            raise KeyboardInterrupt()
                        value["results"][-1]["dimensions"]["technical"]["score"] = True
                    else:
                        self.assertTrue(failure_saved.wait(10), "Other requests were not drained after failure")

                original_update = self.store.update_review_batch
                def update(batch_id, **changes):
                    result = original_update(batch_id, **changes)
                    if changes.get("status") in ("failed", "interrupted"):
                        failure_saved.set()
                    return result

                provider = FakeReviewProvider(concurrent)
                with patch.object(self.store, "update_review_batch", side_effect=update):
                    failed = self.execute(plan, provider)
                self.assertEqual(failed["status"], "interrupted" if mode == "interrupt" else "partial")
                self.assertEqual(len(provider.requests), 3)
                states = ["completed", "interrupted" if mode == "interrupt" else "failed", "completed"]
                self.assertEqual([b["status"] for b in failed["batches"]], states + ["pending"] * 6)
                self.assertEqual([b["attempts"] for b in failed["batches"]], [1] * 3 + [0] * 6)
                self.assertEqual(failed["summary"]["reviewed"], 2)
                self.assertNotIn("Private provider diagnostic", json.dumps(failed))
                self.assertEqual(failed["retry"]["remaining_batches"], 7)
                self.assertTrue(failed["retry"]["possible_duplicate_charge"])
                retry_provider = FakeReviewProvider()
                done = review.execute_plan(plan["run_id"], store=self.store, resume=True,
                                           confirm=failed["retry"]["digest"], provider_factory=lambda: retry_provider)
                self.assertEqual(done["status"], "completed", done.get("error"))
                self.assertEqual(len(retry_provider.requests), 7)
                self.assertEqual(done["request_attempts"], 10)
                self.assertEqual(done["summary"]["reviewed"], 9)
                self.assertEqual(done["max_concurrency"], 3)
                self.assertEqual({r.images[0].data for r in retry_provider.requests},
                                 {self.preview_bytes[p] for p in self.ids[:9] if p not in (self.ids[0], self.ids[2])})
                if mode == "rate_limit":
                    previous = done["approval_history"][-1]["previous_attempts"]
                    self.assertEqual(previous[0]["metadata"]["input_tokens"], 17)

    def test_caller_interrupt_drains_requests_before_unlocking(self):
        released = threading.Event()
        def blocked(request, value, ordinal):
            self.assertTrue(released.wait(10))
        real_wait = review.wait
        def interrupt_once(futures, **kwargs):
            if not released.is_set():
                released.set()
                raise KeyboardInterrupt()
            return real_wait(futures, **kwargs)
        plan = self.plan(self.ids[:9], batch_size=1, max_concurrency=3)
        provider = FakeReviewProvider(blocked)
        with patch("photography_lib.review.wait", side_effect=interrupt_once):
            interrupted = self.execute(plan, provider)
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(interrupted["summary"]["reviewed"], 3)
        self.assertEqual([b["status"] for b in interrupted["batches"]], ["completed"] * 3 + ["pending"] * 6)
        self.assertFalse(interrupted["retry"]["confirm_stopped_required"])
        self.assertFalse(interrupted["retry"]["possible_duplicate_charge"])
        done = review.execute_plan(plan["run_id"], store=self.store, resume=True,
                                   confirm=interrupted["retry"]["digest"], provider_factory=FakeReviewProvider)
        self.assertEqual(done["status"], "completed")

    def test_invalid_concurrency_rejected_and_legacy_plan_remains_serial(self):
        for value in (0, -1, True, 1.5, "5", None):
            with self.subTest(value=value), self.assertRaises(PhotographyError):
                self.plan(max_concurrency=value)
        dry = self.plan(self.ids[:9], persist=False)
        legacy = {k: v for k, v in dry.items() if k not in (
            "max_concurrency", "status", "request_attempts", "provider_calls_this_operation", "album", "digest")}
        legacy["digest"] = fingerprint(legacy)
        self.store.create_review_run(legacy)
        def fail_second(request, value, ordinal):
            if ordinal == 2:
                raise PhotographyError("REVIEW_TIMEOUT", "Synthetic timeout.")
        provider = FakeReviewProvider(fail_second)
        done = self.execute(legacy, provider)
        self.assertEqual(done["status"], "partial")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual([b["status"] for b in done["batches"]], ["completed", "failed", "pending"])
        for value in (0, -1, True, 1.5, "5", None):
            corrupted = {**legacy, "max_concurrency": value}
            corrupted["digest"] = fingerprint({k: v for k, v in corrupted.items() if k != "digest"})
            with self.subTest(stored=value), self.assertRaises(PhotographyError):
                self.store._review_plan(corrupted)


if __name__ == "__main__":
    unittest.main()
