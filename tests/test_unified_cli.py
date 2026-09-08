"""Numbered unified search through the CLI, without real models or original reads."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

from photography_lib import cli, unified_search, virtual_folders
from photography_lib import feature_inputs
from photography_lib.config import PhotographyError
from photography_lib.feature_profiles import default_profile
from tests import test_management as fixtures


class UnifiedCliTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagementTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.base, self.store = self.fixture.base, self.fixture.store
        self.config, self.ids = self.fixture.config, self.fixture.ids
        self.fixture.configure()
        for pid, vector in zip(self.ids, ((1., 0., 0.), (.8, .6, 0.), (0., 1., 0.))):
            self.fixture.seed(pid, vector)
        self.encoder = fixtures.FakeEncoder(self.fixture.profile)

    def write(self, name, value):
        path = self.base / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def command(self, *args):
        with patch("photography_lib.siglip_embedding.SiglipEncoder", return_value=self.encoder):
            return self.fixture.command(*(str(arg) for arg in args))

    def error(self, *args, code="INVALID_ARGUMENT"):
        with self.assertRaises(PhotographyError) as raised:
            self.command(*args)
        self.assertEqual(raised.exception.code, code)

    def search(self, *args, name="private.json"):
        path = self.base / name
        return self.command("search", "sky", "--output", path, *args), path

    def select(self, evidence, snapshot, numbers, *, name="selected.json", html=None):
        ids = self.write("numbers.json", numbers)
        output = self.base / name
        arguments = ["show-results", snapshot, "--ids-file", ids, "--review-id", evidence["review_id"],
                     "--output", output]
        if html is not None:
            arguments.extend(("--html", html))
        return self.command(*arguments), output, ids

    def test_default_route_saves_private_snapshot_and_returns_compact_numbered_evidence(self):
        before = self.store.db.total_changes
        evidence, path = self.search()
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(evidence["schema"], unified_search.REVIEW_SCHEMA)
        self.assertEqual(snapshot["schema"], unified_search.SNAPSHOT_SCHEMA)
        self.assertEqual(snapshot["query"]["candidate_limit"], 100)
        self.assertEqual([row["id"] for row in evidence["candidates"]], [1, 2, 3])
        public = json.dumps(evidence)
        for forbidden in ("coverage_items", "input_image_hash", "vector_hash", "thumbnail_profile",
                          "result_id", "profile_id", "original_absolute_path", "data:image", self.ids[0]):
            self.assertNotIn(forbidden, public)
        self.assertIn("vector_hash", path.read_text(encoding="utf-8"))
        self.assertEqual(self.encoder.queries, ["sky"])
        self.assertEqual(self.store.db.total_changes, before)
        self.assertEqual(evidence["model_calls"], 1)
        reread = self.command("search-evidence", path)
        self.assertEqual(reread, {**{k: v for k, v in evidence.items() if k != "output"}, "model_calls": 0})
        self.assertEqual(reread["query_model_calls"], 1)
        self.assertEqual(self.encoder.queries, ["sky"])

    def test_limit_has_no_numeric_ceiling_or_automatic_byte_reduction(self):
        for limit in ("1001", str(2 ** 40), "all"):
            with self.subTest(limit=limit):
                evidence, path = self.search("--limit", limit)
                self.assertEqual(len(evidence["candidates"]), 3)
                expected = limit if limit == "all" else int(limit)
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["query"]["candidate_limit"], expected)
        evidence, _ = self.search("--limit", 1)
        self.assertEqual(len(evidence["candidates"]), 1)

    def test_default_really_reviews_100_and_all_is_not_reduced_to_a_byte_budget(self):
        template = self.store.photo(self.ids[0])
        preview = self.fixture.previews[self.ids[0]]
        with self.store.transaction():
            for index in range(1100):
                pid = "photo_synthetic_" + f"{index:032d}"
                photo = {**template, "photo_id": pid,
                         "original_absolute_path": str(self.base / "offline" / (pid + ".jpg")),
                         "original_relative_path": None}
                self.store.put_photo(photo)
                self.store.put_thumbnail(photo, preview)
                self.fixture.seed(pid)
        default, _ = self.search()
        large, _ = self.search("--limit", 1001, name="large-private.json")
        expanded, _ = self.search("--limit", "all", name="all-private.json")
        self.assertEqual(len(default["candidates"]), 100)
        self.assertEqual(len(large["candidates"]), 1001)
        self.assertEqual(len(expanded["candidates"]), 1103)
        self.assertEqual([row["id"] for row in expanded["candidates"]], list(range(1, 1104)))
        self.assertGreater(len(json.dumps(expanded, separators=(",", ":")).encode("utf-8")), 4096)

    def test_query_file_uses_same_engine_and_explicit_cli_limit_override(self):
        specification = {"schema": unified_search.QUERY_SCHEMA, "query": "sky", "candidate_limit": 1,
                         "conditions": [{"id": "S", "kind": "semantic", "query": "sky"}]}
        source = self.write("query.json", specification)
        output = self.base / "private.json"
        result = self.command("search", "--query-file", source, "--output", output, "--limit", "all")
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(json.loads(source.read_text(encoding="utf-8")), specification)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["query"]["candidate_limit"], "all")

    def test_explicit_folder_scope_and_visual_intent_are_preserved(self):
        folder = virtual_folders.create_folder("one photo", store=self.store)["folder"]["folder_id"]
        virtual_folders.add_photos(folder, [self.ids[1]], store=self.store)
        output = self.base / "scoped.json"
        result = self.command("search", "a sky request", "--visual-query", "sky", "--folder-id", folder,
                              "--output", output)
        self.assertEqual(len(result["candidates"]), 1)
        snapshot = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["scope"]["folders"][0]["folder_id"], folder)
        self.assertEqual(snapshot["candidates"][0]["photo_id"], self.ids[1])
        self.assertEqual(self.encoder.queries, ["sky"])

    def test_structured_filter_and_optional_evidence_are_delivered_through_one_search(self):
        profile_id = self.store.put_feature_profile(default_profile("color"))
        profile = self.store.feature_profile(profile_id)
        for pid, blue in zip(self.ids[:2], (.9, .2)):
            payload = {"width": 32, "height": 24, "complete": True,
                       "palette": [{"rgb": [0, 0, 255], "fraction": blue},
                                   {"rgb": [255, 0, 0], "fraction": 1 - blue}],
                       "features": {"mean_saturation": 1., "low_saturation_fraction": 0.,
                                    "hue_histogram": [1.] + [0.] * 11}}
            self.store.put_feature_result(pid, profile_id,
                                          feature_inputs.manifest_for(pid, profile, store=self.store), payload)
        specification = {"query": "sky with blue", "operator": "and", "conditions": [
            {"id": "S", "kind": "semantic", "query": "sky with blue"},
            {"id": "C", "kind": "color_fraction", "color": "blue", "minimum": .5, "profile_id": profile_id}],
            "evidence_conditions": [
                {"id": "E", "kind": "color_fraction", "color": "red", "minimum": .5, "profile_id": profile_id}]}
        source = self.write("mixed-query.json", specification)
        output = self.base / "mixed-private.json"
        evidence = self.command("search", "--query-file", source, "--output", output)
        self.assertEqual(len(evidence["candidates"]), 1)
        self.assertIn("0.9", json.dumps(evidence))
        self.assertNotIn(profile_id, json.dumps(evidence))
        _, selected, _ = self.select(evidence, output, [1])
        rows = unified_search.selected_rows(json.loads(selected.read_text(encoding="utf-8")), store=self.store)
        self.assertEqual([row["photo_id"] for row in rows], self.ids[:1])
        self.assertEqual(self.encoder.queries, ["sky with blue"])

    def test_invalid_or_ambiguous_inputs_fail_before_encoding_or_export(self):
        source = self.write("query.json", {"query": "sky", "conditions": [
            {"id": "S", "kind": "semantic", "query": "sky"}]})
        output = self.base / "must-not-exist.json"
        for arguments in (
            ("search", "sky"),
            ("search", "--output", output),
            ("search", "sky", "--output", output, "--html", self.base / "raw.html"),
            ("search", "sky", "--output", output, "--after", "cursor"),
            ("search", "sky", "--query-file", source, "--output", output),
            ("search", "--query-file", source, "--visual-query", "sky", "--output", output),
            ("search", "--query-file", source, "--profile-id", self.fixture.profile_id, "--output", output),
            ("search", "--query-file", source, "--folder-id", "fake", "--output", output),
            ("search", "sky", "--mode", "semantic", "--query-file", source, "--output", output),
            ("search", "sky", "--mode", "metadata", "--limit", "all"),
        ):
            with self.subTest(arguments=arguments):
                self.error(*arguments)
                self.assertFalse(output.exists())
        self.assertEqual(self.encoder.queries, [])

    def test_export_preflight_protects_query_input_and_hardlink_race(self):
        source = self.write("query.json", {"query": "sky", "conditions": [
            {"id": "S", "kind": "semantic", "query": "sky"}]})
        before = source.read_bytes()
        self.error("search", "--query-file", source, "--output", source)
        self.assertEqual(self.encoder.queries, [])
        output = self.base / "race.json"
        self.encoder.on_encode = lambda: os.link(source, output)
        self.error("search", "--query-file", source, "--output", output, code="EXPORT_PATH_CHANGED")
        self.assertEqual(source.read_bytes(), before)

    def test_numbered_selection_summary_and_user_report_never_repeat_inference(self):
        evidence, snapshot = self.search()
        original = snapshot.read_bytes()
        html = self.base / "selected.html"
        summary, selected, _ = self.select(evidence, snapshot, [3, 1], html=html)
        self.assertEqual(summary["selected_count"], 2)
        self.assertNotIn("candidates", summary)
        self.assertNotIn("results", summary)
        self.assertNotIn("vector_hash", json.dumps(summary))
        self.assertEqual(snapshot.read_bytes(), original)
        saved = json.loads(selected.read_text(encoding="utf-8"))
        self.assertEqual(saved["stage"], "selected")
        rows = unified_search.selected_rows(saved, store=self.store)
        self.assertEqual([row["photo_id"] for row in rows], [self.ids[0], self.ids[2]])
        self.assertEqual(html.read_text(encoding="utf-8").count('src="data:image/jpeg;base64,'), 2)
        self.assertEqual(self.encoder.queries, ["sky"])

    def test_selection_requires_explicit_binding_and_separate_snapshot_destination(self):
        evidence, path = self.search()
        ids = self.write("numbers.json", [1])
        output = self.base / "selected.json"
        self.error("show-results", path, "--ids-file", ids, "--output", output)
        self.error("show-results", path, "--ids-file", ids, "--review-id", evidence["review_id"])
        for destination in (path, ids):
            self.error("show-results", path, "--ids-file", ids, "--review-id", evidence["review_id"],
                       "--output", destination)
        self.assertFalse(output.exists())

    def test_empty_numbered_selection_stays_empty_and_has_no_preview(self):
        evidence, snapshot = self.search()
        html = self.base / "empty.html"
        summary, selected, _ = self.select(evidence, snapshot, [], html=html)
        self.assertEqual(summary["selected_count"], 0)
        self.assertEqual(unified_search.selected_rows(json.loads(selected.read_text(encoding="utf-8")),
                                                       store=self.store), [])
        self.assertNotIn('src="data:image/jpeg;base64,', html.read_text(encoding="utf-8"))
        self.assertEqual(self.encoder.queries, ["sky"])

    def test_other_review_identifier_cannot_reinterpret_the_same_number_array(self):
        evidence, first = self.search()
        other, _ = self.search(name="other-private.json")
        self.assertNotEqual(evidence["review_id"], other["review_id"])
        numbers = self.write("numbers.json", [1])
        output = self.base / "invalid-selection.json"
        with self.assertRaises(PhotographyError):
            self.command("show-results", first, "--ids-file", numbers, "--review-id", other["review_id"],
                         "--output", output)
        self.assertFalse(output.exists())

    def test_numbered_folder_add_is_atomic_and_only_accepts_previously_selected_numbers(self):
        evidence, snapshot = self.search()
        _, selected, ids = self.select(evidence, snapshot, [1])
        folder = virtual_folders.create_folder("selected", store=self.store)["folder"]["folder_id"]
        before = self.store.db.total_changes
        result = self.command("folders", "add", folder, "--ids-file", ids, "--review-snapshot", selected,
                              "--review-id", evidence["review_id"])
        self.assertEqual(result["counts"]["added"], 1)
        self.assertNotIn("photo_ids", result)
        self.assertNotIn("photo_ids", result["source_review"])
        self.assertGreater(self.store.db.total_changes, before)
        self.assertEqual([row["photo_id"] for row in self.store.photos_in_folders([folder], "union")], self.ids[:1])
        invalid = self.write("other-numbers.json", [2])
        with self.assertRaises(PhotographyError):
            self.command("folders", "add", folder, "--ids-file", invalid, "--review-snapshot", selected,
                         "--review-id", evidence["review_id"])
        self.assertEqual([row["photo_id"] for row in self.store.photos_in_folders([folder], "union")], self.ids[:1])
        empty = self.write("empty.json", [])
        self.assertEqual(self.command("folders", "add", folder, "--ids-file", empty, "--review-snapshot", selected,
                                      "--review-id", evidence["review_id"])["counts"]["added"], 0)
        self.assertEqual(self.encoder.queries, ["sky"])

    def test_numbered_folder_add_rejects_stale_vectors_without_partial_membership(self):
        evidence, snapshot = self.search()
        _, selected, ids = self.select(evidence, snapshot, [1, 2])
        self.fixture.seed(self.ids[1], (0., 0., 1.))
        folder = virtual_folders.create_folder("empty", store=self.store)["folder"]["folder_id"]
        with self.assertRaises(PhotographyError):
            self.command("folders", "add", folder, "--ids-file", ids, "--review-snapshot", selected,
                         "--review-id", evidence["review_id"])
        self.assertEqual(self.store.photos_in_folders([folder], "union"), [])

    def test_explicit_legacy_modes_and_selected_snapshots_remain_compatible(self):
        legacy = self.command("search", "sky", "--mode", "semantic")
        self.assertEqual((legacy["schema"], legacy["limit"]), ("album-snapshot-v2", 10))
        path = self.write("legacy.json", legacy)
        ids = self.write("legacy-ids.json", [self.ids[0]])
        selected = self.command("show-results", path, "--ids-file", ids)
        self.assertEqual(selected["results"][0]["photo_id"], self.ids[0])
        self.error("show-results", path, "--ids-file", ids, "--review-id", "wrong-kind")
        literal = self.command("search", "JPG", "--mode", "metadata")
        self.assertEqual(literal["mode"], "metadata")

    def test_real_cli_serializes_compact_response_without_reintroducing_album_paths(self):
        output = io.StringIO()
        private = self.base / "cli-private.json"
        with patch("photography_lib.siglip_embedding.SiglipEncoder", return_value=self.encoder), redirect_stdout(output):
            code = cli.main(["--database", str(self.config.database_path), "management", "search",
                             "sky", "--output", str(private)])
        self.assertEqual(code, 0, output.getvalue())
        text = output.getvalue()
        result = json.loads(text)
        self.assertEqual(result["schema"], unified_search.REVIEW_SCHEMA)
        self.assertEqual(text.count("\n"), 1)
        self.assertNotIn("database_path", text)
        self.assertEqual(set(result["album"]), {"id"})


if __name__ == "__main__":
    unittest.main()
