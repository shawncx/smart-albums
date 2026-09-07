"""OR workflow integration over synthetic saved data; no real image/model queries."""
from contextlib import contextmanager
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
from unittest.mock import patch

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))
from photography_lib import condition_search, feature_inputs, virtual_folders
from photography_lib.config import Config, PhotographyError
from photography_lib.feature_profiles import default_profile, normalize_ocr_text
from photography_lib.fingerprints import fingerprint
from photography_lib.image_vectors import pack_vector
from photography_lib.sqlite_storage import SQLiteStorage


EMBEDDING = {
    "profile_schema": "image-embedding-profile-v1", "embedding_kind": "image_text_semantic",
    "stored_modality": "image", "input_scope": "stored_thumbnail", "granularity": "whole_image",
    "model": "condition-workflow-fixture", "dimensions": 3, "dtype": "float32-le", "normalized": True,
}


class Encoder:
    def __init__(self, profile, callback=None):
        self.identity, self.calls, self.callback = deepcopy(profile), [], callback

    def profile(self):
        return deepcopy(self.identity)

    def encode_text(self, text):
        self.calls.append(text)
        if self.callback:
            self.callback()
        return SimpleNamespace(vector=[0., 1., 0.] if text == "other" else [1., 0., 0.])


class ConditionSearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="condition-search-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = Config(self.root / "album.sqlite", model_cache_root=self.root / "no-models")
        self.store = SQLiteStorage.create(self.config.database_path)
        self.addCleanup(self.store.close)
        self.profiles = {}
        for pid in ("a", "b", "c"):
            self.photo(pid)
        self.embedding_id = self.store.put_embedding_profile(EMBEDDING)
        self.store.set_default_embedding_profile(self.embedding_id)
        self.encoder = Encoder(EMBEDDING, lambda: self.assertFalse(self.store.db.in_transaction))
        self.factory_calls = 0

    def photo(self, pid):
        image = io.BytesIO()
        Image.new("RGB", (12, 6), "navy").save(image, "JPEG")
        photo = {"photo_id": pid, "content_version": hashlib.sha256(pid.encode()).hexdigest(),
                 "thumbnail_profile": "fixture-preview", "original_absolute_path": str(self.root / "absent" / (pid + ".jpg")),
                 "size_bytes": 1, "mtime_ns": 1, "metadata": {"display_width": 12, "display_height": 6},
                 "ingest_state": "available", "original_status": "missing",
                 "created_at": "2026-09-06T00:00:00Z", "updated_at": "2026-09-06T00:00:00Z"}
        self.store.put_photo(photo)
        self.store.put_thumbnail(photo, image.getvalue())

    def seed_vectors(self):
        for pid, vector in zip(("a", "b", "c"), ((1., 0., 0.), (.6, .8, 0.), (0., 1., 0.))):
            self.vector(pid, vector)

    def vector(self, pid, values=(1., 0., 0.)):
        photo = self.store.photo(pid)
        raw = pack_vector(values, 3)
        self.store._put_embedding_result(
            {**photo, "input_image_hash": self.store.thumbnail(pid, include_data=False)["image_hash"]},
            self.embedding_id, raw, hashlib.sha256(raw).hexdigest(), 3)

    def put(self, pid, component, payload):
        if component not in self.profiles:
            profile = default_profile(component)
            profile_id = self.store.put_feature_profile(profile)
            self.store.set_default_feature_profile(component, profile_id)
            self.profiles[component] = profile
        profile = self.profiles[component]
        return self.store.put_feature_result(pid, fingerprint(profile),
                                             feature_inputs.manifest_for(pid, profile, store=self.store), payload)

    def ocr(self, pid, text):
        return self.put(pid, "ocr", {"width": 12, "height": 6, "complete": True, "text": text,
                                    "normalized_text": normalize_ocr_text(text), "blocks": [
            {"text": text, "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]],
             "recognition_score": .9, "detection_score": None}] if text else []})

    def color(self, pid, blue):
        palette = [{"rgb": [0, 0, 255], "fraction": blue}] if blue else []
        if blue < 1:
            palette.append({"rgb": [255, 0, 0], "fraction": 1 - blue})
        return self.put(pid, "color", {"width": 12, "height": 6, "complete": True, "palette": palette,
                                      "features": {"mean_saturation": 1., "low_saturation_fraction": 0.,
                                                   "hue_histogram": [1.] + [0.] * 11}})

    def objects(self, pid, count):
        return self.put(pid, "objects", {"width": 12, "height": 6, "complete": True, "objects": [
            {"class_id": "person", "score": .9, "bbox": [.1, .1, .9, .9]} for _ in range(count)]})

    def hash(self, pid, value):
        return self.put(pid, "perceptual_hash", {"complete": True, "algorithm": "dhash",
                                                "bits": 64, "hash_hex": f"{value:016x}"})

    def factory(self, profile):
        self.factory_calls += 1
        self.assertEqual(fingerprint(profile), self.embedding_id)
        return self.encoder

    def query(self, conditions, **options):
        return condition_search.query({"conditions": conditions, **options}, store=self.store,
                                      config=self.config, encoder_factory=self.factory)

    def decisions(self, snapshot, selections):
        pages = []
        for entry in condition_search.summary(snapshot)["semantic_conditions"]:
            for page in range(entry["page_count"]):
                evidence = condition_search.semantic_evidence(snapshot, entry["condition_id"], page, store=self.store)
                offered = {item["photo_id"] for item in evidence["items"]}
                pages.append({"condition_id": entry["condition_id"], "page_id": evidence["page_id"],
                              "matched_photo_ids": sorted(offered & set(selections.get(entry["condition_id"], [])))})
        return {"schema": "condition-decisions-v1", "snapshot_id": snapshot["snapshot_id"], "pages": pages}

    @contextmanager
    def read_only(self):
        blocked = []

        def authorizer(action, table, column, *_):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) or (
                    action == sqlite3.SQLITE_READ and table == "thumbnails" and column == "data"):
                blocked.append((action, table, column))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        before = self.config.database_path.read_bytes()
        self.store.db.set_authorizer(authorizer)
        try:
            with patch("photography_lib.feature_inputs.resolve_original", side_effect=AssertionError("No originals")), \
                    patch("photography_lib.feature_inputs.load_input", side_effect=AssertionError("No image processing")), \
                    patch("urllib.request.urlopen", side_effect=AssertionError("No network")):
                yield
        finally:
            self.store.db.set_authorizer(None)
        self.assertEqual(blocked, [])
        self.assertEqual(self.config.database_path.read_bytes(), before)

    def test_semantic_top_k_is_not_a_match_and_all_review_pages_are_required(self):
        self.seed_vectors()
        with self.read_only():
            snapshot = self.query([{"id": "A", "kind": "semantic", "query": "beach"}], review_page_size=2)
            self.assertEqual(snapshot["stage"], "awaiting_semantic_decisions")
            self.assertEqual(snapshot["results"], [])
            choices = self.decisions(snapshot, {"A": ["a"]})
            with self.assertRaises(PhotographyError):
                condition_search.finalize_query(snapshot, {**choices, "pages": choices["pages"][:1]}, store=self.store)
            finalized = condition_search.finalize_query(snapshot, choices, store=self.store)
            page = condition_search.show_results(finalized, store=self.store)
        self.assertEqual([row["photo_id"] for row in page["results"]], ["a"])
        self.assertEqual(page["evaluated_coverage"]["A"], {"matched": 1, "not_matched": 2})
        self.assertEqual(self.encoder.calls, ["beach"])
        self.assertEqual(page["model_calls"], 0)

    def test_semantic_evidence_never_reveals_other_condition_matches_or_ocr(self):
        self.seed_vectors()
        self.ocr("a", "SECRET_PRIVATE OCR")
        self.ocr("b", "")
        self.ocr("c", "")
        with self.read_only():
            snapshot = self.query([{"id": "A", "kind": "semantic", "query": "beach"},
                                   {"id": "B", "kind": "ocr_contains", "text": "PRIVATE"}])
            safe = condition_search.semantic_evidence(snapshot, "A", store=self.store)
            summary = condition_search.summary(snapshot)
        for token in ("SECRET_PRIVATE", "snippet", "ocr_contains", "matched_count", "original_absolute_path",
                      "data:image", "metadata"):
            self.assertNotIn(token.casefold(), json.dumps(safe).casefold())
            self.assertNotIn(token.casefold(), json.dumps(summary).casefold())
        self.assertIn("secret_private", json.dumps(snapshot).casefold())

    def test_other_branch_candidates_receive_all_semantic_scores_outside_original_top_k(self):
        self.seed_vectors()
        for pid, text in (("a", ""), ("b", ""), ("c", "needle")):
            self.ocr(pid, text)
        with self.read_only():
            snapshot = self.query([{"id": "A", "kind": "semantic", "query": "beach"},
                                   {"id": "B", "kind": "ocr_contains", "text": "needle"}], semantic_candidates=1)
            self.assertEqual([row["photo_id"] for row in snapshot["candidates"]], ["a", "c"])
            evidence = condition_search.semantic_evidence(snapshot, "A", store=self.store)
            self.assertEqual([item["candidate_rank"] for item in evidence["items"]], [1, 3])
            selected = condition_search.finalize_query(snapshot, self.decisions(snapshot, {"A": ["c"]}), store=self.store)
            page = condition_search.show_results(selected, store=self.store)
        self.assertEqual([(row["photo_id"], row["matched_count"]) for row in page["results"]], [("c", 2)])
        self.assertTrue(page["retrieval"]["semantic_retrieval_limited"])
        self.assertEqual(self.encoder.calls, ["beach"])

    def test_multiple_semantic_conditions_share_encoder_and_keep_independent_decisions(self):
        self.seed_vectors()
        snapshot = self.query([{"id": "A", "kind": "semantic", "query": "beach"},
                               {"id": "B", "kind": "semantic", "query": "other"}],
                              semantic_candidates=1, review_page_size=1)
        self.assertEqual([row["photo_id"] for row in snapshot["candidates"]], ["a", "c"])
        selected = condition_search.finalize_query(snapshot, self.decisions(snapshot, {"A": ["a", "c"], "B": ["c"]}),
                                                   store=self.store)
        self.assertEqual([(row["photo_id"], row["matched_count"]) for row in selected["results"]], [("c", 2), ("a", 1)])
        self.assertEqual(self.factory_calls, 1)
        self.assertEqual(self.encoder.calls, ["beach", "other"])
        condition_search.show_results(selected, store=self.store)
        self.assertEqual(self.encoder.calls, ["beach", "other"])

    def test_duplicate_conditions_do_not_add_weight_or_model_calls(self):
        self.seed_vectors()
        snapshot = self.query([{"id": "A", "kind": "semantic", "query": "beach"},
                               {"id": "duplicate", "kind": "semantic", "query": " beach "}])
        self.assertEqual(snapshot["aliases"], {"A": "A", "duplicate": "A"})
        final = condition_search.finalize_query(snapshot, self.decisions(snapshot, {"A": ["a"]}), store=self.store)
        self.assertEqual(final["results"][0]["matched_count"], 1)
        self.assertEqual(self.encoder.calls, ["beach"])

    def test_structured_or_random_interleaving_preserves_same_pattern_score_order_and_paging(self):
        for pid, blue, count, text in (("a", .9, 2, ""), ("b", .4, 2, ""), ("c", .7, 0, "hit")):
            self.color(pid, blue)
            self.objects(pid, count)
            self.ocr(pid, text)
        conditions = [{"id": "A", "kind": "color_fraction", "color": "blue", "minimum": .1},
                      {"id": "B", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 2},
                      {"id": "C", "kind": "ocr_contains", "text": "hit"}]
        with self.read_only():
            snapshot = self.query(conditions, random_seed="stable")
            self.assertEqual(snapshot["stage"], "finalized")
            complete = condition_search.show_results(snapshot, store=self.store)
            ids = [row["photo_id"] for row in complete["results"]]
            paged, cursor = [], ""
            while True:
                page = condition_search.show_results(snapshot, store=self.store, limit=1, after=cursor)
                paged.extend(row["photo_id"] for row in page["results"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
        self.assertEqual(set(ids), {"a", "b", "c"})
        self.assertLess(ids.index("a"), ids.index("b"))
        self.assertEqual(ids, paged)
        self.assertTrue(all(row["matched_count"] == 2 for row in complete["results"]))
        self.assertEqual(self.encoder.calls, [])

    def test_missing_index_does_not_count_as_negative_or_block_other_match(self):
        self.color("a", .9)
        self.color("b", .3)
        self.color("c", .0)
        self.objects("b", 0)
        snapshot = self.query([{"id": "C", "kind": "color_fraction", "color": "blue", "minimum": .5},
                               {"id": "O", "kind": "object_count", "class_id": "person", "operator": "eq", "value": 0}])
        rows = {row["photo_id"]: row for row in snapshot["results"]}
        self.assertEqual(set(rows), {"a", "b"})
        self.assertEqual(rows["a"]["conditions"]["O"]["status"], "unknown_missing_index")
        self.assertEqual(rows["a"]["matched_count"], 1)
        self.assertEqual(rows["b"]["conditions"]["O"]["status"], "matched")
        condition_search.show_results(snapshot, store=self.store)

    def test_empty_scope_and_empty_selections_are_valid_without_implicit_expansion(self):
        self.seed_vectors()
        folder = virtual_folders.create_folder("empty", store=self.store)["folder"]["folder_id"]
        snapshot = self.query([{"id": "A", "kind": "semantic", "query": "beach"}],
                              scope={"folder_ids": [folder]})
        self.assertEqual(snapshot["stage"], "finalized")
        self.assertEqual(snapshot["results"], [])
        self.assertEqual(self.encoder.calls, [])
        snapshot = self.query([{"id": "A", "kind": "semantic", "query": "beach"}])
        final = condition_search.finalize_query(snapshot, self.decisions(snapshot, {}), store=self.store)
        self.assertEqual(final["results"], [])
        self.assertEqual(condition_search.show_results(final, store=self.store)["total"], 0)

    def test_folder_scope_is_hard_and_historical_after_membership_changes(self):
        self.seed_vectors()
        folder = virtual_folders.create_folder("only b", store=self.store)["folder"]["folder_id"]
        virtual_folders.add_photos(folder, ["b"], store=self.store)
        snapshot = self.query([{"id": "A", "kind": "semantic", "query": "beach"}],
                              scope={"folder_ids": [folder]}, semantic_candidates=1)
        self.assertEqual([row["photo_id"] for row in snapshot["candidates"]], ["b"])
        virtual_folders.delete_folder(folder, store=self.store)
        final = condition_search.finalize_query(snapshot, self.decisions(snapshot, {"A": ["b"]}), store=self.store)
        page = condition_search.show_results(final, store=self.store)
        self.assertEqual(page["scope"]["folders"][0]["name"], "only b")
        self.assertEqual(page["results"][0]["matched_count"], 1)

    def test_stale_inputs_and_wrong_album_fail_without_requery(self):
        self.seed_vectors()
        snapshot = self.query([{"id": "A", "kind": "semantic", "query": "beach"}])
        choices = self.decisions(snapshot, {"A": ["a"]})
        with SQLiteStorage.create(self.root / "other.sqlite") as other:
            with self.assertRaises(PhotographyError) as failure:
                condition_search.finalize_query(snapshot, choices, store=other)
            self.assertEqual(failure.exception.code, "ALBUM_MISMATCH")
        photo = self.store.photo("a")
        self.store.put_photo({**photo, "content_version": hashlib.sha256(b"changed").hexdigest()})
        with self.assertRaises(PhotographyError) as failure:
            condition_search.finalize_query(snapshot, choices, store=self.store)
        self.assertEqual(failure.exception.code, "QUERY_SNAPSHOT_STALE")
        self.assertEqual(self.encoder.calls, ["beach"])

    def test_malformed_decision_pages_and_raw_match_forgery_are_rejected(self):
        self.seed_vectors()
        snapshot = self.query([{"id": "A", "kind": "semantic", "query": "beach"}], review_page_size=1)
        choices = self.decisions(snapshot, {"A": ["a"]})
        for change in ("duplicate_page", "wrong_condition", "outside_id", "wrong_snapshot"):
            bad = deepcopy(choices)
            if change == "duplicate_page":
                bad["pages"].append(bad["pages"][0])
            elif change == "wrong_condition":
                bad["pages"][0]["condition_id"] = "missing"
            elif change == "outside_id":
                bad["pages"][0]["matched_photo_ids"] = ["outside"]
            else:
                bad["snapshot_id"] = "another"
            with self.subTest(change=change), self.assertRaises(PhotographyError):
                condition_search.finalize_query(snapshot, bad, store=self.store)
        bad = deepcopy(snapshot)
        bad["candidates"][0]["conditions"]["A"]["status"] = "matched"
        condition_search._seal(bad)
        with self.assertRaises(PhotographyError):
            condition_search.finalize_query(bad, choices, store=self.store)

    def test_snapshot_whitelist_and_cursor_are_bound_to_exact_result(self):
        for pid in ("a", "b", "c"):
            self.color(pid, .8)
        conditions = [{"id": "A", "kind": "color_fraction", "color": "blue", "minimum": .2}]
        first = self.query(conditions)
        second = self.query(conditions)
        page = condition_search.show_results(first, store=self.store, limit=1)
        with self.assertRaises(PhotographyError):
            condition_search.show_results(second, store=self.store, after=page["next_cursor"])
        bad = deepcopy(first)
        bad["image_payload"] = "DO_NOT_FORWARD"
        condition_search._seal(bad)
        with self.assertRaises(PhotographyError):
            condition_search.show_results(bad, store=self.store)
        bad = deepcopy(first)
        bad["results"].reverse()
        condition_search._seal(bad)
        with self.assertRaises(PhotographyError):
            condition_search.show_results(bad, store=self.store)

    def test_evaluated_conditions_cannot_drop_required_source_witnesses(self):
        self.seed_vectors()
        self.ocr("a", "needle")
        for pid in ("b", "c"):
            self.ocr(pid, "")
        snapshot = self.query([{"id": "A", "kind": "semantic", "query": "beach"},
                               {"id": "B", "kind": "ocr_contains", "text": "needle"}])
        choices = self.decisions(snapshot, {})
        snapshot["candidates"][0]["conditions"]["B"]["sources"] = []
        condition_search._seal(snapshot)
        with self.assertRaises(PhotographyError):
            condition_search.finalize_query(snapshot, choices, store=self.store)

        photo = self.store.photo("b")
        self.store.put_photo({**photo, "content_version": self.store.photo("a")["content_version"]})
        final = self.query([{"id": "D", "kind": "has_near_duplicate", "metric": "exact"}])
        for mutation in ("missing_own", "missing_peer", "wrong_peer"):
            pending = deepcopy(final)
            pending.update(stage="awaiting_semantic_decisions", results=[], normalization={}, decisions=None,
                           ranking_version=None, source_digest=None, finalized_at=None, evaluated_coverage={})
            cell = pending["candidates"][0]["conditions"]["D"]
            if mutation == "missing_own":
                cell["sources"] = []
            elif mutation == "missing_peer":
                cell["sources"] = cell["sources"][:1]
            else:
                cell["evidence"]["peer_id"] = "outside"
            condition_search._seal(pending)
            with self.subTest(mutation=mutation), self.assertRaises(PhotographyError):
                condition_search.finalize_query(
                    pending, {"schema": "condition-decisions-v1", "snapshot_id": pending["snapshot_id"], "pages": []},
                    store=self.store)

    def test_malformed_album_types_return_structured_snapshot_errors(self):
        self.color("a", .8)
        final = self.query([{"id": "A", "kind": "color_fraction", "color": "blue", "minimum": .2}])
        for value in (123, None, {}, [], "", "not-a-uuid"):
            bad = deepcopy(final)
            bad["album"]["id"] = value
            with self.subTest(value=value), self.assertRaises(PhotographyError) as failure:
                condition_search.show_results(bad, store=self.store)
            self.assertEqual(failure.exception.code, "QUERY_SNAPSHOT_INVALID")
        bad = deepcopy(final)
        bad["album"]["image_payload"] = "DO_NOT_FORWARD"
        condition_search._seal(bad)
        with self.assertRaises(PhotographyError):
            condition_search.show_results(bad, store=self.store)

    def test_exact_pair_paging_compares_content_linearly_not_all_combinations(self):
        comparisons = 0

        class Content(str):
            __hash__ = str.__hash__

            def __eq__(self, other):
                nonlocal comparisons
                comparisons += 1
                if comparisons > 32000:
                    raise AssertionError("Exact pairing must not compare every image with every other image.")
                return str.__eq__(self, other)

            def __ne__(self, other):
                return not self == other

        rows = [{"photo_id": f"p{index:05d}"} for index in range(8000)]
        values = {row["photo_id"]: Content(f"group-{index // 2}") for index, row in enumerate(rows)}
        total, pairs = condition_search._exact_pair_page(rows, values, 3900, 100)
        self.assertEqual(total, 4000)
        self.assertEqual(len(pairs), 100)
        self.assertEqual((pairs[0]["photo_id_a"], pairs[0]["photo_id_b"]), ("p07800", "p07801"))
        self.assertEqual((pairs[-1]["photo_id_a"], pairs[-1]["photo_id_b"]), ("p07998", "p07999"))
        self.assertLess(comparisons, 32000)
        rows = [{"photo_id": pid} for pid in ("a", "b", "c", "d", "e")]
        total, pairs = condition_search._exact_pair_page(rows, {"a": "x", "b": "y", "c": "x", "d": "y", "e": "x"}, 1, 2)
        self.assertEqual(total, 4)
        self.assertEqual([(pair["photo_id_a"], pair["photo_id_b"]) for pair in pairs], [("a", "e"), ("b", "d")])

    def test_all_semantic_candidates_exceed_legacy_thousand_result_limit(self):
        self.seed_vectors()
        with self.store.transaction():
            for index in range(1000):
                pid = f"extra{index:04d}"
                self.photo(pid)
                self.vector(pid)
        snapshot = self.query([{"id": "A", "kind": "semantic", "query": "beach"}],
                              semantic_candidates="all", review_page_size=1000)
        self.assertEqual(snapshot["candidate_count"], 1003)
        self.assertFalse(snapshot["retrieval"]["semantic_retrieval_limited"])
        self.assertEqual(condition_search.summary(snapshot)["semantic_conditions"][0]["page_count"], 2)
        self.assertEqual(self.encoder.calls, ["beach"])
        final = condition_search.finalize_query(snapshot, self.decisions(snapshot, {"A": ["extra0999"]}), store=self.store)
        self.assertEqual([row["photo_id"] for row in final["results"]], ["extra0999"])

    def test_duplicate_pair_view_does_not_turn_a_chain_into_all_pair_matches(self):
        for pid, value in (("a", 0), ("b", 1), ("c", 3)):
            self.hash(pid, value)
        condition = {"id": "D", "kind": "has_near_duplicate", "metric": "hamming", "max_distance": 1}
        with self.read_only():
            snapshot = self.query([condition])
            first = condition_search.duplicate_pairs(snapshot, "D", store=self.store, limit=1)
            second = condition_search.duplicate_pairs(snapshot, "D", store=self.store, limit=1, after=first["next_cursor"])
        self.assertEqual(first["total"], 2)
        self.assertEqual([(pair["photo_id_a"], pair["photo_id_b"]) for pair in first["pairs"] + second["pairs"]],
                         [("a", "b"), ("b", "c")])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(first["grouping"], "pairwise_only_not_transitive")
        folder = virtual_folders.create_folder("without bridge", store=self.store)["folder"]["folder_id"]
        virtual_folders.add_photos(folder, ["a", "c"], store=self.store)
        scoped = self.query([condition], scope={"folder_ids": [folder]})
        self.assertEqual(condition_search.duplicate_pairs(scoped, "D", store=self.store)["total"], 0)

    def test_exact_duplicate_pairs_need_no_feature_profiles_or_image_models(self):
        photo = self.store.photo("b")
        self.store.put_photo({**photo, "content_version": self.store.photo("a")["content_version"]})
        with self.read_only():
            snapshot = self.query([{"id": "D", "kind": "has_near_duplicate", "metric": "exact"}])
            pairs = condition_search.duplicate_pairs(snapshot, "D", store=self.store)
        self.assertEqual(pairs["pairs"], [{"photo_id_a": "a", "photo_id_b": "b", "distance": 0, "metric": "exact"}])
        self.assertEqual(self.encoder.calls, [])


if __name__ == "__main__":
    unittest.main()
