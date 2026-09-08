from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "photography" / "scripts"))
from photography_lib.config import Config, PhotographyError
from photography_lib import review
from photography_lib.review_report import review_report
from tests.test_review_execution import FakeReviewProvider, ReviewFixture


class MeteredProvider(FakeReviewProvider):
    def review(self, request):
        reply = super().review(request)
        reply.metadata.update(input_tokens=100, output_tokens=40, cache_read_tokens=20,
                              usage_source="synthetic-test-event")
        return reply


class ReviewReportTests(ReviewFixture):
    def export(self, plan, name="report.html"):
        output = self.root / name
        result = review_report(plan["run_id"], str(output), store=self.store, config=Config(self.database))
        return result, output.read_text(encoding="utf-8")

    def test_single_photo_metrics_and_safe_structured_report(self):
        def hostile_text(request, value, ordinal):
            value["reviews"][0]["description"] = '<script>alert("untrusted")</script>'
        plan = self.plan(self.ids[:2], batch_size=1)
        done = self.execute(plan, MeteredProvider(hostile_text))
        self.assertGreater(done["elapsed_seconds"], 0)
        self.assertTrue(all(batch["metadata"]["elapsed_seconds"] > 0 for batch in done["batches"]))
        before = self.database.read_bytes()
        with patch("photography_lib.review._provider", side_effect=AssertionError("Reports must be local")):
            result, page = self.export(plan)
        self.assertEqual(result["review_count"], 2)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertNotIn("<script", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertEqual(page.count('src="data:image/jpeg;base64,'), 2)
        self.assertIn("Per-photo measurement (one image", page)
        self.assertIn("Not reported", page)
        self.assertIn("200</strong><span>Input tokens observed", page)
        self.assertIn("80</strong><span>Output tokens observed", page)
        self.assertIn("not Azure credits", page)
        self.assertIn("Structured result and provenance", page)

    def test_shared_batch_usage_is_not_duplicated_per_photo(self):
        plan = self.plan(self.ids[:4])
        self.execute(plan, MeteredProvider())
        _, page = self.export(plan)
        self.assertIn("100</strong><span>Input tokens observed", page)
        self.assertNotIn("400</strong>", page)
        self.assertIn("Shared batch measurement (4 images); NOT per-photo usage", page)
        self.assertNotIn("Per-photo measurement (one image", page)

    def test_pending_report_is_honest_and_does_not_request_review(self):
        plan = self.plan(self.ids[:2])
        with patch("photography_lib.review._provider", side_effect=AssertionError("No confirmation")):
            result, page = self.export(plan)
        self.assertEqual(result["review_count"], 0)
        self.assertIn("Not executed", page)
        self.assertEqual(page.count("No successful review saved"), 2)

    def test_no_overwrite_no_relative_paths_and_stale_preview_is_not_replaced(self):
        plan = self.plan()
        self.execute(plan)
        output, _ = self.export(plan)
        original = Path(output["output"]).read_bytes()
        with self.assertRaises(PhotographyError) as error:
            self.export(plan)
        self.assertEqual(error.exception.code, "EXPORT_EXISTS")
        self.assertEqual(Path(output["output"]).read_bytes(), original)
        with self.assertRaises(PhotographyError):
            review_report(plan["run_id"], "relative.html", store=self.store, config=Config(self.database))
        self.store.db.execute("UPDATE thumbnails SET image_hash=? WHERE photo_id=?", ("changed", self.ids[0]))
        _, page = self.export(plan, "stale.html")
        self.assertIn("preview-error", page)
        self.assertNotIn('src="data:image/jpeg;base64,', page)

    def test_cache_does_not_misreport_historical_usage_as_new(self):
        original = self.plan()
        self.execute(original, MeteredProvider())
        cached = self.plan()
        self.execute(cached)
        _, page = self.export(cached)
        self.assertIn("Cached review: no new Copilot request", page)
        self.assertIn("Not reported</strong><span>Input tokens observed", page)

    def test_invalid_review_can_still_show_observed_failed_attempt_usage(self):
        def invalid(request, value, ordinal):
            value["reviews"][0]["scores"]["composition"]["score"] = False
        plan = self.plan(batch_size=1)
        failed = self.execute(plan, MeteredProvider(invalid))
        self.assertEqual(failed["status"], "failed")
        _, page = self.export(plan)
        self.assertIn("No successful review saved", page)
        self.assertIn("<dt>Input tokens</dt><dd>100</dd>", page)
        self.assertIn("Per-photo measurement (one image", page)

    def test_retry_keeps_previous_failed_token_usage_in_history_and_total(self):
        def invalid(request, value, ordinal):
            value["reviews"][0]["scores"]["composition"]["score"] = False
        plan = self.plan(batch_size=1)
        failed = self.execute(plan, MeteredProvider(invalid))
        completed = review.execute_plan(plan["run_id"], store=self.store, resume=True,
                                        confirm=failed["retry"]["digest"], provider_factory=MeteredProvider)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["approval_history"][-1]["previous_attempts"][0]["metadata"]["input_tokens"], 100)
        _, page = self.export(plan)
        self.assertIn("200</strong><span>Input tokens observed", page)
        self.assertIn("80</strong><span>Output tokens observed", page)
        self.assertIn("Batch attempt history", page)
        self.assertIn("<td>failed</td>", page)
