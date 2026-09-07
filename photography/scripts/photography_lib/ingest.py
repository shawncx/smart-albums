"""Incremental photo ingestion into an explicitly opened single-album file."""
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import stat
from uuid import uuid4

from .config import Config, PhotographyError
from .images import inspect_photo, signature
from .source_paths import (
    absolute_candidate, path_identity, persist_resolution, relative_candidate,
    relative_original_path, resolve_original,
)
from .sqlite_storage import SQLiteStorage
from .thumbnails import stored_preview


def _now():
    return datetime.now(timezone.utc).isoformat()


def _root(path, config):
    root = Path(path).expanduser()
    if not root.is_absolute():
        raise PhotographyError("INVALID_PATH", "The photo directory must be absolute.")
    try:
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise PhotographyError("INVALID_PATH", "The photo root must be a directory.")
        if root.is_relative_to(config.model_cache_root):
            raise PhotographyError("INVALID_PATH", "A model cache is not an original photo directory.")
        with os.scandir(root) as entries:
            next(entries, None)
    except FileNotFoundError as exc:
        raise PhotographyError("INVALID_PATH", "The photo directory does not exist.") from exc
    except OSError as exc:
        raise PhotographyError("DIRECTORY_UNREADABLE", str(exc)) from exc
    return root


def _walk(root, extensions, excluded_dirs=(), excluded_files=()):
    pending = [root]
    forbidden = {path_identity(path) for path in excluded_files}
    while pending:
        folder = pending.pop()
        with os.scandir(folder) as entries:
            children = sorted(entries, key=lambda entry: entry.name)
        for entry in children:
            path = Path(entry.path)
            if entry.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                continue
            if any(path.is_relative_to(directory) for directory in excluded_dirs):
                continue
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
            elif (entry.is_file(follow_symlinks=False) and path.suffix.lower() in extensions
                  and path_identity(path) not in forbidden):
                yield path


def _candidates(path, records, database_path):
    key = path_identity(path)
    matches = {}
    for record in records:
        absolute = absolute_candidate(record["original_absolute_path"])
        relative = relative_candidate(record["original_relative_path"], database_path)
        if any(candidate is not None and path_identity(candidate) == key for candidate in (absolute, relative)):
            matches[record["photo_id"]] = record
    if len(matches) > 1:
        raise PhotographyError("PHOTO_PATH_CONFLICT", "More than one saved photo matches this location.",
                               details={"photo_ids": sorted(matches)})
    return next(iter(matches.values()), None)


def _choose(path, records, database_path):
    old = _candidates(path, records, database_path)
    if old is not None:
        absolute = absolute_candidate(old["original_absolute_path"])
        if absolute is None or path_identity(absolute) != path_identity(path):
            resolved = resolve_original(old, database_path)
            if resolved["status"] == "unavailable":
                raise PhotographyError(resolved["error"]["code"], resolved["error"]["message"])
            if resolved["status"] != "available":
                raise PhotographyError("FILE_CHANGED_DURING_SCAN", "Original disappeared during path matching.")
            if path_identity(resolved["path"]) != path_identity(path):
                raise PhotographyError("PHOTO_PATH_CONFLICT",
                                       "The preferred absolute location exists elsewhere; this relative copy cannot silently replace it.")
    return old


class _PhotoLocations:
    def __init__(self, store, records):
        self.store = store
        self._rebuild(records)

    def _rebuild(self, records):
        self.by_path, self.by_id, self.errors = {}, {}, {}
        for record in records:
            self._update(record)
        self.token = self.store.photo_location_token()

    def _update(self, record):
        pid = record["photo_id"]
        for key in self.by_id.pop(pid, ()):
            self.by_path[key].discard(pid)
            if not self.by_path[key]:
                del self.by_path[key]
        self.errors.pop(pid, None)
        try:
            candidates = (absolute_candidate(record["original_absolute_path"]),
                          relative_candidate(record["original_relative_path"], self.store.database_path))
            keys = {path_identity(candidate) for candidate in candidates if candidate is not None}
        except PhotographyError as exc:
            self.errors[pid] = exc
            return
        self.by_id[pid] = keys
        for key in keys:
            self.by_path.setdefault(key, set()).add(pid)

    def refresh(self):
        # Normalized paths cannot use the raw-path indexes (case, '..', album moves).
        # Rebuild only after unaccounted writes; fetch matching photos by primary key.
        token, changed_ids = self.store.photo_location_changes(self.token)
        if changed_ids is None:
            self._rebuild(self.store.photo_locations())
        else:
            for record in self.store.photo_locations(changed_ids) if changed_ids else ():
                self._update(record)
            self.token = token

    def candidates(self, path):
        self.refresh()
        if self.errors:
            raise next(iter(self.errors.values()))
        return [self.store.photo(pid) for pid in sorted(self.by_path.get(path_identity(path), ()))]

    def accepted(self, token, photo=None):
        if photo is not None:
            self._update(photo)
        self.token = token


