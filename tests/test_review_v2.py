"""Contract, persistence and compatibility checks without provider contact."""
from copy import deepcopy
import json
import sqlite3
from unittest.mock import patch

from tests.test_review_execution import ReviewFixture, FakeReviewProvider
from tests.test_review_schema import response
from tests import test_review_storage
from photography_lib import review, review_schema_v1, review_storage_v1, sqlite_storage
from photography_lib.config import Config, PhotographyError
from photography_lib.review_report import review_report
from photography_lib.review_schema import DIMENSIONS, RESPONSE_SCHEMA, build_prompt, parse_response, review_profile
from photography_lib.sqlite_storage import SQLiteStorage


class ReviewV2Tests(ReviewFixture):
    def test_mixed_statuses_round_trip_and_render_unknowns_without_meters(self):
        def mixed(request, value, ordinal):
            for entry, unknown in zip(value["results"], (0, 1, 6)):
                entry["review_status"] = "reviewed" if not unknown else "partial" if unknown == 1 else "unreviewable"
                for key in DIMENSIONS[:unknown]:
                    entry["dimensions"][key] = {"score": None, "reason": "Input lacks interpretable detail."}
                if unknown:
                    entry.update(strengths=[], improvements=[])
                if unknown == 6:
                    entry["description"] = "The attachment is unreadable; visible content cannot be described."
                    entry["limitations"].append("The supplied attachment is unreadable.")
            value["results"][0]["improvements"][0].update(
                action="<script>crop</script>", rationale="<b>Distracting edge</b>", tradeoff="<i>Less context</i>")
        plan = self.plan(self.ids[:3])
        provider = FakeReviewProvider(mixed)
        done = self.execute(plan, provider)
        self.assertEqual(done["status"], "completed", done.get("error"))
        self.assertEqual(done["summary"]["review_status_counts"], {"reviewed": 1, "partial": 1, "unreviewable": 1})
        self.assertEqual(len(provider.requests), 1)
        self.assertTrue(provider.requests[0].prompt.startswith(review_profile("vision-test")["prompt_text"]))
        before = self.database.read_bytes()
        with SQLiteStorage.open(self.database) as reopened:
            for photo_id, status, score in zip(self.ids, ("reviewed", "partial", "unreviewable"), (7, None, None)):
                payload = review.result(reopened, photo_id)["result"]["payload"]
                self.assertEqual((payload["review_status"], payload["overall_score"]), (status, score))
            output = self.root / "mixed.html"
            review_report(plan["run_id"], str(output), store=reopened, config=Config(self.database))
        self.assertEqual(self.database.read_bytes(), before)
        page = output.read_text(encoding="utf-8")
        self.assertEqual(page.count("<meter "), 11)
        self.assertEqual(page.count("<span>Not assessable</span>"), 7)
        self.assertIn("Review status: unreviewable", page)
        self.assertIn("&lt;script&gt;crop&lt;/script&gt;", page)
        self.assertIn("&lt;b&gt;Distracting edge&lt;/b&gt;", page)
        self.assertIn("&lt;i&gt;Less context&lt;/i&gt;", page)
        self.assertNotIn("<script", page)
        self.assertNotIn('value="None"', page)

    def test_invalid_late_result_rejects_entire_batch_and_stops_following_batch(self):
        for mode in ("reordered", "fractional", "status", "extra", "suggestion"):
            def invalid(request, value, ordinal):
                if mode == "reordered":
                    value["results"].reverse()
                elif mode == "fractional":
                    value["results"][-1]["dimensions"]["technical"]["score"] = 7.25
                elif mode == "status":
                    value["results"][-1]["review_status"] = "partial"
                elif mode == "extra":
                    value["results"][-1]["overall_score"] = 7
                else:
                    value["results"][-1]["improvements"] = ["Crop."]
            plan = self.plan(self.ids[:5], force=True)
            provider = FakeReviewProvider(invalid)
            failed = self.execute(plan, provider)
            with self.subTest(mode=mode):
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["error"]["code"], "REVIEW_RESPONSE_INVALID")
                self.assertEqual(failed["results"], [])
                self.assertEqual(len(provider.requests), 1)
                self.assertEqual(failed["batches"][1]["status"], "pending")

    def test_sql_projections_reject_v2_contract_violations(self):
        plan = self.plan(self.ids[:2])
        batch = plan["batches"][0]
        self.store.update_review_run(plan["run_id"], status="running", confirmed_digest=plan["digest"])
        self.store.update_review_batch(batch["batch_id"], status="running")
        payload = parse_response(json.dumps(response()), ["image_1"])["image_1"]
        result_id = self.store.put_review_result(self.ids[0], plan["run_id"], batch["batch_id"],
            plan["profile"], batch["items"][0]["input_manifest"], payload, {})
        row = dict(self.store.db.execute("SELECT * FROM ai_review_results WHERE result_id=?", (result_id,)).fetchone())
        # Insert the second frozen member with matching provenance and a new ID.
        manifest = batch["items"][1]["input_manifest"]
        row.update(photo_id=self.ids[1], result_id=row["result_id"][:-32] + "a" * 32,
                   input_manifest_json=json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                   input_fingerprint=batch["items"][1]["input_fingerprint"], **manifest)
        mutations = [
            lambda p: p.update(review_status="partial"),
            lambda p: p.update(strengths=["x"] * 4),
            lambda p: p.update(limitations=[]),
            lambda p: p["improvements"][0].pop("tradeoff"),
            lambda p: p["improvements"][0].update(kind="select"),
            lambda p: p["improvements"][0].update(tradeoff=" "),
            lambda p: p["improvements"][0].update(extra="x"),
            lambda p: p["dimensions"]["technical"].update(score=True),
        ]
        for mutate in mutations:
            invalid = deepcopy(payload)
            mutate(invalid)
            candidate = {**row, "payload_json": json.dumps(invalid)}
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.db.execute("INSERT INTO ai_review_results (" + ",".join(candidate) + ") VALUES ("
                                      + ",".join("?" for _ in candidate) + ")", tuple(candidate.values()))
        # A valid second row proves that the invalid cases did not fail only on provenance.
        self.store.db.execute("INSERT INTO ai_review_results (" + ",".join(row) + ") VALUES ("
                              + ",".join("?" for _ in row) + ")", tuple(row.values()))


