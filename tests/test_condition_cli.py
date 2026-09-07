"""Stage-two CLI and user-only reports on project-local synthetic albums."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "photography" / "scripts"))

import test_management as fixtures
from photography_lib import cli, condition_search, exports, management_cli, virtual_folders
from photography_lib import condition_report as report_module
from photography_lib.condition_report import condition_report
from photography_lib.config import PhotographyError
from photography_lib.sqlite_storage import SQLiteStorage


class ConditionCliTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagementTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.base, self.store, self.config = self.fixture.base, self.fixture.store, self.fixture.config
        self.ids, self.profile_id = self.fixture.ids, self.fixture.profile_id

    def write(self, name, value):
        path = self.base / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def command(self, *arguments):
        return self.fixture.command(*(str(value) for value in arguments))

    def assert_invalid(self, function, *arguments, code="INVALID_ARGUMENT", **kwargs):
        with self.assertRaises(PhotographyError) as raised:
            function(*arguments, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def semantic(self, *, page_size=2, text="synthetic beach", extra_conditions=()):
        for photo_id, vector in zip(self.ids, ((1.0, 0.0, 0.0), (0.8, 0.6, 0.0), (0.0, 1.0, 0.0))):
            self.fixture.seed(photo_id, vector)
        specification = {
            "schema": "multi-condition-query-v1", "operator": "or", "semantic_candidates": "all",
            "review_page_size": page_size, "random_seed": "cli-fixture",
            "conditions": [{"id": "A", "kind": "semantic", "query": text,
                            "profile_id": self.profile_id}, *extra_conditions],
        }
        query_file = self.write("query.json", specification)
        output = self.base / "private.json"
        encoder = fixtures.FakeEncoder(self.fixture.profile)
        with patch("photography_lib.siglip_embedding.SiglipEncoder", return_value=encoder):
            result = self.command("query", "--query-file", query_file, "--output", output)
        return result, output, encoder

    def decisions(self, summary, snapshot, matched=()):
        pages = []
        for condition in summary["semantic_conditions"]:
            for index in range(condition["page_count"]):
                evidence = self.command("query-evidence", snapshot, "--condition-id", condition["condition_id"],
                                        "--page", index)
                pages.append({"condition_id": condition["condition_id"], "page_id": evidence["page_id"],
                              "matched_photo_ids": [row["photo_id"] for row in evidence["items"]
                                                    if row["photo_id"] in matched]})
        return {"schema": "condition-decisions-v1", "snapshot_id": summary["snapshot_id"], "pages": pages}

    def finalized(self, matched=None, **kwargs):
        summary, snapshot, encoder = self.semantic(**kwargs)
        decision = self.write("decisions.json", self.decisions(summary, snapshot,
                                                              self.ids[:2] if matched is None else matched))
        ranked = self.base / "ranked.json"
        result = self.command("finalize-query", snapshot, "--decisions-file", decision, "--output", ranked)
        return result, ranked, snapshot, decision, encoder

    def duplicate_snapshot(self):
        original = self.store.photo(self.ids[0])
        for photo_id in self.ids[1:3]:
            photo = self.store.photo(photo_id)
            photo["content_version"] = original["content_version"]
            self.store.put_photo(photo)
            self.store.put_thumbnail(photo, self.fixture.previews[self.ids[0]])
        query = self.write("duplicate-query.json", {
            "schema": "multi-condition-query-v1", "operator": "or",
            "conditions": [{"id": "D", "kind": "has_near_duplicate", "metric": "exact"}],
        })
        snapshot = self.base / "duplicates.json"
        result = self.command("query", "--query-file", query, "--output", snapshot)
        self.assertEqual(result["stage"], "finalized")
        return snapshot

    def test_commands_parse_defaults_required_inputs_and_mutually_exclusive_sources(self):
        parsed = self.fixture.parse("query-evidence", "private.json", "--condition-id", "A")
        self.assertEqual(parsed.page, 0)
        parsed = self.fixture.parse("show-query-results", "ranked.json")
        self.assertEqual((parsed.limit, parsed.after), (100, ""))
        parsed = self.fixture.parse("query-pairs", "ranked.json", "--condition-id", "D")
        self.assertEqual((parsed.condition_id, parsed.limit, parsed.after, parsed.output), ("D", 100, "", None))
        parsed = self.fixture.parse("folders", "add", "folder", "--ids-file", "ids.json",
                                    "--query-snapshot", "ranked.json")
        self.assertIsNone(parsed.search_snapshot)
        self.assertEqual(parsed.query_snapshot, "ranked.json")
        with redirect_stderr(io.StringIO()):
            for arguments in (("query", "--query-file", "query.json"),
                              ("finalize-query", "private.json", "--output", "ranked.json"),
                              ("query-pairs", "ranked.json"),
                              ("query-pairs", "ranked.json", "--condition-id", "D", "--html", "pairs.html"),
                              ("folders", "add", "folder", "--ids-file", "ids.json",
                               "--query-snapshot", "ranked.json", "--search-snapshot", "old.json")):
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                    self.fixture.parse(*arguments)

    def test_query_stdout_is_summary_only_and_preserves_input(self):
        summary, snapshot, encoder = self.semantic()
        self.assertEqual(summary["schema"], "condition-query-summary-v1")
        self.assertEqual(summary["stage"], "awaiting_semantic_decisions")
        self.assertEqual(summary["model_calls"], 1)
        self.assertEqual(encoder.queries, ["synthetic beach"])
        self.assertEqual(summary["output"], str(snapshot))
        for key in ("conditions", "candidates", "results", "coverage", "evaluated_coverage"):
            self.assertNotIn(key, summary)
        private = json.loads(snapshot.read_text(encoding="utf-8"))
        self.assertEqual(private["query_model_calls"], 1)
        self.assertEqual(len(private["candidates"]), 3)
        self.assertEqual(json.loads((self.base / "query.json").read_text(encoding="utf-8"))["conditions"][0]["query"],
                         "synthetic beach")

    def test_condition_exports_cannot_truncate_inputs_linked_after_preflight(self):
        _, ranked, _, _, _ = self.finalized()
        before = ranked.read_bytes()
        for extension in (".json", ".html"):
            output = self.base / ("racing-page" + extension)
            real_write = exports.write_export

            def race(target, data):
                os.link(ranked, output)
                return real_write(target, data)

            module = management_cli if extension == ".json" else report_module
            with self.subTest(extension=extension), patch.object(module, "write_export", side_effect=race):
                self.assert_invalid(self.command, "show-query-results", ranked,
                                    "--output" if extension == ".json" else "--html", output, code="EXPORT_PATH_CHANGED")
            self.assertEqual(ranked.read_bytes(), before)
            self.assertTrue(output.samefile(ranked))
            output.unlink()

    def test_evidence_contains_only_query_numeric_similarity_and_identity(self):
        summary, snapshot, _ = self.semantic()
        evidence = self.command("query-evidence", snapshot, "--condition-id", "A")
        self.assertEqual(evidence["page"], 0)
        self.assertEqual(evidence["page_count"], 2)
        self.assertEqual(evidence["snapshot_id"], summary["snapshot_id"])
        expected = {"photo_id", "score", "profile_id", "result_id", "content_version", "thumbnail_profile",
                    "input_image_hash", "vector_hash", "candidate_rank", "score_gap_from_best", "score_gap_to_next"}
        for row in evidence["items"]:
            self.assertEqual(set(row), expected)
        for forbidden in ("filename", "original_absolute_path", "snippet", "scene_id", "normalized_text",
                          "conditions", "data:image", "base64"):
            self.assertNotIn(forbidden, json.dumps(evidence))
        stream = io.StringIO()
        with redirect_stdout(stream):
            status = cli.main(["--database", str(self.config.database_path), "management", "query-evidence",
                               str(snapshot), "--condition-id", "A"])
        self.assertEqual(status, 0)
        printed = json.loads(stream.getvalue())
        self.assertEqual(printed["album"], {"id": self.store.album()["id"]})
        self.assertNotIn("database_path", stream.getvalue())
        self.assertNotIn(self.store.album()["name"], stream.getvalue())
        self.assert_invalid(self.command, "query-evidence", snapshot, "--condition-id", "missing")
        self.assert_invalid(self.command, "query-evidence", snapshot, "--condition-id", "A", "--page", -1)

    def test_finalize_retains_historical_calls_and_requires_every_page(self):
        summary, snapshot, encoder = self.semantic(page_size=1)
        valid = self.decisions(summary, snapshot, self.ids[:2])
        invalid = [None, {}, {"schema": "condition-decisions-v1", "snapshot_id": summary["snapshot_id"], "pages": []},
                   {**valid, "snapshot_id": "another"}, {**valid, "pages": valid["pages"][:-1]},
                   {**valid, "pages": [*valid["pages"], valid["pages"][0]]}]
        foreign_id = deepcopy(valid)
        foreign_id["pages"][0]["matched_photo_ids"] = [self.ids[5]]
        invalid.append(foreign_id)
        wrong_condition = deepcopy(valid)
        wrong_condition["pages"][0]["condition_id"] = "B"
        invalid.append(wrong_condition)
        output = self.base / "ranked.json"
        for value in invalid:
            with self.subTest(value=value):
                decision = self.write("bad-decisions.json", value)
                self.assert_invalid(self.command, "finalize-query", snapshot, "--decisions-file", decision,
                                    "--output", output, code="QUERY_SNAPSHOT_INVALID")
                self.assertFalse(output.exists())
        decision = self.write("decisions.json", valid)
        original = snapshot.read_bytes(), decision.read_bytes()
        result = self.command("finalize-query", snapshot, "--decisions-file", decision, "--output", output)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["result_count"], 2)
        self.assertNotIn("results", result)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["query_model_calls"], 1)
        self.assertEqual((snapshot.read_bytes(), decision.read_bytes()), original)
        self.assertEqual(len(encoder.queries), 1)

    def test_explicit_empty_decisions_are_valid_and_render_no_photos(self):
        _, ranked, _, _, _ = self.finalized(matched=[])
        with patch("photography_lib.condition_report._preview", side_effect=AssertionError("no selected images")):
            page = self.command("show-query-results", ranked, "--html", self.base / "empty.html")
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["candidate_count"], 3)
        self.assertEqual(page["results"], [])
        self.assertIsNone(page["next_cursor"])
        self.assertEqual(page["evaluated_coverage"]["A"]["not_matched"], 3)

    def test_json_and_html_page_only_selected_rows_and_keep_global_rank(self):
        _, ranked, _, _, _ = self.finalized(matched=self.ids[:3])
        first = self.command("show-query-results", ranked, "--limit", 1)
        self.assertEqual(first["results"][0]["result_rank"], 1)
        output, report = self.base / "page.json", self.base / "page.html"
        from photography_lib import condition_report as renderer
        preview = renderer._preview
        with patch.object(renderer, "_preview", wraps=preview) as read:
            page = self.command("show-query-results", ranked, "--limit", 1, "--after", first["next_cursor"],
                                "--output", output, "--html", report)
        self.assertEqual(page["results"][0]["result_rank"], 2)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(read.call_args.args[0]["photo_id"], page["results"][0]["photo_id"])
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), page)
        markup = report.read_text(encoding="utf-8")
        self.assertEqual(markup.count("<img "), 1)
        self.assertIn("全局排名 2", markup)
        self.assertIn("Content-Security-Policy", markup)
        self.assertIn("form-action 'none'", markup)
        for forbidden in ("<script", "<form", "<input", "<button", "http://", "https://"):
            self.assertNotIn(forbidden, markup)
        self.assertEqual(page["coverage"]["A"]["not_reviewed"], 3)
        self.assertEqual(page["evaluated_coverage"]["A"]["matched"], 3)

    def test_report_escapes_user_conditions_scope_and_snippets(self):
        _, ranked, _, _, _ = self.finalized(text="<script>alert('query')</script>")
        page = self.command("show-query-results", ranked, "--limit", 1)
        hostile = '<img src=x onerror="alert(1)">'
        page["album"]["name"] = hostile
        page["scope"] = {"kind": "virtual_folders", "match": "union",
                         "folders": [{"folder_id": "folder-saved", "name": hostile}]}
        page["conditions"].append({"id": "OCR", "kind": "ocr_contains", "text": hostile,
                                   "profile_id": "saved-ocr", "scoring": "exact"})
        page["results"][0]["conditions"]["OCR"] = {
            "status": "matched", "raw_score": 1.0, "normalized_score": 1.0,
            "score_type": "exact", "direction": "higher", "sources": [], "evidence": {"snippet": hostile},
            "reason": None,
        }
        page["results"][0]["matched_count"] += 1
        page["results"][0]["matched_condition_ids"].append("OCR")
        output = self.base / "escaped.html"
        condition_report(page, output, config=self.config, store=self.store)
        markup = output.read_text(encoding="utf-8")
        self.assertNotIn(hostile, markup)
        self.assertNotIn("<script", markup)
        self.assertIn("&lt;script&gt;", markup)
        self.assertIn("&lt;img src=x", markup)

    def test_query_targets_rejected_before_encoding_and_preserve_inputs(self):
        query = self.write("query.json", {"schema": "multi-condition-query-v1"})
        before = query.read_bytes()
        targets = [query, self.base / "wrong.txt", self.config.model_cache_root / "out.json",
                   self.config.database_path]
        hardlink = self.base / "linked.json"
        os.link(query, hardlink)
        targets.append(hardlink)
        with patch.object(condition_search, "query", side_effect=AssertionError("must validate target first")):
            for target in targets:
                with self.subTest(target=target):
                    self.assert_invalid(self.command, "query", "--query-file", query, "--output", target)
        self.assertEqual(query.read_bytes(), before)

    def test_finalize_cannot_overwrite_candidate_or_decisions(self):
        summary, snapshot, _ = self.semantic()
        decision = self.write("decisions.json", self.decisions(summary, snapshot))
        before = snapshot.read_bytes(), decision.read_bytes()
        with patch.object(condition_search, "finalize_query", side_effect=AssertionError("input protection first")):
            for target in (snapshot, decision):
                self.assert_invalid(self.command, "finalize-query", snapshot, "--decisions-file", decision,
                                    "--output", target)
        self.assertEqual((snapshot.read_bytes(), decision.read_bytes()), before)

    def test_show_validates_all_destinations_before_sources_previews_or_any_writes(self):
        snapshot = self.write("untrusted.json", None)
        output = self.base / "safe.json"
        with patch.object(condition_search, "show_results", side_effect=AssertionError("destinations first")), \
                patch("photography_lib.condition_report._preview", side_effect=AssertionError("no preview")):
            self.assert_invalid(self.command, "show-query-results", snapshot, "--output", output,
                                "--html", self.config.model_cache_root / "bad.html")
            self.assert_invalid(self.command, "show-query-results", snapshot, "--output", snapshot)
        self.assertFalse(output.exists())
        self.assertEqual(snapshot.read_text(encoding="utf-8"), "null")

    def test_wrong_album_and_malformed_page_never_read_previews(self):
        _, ranked, _, _, _ = self.finalized()
        page = self.command("show-query-results", ranked, "--limit", 1)
        output = self.base / "not-created.html"
        wrong_album = deepcopy(page)
        wrong_album["album"]["id"] = str(uuid4())
        malformed = deepcopy(page)
        malformed["results"][0]["result_rank"] = False
        with patch("photography_lib.condition_report._preview", side_effect=AssertionError("no preview")):
            self.assert_invalid(condition_report, wrong_album, output, config=self.config, store=self.store,
                                code="ALBUM_MISMATCH")
            self.assert_invalid(condition_report, malformed, output, config=self.config, store=self.store)
        self.assertFalse(output.exists())
        with SQLiteStorage.create(self.base / "other.sqlite") as other:
            args = self.fixture.parse("show-query-results", str(ranked))
            self.assert_invalid(management_cli.command, args, other, self.config, code="ALBUM_MISMATCH")

    def test_malformed_unfinalized_and_stale_snapshots_fail_without_exports(self):
        summary, snapshot, _ = self.semantic()
        output = self.base / "should-not-exist.json"
        self.assert_invalid(self.command, "show-query-results", snapshot, "--output", output,
                            code="QUERY_STAGE_INVALID")
        malformed = self.write("malformed.json", {})
        self.assert_invalid(self.command, "show-query-results", malformed, "--output", output,
                            code="QUERY_SNAPSHOT_INVALID")
        decision = self.write("decisions.json", self.decisions(summary, snapshot, self.ids[:1]))
        photo = self.store.photo(self.ids[0])
        photo["content_version"] = hashlib.sha256(b"changed synthetic input").hexdigest()
        self.store.put_photo(photo)
        with self.assertRaises(PhotographyError) as raised:
            self.command("finalize-query", snapshot, "--decisions-file", decision, "--output", output)
        self.assertIn("STALE", raised.exception.code)
        self.assertFalse(output.exists())

    def test_query_folder_add_requires_finalized_selected_ids_and_is_atomic(self):
        _, ranked, private, _, encoder = self.finalized()
        folder = virtual_folders.create_folder("query picks", store=self.store)["folder"]
        selected = self.write("ids.json", [self.ids[0], self.ids[0]])
        result = self.command("folders", "add", folder["folder_id"], "--ids-file", selected,
                              "--query-snapshot", ranked)
        self.assertEqual(result["counts"]["added"], 1)
        self.assertEqual(result["counts"]["duplicates"], 1)
        self.assertEqual(result["source_query"]["photo_ids"], self.ids[:1])
        self.assertNotIn("source_search", result)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(len(encoder.queries), 1)
        invalid = self.write("not-selected.json", self.ids[:3])
        self.assert_invalid(self.command, "folders", "add", folder["folder_id"], "--ids-file", invalid,
                            "--query-snapshot", ranked)
        self.assertEqual(self.store.folder(folder["folder_id"])["photo_count"], 1)
        self.assert_invalid(self.command, "folders", "add", folder["folder_id"], "--ids-file", selected,
                            "--query-snapshot", private, code="QUERY_STAGE_INVALID")
        empty = self.write("empty-ids.json", [])
        empty_result = self.command("folders", "add", folder["folder_id"], "--ids-file", empty,
                                    "--query-snapshot", ranked)
        self.assertEqual(empty_result["counts"]["added"], 0)
        self.assertEqual(empty_result["source_query"]["selected_count"], 0)

    def test_query_folder_add_stale_source_rolls_back_and_null_is_not_manual(self):
        _, ranked, _, _, _ = self.finalized()
        folder = virtual_folders.create_folder("empty destination", store=self.store)["folder"]
        ids = self.write("ids.json", self.ids[:2])
        null = self.write("null.json", None)
        for flag in ("--search-snapshot", "--query-snapshot"):
            self.assert_invalid(self.command, "folders", "add", folder["folder_id"], "--ids-file", ids, flag, null)
        self.assert_invalid(virtual_folders.add_photos, folder["folder_id"], self.ids[:1], store=self.store,
                            query_snapshot={}, search_snapshot={})
        photo = self.store.photo(self.ids[1])
        photo["content_version"] = hashlib.sha256(b"stale").hexdigest()
        self.store.put_photo(photo)
        with self.assertRaises(PhotographyError) as raised:
            self.command("folders", "add", folder["folder_id"], "--ids-file", ids, "--query-snapshot", ranked)
        self.assertIn("STALE", raised.exception.code)
        self.assertEqual(self.store.folder(folder["folder_id"])["photo_count"], 0)

    def test_old_metadata_semantic_and_search_selected_folder_apis_unchanged(self):
        metadata = self.command("search", "STRASSE", "--mode", "metadata")
        self.assertEqual(metadata["schema"], "album-snapshot-v2")
        self.assertEqual([row["photo_id"] for row in metadata["items"]], [self.ids[1]])
        self.fixture.seed(self.ids[0])
        self.fixture.configure()
        encoder = fixtures.FakeEncoder(self.fixture.profile)
        with patch("photography_lib.siglip_embedding.SiglipEncoder", return_value=encoder):
            semantic = self.command("search", "legacy semantic", "--mode", "semantic")
        saved = self.write("old.json", semantic)
        ids = self.write("selected.json", self.ids[:1])
        displayed = self.command("show-results", saved, "--ids-file", ids)
        self.assertEqual(displayed["schema"], "album-snapshot-v2")
        folder = virtual_folders.create_folder("legacy destination", store=self.store)["folder"]
        added = self.command("folders", "add", folder["folder_id"], "--ids-file", ids, "--search-snapshot", saved)
        self.assertIn("source_search", added)
        self.assertNotIn("source_query", added)
        self.assertEqual(len(encoder.queries), 1)

    def test_readonly_reopen_roundtrip_never_changes_database_or_runs_models(self):
        query = self.write("exact.json", {
            "schema": "multi-condition-query-v1", "operator": "or",
            "conditions": [{"id": "D", "kind": "has_near_duplicate", "metric": "exact"}],
        })
        output = self.base / "exact-result.json"
        before = self.config.database_path.read_bytes()
        with patch("photography_lib.siglip_embedding.SiglipEncoder", side_effect=AssertionError("no model")):
            for arguments in (
                    ["query", "--query-file", str(query), "--output", str(output)],
                    ["show-query-results", str(output)],
                    ["search", "STRASSE", "--mode", "metadata"]):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    status = cli.main(["--database", str(self.config.database_path), "management", *arguments])
                self.assertEqual(status, 0, stream.getvalue())
                result = json.loads(stream.getvalue())
                self.assertEqual(result["model_calls"], 0)
                self.assertNotIn("candidates", result)
        self.assertEqual(self.config.database_path.read_bytes(), before)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["stage"], "finalized")
        with SQLiteStorage.open(self.config.database_path, writable=False) as readonly:
            self.assertEqual(readonly.folders(), [])
            self.assertEqual(readonly.photos(), self.store.photos())

    def test_query_pairs_exports_actual_pairs_with_independent_pagination_readonly(self):
        snapshot = self.duplicate_snapshot()
        before = self.config.database_path.read_bytes(), snapshot.read_bytes()
        page = self.command("query-pairs", snapshot, "--condition-id", "D", "--limit", 1)
        self.assertEqual(page["schema"], "condition-duplicate-pairs-v1")
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["grouping"], "pairwise_only_not_transitive")
        self.assertEqual(page["pairs"], [{"photo_id_a": self.ids[0], "photo_id_b": self.ids[1],
                                         "distance": 0, "metric": "exact"}])
        output = self.base / "pairs.json"
        with patch("photography_lib.siglip_embedding.SiglipEncoder", side_effect=AssertionError("no model")), \
                patch("photography_lib.condition_report._preview", side_effect=AssertionError("no preview")):
            stream = io.StringIO()
            with redirect_stdout(stream):
                status = cli.main(["--database", str(self.config.database_path), "management", "query-pairs",
                                   str(snapshot), "--condition-id", "D", "--limit", "2",
                                   "--after", page["next_cursor"], "--output", str(output)])
        self.assertEqual(status, 0, stream.getvalue())
        remaining = json.loads(stream.getvalue())
        self.assertEqual(remaining, json.loads(output.read_text(encoding="utf-8")))
        self.assertEqual([(row["photo_id_a"], row["photo_id_b"]) for row in remaining["pairs"]],
                         [(self.ids[0], self.ids[2]), (self.ids[1], self.ids[2])])
        self.assertIsNone(remaining["next_cursor"])
        self.assertEqual(remaining["condition"]["id"], "D")
        self.assertEqual(remaining["input_coverage"]["matched"], 3)
        self.assertEqual(remaining["model_calls"], 0)
        self.assertEqual(remaining["image_model_calls"], 0)
        self.assertEqual((self.config.database_path.read_bytes(), snapshot.read_bytes()), before)
        self.assertEqual(len(self.store.photos()), 6)
        self.assertEqual(self.store.folders(), [])

    def test_query_pairs_target_and_input_alias_protection_precedes_source_validation(self):
        snapshot = self.write("pair-input.json", None)
        alias = self.base / "pair-alias.json"
        os.link(snapshot, alias)
        targets = (snapshot, alias, self.base / "wrong.html", self.config.model_cache_root / "pairs.json")
        with patch.object(condition_search, "duplicate_pairs", side_effect=AssertionError("targets first")):
            for target in targets:
                with self.subTest(target=target):
                    self.assert_invalid(self.command, "query-pairs", snapshot, "--condition-id", "D",
                                        "--output", target)
        self.assertEqual(snapshot.read_text(encoding="utf-8"), "null")
        self.assertFalse((self.base / "wrong.html").exists())

    def test_query_pairs_rejects_wrong_condition_stage_cursor_album_and_stale_inputs(self):
        snapshot = self.duplicate_snapshot()
        output = self.base / "invalid-pairs.json"
        self.assert_invalid(self.command, "query-pairs", snapshot, "--condition-id", "missing",
                            "--output", output)
        self.assert_invalid(self.command, "query-pairs", snapshot, "--condition-id", "D", "--limit", 0)
        self.assert_invalid(self.command, "query-pairs", snapshot, "--condition-id", "D", "--after", "bad")
        result_cursor = self.command("show-query-results", snapshot, "--limit", 1)["next_cursor"]
        self.assert_invalid(self.command, "query-pairs", snapshot, "--condition-id", "D",
                            "--after", result_cursor)
        _, semantic_snapshot, _ = self.semantic()
        self.assert_invalid(self.command, "query-pairs", semantic_snapshot, "--condition-id", "A",
                            code="QUERY_STAGE_INVALID")
        with SQLiteStorage.create(self.base / "pair-other.sqlite") as other:
            args = self.fixture.parse("query-pairs", str(snapshot), "--condition-id", "D")
            self.assert_invalid(management_cli.command, args, other, self.config, code="ALBUM_MISMATCH")
        photo = self.store.photo(self.ids[0])
        photo["content_version"] = hashlib.sha256(b"changed duplicate original identity").hexdigest()
        self.store.put_photo(photo)
        with self.assertRaises(PhotographyError) as raised:
            self.command("query-pairs", snapshot, "--condition-id", "D", "--output", output)
        self.assertIn("STALE", raised.exception.code)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