def _identity(photo):
    if photo is None:
        return None
    keys = ("original_absolute_path", "original_relative_path", "content_version",
            "thumbnail_profile", "size_bytes", "mtime_ns", "ingest_state")
    return tuple(photo[key] for key in keys)


def _prepare(path, old, config, store):
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise PhotographyError("FILE_CHANGED_DURING_SCAN", "The source is not a regular file.")
    reusable = False
    if old and old["thumbnail_profile"] == config.thumbnail_profile:
        try:
            stored_preview(old, store)
            reusable = True
        except PhotographyError:
            reusable = False
    absolute = absolute_candidate(old["original_absolute_path"]) if old else None
    same_location = absolute is not None and path_identity(absolute) == path_identity(path)
    if (old and same_location and old["ingest_state"] == "available" and reusable
            and (old["size_bytes"], old["mtime_ns"]) == (info.st_size, info.st_mtime_ns)):
        details, thumbnail = {key: old[key] for key in ("content_version", "size_bytes", "mtime_ns", "metadata")}, None
    else:
        details, thumbnail = inspect_photo(path, old, config, preview_reusable=reusable)
    relative, warning = relative_original_path(path, config.database_path)
    changed = old is None or old["content_version"] != details["content_version"] or thumbnail is not None
    stamp = _now()
    paths_changed = old is None or (old["original_absolute_path"], old["original_relative_path"]) != (str(path), relative)
    data_changed = old is None or changed or old["ingest_state"] == "error" or (
        old["size_bytes"], old["mtime_ns"]) != (details["size_bytes"], details["mtime_ns"])
    photo = {**(old or {}), **details,
             "photo_id": old["photo_id"] if old else "photo_" + uuid4().hex,
             "original_absolute_path": str(path), "original_relative_path": relative,
             "thumbnail_profile": config.thumbnail_profile, "ingest_state": "available",
             "original_status": "available", "last_ingest_error": None, "last_path_error": None,
             "last_original_check": stamp, "created_at": old["created_at"] if old else stamp,
             "updated_at": stamp if data_changed else old["updated_at"],
             "path_updated_at": stamp if paths_changed else old["path_updated_at"]}
    outcome = ("added" if old is None else "restored" if old["original_status"] == "missing" else
               "updated" if changed or old["ingest_state"] == "error" else "unchanged")
    return photo, outcome, changed, thumbnail, warning, info


def _summary(result, store):
    profile_id = store.default_embedding_profile()
    if profile_id is None:
        result["index_summary"] = {"profile_id": None, "component": "image_embedding",
            "status": "not_configured", "counts": {"total": len(result["successful_photo_ids"])},
            "reason": "Select an image embedding profile before planning.", "model_calls": 0}
        ids = list(result["successful_photo_ids"])
    else:
        from .image_embedding import embedding_status
        status = embedding_status([store.photo(pid) for pid in result["successful_photo_ids"]],
                                  store, store.embedding_profile(profile_id))
        result["index_summary"] = {key: value for key, value in status.items() if key != "items"}
        ids = [item["photo_id"] for item in status["items"] if item["status"] != "ready"]
    result["index_suggested"] = bool(ids)
    result["index_prompt"] = ({
        "action": "create_index", "component": "image_embedding",
        "question": f"Would you like to create or update the semantic-search index for these {len(ids)} photos?",
        "without_index": ["Browse photos, saved previews and basic metadata.",
                          "Find photos by filename or recorded path, including within virtual folders.",
                          "Create custom virtual folders and manually add or remove photo memberships.",
                          "Organize saved photos once by EXIF capture date; missing or invalid dates are reported."],
        "with_index": ["All of the above, plus Chinese/English semantic search for visual content such as people, scenes and colors."],
        "limitations": "Semantic search requires a compatible local model and ranks similar candidates, not guaranteed detections or exact filters.",
        "photo_count": len(ids), "photo_ids": ids, "profile_id": profile_id,
        "configuration_required": profile_id is None, "requires_confirmation": True,
    } if ids else None)
    result.update(index_scope="successful_photos_in_this_scan", model_calls=0)


def _within(path, root):
    return path is not None and Path(path_identity(path)).is_relative_to(Path(path_identity(root)))


