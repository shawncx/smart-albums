"""Original-file locations, independent of thumbnail and embedding validity."""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat

from .config import PhotographyError


def _now():
    return datetime.now(timezone.utc).isoformat()


def absolute_candidate(value):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise PhotographyError("ORIGINAL_PATH_INVALID", "Original absolute path is invalid.")
    path = Path(value)
    if path.is_absolute():
        return path
    # A saved address from another platform is unavailable here, never relative to CWD.
    if PureWindowsPath(value).is_absolute() or value.startswith("/"):
        return None
    raise PhotographyError("ORIGINAL_PATH_INVALID", "Original path must be absolute.")


def relative_candidate(value, database_path):
    if value is None:
        return None
    if (not isinstance(value, str) or not value or "\0" in value or "\\" in value
            or PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive):
        raise PhotographyError("ORIGINAL_PATH_INVALID", "Stored relative path must use relative slash-separated components.")
    return Path(os.path.abspath(Path(database_path).parent.joinpath(*PurePosixPath(value).parts)))


def path_identity(path):
    return os.path.normcase(os.path.abspath(str(path)))


def relative_original_path(absolute_path, database_path):
    path = Path(absolute_path)
    if not path.is_absolute():
        raise PhotographyError("ORIGINAL_PATH_INVALID", "Original location must be an absolute path.")
    try:
        value = os.path.relpath(path, Path(database_path).parent)
    except ValueError:
        return None, {
            "code": "RELATIVE_PATH_UNAVAILABLE",
            "message": "Original and album are on different drives or shares. The absolute path works, but moving devices may require relink.",
        }
    return Path(value).as_posix(), None


def photo_filename(photo):
    value = photo["original_absolute_path"]
    return PureWindowsPath(value).name if PureWindowsPath(value).drive else PurePosixPath(value).name


def _probe(path):
    if path is None:
        return "missing", None
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return "unavailable", {"code": "ORIGINAL_NOT_FILE", "message": "Original location is not a regular file."}
        with path.open("rb"):
            pass
        return "available", None
    except (FileNotFoundError, NotADirectoryError):
        return "missing", None
    except OSError as exc:
        return "unavailable", {"code": "ORIGINAL_UNAVAILABLE", "message": str(exc)}


def resolve_original(photo, database_path):
    result = {"photo_id": photo["photo_id"], "status": "missing", "path": None,
              "via": None, "repair_required": False, "error": None, "content_verified": False,
              "checked_at": _now()}
    try:
        absolute = absolute_candidate(photo["original_absolute_path"])
        status, error = _probe(absolute)
        if status != "missing":
            return {**result, "status": status, "path": str(absolute), "via": "absolute", "error": error}
        relative = relative_candidate(photo.get("original_relative_path"), database_path)
        status, error = _probe(relative)
        if status != "missing":
            return {**result, "status": status, "path": str(relative), "via": "relative",
                    "repair_required": status == "available", "error": error}
        return {**result, "reason": "both_paths_missing" if relative is not None else "absolute_missing_no_relative"}
    except PhotographyError as exc:
        return {**result, "status": "unavailable", "error": exc.to_dict()}


def relink_original(photo, new_path, database_path):
    path = Path(new_path).expanduser()
    if not path.is_absolute():
        raise PhotographyError("ORIGINAL_PATH_INVALID", "Relink requires an absolute original path.")
    status, error = _probe(path)
    if status != "available":
        raise PhotographyError(error["code"] if error else "ORIGINAL_MISSING",
                               error["message"] if error else "The supplied original file does not exist.")
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
            after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise PhotographyError("ORIGINAL_CHANGED", "Original changed while verifying the new location.")
    except OSError as exc:
        raise PhotographyError("ORIGINAL_UNAVAILABLE", str(exc)) from exc
    if digest != photo["content_version"]:
        raise PhotographyError("ORIGINAL_CONTENT_MISMATCH",
                               "The new file does not match the saved photo. Import changed content explicitly instead of relinking it.")
    relative, warning = relative_original_path(path, database_path)
    return {"photo_id": photo["photo_id"], "status": "available", "path": str(path),
            "relative_path": relative, "via": "manual", "repair_required": True,
            "content_verified": True, "error": None, "warning": warning, "checked_at": _now()}


def persist_resolution(store, original_photo, resolution):
    store.assert_writable()
    if resolution["status"] not in ("available", "missing", "unavailable"):
        raise PhotographyError("ORIGINAL_PATH_INVALID", "Unknown original resolution status.")
    identity = ("original_absolute_path", "original_relative_path", "content_version", "thumbnail_profile")
    with store.transaction():
        current = store.photo(original_photo["photo_id"])
        if any(current[key] != original_photo[key] for key in identity):
            raise PhotographyError("PHOTO_PATH_CHANGED", "Photo or original paths changed during location checking; retry.")
        updated = {**current, "original_status": resolution["status"],
                   "last_original_check": resolution["checked_at"], "last_path_error": resolution.get("error")}
        if resolution.get("repair_required"):
            if resolution["status"] != "available" or not resolution.get("path"):
                raise PhotographyError("ORIGINAL_PATH_INVALID", "Only an available location can repair a path.")
            key = path_identity(resolution["path"])
            for other in store.photos():
                if other["photo_id"] == current["photo_id"]:
                    continue
                candidates = (absolute_candidate(other["original_absolute_path"]),
                              relative_candidate(other["original_relative_path"], store.database_path))
                if any(candidate is not None and path_identity(candidate) == key for candidate in candidates):
                    raise PhotographyError("PHOTO_PATH_CONFLICT", "The resolved location belongs to another photo record.")
            updated.update(original_absolute_path=resolution["path"], path_updated_at=resolution["checked_at"])
            if resolution["via"] == "manual":
                updated["original_relative_path"] = resolution["relative_path"]
        store.put_photo(updated)
    return updated
