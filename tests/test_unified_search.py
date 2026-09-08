"""Unified search integration with synthetic SQLite evidence and no real models."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))
from photography_lib import unified_search, virtual_folders
from photography_lib.config import Config, PhotographyError
from photography_lib.fingerprints import fingerprint
from photography_lib.image_vectors import pack_vector
from photography_lib.sqlite_storage import SQLiteStorage
from tests import test_condition_search as fixtures


class UnifiedSearchTests(unittest.TestCase):
    photo = fixtures.ConditionSearchTests.photo
    vector = fixtures.ConditionSearchTests.vector
    seed_vectors = fixtures.ConditionSearchTests.seed_vectors
    put = fixtures.ConditionSearchTests.put
    objects = fixtures.ConditionSearchTests.objects
    ocr = fixtures.ConditionSearchTests.ocr
    color = fixtures.ConditionSearchTests.color
    hash = fixtures.ConditionSearchTests.hash
    factory = fixtures.ConditionSearchTests.factory
    read_only = fixtures.ConditionSearchTests.read_only

    def setUp(self):
        self.root = PROJECT / (".unified-test-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.config = Config(self.root / "album.sqlite", model_cache_root=self.root / "no-models")
        self.store = SQLiteStorage.create(self.config.database_path)
        self.addCleanup(self.store.close)
        self.profiles = {}
        for photo_id in ("a", "b", "c"):
            self.photo(photo_id)
        self.embedding_id = self.store.put_embedding_profile(fixtures.EMBEDDING)
        self.store.set_default_embedding_profile(self.embedding_id)
        self.encoder = fixtures.Encoder(fixtures.EMBEDDING, lambda: self.assertFalse(self.store.db.in_transaction))
        self.factory_calls = 0

    def query(self, conditions=None, **options):
        conditions = conditions or [{"id": "S", "kind": "semantic", "query": "beach"}]
        return unified_search.query({"query": "the whole original request", "conditions": conditions, **options},
                                    store=self.store, config=self.config, encoder_factory=self.factory)

    def select(self, snapshot, numbers):
        return unified_search.select(snapshot, numbers, review_id=snapshot["review_id"], store=self.store)

    def assert_code(self, code, callable_, *args, **kwargs):
        with self.assertRaises(PhotographyError) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_defaults_all_and_unbounded_positive_explicit_count(self):
        self.seed_vectors()
        for index in range(118):
            photo_id = f"p{index:04d}"
            self.photo(photo_id)
            self.vector(photo_id)
        with self.read_only():
            default = self.query()
            self.assertEqual(default["candidate_count"], 100)
            self.assertEqual(default["candidate_total"], 121)
            self.assertTrue(default["partial"])
            for limit in (1001, 2**80, "all"):
                snapshot = self.query(candidate_limit=limit)
                self.assertEqual(snapshot["candidate_count"], 121)
                self.assertFalse(snapshot["partial"])
                unified_search.review(snapshot, store=self.store)
        for limit in (0, -1, True, False, 1.0, "1001", None, "ALL"):
            with self.subTest(limit=limit):
                self.assert_code("INVALID_ARGUMENT", self.query, candidate_limit=limit)

    def test_multiple_original_conditions_require_operator_before_dedup(self):
        conditions = [{"id": "A", "kind": "semantic", "query": "beach"},
                      {"id": "alias", "kind": "semantic", "query": " beach "}]
        self.assert_code("INVALID_ARGUMENT", self.query, conditions)
        self.seed_vectors()
        snapshot = self.query(conditions, operator="or")
        self.assertEqual(snapshot["aliases"], {"A": "A", "alias": "A"})
        self.assertEqual(self.encoder.calls, ["beach"])
        self.assertEqual(len(snapshot["query"]["conditions"]), 1)
        self.assertEqual(snapshot["candidate_count"], 3)

    def test_global_cap_not_per_condition_and_no_matched_count_reranking(self):
        self.seed_vectors()
        self.objects("c", 1)
        conditions = [{"id": "A", "kind": "semantic", "query": "beach"},
                      {"id": "B", "kind": "semantic", "query": "other"},
                      {"id": "P", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 1}]
        snapshot = self.query(conditions, operator="or", candidate_limit=2)
        self.assertEqual([row["photo_id"] for row in snapshot["candidates"]], ["a", "b"])
        self.assertEqual(snapshot["candidate_total"], 3)
        selected = self.select(snapshot, [2, 1])
        self.assertEqual(selected["selected_numbers"], [1, 2])
        self.assertEqual([row["photo_id"] for row in selected["results"]], ["a", "b"])
        self.assertEqual(selected["results"][0]["conditions"]["A"]["status"], "eligible")
        self.assertNotIn("matched_count", json.dumps(selected))

    def test_and_saved_hard_predicates_prefilter_before_top_k(self):
        self.seed_vectors()
        for photo_id, count in (("a", 0), ("b", 0), ("c", 1)):
            self.objects(photo_id, count)
        conditions = [{"id": "S", "kind": "semantic", "query": "beach"},
                      {"id": "P", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 1}]
        snapshot = self.query(conditions, operator="and", candidate_limit=1)
        self.assertEqual([row["photo_id"] for row in snapshot["candidates"]], ["c"])
        self.assertEqual(snapshot["candidates"][0]["conditions"]["S"]["evidence"]["candidate_rank"], 1)
        self.assertEqual(snapshot["candidates"][0]["conditions"]["S"]["raw_score"], 0)
        self.assertEqual(snapshot["coverage"]["S"], {"not_ranked": 2, "eligible": 1})
        unified_search.review(snapshot, store=self.store)
        self.select(snapshot, [1])

    def test_and_semantic_conditions_are_not_intersected_top_k_or_numeric_thresholds(self):
        self.seed_vectors()
        self.vector("a", (-1., 0., 0.))
        conditions = [{"id": "A", "kind": "semantic", "query": "beach"},
                      {"id": "B", "kind": "semantic", "query": "other"}]
        snapshot = self.query(conditions, operator="and", candidate_limit=1)
        self.assertEqual([row["photo_id"] for row in snapshot["candidates"]], ["b"])
        self.assertEqual(snapshot["candidate_total"], 3)
        all_rows = self.query(conditions, operator="and", candidate_limit="all")
        self.assertEqual([row["photo_id"] for row in all_rows["candidates"]], ["b", "c", "a"])
        self.assertEqual(all_rows["candidates"][-1]["conditions"]["A"]["raw_score"], -1)

    def test_and_requires_all_profile_vectors_or_keeps_independent_branch(self):
        other_profile = {**fixtures.EMBEDDING, "model": "separate-space"}
        other_id = self.store.put_embedding_profile(other_profile)
        self.vector("a")
        photo = self.store.photo("c")
        raw = pack_vector([0., 1., 0.], 3)
        self.store._put_embedding_result(
            {**photo, "input_image_hash": self.store.thumbnail("c", include_data=False)["image_hash"]},
            other_id, raw, hashlib.sha256(raw).hexdigest(), 3)
        conditions = [{"id": "A", "kind": "semantic", "query": "beach", "profile_id": self.embedding_id},
                      {"id": "B", "kind": "semantic", "query": "other", "profile_id": other_id}]
        factory = lambda profile: fixtures.Encoder(profile)
        with self.read_only():
            query = {"query": "whole request", "conditions": conditions, "operator": "and"}
            snapshot = unified_search.query(query, store=self.store, encoder_factory=factory)
            self.assertEqual(snapshot["candidate_count"], 0)
            self.assertEqual(snapshot["query_model_calls"], 0)
            self.select(snapshot, [])
            snapshot = unified_search.query({**query, "operator": "or"}, store=self.store, encoder_factory=factory)
            self.assertEqual([row["photo_id"] for row in snapshot["candidates"]], ["a", "c"])
            self.select(snapshot, [2, 1])

    def test_or_keeps_false_and_unknown_cells_without_rejecting_supported_branches(self):
        self.vector("a")
        self.vector("b")
        self.objects("a", 0)
        self.objects("c", 1)
        conditions = [{"id": "S", "kind": "semantic", "query": "beach"},
                      {"id": "P", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 1}]
        snapshot = self.query(conditions, operator="or")
        cells = {row["photo_id"]: row["conditions"] for row in snapshot["candidates"]}
        self.assertEqual(cells["a"]["P"]["status"], "not_matched")
        self.assertEqual(cells["b"]["P"]["status"], "unknown_missing_index")
        self.assertEqual(cells["c"]["S"]["status"], "unknown_missing_index")
        with self.read_only():
            selected = self.select(snapshot, [3, 2, 1])
            self.assertEqual(len(unified_search.selected_rows(selected, store=self.store)), 3)

    def test_unchanged_unknown_stale_feature_does_not_reject_or_semantic_branch(self):
        self.vector("a")
        self.ocr("a", "needle")
        photo = self.store.photo("a")
        photo["metadata"]["display_width"] = 13
        self.store.put_photo(photo)
        conditions = [{"id": "S", "kind": "semantic", "query": "beach"},
                      {"id": "O", "kind": "ocr_contains", "text": "needle"}]
        snapshot = self.query(conditions, operator="or")
        self.assertEqual(snapshot["candidates"][0]["conditions"]["O"]["status"], "unknown_stale")
        with self.read_only():
            self.select(snapshot, [1])

    def test_structured_only_stable_ids_hard_predicates_and_ocr_redaction(self):
        for photo_id, text in (("a", "SECRET_PRIVATE needle"), ("b", ""), ("c", "needle")):
            self.ocr(photo_id, text)
        snapshot = self.query([{"id": "O", "kind": "ocr_contains", "text": "needle"}])
        self.assertEqual([row["photo_id"] for row in snapshot["candidates"]], ["a", "c"])
        self.assertEqual(snapshot["query_model_calls"], 0)
        review = unified_search.review(snapshot, store=self.store)
        self.assertEqual(review["candidates"][0]["conditions"]["O"], {"status": "matched", "hit": True})
        for token in ("SECRET_PRIVATE", "snippet", "photo_id", "input_image_hash", "profile_id", "filename",
                      "metadata", "path", "vector", "payload_hash", "coverage_items", "source_digest"):
            self.assertNotIn(token, json.dumps(review))
        self.assertEqual(json.dumps(review).count(snapshot["review_id"]), 1)
        self.assertNotIn(self.embedding_id, json.dumps(review))

    def test_duplicate_witness_can_be_outside_global_cap_and_color_is_precise(self):
        photo = self.store.photo("b")
        photo["content_version"] = self.store.photo("a")["content_version"]
        self.store.put_photo(photo)
        snapshot = self.query([{"id": "D", "kind": "has_near_duplicate", "metric": "exact"}],
                              candidate_limit=1)
        self.assertEqual(snapshot["candidate_total"], 2)
        self.assertEqual(snapshot["candidates"][0]["conditions"]["D"]["evidence"]["peer_id"], "b")
        review = unified_search.review(snapshot, store=self.store)
        self.assertNotIn("peer_id", json.dumps(review))
        self.select(snapshot, [1])
        self.color("a", .4)
        self.color("c", .6)
        snapshot = self.query([{"id": "C", "kind": "color_fraction", "color": "blue", "minimum": .5}])
        self.assertEqual([row["photo_id"] for row in snapshot["candidates"]], ["c"])
        cell = unified_search.review(snapshot, store=self.store)["candidates"][0]["conditions"]["C"]
        self.assertEqual(cell["fraction"], .6)
        self.assertEqual(snapshot["scope_items"][0]["conditions"]["C"]["status"], "not_matched")
        self.select(snapshot, [1])

    def test_optional_saved_evidence_never_filters_or_adds_or_candidates(self):
        self.vector("a")
        self.vector("b", (.6, .8, 0.))
        self.objects("a", 0)
        self.objects("c", 1)
        evidence = [{"id": "P", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 1}]
        for operator in ("and", "or"):
            with self.subTest(operator=operator), self.read_only():
                snapshot = self.query(operator=operator, evidence_conditions=evidence)
                self.assertEqual([row["photo_id"] for row in snapshot["candidates"]], ["a", "b"])
                self.assertEqual(snapshot["candidates"][0]["conditions"]["P"]["status"], "not_matched")
                self.assertEqual(snapshot["candidates"][1]["conditions"]["P"]["status"], "unknown_missing_index")
                self.assertEqual(snapshot["candidate_total"], 2)
                review = unified_search.review(snapshot, store=self.store)
                self.assertEqual([condition["id"] for condition in review["conditions"]], ["S"])
                self.assertEqual(review["evidence_conditions"][0]["scoring"]["role"], "supporting_evidence")
                selected = self.select(snapshot, [1, 2])
                self.assertEqual(len(unified_search.selected_rows(selected, store=self.store)), 2)

    def test_optional_evidence_cannot_support_an_unsatisfied_structural_main_condition(self):
        self.objects("a", 0)
        self.objects("b", 1)
        self.color("a", 1.)
        self.color("b", 0.)
        self.color("c", 1.)
        conditions = [{"id": "P", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 1}]
        evidence = [{"id": "C", "kind": "color_fraction", "color": "blue", "minimum": .5}]
        for operator in ("and", "or"):
            snapshot = self.query(conditions, operator=operator, evidence_conditions=evidence)
            self.assertEqual([row["photo_id"] for row in snapshot["candidates"]], ["b"])
            self.assertEqual(snapshot["candidates"][0]["conditions"]["C"]["status"], "not_matched")
            self.select(snapshot, [1])

    def test_optional_evidence_ids_are_global_and_duplicate_predicates_alias(self):
        self.objects("a", 1)
        main = {"id": "P", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 1}
        self.assert_code("INVALID_ARGUMENT", self.query, [main], evidence_conditions=[main])
        self.assert_code("INVALID_ARGUMENT", self.query, [main], evidence_conditions=[
            {"id": "E", "kind": "semantic", "query": "beach"}])
        self.assert_code("INVALID_ARGUMENT", self.query, [main], evidence_conditions={})
        snapshot = self.query([main], evidence_conditions=[{**main, "id": "alias"}])
        self.assertEqual(snapshot["query"]["evidence_conditions"], [])
        self.assertEqual(snapshot["aliases"], {"P": "P", "alias": "P"})
        self.assertEqual(set(snapshot["candidates"][0]["conditions"]), {"P"})
        self.select(snapshot, [1])

    def test_optional_source_is_frozen_redacted_and_revalidated_without_filtering(self):
        self.seed_vectors()
        self.ocr("a", "PRIVATE needle")
        snapshot = self.query(evidence_conditions=[{"id": "O", "kind": "ocr_contains", "text": "needle"}])
        self.assertEqual(snapshot["candidate_count"], 3)
        review = unified_search.review(snapshot, store=self.store)
        self.assertNotIn("PRIVATE", json.dumps(review))
        self.assertEqual(review["candidates"][0]["conditions"]["O"], {"status": "matched", "hit": True})
        selected = self.select(snapshot, [1, 2])
        photo = self.store.photo("a")
        photo["metadata"]["display_width"] = 13
        self.store.put_photo(photo)
        self.assert_code("QUERY_SNAPSHOT_STALE", unified_search.selected_rows, selected, store=self.store)

    def test_selection_rechecks_only_reviewed_rows_not_full_scope(self):
        self.seed_vectors()
        self.objects("a", 1)
        snapshot = self.query(candidate_limit=1, evidence_conditions=[
            {"id": "P", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 1}])
        self.vector("c", (.8, .6, 0.))
        with self.read_only(), patch.object(self.store, "photo", wraps=self.store.photo) as lookup, \
                patch("photography_lib.unified_search.evaluate_conditions",
                      wraps=unified_search.evaluate_conditions) as evaluate:
            selected = self.select(snapshot, [1])
            unified_search.selected_rows(selected, store=self.store)
            unified_search.select_for_folder(selected, [1], review_id=selected["review_id"], store=self.store)
        self.assertEqual({call.args[0] for call in lookup.call_args_list}, {"a"})
        self.assertTrue(evaluate.call_args_list)
        self.assertTrue(all([row["photo_id"] for row in call.args[1]] == ["a"] for call in evaluate.call_args_list))

    def test_duplicate_witness_outside_cap_is_rechecked_without_all_pairs(self):
        for photo_id, value in (("a", 0), ("b", 0), ("c", 1024)):
            self.hash(photo_id, value)
        snapshot = self.query([{"id": "D", "kind": "has_near_duplicate", "metric": "hamming", "max_distance": 0}],
                              candidate_limit=1)
        self.assertEqual([row["photo_id"] for row in snapshot["candidates"]], ["a"])
        with self.read_only(), patch("photography_lib.feature_predicates._duplicates",
                                     side_effect=AssertionError("Do not reevaluate all pairs")):
            self.select(snapshot, [1])
        photo = self.store.photo("b")
        photo["content_version"] = "changed-witness"
        self.store.put_photo(photo)
        self.assert_code("QUERY_SNAPSHOT_STALE", self.select, snapshot, [1])

    def test_number_validation_explicit_review_binding_and_empty_selection(self):
        self.seed_vectors()
        snapshot = self.query()
        self.assert_code("QUERY_REVIEW_MISMATCH", unified_search.select, snapshot, [1], review_id="wrong", store=self.store)
        for numbers in ([True], [1, 1], [0], [4], [1.0], ["1"], None, {"numbers": [1]}):
            with self.subTest(numbers=numbers):
                self.assert_code("INVALID_ARGUMENT", self.select, snapshot, numbers)
        selected = self.select(snapshot, [])
        self.assertEqual(unified_search.selected_rows(selected, store=self.store), [])
        self.assertEqual(unified_search.summary(selected)["selected_numbers"], [])
        self.assert_code("QUERY_STAGE_INVALID", unified_search.review, selected, store=self.store)

    def test_selected_folder_subset_and_no_inference_image_original_or_writes(self):
        self.seed_vectors()
        with self.read_only():
            snapshot = self.query()
            with patch.object(self.encoder, "encode_text", side_effect=AssertionError("No further inference")):
                evidence = unified_search.review(snapshot, store=self.store)
                selected = self.select(snapshot, [3, 1])
                rows = unified_search.selected_rows(selected, store=self.store)
                self.assertEqual([row["number"] for row in rows], [1, 3])
                self.assertTrue(all(row["input_image_hash"] for row in rows))
                result = unified_search.select_for_folder(
                    selected, [3], review_id=evidence["review_id"], store=self.store)
                self.assertEqual(result["photo_ids"], ["c"])
                self.assertEqual(result["selected_numbers"], [3])
                self.assertNotIn("conditions", result)
                self.assertNotIn("results", result)
                self.assert_code("INVALID_ARGUMENT", unified_search.select_for_folder, selected, [2],
                                 review_id=evidence["review_id"], store=self.store)
                self.assert_code("QUERY_REVIEW_MISMATCH", unified_search.select_for_folder, selected, [1],
                                 review_id="wrong", store=self.store)

    def test_vector_repair_same_result_id_invalidates_frozen_selection(self):
        self.seed_vectors()
        snapshot = self.query()
        selected = self.select(snapshot, [1])
        old = snapshot["candidates"][0]["conditions"]["S"]["sources"][0]
        self.vector("a", (0., 1., 0.))
        new = self.store._embedding_result(old, self.embedding_id)
        self.assertEqual(new["result_id"], old["result_id"])
        self.assertNotEqual(new["vector_hash"], old["vector_hash"])
        self.assert_code("QUERY_SNAPSHOT_STALE", self.select, snapshot, [1])
        self.assert_code("QUERY_SNAPSHOT_STALE", unified_search.selected_rows, selected, store=self.store)

    def test_hard_evidence_tampering_and_photo_change_invalidate_selection(self):
        self.seed_vectors()
        self.objects("a", 1)
        conditions = [{"id": "S", "kind": "semantic", "query": "beach"},
                      {"id": "P", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 1}]
        snapshot = self.query(conditions, operator="and")
        snapshot["scope_items"][0]["conditions"]["P"]["evidence"]["count"] = 0
        snapshot["candidates"][0]["conditions"]["P"]["evidence"]["count"] = 0
        snapshot["review_id"] = unified_search._review_id(snapshot)
        unified_search._seal(snapshot)
        self.assert_code("QUERY_SNAPSHOT_STALE", self.select, snapshot, [1])
        snapshot = self.query()
        photo = self.store.photo("b")
        photo["content_version"] = "changed"
        self.store.put_photo(photo)
        self.assert_code("QUERY_SNAPSHOT_STALE", self.select, snapshot, [1])

    def test_historical_folder_scope_ignores_membership_changes(self):
        self.seed_vectors()
        folder = virtual_folders.create_folder("original folder", store=self.store)["folder"]["folder_id"]
        virtual_folders.add_photos(folder, ["a", "b"], store=self.store)
        snapshot = self.query(scope={"folder_ids": [folder]})
        virtual_folders.remove_photos(folder, ["a", "b"], store=self.store)
        virtual_folders.add_photos(folder, ["c"], store=self.store)
        with self.read_only():
            selected = self.select(snapshot, [1, 2])
        self.assertEqual([row["photo_id"] for row in selected["results"]], ["a", "b"])
        self.assertEqual(selected["scope"]["folders"][0]["name"], "original folder")

    def test_wrong_album_and_tampered_or_injected_private_fields_rejected(self):
        self.seed_vectors()
        snapshot = self.query()
        other = SQLiteStorage.create(self.root / "other.sqlite")
        self.addCleanup(other.close)
        self.assert_code("ALBUM_MISMATCH", unified_search.review, snapshot, store=other)
        modified = deepcopy(snapshot)
        modified["candidates"][0]["number"] = 2
        self.assert_code("QUERY_SNAPSHOT_INVALID", unified_search.review, modified, store=self.store)
        modified = deepcopy(snapshot)
        modified["candidates"][0]["number"] = True
        modified["review_id"] = unified_search._review_id(modified)
        unified_search._seal(modified)
        self.assert_code("QUERY_SNAPSHOT_INVALID", unified_search.review, modified, store=self.store)
        selected = self.select(snapshot, [1])
        selected["selected_numbers"] = [2]
        unified_search._seal(selected)
        self.assert_code("QUERY_SNAPSHOT_INVALID", unified_search.selected_rows, selected, store=self.store)
        for location in ("snapshot", "row", "cell", "source"):
            modified = deepcopy(snapshot)
            target = (modified if location == "snapshot" else modified["scope_items"][0] if location == "row" else
                      modified["scope_items"][0]["conditions"]["S"] if location == "cell" else
                      modified["scope_items"][0]["conditions"]["S"]["sources"][0])
            target["image"] = "data:image/png;base64,AAAA"
            modified["review_id"] = unified_search._review_id(modified)
            unified_search._seal(modified)
            with self.subTest(location=location):
                self.assert_code("QUERY_SNAPSHOT_INVALID", unified_search.review, modified, store=self.store)

    def test_compact_fixed_k_does_not_expand_with_scope_or_impose_byte_budget(self):
        self.seed_vectors()
        small = json.dumps(unified_search.review(self.query(candidate_limit=2), store=self.store))
        for index in range(107):
            photo_id = f"p{index:04d}"
            self.photo(photo_id)
            self.vector(photo_id)
        large = json.dumps(unified_search.review(self.query(candidate_limit=2), store=self.store))
        self.assertLess(abs(len(large) - len(small)), 100)
        full = unified_search.review(self.query(candidate_limit="all"), store=self.store)
        self.assertEqual(full["candidate_count"], 110)
        self.assertGreater(len(json.dumps(full)), 4096)

    def test_frozen_english_recipe_and_unavailable_profiles_are_explicit(self):
        self.seed_vectors()
        self.assert_code("VISUAL_QUERY_REQUIRED", self.query,
                         [{"id": "S", "kind": "semantic", "query": "海滩"}])
        snapshot = self.query([{"id": "S", "kind": "semantic", "query": "海滩", "visual_query": "a sandy beach"}])
        self.assertEqual(self.encoder.calls, ["a sandy beach"])
        plan = snapshot["query_encodings"]["S"]
        self.assertEqual(plan["strategy"], "english-visual-intent-v1")
        self.assertEqual(plan["query"], "海滩")
        self.assertEqual(plan["prompts"], ["a sandy beach"])
        self.assertEqual(plan["weights"], [1.0])
        selected = self.select(snapshot, [1])
        self.assertEqual(selected["query_encodings"], snapshot["query_encodings"])
        with self.assertRaises(PhotographyError):
            self.query([{"id": "S", "kind": "semantic", "query": "beach", "profile_id": "missing"}])
        self.assert_code("FEATURE_CONFIGURATION_REQUIRED", self.query,
                         [{"id": "O", "kind": "ocr_contains", "text": "needle"}])


if __name__ == "__main__":
    unittest.main()
