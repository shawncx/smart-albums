"""Small real SQLite fixtures with explicit synthetic content/hash evidence."""
import hashlib
import io
from pathlib import Path
import sys
import tempfile

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "photography" / "scripts"))
from photography_lib import duplicates, feature_inputs
from photography_lib.config import Config
from photography_lib.feature_profiles import default_profile
from photography_lib.fingerprints import fingerprint
from photography_lib.sqlite_storage import SQLiteStorage


class DuplicateFixture:
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="duplicates-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = Config(self.root / "album.sqlite", model_cache_root=self.root / "models")
        self.store = SQLiteStorage.create(self.config.database_path)
        self.addCleanup(self.store.close)
        self.profile = default_profile("perceptual_hash")
        self.profile_id = None

    def photo(self, pid, *, content=None, preview=True, state="available", metadata=None):
        photo = {"photo_id": pid, "content_version": hashlib.sha256((content or pid).encode()).hexdigest(),
                 "thumbnail_profile": "synthetic-preview", "original_absolute_path": str(self.root / "offline" / (pid + ".jpg")),
                 "size_bytes": 15000, "mtime_ns": 1, "metadata": metadata or {"display_width": 6000, "display_height": 4000},
                 "ingest_state": state, "original_status": "missing",
                 "created_at": "2026-09-08T00:00:00Z", "updated_at": "2026-09-08T00:00:00Z"}
        self.store.put_photo(photo)
        if preview:
            image = io.BytesIO()
            Image.new("RGB", (36, 24), "navy").save(image, "JPEG")
            self.store.put_thumbnail(photo, image.getvalue())
        return photo

    def hash(self, pid, value):
        if self.profile_id is None:
            self.profile_id = self.store.put_feature_profile(self.profile)
            self.store.set_default_feature_profile("perceptual_hash", self.profile_id)
        return self.store.put_feature_result(pid, self.profile_id,
                                             feature_inputs.manifest_for(pid, self.profile, store=self.store),
                                             {"complete": True, "algorithm": "dhash", "bits": 64,
                                              "hash_hex": f"{value:016x}"})

    def scan(self, **kwargs):
        if not any(key in kwargs for key in ("all_photos", "photo_ids", "folder_ids")):
            kwargs["all_photos"] = True
        return duplicates.scan(store=self.store, **kwargs)

    def read_page(self, snapshot, **kwargs):
        return duplicates.page(snapshot, store=self.store, **kwargs)

    def seal(self, snapshot):
        snapshot.pop("digest", None)
        snapshot["digest"] = fingerprint(snapshot)
        return snapshot
