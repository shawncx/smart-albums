from copy import deepcopy
import itertools
import random
import unittest
from unittest.mock import patch

from tests.duplicate_fixtures import DuplicateFixture
from photography_lib import duplicates, virtual_folders
from photography_lib.config import PhotographyError
from photography_lib.feature_profiles import default_profile
from photography_lib.sqlite_storage import SQLiteStorage


class DuplicateTests(DuplicateFixture, unittest.TestCase):
    def test_exact_works_without_profile_previews_originals_or_models(self):
        self.photo("a", content="copy", preview=False)
        self.photo("b", content="copy", preview=False)
        self.photo("c", preview=False)
        before = self.store.db.total_changes
        with patch("photography_lib.feature_index.inspect_feature", side_effect=AssertionError("No feature read")), \
             patch("photography_lib.feature_inputs.load_input", side_effect=AssertionError("No pixels")), \
             patch.object(self.store, "assert_writable", side_effect=AssertionError("No write")):
            result = self.scan(mode="exact")
            self.assertEqual(result["groups"][0]["member_ids"], ["a", "b"])
            self.assertTrue(result["complete"])
            self.assertEqual(self.read_page(result)["total"], 1)
        self.assertEqual(self.store.db.total_changes, before)
        self.assertEqual(result["summary"]["pair_count"], 1)

    def test_chain_threshold_and_direct_pairs(self):
        for pid, value in (("a", 0), ("b", 1), ("c", 3), ("d", (1 << 64) - 1)):
            self.photo(pid)
            self.hash(pid, value)
        result = self.scan(mode="similar", max_distance=1)
        group = result["groups"][0]
        self.assertEqual(group["member_ids"], ["a", "b", "c"])
        page = self.read_page(result, view="pairs", group_id=group["group_id"], limit=1)
        self.assertEqual(page["pairs"][0], {"photo_id_a": "a", "photo_id_b": "b", "metric": "hamming", "distance": 1})
        second = self.read_page(result, view="pairs", group_id=group["group_id"], limit=1, after=page["next_cursor"])
        self.assertEqual([(p["photo_id_a"], p["photo_id_b"]) for p in second["pairs"]], [("b", "c")])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(group["grouping"], duplicates.GROUPING)
        self.assertEqual(self.scan(mode="similar", max_distance=0)["summary"]["pair_count"], 0)

    def test_mixed_exact_precedence_and_missing_hash_not_borrowed(self):
        self.photo("a", content="same")
        self.photo("b", content="same")
        self.photo("c")
        self.hash("a", 0)
        self.hash("c", 1)
        result = self.scan(max_distance=1)
        group = result["groups"][0]
        self.assertEqual(group["kind"], "mixed")
        self.assertEqual(group["exact_subgroups"], [["a", "b"]])
        self.assertEqual(group["pair_counts"], {"exact": 1, "similar": 1, "total": 2})
        self.assertFalse(result["complete"])
        pairs = self.read_page(result, view="pairs", group_id=group["group_id"], limit="all")["pairs"]
        self.assertEqual([(p["photo_id_a"], p["photo_id_b"]) for p in pairs], [("a", "b"), ("a", "c")])
        self.hash("b", 0)
        result = self.scan(max_distance=1)
        self.assertTrue(result["complete"])
        self.assertEqual(result["groups"][0]["pair_counts"], {"exact": 1, "similar": 2, "total": 3})

    def test_empty_single_invalid_and_profile_selection(self):
        self.assertTrue(self.scan()["complete"])
        self.photo("a")
        self.assertEqual(self.scan()["coverage"]["similar"]["counts"], {"not_configured": 1})
        self.assertEqual(self.scan(mode="similar")["status"], "partial")
        self.assertTrue(self.scan(photo_ids=[])["complete"])
        self.assertTrue(self.scan(mode="exact")["complete"])
        self.photo("bad", state="error")
        self.assertFalse(self.scan(mode="exact")["complete"])
        self.assertEqual(self.scan(mode="exact")["coverage"]["exact"]["counts"]["invalid_input"], 1)
        color = self.store.put_feature_profile(default_profile("color"))
        for args in ({"profile_id": color}, {"photo_ids": ["absent"]}, {"photo_ids": [None]},
                     {"mode": "exact", "max_distance": 8}, {"max_distance": -1}, {"max_distance": 65},
                     {"max_distance": True}, {"all_photos": False}, {"all_photos": True, "photo_ids": []}):
            with self.subTest(args=args), self.assertRaises(PhotographyError):
                self.scan(**args)

    def test_missing_stale_invalid_hashes_and_explicit_profile(self):
        for pid in ("a", "b", "c", "d"):
            self.photo(pid)
        self.hash("a", 0)
        self.hash("b", 0)
        self.photo("b", content="changed")
        self.photo("d", state="error")
        snapshot = self.scan(mode="similar", profile_id=self.profile_id)
        self.assertEqual(snapshot["coverage"]["similar"]["counts"],
                         {"eligible": 1, "stale": 1, "missing": 1, "invalid_input": 1})
        self.read_page(snapshot)
        with patch("photography_lib.feature_index.inspect_feature", return_value=({"status": "ready"}, {"payload": {"complete": False}})):
            result = self.scan(mode="similar", photo_ids=["a"])
        self.assertEqual(result["coverage"]["similar"]["counts"], {"incomplete_result": 1})

    def test_corrupt_saved_result_and_other_profiles_do_not_supply_evidence(self):
        self.photo("a")
        self.hash("a", 0)
        other = deepcopy(self.profile)
        other["runtime"]["packages"]["Pillow"] = "fixture-other-version"
        other_id = self.store.put_feature_profile(other)
        self.assertEqual(self.scan(mode="similar", profile_id=other_id)["coverage"]["similar"]["counts"], {"missing": 1})
        # Deliberately corrupt only this disposable fixture, bypassing its normal immutable-write guard.
        self.store.db.execute("DROP TRIGGER image_feature_results_update_immutable")
        self.store.db.execute("UPDATE image_feature_results SET payload_hash=?", ("0" * 64,))
        result = self.scan(mode="similar")
        self.assertEqual(result["coverage"]["similar"]["counts"], {"invalid_result": 1})
        self.assertEqual(self.read_page(result)["total"], 0)

    def test_distinct_bytes_with_identical_hash_stay_suspected_duplicates(self):
        for pid in ("a", "b"):
            self.photo(pid)
            self.hash(pid, 12)
        result = self.scan(max_distance=0)
        self.assertEqual(result["groups"][0]["kind"], "similar")
        self.assertEqual(result["groups"][0]["pair_counts"], {"exact": 0, "similar": 1, "total": 1})

    def test_comparison_runs_after_read_transaction_is_released(self):
        self.photo("a")
        original = duplicates._build_groups

        def build(*args):
            self.assertFalse(self.store.db.in_transaction)
            return original(*args)

        with patch("photography_lib.duplicates._build_groups", side_effect=build):
            self.scan(mode="exact")

    def test_scopes_and_frozen_results_after_album_changes(self):
        for pid in ("a", "b", "c"):
            self.photo(pid, content="same")
        a = virtual_folders.create_folder("First", store=self.store)["folder"]["folder_id"]
        b = virtual_folders.create_folder("Second", store=self.store)["folder"]["folder_id"]
        virtual_folders.add_photos(a, ["a", "b"], store=self.store)
        virtual_folders.add_photos(b, ["b", "c"], store=self.store)
        result = self.scan(folder_ids=[b, a], folder_match="union", mode="exact")
        self.assertEqual(result["summary"]["matched_photo_count"], 3)
        self.assertEqual(self.scan(folder_ids=[a, b], folder_match="intersection", mode="exact")["summary"]["pair_count"], 0)
        selected = self.scan(photo_ids=["b", "a", "a"], mode="exact")
        reordered = self.scan(photo_ids=["a", "b"], mode="exact")
        self.assertEqual(selected["groups"], reordered["groups"])
        with self.assertRaises(PhotographyError):
            self.scan(folder_ids=[a, b], mode="exact")
        self.photo("a", content="new")
        virtual_folders.remove_photos(a, ["b"], store=self.store)
        with patch.object(self.store, "photo", side_effect=AssertionError("Historical view must not read photos")), \
             patch.object(self.store, "feature_result", side_effect=AssertionError("No current features")):
            self.assertEqual(self.read_page(result, view="pairs", group_id=result["groups"][0]["group_id"], limit="all")["total"], 3)
        self.assertEqual(self.scan(mode="exact")["summary"]["pair_count"], 1)

    def test_more_than_100_groups_and_members_are_fully_enumerable(self):
        with self.store.transaction():
            for index in range(205):
                for suffix in ("a", "b"):
                    self.photo(f"{index:03}-{suffix}", content=str(index), preview=False)
        snapshot = self.scan(mode="exact")
        self.assertEqual(snapshot["summary"]["group_count"], 205)
        seen, after = [], ""
        while True:
            page = self.read_page(snapshot, limit=33, after=after)
            self.assertTrue(all("member_ids" not in g for g in page["items"]))
            seen.extend(g["group_id"] for g in page["items"])
            after = page["next_cursor"]
            if after is None:
                break
        self.assertEqual(len(set(seen)), 205)
        with self.store.transaction():
            for index in range(205):
                self.photo(f"{index:03}-a", content="all", preview=False)
        snapshot = self.scan(mode="exact")
        group = snapshot["groups"][0]
        first = self.read_page(snapshot, view="group", group_id=group["group_id"], limit=100)
        second = self.read_page(snapshot, view="group", group_id=group["group_id"], limit="all", after=first["next_cursor"])
        self.assertEqual(len(first["items"]) + len(second["items"]), 205)
        self.assertEqual(self.read_page(snapshot, view="pairs", group_id=group["group_id"], limit="all")["total"], 20910)

    def test_pairs_match_independent_bruteforce_and_component_membership(self):
        randomizer = random.Random(134)
        values = {str(i): randomizer.getrandbits(10) for i in range(30)}
        with self.store.transaction():
            for pid, value in values.items():
                self.photo(pid)
                self.hash(pid, value)
        result = self.scan(mode="similar", max_distance=2)
        expected = {(a, b) for a, b in itertools.combinations(sorted(values), 2)
                    if bin(values[a] ^ values[b]).count("1") <= 2}
        actual = set()
        for group in result["groups"]:
            after = ""
            while True:
                page = self.read_page(result, view="pairs", group_id=group["group_id"], limit=3, after=after)
                for pair in page["pairs"]:
                    key = pair["photo_id_a"], pair["photo_id_b"]
                    self.assertNotIn(key, actual)
                    actual.add(key)
                after = page["next_cursor"]
                if after is None:
                    break
        self.assertEqual(expected, actual)
        self.assertEqual(result["summary"]["pair_count"], len(expected))

    def test_malformed_snapshot_foreign_album_and_cursor_reuse(self):
        for pid in ("a", "b", "c"):
            self.photo(pid, content="same")
        result = self.scan(mode="exact")
        group_id = result["groups"][0]["group_id"]
        page = self.read_page(result, view="group", group_id=group_id, limit=1)
        for source, view in ((self.scan(mode="exact"), "group"), (result, "pairs")):
            with self.assertRaises(PhotographyError):
                self.read_page(source, view=view, group_id=group_id, after=page["next_cursor"])
        for mutation in (lambda s: s["groups"][0]["member_ids"].append("absent"),
                         lambda s: s["inputs"][0]["match"].update(best_match={"photo_id": "a", "metric": "exact", "distance": 0}),
                         lambda s: s["groups"][0]["pair_counts"].update(total=999)):
            broken = deepcopy(result)
            mutation(broken)
            self.seal(broken)
            with self.assertRaises(PhotographyError):
                self.read_page(broken)
        other = SQLiteStorage.create(self.root / "other.sqlite")
        with other, self.assertRaises(PhotographyError) as raised:
            duplicates.page(result, store=other)
        self.assertEqual(raised.exception.code, "ALBUM_MISMATCH")


if __name__ == "__main__":
    unittest.main()
