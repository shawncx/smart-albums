from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from legacy_fixtures import create_legacy_schema, seed_observation
from PIL import Image
from photography_lib import Config, ingestion
from photography_lib.config import PhotographyError
from photography_lib.indexing import create_plan, execute_plan, index_status
from photography_lib.sqlite_storage import RETIRED_TABLES, SQLiteStorage
from photography_lib.index_storage import INDEX_SCHEMA
from test_indexing import FakeEncoder, PROFILE


ACTIVE_TABLES = {
    "libraries", "photos", "scans", "scan_events", "albums", "album_photos", "thumbnails",
    "image_index_profiles", "image_index_results", "image_index_runs", "image_index_items",
    "image_index_claims", "image_index_settings",
}


def dump(db):
    names = [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: [tuple(row) for row in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
            for name in names}


class RetiredTableMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="smart-albums-v7-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = self.root / "originals"
        source.mkdir()
        Image.new("RGB", (100, 50), "navy").save(source / "photo.jpg")
        self.config = Config(self.root / "state")
        scan = ingestion(source, config=self.config, album_name="Migration fixture")
        self.photo_id = scan["successful_photo_ids"][0]
        self.store = SQLiteStorage(self.config.state_dir)
        self.addCleanup(lambda: self.store.close())
        profile_id = self.store.put_index_profile(PROFILE)
        self.store.set_default_index_profile(profile_id)
        plan = create_plan([self.photo_id], store=self.store, config=self.config, profile=PROFILE)
        execute_plan(plan["run_id"], store=self.store, config=self.config, encoder=FakeEncoder(),
                     confirm=plan["digest"])
        self.active = dump(self.store.db)

    def legacy(self, *, populated=True):
        if populated:
            create_legacy_schema(self.store.db)
            observation = seed_observation(self.store, self.photo_id)
            self.store.db.execute("INSERT INTO analysis_runs VALUES ('old-run','completed','{}')")
            self.store.db.execute("INSERT INTO analysis_settings VALUES ('default','{\"old\":true}')")
            self.store.db.execute("INSERT INTO analysis_plans VALUES ('old-plan','{}')")
            self.store.db.execute("INSERT INTO analysis_plan_items VALUES ('old-plan',?,'{}')", (self.photo_id,))
            self.store.db.execute("INSERT INTO analysis_requests VALUES ('old-request','old-plan','{}')")
            self.store.db.execute("INSERT INTO analysis_claims VALUES ('old-claim','old-request')")
            self.store.db.execute("INSERT INTO embedding_encoders VALUES ('old-encoder','{}','old')")
            self.store.db.execute("INSERT INTO photo_embeddings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.photo_id, "old-encoder", observation["analysis_id"], "old-input", "old-hash",
                 "old-recipe", 1, "float32-le", 1, b"old-vector-bytes", "old-checksum", 1, 0, "old"))
        self.store.db.execute("PRAGMA user_version=6")
        before = dump(self.store.db)
        self.store.close()
        return before

    def test_new_database_has_only_active_tables(self):
        self.assertEqual(set(self.active), ACTIVE_TABLES)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 7)
        self.assertTrue(set(RETIRED_TABLES).isdisjoint(self.active))
        self.assertEqual(len(RETIRED_TABLES), 9)

    def test_v6_backup_then_drop_preserves_every_active_row_and_vector(self):
        # A pending task's claim is active infrastructure, even when no worker is running.
        other = {**PROFILE, "revision": "pending-profile"}
        plan = create_plan([self.photo_id], store=self.store, config=self.config, profile=other)
        with self.store.transaction():
            self.store._claim_index_input(plan["run_id"], plan["snapshots"][0], plan["profile_id"])
        active = dump(self.store.db)
        before = self.legacy()
        with patch("urllib.request.urlopen", side_effect=AssertionError("No network during migration")):
            self.store = SQLiteStorage(self.config.state_dir)
        self.assertEqual(dump(self.store.db), active)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 7)
        self.assertEqual(index_status(self.store.index_photos(), self.store, PROFILE)["counts"]["ready"], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM image_index_claims").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        backups = list((self.config.state_dir / "backups").glob("schema-v6-*.db"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(dump(backup), before)
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.store.close()
        self.store = SQLiteStorage(self.config.state_dir)
        self.assertEqual(dump(self.store.db), active)
        self.assertEqual(len(list((self.config.state_dir / "backups").glob("*.db"))), 1)

    def test_v6_without_obsolete_tables_does_not_recreate_them(self):
        self.legacy(populated=False)
        self.store = SQLiteStorage(self.config.state_dir)
        self.assertEqual(dump(self.store.db), self.active)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 7)

    def test_failure_after_table_drops_rolls_back_entire_migration(self):
        before = self.legacy()
        original = SQLiteStorage._drop_retired_tables

        def fail_after_drop(store):
            original(store)
            self.assertTrue(set(RETIRED_TABLES).isdisjoint(dump(store.db)))
            raise PhotographyError("SCHEMA_MIGRATION_FAILED", "Synthetic failure after DROP")

        with patch.object(SQLiteStorage, "_drop_retired_tables", fail_after_drop), self.assertRaises(PhotographyError):
            SQLiteStorage(self.config.state_dir)
        with closing(sqlite3.connect(self.config.state_dir / "photography.db")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(dump(db), before)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_active_index_mutation_is_detected_and_rolled_back(self):
        before = self.legacy()
        with patch("photography_lib.sqlite_storage.INDEX_SCHEMA",
                   INDEX_SCHEMA + ("UPDATE image_index_runs SET status='unexpected-change'",)), self.assertRaises(PhotographyError) as error:
            SQLiteStorage(self.config.state_dir)
        self.assertEqual(error.exception.code, "SCHEMA_MIGRATION_FAILED")
        with closing(sqlite3.connect(self.config.state_dir / "photography.db")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(dump(db), before)

    def test_custom_dependency_on_obsolete_data_blocks_destructive_migration(self):
        observation = seed_observation(self.store, self.photo_id)
        self.store.db.execute("CREATE TABLE custom_notes(id TEXT PRIMARY KEY, source TEXT REFERENCES analyses(analysis_id))")
        self.store.db.execute("INSERT INTO custom_notes VALUES ('note',?)", (observation["analysis_id"],))
        self.store.db.execute("PRAGMA user_version=6")
        before = dump(self.store.db)
        self.store.close()
        with self.assertRaises(PhotographyError) as error:
            SQLiteStorage(self.config.state_dir)
        self.assertEqual(error.exception.code, "SCHEMA_MIGRATION_FAILED")
        self.assertIn("custom_notes", str(error.exception))
        with closing(sqlite3.connect(self.config.state_dir / "photography.db")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(dump(db), before)


if __name__ == "__main__":
    unittest.main()
