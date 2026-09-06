from __future__ import annotations

import os
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .config import Config, PhotographyError, path_key
from .images import inspect_photo
from .sqlite_storage import SQLiteStorage
from .storage import Storage
from .thumbnails import stored_preview


def _now():
    return datetime.now(timezone.utc).isoformat()


def _root(path: str | Path, config: Config) -> Path:
    root = Path(path).expanduser()
    if not root.is_absolute():
        raise PhotographyError("INVALID_PATH", "The photo directory must be an absolute path.")
    try:
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise PhotographyError("INVALID_PATH", "The photo root must be a directory.")
        if root.is_relative_to(config.state_dir) or config.state_dir.is_relative_to(root):
            raise PhotographyError("STATE_OVERLAPS_LIBRARY", "Photo and state directories must be separate, non-overlapping trees.")
        with os.scandir(root) as entries:
            next(entries, None)
    except FileNotFoundError as exc:
        raise PhotographyError("INVALID_PATH", "The photo directory does not exist.") from exc
    except OSError as exc:
        raise PhotographyError("DIRECTORY_UNREADABLE", str(exc)) from exc
    return root


def _walk(root: Path, extensions: tuple[str, ...]):
    pending = [root]
    while pending:
        folder = pending.pop()
        # Enumeration failures must propagate; never silently skip an unreadable folder.
        with os.scandir(folder) as entries:
            children = sorted(entries, key=lambda entry: entry.name)
        for entry in children:
            path = Path(entry.path)
            if entry.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                continue
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
            elif entry.is_file(follow_symlinks=False) and path.suffix.lower() in extensions:
                yield path


def _process(path: Path, root: Path, library_id: str, old: dict | None, config: Config, storage):
    current = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(current.st_mode):
        raise PhotographyError("FILE_CHANGED_DURING_SCAN", "The photo is no longer a regular file.")
    reusable = False
    if old and old["thumbnail_profile"] == config.thumbnail_profile:
        try:
            stored_preview(old, storage)
            reusable = True
        except PhotographyError:
            pass
    if (old and old["state"] == "available" and old["size_bytes"] == current.st_size
            and old["mtime_ns"] == current.st_mtime_ns
            and old["thumbnail_profile"] == config.thumbnail_profile
            and reusable):
        return old, "unchanged", False, None

    photo_id = old["photo_id"] if old else f"photo_{uuid4().hex}"
    details, thumbnail = inspect_photo(path, old, config, preview_reusable=reusable)
    content_changed = old is None or old["content_hash"] != details["content_hash"]
    photo = dict(old or {})
    photo.update(details)
    photo.update(photo_id=photo_id, library_id=library_id,
                 original_path=str(path), relative_path=path.relative_to(root).as_posix(),
                 path_key=path_key(path.relative_to(root)), state="available",
                 content_version=details["content_hash"], thumbnail_id=photo_id,
                 thumbnail_profile=config.thumbnail_profile, last_error=None, missing_since=None,
                 created_at=old["created_at"] if old else _now(), updated_at=_now())
    photo.pop("thumbnail_path", None)
    if old is None:
        outcome = "added"
    elif old["state"] == "missing" or old.get("missing_since"):
        outcome = "restored"
    elif content_changed or thumbnail is not None or old["state"] == "error":
        outcome = "updated"
    else:
        outcome = "unchanged"
    input_changed = content_changed or not reusable or thumbnail is not None
    return photo, outcome, outcome != "unchanged" and input_changed, thumbnail