def _scan(root, config, store):
    store.assert_writable()
    scan_id = "scan_" + uuid4().hex
    root_relative, root_warning = relative_original_path(root, config.database_path)
    with store.read_snapshot():
        initial = store.photos()
        locations = _PhotoLocations(store, initial)
    result = {"scan_id": scan_id, "album": store.album(), "source_root": str(root), "status": "completed",
              "scanned": 0, "added": 0, "updated": 0, "restored": 0, "unchanged": 0,
              "missing": 0, "failed": 0, "successful_photo_ids": [], "changed_photo_ids": [],
              "errors": [], "warnings": [root_warning] if root_warning else [], "source_errors": []}
    with store.transaction():
        locations.refresh()
        store.start_scan(scan_id, str(root), root_relative)
        token = store.photo_location_token()
    locations.accepted(token)
    seen = set()
    fatal = None
    protected = [config.database_path]
    protected.extend(config.database_path.with_name(config.database_path.name + suffix)
                     for suffix in ("-journal", "-wal", "-shm", ".image-embedding.lock"))
    try:
        root_info = root.stat()
        for path in _walk(root, config.extensions, (config.model_cache_root,), protected):
            old = None
            result["scanned"] += 1
            try:
                with store.read_snapshot():
                    records = locations.candidates(path)
                old = _choose(path, records, config.database_path)
                if old:
                    seen.add(old["photo_id"])
                photo, outcome, changed, thumbnail, warning, observed = _prepare(path, old, config, store)
                with store.transaction():
                    current = _candidates(path, locations.candidates(path), config.database_path)
                    if (current is None) != (old is None) or (current and (
                            current["photo_id"] != old["photo_id"] or _identity(current) != _identity(old))):
                        raise PhotographyError("PHOTO_PATH_CHANGED", "Another operation changed this path or photo while reading it; retry.")
                    if signature(path.stat(follow_symlinks=False)) != signature(observed):
                        raise PhotographyError("FILE_CHANGED_DURING_SCAN", "The source changed before its data could be saved.")
                    store.put_photo(photo)
                    if thumbnail is not None:
                        store.put_thumbnail(photo, thumbnail)
                    store.event(scan_id, outcome, photo["photo_id"], str(path))
                    token = store.photo_location_token()
                locations.accepted(token, photo)
                seen.add(photo["photo_id"])
                result[outcome] += 1
                result["successful_photo_ids"].append(photo["photo_id"])
                if changed:
                    result["changed_photo_ids"].append(photo["photo_id"])
                if warning:
                    result["warnings"].append({**warning, "photo_id": photo["photo_id"]})
            except (PhotographyError, OSError) as exc:
                error = {"path": str(path), "photo_id": old["photo_id"] if old else None,
                         "code": exc.code if isinstance(exc, PhotographyError) else "FILE_READ_FAILED", "message": str(exc)}
                result["failed"] += 1
                result["errors"].append(error)
                with store.transaction():
                    locations.refresh()
                    updated = None
                    if old and error["code"] not in ("PHOTO_PATH_CHANGED", "PHOTO_PATH_CONFLICT"):
                        current = store.photo(old["photo_id"])
                        if _identity(current) == _identity(old):
                            updated = {**current, "ingest_state": "error", "last_ingest_error": error, "updated_at": _now()}
                            store.put_photo(updated)
                        else:
                            error["state_update"] = "not_applied_due_to_concurrent_change"
                    store.event(scan_id, "failed", error["photo_id"], str(path), error)
                    token = store.photo_location_token()
                locations.accepted(token, updated)
        final = root.stat()
        if (root_info.st_dev, root_info.st_ino) != (final.st_dev, final.st_ino):
            raise OSError("The source directory changed during enumeration.")
    except OSError as exc:
        fatal = PhotographyError("SCAN_INCOMPLETE", str(exc), scan_id)
    except KeyboardInterrupt:
        fatal = PhotographyError("SCAN_INTERRUPTED", "Scan interrupted; no new missing markers were applied.", scan_id)
    if fatal is None:
        for old in initial:
            if old["photo_id"] in seen:
                continue
            candidates = (absolute_candidate(old["original_absolute_path"]),
                          relative_candidate(old["original_relative_path"], config.database_path))
            if not any(_within(candidate, root) for candidate in candidates):
                continue
            resolution = resolve_original(old, config.database_path)
            try:
                persist_resolution(store, old, resolution)
                if resolution["status"] == "missing" and old["original_status"] != "missing":
                    result["missing"] += 1
                    with store.transaction():
                        store.event(scan_id, "missing", old["photo_id"], old["original_absolute_path"])
                if resolution["status"] == "unavailable":
                    result["source_errors"].append({"photo_id": old["photo_id"], **resolution["error"]})
            except PhotographyError as exc:
                result["source_errors"].append({"photo_id": old["photo_id"], **exc.to_dict()})
        result["status"] = "partial" if result["failed"] or result["source_errors"] else "completed"
    else:
        result.update(status="failed", error=fatal.to_dict())
    with store.transaction():
        _summary(result, store)
        store.finish_scan(scan_id, result)
    if fatal:
        raise fatal
    return result


def ingestion(path, *, config: Config, storage=None):
    root = _root(path, config)
    try:
        if storage is not None:
            if storage.database_path != config.database_path:
                raise PhotographyError("ALBUM_MISMATCH", "Storage is not the selected album file.")
            return _scan(root, config, storage)
        with SQLiteStorage.open(config.database_path, writable=True) as store:
            return _scan(root, config, store)
    except sqlite3.Error as exc:
        raise PhotographyError("STORAGE_UNAVAILABLE", str(exc)) from exc