class LegacyReviewCompatibilityTests(ReviewFixture):
    def setUp(self):
        with patch.multiple(sqlite_storage, SCHEMA_VERSION=11, REVIEW_SCHEMA=review_storage_v1.REVIEW_SCHEMA):
            super().setUp()
        with patch.object(review, "review_profile", review_schema_v1.review_profile):
            self.legacy_plan = self.plan(self.ids[:1])
        plan = self.legacy_plan
        batch = plan["batches"][0]
        self.store.update_review_run(plan["run_id"], status="running", confirmed_digest=plan["digest"])
        self.store.update_review_batch(batch["batch_id"], status="running")
        self.legacy_id = self.store.put_review_result(self.ids[0], plan["run_id"], batch["batch_id"],
            plan["profile"], batch["items"][0]["input_manifest"], test_review_storage.ReviewStorageTests.payload(), {})
        self.store.update_review_batch(batch["batch_id"], status="completed")
        self.store.update_review_run(plan["run_id"], status="completed")

    def test_legacy_reads_and_upgrade_copy_preserve_every_record_and_source_byte(self):
        original = self.database.read_bytes()
        record = self.store.review_result(self.legacy_id)
        self.assertEqual(review.result(self.store, self.ids[0])["status"], "stale")
        self.assertEqual(review.job(self.store, self.legacy_plan["run_id"])["status"], "completed")
        with self.assertRaises(PhotographyError) as error:
            self.plan(self.ids[:1])
        self.assertEqual(error.exception.code, "REVIEW_UPGRADE_REQUIRED")
        destination = self.root / "v2.sqlite"
        upgraded = self.store.upgrade_review(destination)
        self.assertTrue(upgraded["source_unchanged"])
        self.assertEqual(upgraded["album"]["name"], "v2")
        with SQLiteStorage.open(destination, writable=True) as copied:
            self.assertEqual(copied.review_result(self.legacy_id), record)
            plan = review.create_plan(self.ids[:1], store=copied, model="vision-test")
            self.assertEqual(plan["counts"]["cached"], 0)
            done = review.execute_plan(plan["run_id"], store=copied, confirm=plan["digest"],
                                       provider_factory=FakeReviewProvider)
            self.assertEqual(done["status"], "completed", done.get("error"))
            self.assertEqual(len(review.history(copied, self.ids[0])["results"]), 2)
            self.assertEqual(copied.review_result(self.legacy_id), record)
            review_report(self.legacy_plan["run_id"], str(self.root / "legacy.html"),
                          store=copied, config=Config(destination))
        self.assertEqual(self.database.read_bytes(), original)
        with self.assertRaises(PhotographyError):
            self.store.upgrade_review(destination)
        self.assertEqual(self.database.read_bytes(), original)

    def test_failed_upgrade_publishes_nothing_and_preserves_source(self):
        before = self.database.read_bytes()
        destination = self.root / "failure.sqlite"
        with patch.object(sqlite_storage, "REVIEW_SCHEMA", (*sqlite_storage.REVIEW_SCHEMA, "CREATE INDEX broken")):
            with self.assertRaises(PhotographyError):
                self.store.upgrade_review(destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob(".sa-*.tmp")), [])
        self.assertEqual(self.database.read_bytes(), before)


class JsonSchemaParityTests(ReviewFixture):
    def test_published_schema_agrees_with_parser_including_manifest_order(self):
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("Optional JSON Schema test oracle is unavailable.")
        prompt = build_prompt(review_profile("vision-test"), ["image_1", "image_2"])
        bound_schema = json.loads(prompt.split("Return only JSON conforming to this schema:\n", 1)[1])
        Draft202012Validator.check_schema(RESPONSE_SCHEMA)
        Draft202012Validator.check_schema(bound_schema)
        validator = Draft202012Validator(bound_schema)
        cases = [response(("image_1", "image_2")), response(("image_2", "image_1"))]
        for unknown in range(7):
            for status in ("reviewed", "partial", "unreviewable"):
                value = response(("image_1", "image_2"))
                value["results"][0]["review_status"] = status
                for key in DIMENSIONS[:unknown]:
                    value["results"][0]["dimensions"][key]["score"] = None
                cases.append(value)
        for value in cases:
            try:
                parse_response(json.dumps(value), ["image_1", "image_2"])
                accepted = True
            except PhotographyError:
                accepted = False
            self.assertEqual(validator.is_valid(value), accepted)