def _scan(root: Path, config: Config, storage: Storage, album_name=None) -> dict:
    scan_id = f"scan_{uuid4().hex}"
    fatal = None
    with storage.transaction():
        library = storage.library(str(root), path_key(root))
        album = storage.create_album(album_name) if album_name is not None else None
        storage.start_scan(scan_id, library["library_id"])
        previous = {photo["path_key"]: photo for photo in storage.photos_for_library(library["library_id"])}
        seen = set()
        result = dict(scan_id=scan_id, library_id=library["library_id"], status="completed",
                      scanned=0, added=0, updated=0, restored=0, unchanged=0, missing=0,
                      failed=0, changed_photo_ids=[], errors=[], successful_photo_ids=[],
                      album=album, album_added=0, album_unchanged=0)
        try:
            root_info = root.stat()
            for path in _walk(root, config.extensions):
                key = path_key(path.relative_to(root))
                old = previous.get(key)
                seen.add(key)
                result["scanned"] += 1
                try:
                    with storage.savepoint():
                        photo, outcome, downstream, thumbnail = _process(path, root, library["library_id"], old, config, storage)
                        if outcome != "unchanged" or photo is not old:
                            storage.put_photo(photo)
                        if thumbnail is not None:
                            storage.put_thumbnail(photo, thumbnail)
                        membership = storage.change_members(album["album_id"], [photo["photo_id"]]) if album else None
                except (PhotographyError, OSError, KeyboardInterrupt) as exc:
                    error = {"path": str(path), "photo_id": old["photo_id"] if old else None,
                             "code": (exc.code if isinstance(exc, PhotographyError) else
                                      "SCAN_INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "FILE_READ_FAILED"),
                             "message": str(exc) or "Photo processing was interrupted."}
                    result["failed"] += 1
                    result["errors"].append(error)
                    if old:
                        stale = dict(old, state="error", last_error=error, updated_at=_now())
                        storage.put_photo(stale)
                    storage.event(scan_id, "failed", error["photo_id"], str(path), error)
                    if isinstance(exc, KeyboardInterrupt):
                        raise
                    continue
                if membership:
                    result["album_added"] += membership["added"]
                    result["album_unchanged"] += membership["unchanged"]
                result["successful_photo_ids"].append(photo["photo_id"])
                result[outcome] += 1
                if downstream:
                    result["changed_photo_ids"].append(photo["photo_id"])
                storage.event(scan_id, outcome, photo["photo_id"], str(path))
            final_root = root.stat()
            if (root_info.st_dev, root_info.st_ino) != (final_root.st_dev, final_root.st_ino):
                raise OSError("The photo root changed during enumeration.")
        except OSError as exc:
            fatal = PhotographyError("SCAN_INCOMPLETE", f"Directory scan did not complete: {exc}", scan_id)
        except KeyboardInterrupt:
            fatal = PhotographyError("SCAN_INTERRUPTED", "Scan interrupted; no new missing markers were applied.", scan_id)
        # Only a complete directory traversal can establish that a path is missing.
        if fatal is None:
            for key, photo in previous.items():
                if (key not in seen and photo["state"] != "missing"
                        and Path(photo["relative_path"]).suffix.lower() in config.extensions):
                    storage.put_photo(dict(photo, state="missing", missing_since=_now(), updated_at=_now()))
                    result["missing"] += 1
                    storage.event(scan_id, "missing", photo["photo_id"], photo["original_path"])
            result["status"] = "partial" if result["failed"] else "completed"
        else:
            result.update(status="failed", error=fatal.to_dict())
        profile_id = storage.default_index_profile()
        if profile_id is None:
            result["index_summary"] = {
                "profile_id": None, "status": "not_configured",
                "counts": {"total": len(result["successful_photo_ids"])},
                "reason": "Select an installed image index profile before indexing.",
                "model_calls": 0,
            }
            result["index_suggested"] = bool(result["successful_photo_ids"])
        else:
            from .indexing import index_status
            indexed = index_status([storage.photo(p) for p in result["successful_photo_ids"]],
                                   storage, storage.index_profile(profile_id))
            result["index_summary"] = {k: v for k, v in indexed.items() if k != "items"}
            result["index_suggested"] = indexed["counts"]["ready"] < indexed["counts"]["total"]
        result["index_scope"] = "successful_photos_in_this_scan"
        result["model_calls"] = 0
        storage.finish_scan(scan_id, result)
    if fatal:
        raise fatal
    return result


def ingest(path: str | Path, *, config: Config | None = None, storage: Storage | None = None, album_name=None) -> dict:
    """Incrementally index a photo root without modifying its files.

    Config and storage injection are for local setup/testing. Normal callers use ingest(path).
    """
    config = config or Config.from_env()
    root = _root(path, config)
    if album_name is not None:
        SQLiteStorage.album_name(album_name)
    try:
        if storage is not None:
            return _scan(root, config, storage, album_name)
        with SQLiteStorage(config.state_dir) as local:
            return _scan(root, config, local, album_name)
    except sqlite3.Error as exc:
        raise PhotographyError("STORAGE_UNAVAILABLE", str(exc)) from exc
