from __future__ import annotations

import builtins
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

from photography_lib import date_folders, virtual_folders
from photography_lib.config import PhotographyError
from photography_lib.fingerprints import fingerprint
from photography_lib.sqlite_storage import SQLiteStorage


class DateFolderTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parent / (".date-folders-fixture-" + uuid4().hex)
        self.base.mkdir()
        self.addCleanup(shutil.rmtree, self.base)
        self.store = SQLiteStorage.create(self.base / "dates.sqlite")
        self.addCleanup(self.store.close)

    def put(self, photo_id, value="2024:02:29 00:00:00", *, exif=None, **changes):
        record = {
            "photo_id": photo_id,
            "original_absolute_path": str(self.base / "offline" / (photo_id + ".jpg")),
            "original_relative_path": "offline/" + photo_id + ".jpg",
            "content_version": hashlib.sha256(photo_id.encode()).hexdigest(),
            "thumbnail_profile": "synthetic-no-preview",
            "size_bytes": 0, "mtime_ns": 1,
            "metadata": {"exif": {"datetime_original": value} if exif is None else exif},
            "ingest_state": "available", "original_status": "missing",
            "last_ingest_error": None, "last_path_error": None, "last_original_check": None,
            "created_at": "2026-09-06T00:00:00Z", "updated_at": "2026-09-06T00:00:00Z",
            "path_updated_at": None,
            **changes,
        }
        self.store.put_photo(record)
        return record

    def plan(self, *, photo_ids=None, all_photos=None, granularity="day", store=None):
        if all_photos is None:
            all_photos = photo_ids is None
        return date_folders.plan_date_organization(
            store=store or self.store, photo_ids=photo_ids, all_photos=all_photos, granularity=granularity)

    def apply(self, plan, *, store=None):
        return date_folders.apply_date_plan(plan, plan["digest"], store=store or self.store)

    def folder(self, name, ids=(), description=""):
        folder = virtual_folders.create_folder(name, store=self.store, description=description)["folder"]
        if ids:
            virtual_folders.add_photos(folder["folder_id"], list(ids), store=self.store)
        return folder["folder_id"]

    def members(self, folder_id, *, store=None):
        return [photo["photo_id"] for photo in (store or self.store).photos_in_folders([folder_id], "union")]

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(PhotographyError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    @staticmethod
    def redigest(plan):
        plan["digest"] = fingerprint({key: value for key, value in plan.items() if key != "digest"})
        return plan

    def test_exactly_one_explicit_selection_and_valid_granularity_required(self):
        for arguments in ({}, {"all_photos": True, "photo_ids": []},
                          {"all_photos": 1}, {"all_photos": "yes"},
                          {"photo_ids": "p1"}, {"photo_ids": ("p1",)},
                          {"photo_ids": [None]}, {"photo_ids": ["  "]},
                          {"photo_ids": [1]}, {"photo_ids": [True]}):
            with self.subTest(arguments=arguments):
                self.assert_error("INVALID_ARGUMENT", date_folders.plan_date_organization,
                                  store=self.store, granularity="day", **arguments)
        for granularity in (None, "", "week", "DAY", True, {}):
            self.assert_error("INVALID_ARGUMENT", date_folders.plan_date_organization,
                              store=self.store, all_photos=True, granularity=granularity)

    def test_explicit_empty_ids_are_noop_even_in_nonempty_album(self):
        self.put("p1")
        for plan in (self.plan(photo_ids=[]), self.plan(photo_ids=[], granularity="year")):
            self.assertEqual(plan["photo_ids"], [])
            self.assertEqual(plan["selection"], "ids")
            self.assertEqual(plan["folders"], [])
            self.assertEqual(plan["counts"]["selected"], 0)
            result = self.apply(plan)
            self.assertEqual(result["counts"]["added"], 0)
            self.assertEqual(result["counts"]["created"], 0)
        self.assertEqual(self.store.folders(), [])

    def test_all_empty_album_is_explicit_noop(self):
        plan = self.plan()
        self.assertEqual(plan["selection"], "all")
        self.assertEqual(plan["counts"]["selected"], 0)
        self.assertEqual(self.apply(plan)["counts"]["added"], 0)

    def test_unknown_photo_aborts_entire_plan(self):
        self.put("p1")
        self.assert_error("PHOTO_NOT_FOUND", self.plan, photo_ids=["p1", "unknown"])
        self.assertEqual(self.store.folders(), [])

    def test_granularities_use_camera_calendar_not_offset_or_machine_timezone(self):
        cases = [
            ("p1", "2023:12:31 23:59:59", "-12:00", "2023-12-31"),
            ("p2", "2024:01:01 00:00:00", "+14:00", "2024-01-01"),
            ("p3", "2024:02:29 00:00:00", None, "2024-02-29"),
            ("p4", "2000:02:29 12:34:56", "+05:30", "2000-02-29"),
            ("p5", "0001:01:01 00:00:00", None, "0001-01-01"),
            ("p6", "9999:12:31 23:59:59", "-01:00", "9999-12-31"),
        ]
        for photo_id, value, offset, _ in cases:
            exif = {"datetime_original": value}
            if offset is not None:
                exif["offset_time_original"] = offset
            self.put(photo_id, exif=exif)
        for granularity, width in (("year", 4), ("month", 7), ("day", 10)):
            with self.subTest(granularity=granularity), patch.dict("os.environ", {"TZ": "Pacific/Honolulu"}):
                plan = self.plan(granularity=granularity)
                assigned = {photo_id: target["name"] for target in plan["folders"] for photo_id in target["photo_ids"]}
                self.assertEqual(assigned, {photo_id: name[:width] for photo_id, _, _, name in cases})
                self.assertEqual(plan["skipped"], [])
                self.assertEqual(plan["counts"]["expected_added"], len(cases))
        self.assertEqual(self.store.folders(), [])

    def test_missing_and_invalid_exif_dates_have_explicit_skip_reasons(self):
        cases = [
            (None, "missing_datetime_original"), ("", "missing_datetime_original"),
            (7, "invalid_datetime_original_type"), (True, "invalid_datetime_original_type"),
            (["2024:02:29 00:00:00"], "invalid_datetime_original_type"),
            ({"date": "2024:02:29 00:00:00"}, "invalid_datetime_original_type"),
            ("2024-02-29 00:00:00", "invalid_datetime_original_format"),
            ("2024:2:29 00:00:00", "invalid_datetime_original_format"),
            ("2024:02:29 00:00:00Z", "invalid_datetime_original_format"),
            ("2024:02:29 00:00:00\n", "invalid_datetime_original_format"),
            ("２０２４:02:29 00:00:00", "invalid_datetime_original_format"),
            ("2023:02:29 00:00:00", "invalid_datetime_original_value"),
            ("1900:02:29 00:00:00", "invalid_datetime_original_value"),
            ("2024:04:31 00:00:00", "invalid_datetime_original_value"),
            ("0000:01:01 00:00:00", "invalid_datetime_original_value"),
            ("2024:13:01 00:00:00", "invalid_datetime_original_value"),
            ("2024:00:01 00:00:00", "invalid_datetime_original_value"),
            ("2024:01:00 00:00:00", "invalid_datetime_original_value"),
            ("2024:01:01 24:00:00", "invalid_datetime_original_value"),
            ("2024:01:01 23:60:00", "invalid_datetime_original_value"),
            ("2024:01:01 23:59:60", "invalid_datetime_original_value"),
        ]
        for index, (value, _) in enumerate(cases):
            self.put(f"p{index:02d}", value)
        self.put("without-exif", metadata={"datetime_original": "2024:02:29 00:00:00"})
        self.put("wrong-exif-type", exif=["2024:02:29 00:00:00"])
        self.put("wrong-exif-field", exif={"datetime": "2024:02:29 00:00:00"})
        plan = self.plan()
        reasons = {item["photo_id"]: item["reason"] for item in plan["skipped"]}
        self.assertEqual({key: reasons[key] for key in sorted(reasons) if key.startswith("p")},
                         {f"p{index:02d}": reason for index, (_, reason) in enumerate(cases)})
        self.assertEqual(reasons["without-exif"], "missing_datetime_original")
        self.assertEqual(reasons["wrong-exif-type"], "invalid_exif_type")
        self.assertEqual(reasons["wrong-exif-field"], "missing_datetime_original")
        self.assertTrue(all(item["message"] for item in plan["skipped"]))
        self.assertEqual(plan["counts"]["skipped"], len(cases) + 3)
        self.assertEqual(plan["folders"], [])
        self.assertEqual(self.apply(plan)["counts"]["added"], 0)

    def test_failed_ingest_metadata_skipped_but_missing_original_is_allowed(self):
        self.put("failed", ingest_state="error", last_ingest_error={"code": "CHANGED_ORIGINAL"})
        self.put("offline", original_status="missing")
        plan = self.plan()
        self.assertEqual(plan["skipped"][0]["reason"], "metadata_unavailable")
        self.assertEqual(plan["skipped"][0]["photo_id"], "failed")
        self.assertEqual(plan["folders"][0]["photo_ids"], ["offline"])
        self.assertEqual(self.apply(plan)["counts"]["added"], 1)

    def test_offset_is_bound_but_not_required_or_used_to_adjust_local_date(self):
        self.put("p1", exif={"datetime_original": "2024:02:29 00:00:00", "offset_time_original": "unknown"})
        plan = self.plan()
        self.assertEqual(plan["folders"][0]["name"], "2024-02-29")
        self.assertEqual(plan["photos"][0]["offset_time_original"], "unknown")
        self.assertEqual(plan["skipped"], [])

    def test_plans_are_deterministic_deduplicated_and_finite_json(self):
        self.put("p2")
        self.put("p1")
        first = self.plan(photo_ids=["p2", "p1", "p2"])
        second = self.plan(photo_ids=["p1", "p2"])
        self.assertEqual(first, second)
        self.assertEqual(first["photo_ids"], ["p1", "p2"])
        self.assertEqual(first["digest"], fingerprint({key: value for key, value in first.items() if key != "digest"}))
        self.assertEqual(json.loads(json.dumps(first, allow_nan=False)), first)
        self.assertEqual(self.apply(first)["counts"]["added"], 2)

    def test_more_than_1000_photos_and_skipped_ids_are_captured_without_pagination(self):
        with self.store.transaction():
            for index in range(1005):
                self.put(f"p{index:04d}", None if index == 1004 else "2024:02:29 00:00:00")
        plan = self.plan()
        self.assertEqual(len(plan["photo_ids"]), 1005)
        self.assertEqual(plan["photo_ids"][-1], "p1004")
        self.assertEqual(plan["skipped"][0]["photo_id"], "p1004")
        self.assertEqual(len(plan["folders"][0]["photo_ids"]), 1004)
        explicit = self.plan(photo_ids=list(reversed(plan["photo_ids"])))
        self.assertEqual(explicit["photos"], plan["photos"])
        self.assertEqual(explicit["folders"], plan["folders"])
        self.assertEqual(explicit["skipped"], plan["skipped"])
        result = self.apply(plan)
        self.assertEqual(result["counts"]["added"], 1004)
        self.assertEqual(len(self.members(result["folders"][0]["folder"]["folder_id"])), 1004)

    def test_all_reads_are_in_one_snapshot_and_planning_is_read_only(self):
        self.put("p1")
        folder_id = self.folder("2024-02-29", ["p1"])
        before = self.store.database_path.read_bytes()
        with SQLiteStorage.open(self.store.database_path) as readonly:
            reads = []

            def guarded(name):
                method = getattr(readonly, name)

                def run(*args, **kwargs):
                    self.assertTrue(readonly.db.in_transaction)
                    reads.append(name)
                    return method(*args, **kwargs)
                return run

            with patch.object(readonly, "album", side_effect=guarded("album")), \
                    patch.object(readonly, "photos", side_effect=guarded("photos")), \
                    patch.object(readonly, "folder_by_name_key", side_effect=guarded("folder_by_name_key")), \
                    patch.object(readonly, "photos_in_folders", side_effect=guarded("photos_in_folders")), \
                    patch.object(readonly, "read_snapshot", wraps=readonly.read_snapshot) as snapshot:
                plan = self.plan(store=readonly)
                snapshot.assert_called_once_with()
            self.assertEqual(set(reads), {"album", "photos", "folder_by_name_key", "photos_in_folders"})
            self.assertEqual(plan["folders"][0]["folder_id"], folder_id)
            self.assertFalse(readonly.db.in_transaction)
            self.assert_error("STORAGE_READ_ONLY", self.apply, plan, store=readonly)
        self.assertEqual(self.store.database_path.read_bytes(), before)

    def test_no_original_preview_file_output_or_model_access(self):
        self.put("p1")
        original_import = builtins.__import__

        def guard(name, *args, **kwargs):
            if name.split(".")[0] in ("torch", "transformers", "numpy", "huggingface_hub", "tokenizers"):
                raise AssertionError("Date organization must not import a model runtime.")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guard), \
                patch("builtins.open", side_effect=AssertionError("No file access")), \
                patch.object(Path, "open", side_effect=AssertionError("No file access")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("No image access")), \
                patch.object(self.store, "thumbnail", side_effect=AssertionError("No preview access")), \
                patch.object(self.store, "put_photo", side_effect=AssertionError("No photo writes")):
            result = self.apply(self.plan())
        self.assertEqual(result["counts"]["added"], 1)
        self.assertFalse((self.base / "offline").exists())

    def test_new_imports_after_plan_do_not_expand_selection(self):
        self.put("p1")
        self.put("skipped", None)
        plan = self.plan()
        self.put("later")
        result = self.apply(plan)
        self.assertEqual(result["photo_ids"], ["p1", "skipped"])
        self.assertEqual(self.members(result["folders"][0]["folder"]["folder_id"]), ["p1"])

    def test_reuse_preserves_manual_members_and_reports_live_idempotent_counts(self):
        self.put("p1")
        self.put("p2")
        self.put("manual", "1999:01:01 00:00:00")
        folder_id = self.folder("2024-02-29", ["manual"], description="Keep this manual description.")
        plan = self.plan(photo_ids=["p1", "p2"])
        self.assertEqual(plan["folders"][0]["action"], "reuse")
        self.assertEqual(plan["folders"][0]["expected_added"], 2)
        virtual_folders.add_photos(folder_id, ["p1"], store=self.store)
        result = self.apply(plan)
        self.assertEqual(result["counts"]["added"], 1)
        self.assertEqual(result["counts"]["already_present"], 1)
        self.assertEqual(result["folders"][0]["expected_added"], 2)
        self.assertEqual(self.members(folder_id), ["manual", "p1", "p2"])
        before = self.store.folder(folder_id)
        repeated = self.apply(plan)
        self.assertEqual(repeated["counts"]["added"], 0)
        self.assertEqual(repeated["counts"]["already_present"], 2)
        self.assertEqual(self.store.folder(folder_id), before)
        self.assertEqual(before["description"], "Keep this manual description.")

    def test_member_removed_after_planning_can_be_explicitly_readded(self):
        self.put("p1")
        folder_id = self.folder("2024-02-29", ["p1"])
        plan = self.plan()
        self.assertEqual(plan["folders"][0]["expected_already_present"], 1)
        virtual_folders.remove_photos(folder_id, ["p1"], store=self.store)
        result = self.apply(plan)
        self.assertEqual(result["counts"]["added"], 1)
        self.assertEqual(result["counts"]["already_present"], 0)

    def test_content_capture_input_and_ingest_state_changes_make_plan_stale(self):
        original = self.put("p1", exif={"datetime_original": "2024:02:29 00:00:00", "offset_time_original": "+01:00"})
        plan = self.plan()
        changes = [
            {"content_version": "updated-content"},
            {"metadata": {"exif": {"datetime_original": "2024:03:01 00:00:00", "offset_time_original": "+01:00"}}},
            {"metadata": {"exif": {"datetime_original": "2024:02:29 00:00:00", "offset_time_original": "+02:00"}}},
            {"ingest_state": "error"},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.store.put_photo({**original, **change})
                error = self.assert_error("DATE_PLAN_STALE", self.apply, plan)
                self.assertEqual(error.details["reason"], "photo_input_changed")
                self.assertEqual(self.store.folders(), [])
        self.store.put_photo(original)
        self.assertEqual(self.apply(plan)["counts"]["added"], 1)

    def test_skipped_inputs_are_revalidated_too(self):
        original = self.put("skipped", None)
        plan = self.plan()
        self.store.put_photo({**original, "metadata": {"exif": {"datetime_original": "2024:02:29 00:00:00"}}})
        self.assert_error("DATE_PLAN_STALE", self.apply, plan)
        self.assertEqual(self.store.folders(), [])

    def test_unrelated_metadata_and_original_location_status_do_not_invalidate_date_plan(self):
        original = self.put("p1")
        plan = self.plan()
        self.store.put_photo({**original, "metadata": {**original["metadata"], "width": 42},
                             "original_absolute_path": str(self.base / "moved.jpg"), "original_status": "available"})
        result = self.apply(plan)
        self.assertEqual(result["counts"]["added"], 1)
        self.assertEqual(self.store.photo("p1")["metadata"]["width"], 42)

    def test_deleted_photo_reports_stale_without_partial_writes(self):
        self.put("p1")
        self.put("p2", "2025:01:01 00:00:00")
        plan = self.plan()
        self.store.db.execute("DELETE FROM photos WHERE photo_id=?", ("p2",))
        error = self.assert_error("DATE_PLAN_STALE", self.apply, plan)
        self.assertEqual(error.details, {"reason": "photo_missing", "photo_id": "p2"})
        self.assertEqual(self.store.folders(), [])

    def test_cross_album_and_wrong_confirmation_fail_without_writes(self):
        self.put("p1")
        plan = self.plan()
        self.assert_error("DATE_PLAN_CONFIRMATION_MISMATCH", date_folders.apply_date_plan,
                          plan, "wrong", store=self.store)
        with SQLiteStorage.create(self.base / "other.sqlite") as other:
            self.assert_error("ALBUM_MISMATCH", self.apply, plan, store=other)
            self.assertEqual(other.folders(), [])
        self.assertEqual(self.store.folders(), [])

    def test_new_target_name_occupied_is_stale_not_implicitly_reused(self):
        self.put("p1")
        plan = self.plan()
        folder_id = self.folder("2024-02-29")
        error = self.assert_error("DATE_PLAN_STALE", self.apply, plan)
        self.assertEqual(error.details["reason"], "target_name_occupied")
        self.assertEqual(self.members(folder_id), [])

    def test_renamed_or_deleted_target_identity_is_stale(self):
        self.put("p1")
        folder_id = self.folder("2024-02-29")
        plan = self.plan()
        virtual_folders.rename_folder(folder_id, "Other", store=self.store)
        self.folder("2024-02-29")
        error = self.assert_error("DATE_PLAN_STALE", self.apply, plan)
        self.assertEqual(error.details["reason"], "target_identity_changed")
        virtual_folders.delete_folder(folder_id, store=self.store)
        error = self.assert_error("DATE_PLAN_STALE", self.apply, plan)
        self.assertEqual(error.details["reason"], "target_missing")

    def test_recomputed_digest_does_not_authorize_inconsistent_plan_shapes(self):
        self.put("p1")
        self.put("skipped", None)
        original = self.plan()
        mutations = [
            lambda plan: plan.update(schema="unknown"),
            lambda plan: plan.update(schema_version=True),
            lambda plan: plan.update(extra="not allowed"),
            lambda plan: plan.update(selection="dynamic"),
            lambda plan: plan.update(granularity="year"),
            lambda plan: plan.update(album={"id": "not-a-uuid"}),
            lambda plan: plan["photo_ids"].append("p1"),
            lambda plan: plan["photos"].pop(),
            lambda plan: plan["photos"][0].update(extra="not allowed"),
            lambda plan: plan["photos"][0].update(ingest_state="unknown"),
            lambda plan: plan["photos"][0].update(exif_status="missing"),
            lambda plan: plan["folders"][0].update(name="Wrong date"),
            lambda plan: plan["folders"][0].update(name_key="Wrong key"),
            lambda plan: plan["folders"][0].update(photo_ids=["skipped"]),
            lambda plan: plan["folders"][0].update(expected_added=True),
            lambda plan: plan["folders"][0].update(expected_added=-1),
            lambda plan: plan["folders"][0].update(folder_id="unapproved"),
            lambda plan: plan["folders"][0].update(action="reuse"),
            lambda plan: plan["folders"][0].update(requested=2),
            lambda plan: plan["folders"].clear(),
            lambda plan: plan["skipped"].clear(),
            lambda plan: plan["skipped"][0].update(reason="invented"),
            lambda plan: plan["counts"].update(eligible=True),
            lambda plan: plan["counts"].update(expected_added=99),
            lambda plan: plan["photos"][0].update(offset_time_original=float("nan")),
            lambda plan: plan["photos"][0].update(offset_time_original=float("inf")),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                plan = deepcopy(original)
                mutate(plan)
                self.redigest(plan)
                self.assert_error("INVALID_DATE_PLAN", self.apply, plan)
                self.assertEqual(self.store.folders(), [])

    def test_recomputed_digest_cannot_substitute_unrelated_current_photo_inputs(self):
        self.put("p1")
        plan = self.plan()
        plan["photos"][0]["offset_time_original"] = "+03:00"
        self.redigest(plan)
        self.assert_error("DATE_PLAN_STALE", self.apply, plan)
        self.assertEqual(self.store.folders(), [])

    def test_bad_digest_and_non_json_plans_have_explicit_errors(self):
        self.put("p1")
        plan = self.plan()
        for invalid in (None, [], {}, {"schema": date_folders.SCHEMA}):
            self.assert_error("INVALID_DATE_PLAN", date_folders.apply_date_plan, invalid, "", store=self.store)
        plan["digest"] = "0" * 64
        self.assert_error("INVALID_DATE_PLAN", self.apply, plan)
        plan = self.plan()
        plan["photos"][0]["datetime_original"] = object()
        self.assert_error("INVALID_DATE_PLAN", self.apply, plan)
        cyclic = self.plan()
        cyclic["photos"][0]["offset_time_original"] = cyclic
        self.assert_error("INVALID_DATE_PLAN", self.apply, cyclic)

    def test_apply_rolls_back_existing_members_and_new_folders_on_failure(self):
        self.put("p1", "2024:01:01 00:00:00")
        self.put("p2", "2025:01:01 00:00:00")
        self.put("manual", "1999:01:01 00:00:00")
        folder_id = self.folder("2024-01-01", ["manual"])
        before_folders, before_photos = self.store.folders(), self.store.photos()
        plan = self.plan(photo_ids=["p1", "p2"])
        original_add = virtual_folders.add_photos

        def fail_second(target, photo_ids, *, store):
            if photo_ids == ["p2"]:
                raise PhotographyError("SYNTHETIC_FAILURE", "Injected failure after another target was updated.")
            return original_add(target, photo_ids, store=store)

        with patch.object(virtual_folders, "add_photos", side_effect=fail_second):
            self.assert_error("SYNTHETIC_FAILURE", self.apply, plan)
        self.assertEqual(self.store.folders(), before_folders)
        self.assertEqual(self.members(folder_id), ["manual"])
        self.assertEqual(self.store.photos(), before_photos)
        self.assertFalse(self.store.db.in_transaction)

    def test_apply_uses_one_outer_write_transaction_with_nested_savepoints(self):
        self.put("p1", "2024:01:01 00:00:00")
        self.put("p2", "2025:01:01 00:00:00")
        plan = self.plan()
        statements = []
        self.store.db.set_trace_callback(statements.append)
        try:
            self.apply(plan)
        finally:
            self.store.db.set_trace_callback(None)
        self.assertEqual(statements.count("BEGIN IMMEDIATE"), 1)
        self.assertEqual(statements.count("COMMIT"), 1)
        self.assertTrue(any(statement.startswith("SAVEPOINT ") for statement in statements))

    def test_write_lock_fails_explicitly_without_success_or_partial_changes(self):
        self.put("p1")
        plan = self.plan()
        self.store.db.execute("PRAGMA busy_timeout=0")
        with SQLiteStorage.open(self.store.database_path, writable=True) as other:
            with other.transaction():
                self.assert_error("STORAGE_BUSY", self.apply, plan)
        self.assertEqual(self.store.folders(), [])

    def test_commit_lock_rolls_back_created_date_folders_and_members(self):
        self.put("p1")
        plan = self.plan()
        self.store.db.execute("PRAGMA busy_timeout=0")
        with SQLiteStorage.open(self.store.database_path) as reader:
            with reader.read_snapshot():
                reader.photos()
                self.assert_error("STORAGE_BUSY", self.apply, plan)
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.folders(), [])
        self.assertEqual(self.apply(plan)["counts"]["added"], 1)

    def test_membership_is_persistent_and_never_dynamic_after_manual_removal_or_import(self):
        self.put("p1")
        result = self.apply(self.plan())
        folder_id = result["folders"][0]["folder"]["folder_id"]
        with SQLiteStorage.open(self.store.database_path, writable=True) as reopened:
            self.assertEqual(self.members(folder_id, store=reopened), ["p1"])
            virtual_folders.remove_photos(folder_id, ["p1"], store=reopened)
        self.put("later")
        with SQLiteStorage.open(self.store.database_path) as reopened:
            self.assertEqual(self.members(folder_id, store=reopened), [])
            self.assertEqual(len(reopened.photos()), 2)


if __name__ == "__main__":
    unittest.main()
