"""Explicit, static date-folder plans from saved EXIF, without image access."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import math
import re
from uuid import UUID

from .config import PhotographyError
from .fingerprints import fingerprint
from . import virtual_folders


SCHEMA = "date-folder-plan-v1"
_GRANULARITIES = ("year", "month", "day")
_DATE = re.compile(r"([0-9]{4}):([0-9]{2}):([0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})")
_INPUT_KEYS = {"photo_id", "content_version", "ingest_state", "exif_status",
               "datetime_original", "offset_time_original"}
_TARGET_KEYS = {"action", "folder_id", "name", "name_key", "photo_ids", "requested",
                "expected_added", "expected_already_present"}
_PLAN_KEYS = {"schema", "schema_version", "album", "selection", "granularity",
              "photo_ids", "photos", "folders", "skipped", "counts", "digest"}
_SKIP_MESSAGES = {
    "metadata_unavailable": "Saved metadata is unavailable or stale because photo ingestion failed.",
    "invalid_exif_type": "Saved EXIF must be an object.",
    "missing_datetime_original": "Saved EXIF DateTimeOriginal is missing.",
    "invalid_datetime_original_type": "Saved EXIF DateTimeOriginal must be a string.",
    "invalid_datetime_original_format": "Saved EXIF DateTimeOriginal must use YYYY:MM:DD HH:MM:SS.",
    "invalid_datetime_original_value": "Saved EXIF DateTimeOriginal is not a valid calendar date and time.",
}


def _invalid(message):
    raise PhotographyError("INVALID_DATE_PLAN", message)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _finite_json(value):
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_finite_json(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _finite_json(item) for key, item in value.items())
    return False


def _photo_input(photo):
    metadata = photo.get("metadata")
    exif = metadata.get("exif") if isinstance(metadata, dict) else None
    status = "missing" if exif is None else "available" if isinstance(exif, dict) else "invalid_type"
    return {
        "photo_id": photo["photo_id"],
        "content_version": photo["content_version"],
        "ingest_state": photo["ingest_state"],
        "exif_status": status,
        "datetime_original": deepcopy(exif.get("datetime_original")) if isinstance(exif, dict) else None,
        "offset_time_original": deepcopy(exif.get("offset_time_original")) if isinstance(exif, dict) else None,
    }


def _date_name(item, granularity):
    if item["ingest_state"] != "available":
        return None, "metadata_unavailable"
    if item["exif_status"] == "invalid_type":
        return None, "invalid_exif_type"
    value = item["datetime_original"]
    if value is None or value == "":
        return None, "missing_datetime_original"
    if not isinstance(value, str):
        return None, "invalid_datetime_original_type"
    match = _DATE.fullmatch(value)
    if match is None:
        return None, "invalid_datetime_original_format"
    try:
        captured = datetime(*(int(part) for part in match.groups()))
    except ValueError:
        return None, "invalid_datetime_original_value"
    # OffsetTimeOriginal is bound as an input, but never shifts the camera's calendar day.
    name = f"{captured.year:04d}-{captured.month:02d}-{captured.day:02d}"
    return name[:{"year": 4, "month": 7, "day": 10}[granularity]], None


def _group(photos, granularity):
    groups, skipped = {}, []
    for item in photos:
        name, reason = _date_name(item, granularity)
        if reason:
            skipped.append({"photo_id": item["photo_id"], "reason": reason,
                            "message": _SKIP_MESSAGES[reason]})
        else:
            groups.setdefault(name, []).append(item["photo_id"])
    return groups, skipped


def _counts(photos, folders, skipped):
    return {
        "selected": len(photos), "eligible": len(photos) - len(skipped), "skipped": len(skipped),
        "folders": len(folders),
        "create": sum(item["action"] == "create" for item in folders),
        "reuse": sum(item["action"] == "reuse" for item in folders),
        "expected_added": sum(item["expected_added"] for item in folders),
        "expected_already_present": sum(item["expected_already_present"] for item in folders),
    }


def _digest(plan):
    return fingerprint({key: value for key, value in plan.items() if key != "digest"})


def plan_date_organization(*, store, photo_ids=None, all_photos=False, granularity):
    """Capture one read snapshot; an explicit empty ID list selects no photos."""
    if granularity not in _GRANULARITIES:
        raise PhotographyError("INVALID_ARGUMENT", "Date granularity must be year, month, or day.")
    if type(all_photos) is not bool or all_photos == (photo_ids is not None):
        raise PhotographyError("INVALID_ARGUMENT", "Select exactly one of all_photos=True or a photo ID list.")
    if photo_ids is not None and (not isinstance(photo_ids, list) or
                                 any(not _text(item) for item in photo_ids)):
        raise PhotographyError("INVALID_ARGUMENT", "Photo IDs must be an array of non-empty strings.")
    with store.read_snapshot():
        album = store.album()
        records = store.photos() if all_photos else [store.photo(key) for key in sorted(set(photo_ids))]
        inputs = [_photo_input(item) for item in sorted(records, key=lambda item: item["photo_id"])]
        groups, skipped = _group(inputs, granularity)
        folders = []
        for name, ids in sorted(groups.items()):
            display, name_key = virtual_folders.normalize_name(name)
            existing = store.folder_by_name_key(name_key)
            members = {item["photo_id"] for item in store.photos_in_folders(
                [existing["folder_id"]], "union")} if existing else set()
            already = sum(photo_id in members for photo_id in ids)
            folders.append({
                "action": "reuse" if existing else "create",
                "folder_id": existing["folder_id"] if existing else None,
                "name": display, "name_key": name_key, "photo_ids": ids,
                "requested": len(ids), "expected_added": len(ids) - already,
                "expected_already_present": already,
            })
        result = {
            "schema": SCHEMA, "schema_version": 1, "album": {"id": album["id"]},
            "selection": "all" if all_photos else "ids", "granularity": granularity,
            "photo_ids": [item["photo_id"] for item in inputs], "photos": inputs,
            "folders": folders, "skipped": skipped, "counts": _counts(inputs, folders, skipped),
        }
        result["digest"] = _digest(result)
        _validate_plan(result)
    return result


def _validate_plan(plan):
    try:
        finite = _finite_json(plan)
    except RecursionError as exc:
        raise PhotographyError("INVALID_DATE_PLAN", "Date plan must be finite JSON.") from exc
    if not finite or not isinstance(plan, dict) or set(plan) != _PLAN_KEYS:
        _invalid("Date plan must be a finite JSON object with the exact plan fields.")
    if plan["schema"] != SCHEMA or type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        _invalid("Unsupported date-folder plan version.")
    album = plan["album"]
    if not isinstance(album, dict) or set(album) != {"id"} or not _text(album["id"]):
        _invalid("Date plan must identify its album UUID.")
    try:
        if str(UUID(album["id"])) != album["id"]:
            _invalid("Date plan album UUID must use canonical form.")
    except ValueError as exc:
        raise PhotographyError("INVALID_DATE_PLAN", "Date plan album UUID is invalid.") from exc
    if plan["selection"] not in ("ids", "all") or plan["granularity"] not in _GRANULARITIES:
        _invalid("Date plan selection or granularity is invalid.")
    ids = plan["photo_ids"]
    if not isinstance(ids, list) or any(not _text(key) for key in ids) or ids != sorted(set(ids)):
        _invalid("Date plan photo IDs must be unique and sorted.")
    photos = plan["photos"]
    if not isinstance(photos, list) or len(photos) != len(ids):
        _invalid("Date plan must snapshot every selected photo, including skipped photos.")
    for photo_id, item in zip(ids, photos):
        if not isinstance(item, dict) or set(item) != _INPUT_KEYS:
            _invalid("Date plan photo input fields are invalid.")
        if (item["photo_id"] != photo_id or not _text(item["content_version"])
                or item["ingest_state"] not in ("available", "error")
                or item["exif_status"] not in ("available", "missing", "invalid_type")):
            _invalid("Date plan photo input identity is invalid.")
        if item["exif_status"] != "available" and (
                item["datetime_original"] is not None or item["offset_time_original"] is not None):
            _invalid("Unavailable EXIF cannot supply capture-date fields.")
    groups, skipped = _group(photos, plan["granularity"])
    folders = plan["folders"]
    if not isinstance(folders, list) or len(folders) != len(groups):
        _invalid("Date plan targets do not match its capture dates.")
    folder_ids = set()
    for (name, photo_ids), target in zip(sorted(groups.items()), folders):
        if not isinstance(target, dict) or set(target) != _TARGET_KEYS:
            _invalid("Date plan target fields are invalid.")
        display, name_key = virtual_folders.normalize_name(name)
        if (target["name"] != display or target["name_key"] != name_key
                or target["photo_ids"] != photo_ids or target["action"] not in ("create", "reuse")):
            _invalid("Date plan target names and members must follow the saved capture dates.")
        for field in ("requested", "expected_added", "expected_already_present"):
            if type(target[field]) is not int or target[field] < 0:
                _invalid("Date plan target counts must be non-negative integers.")
        if (target["requested"] != len(photo_ids)
                or target["expected_added"] + target["expected_already_present"] != len(photo_ids)):
            _invalid("Date plan target counts do not match its members.")
        if target["action"] == "create":
            if target["folder_id"] is not None or target["expected_already_present"] != 0:
                _invalid("A new target cannot have an existing identity or members.")
        else:
            if not _text(target["folder_id"]) or target["folder_id"] in folder_ids:
                _invalid("Existing date plan target IDs must be non-empty and unique.")
            folder_ids.add(target["folder_id"])
    if plan["skipped"] != skipped:
        _invalid("Date plan skip report does not match its photo inputs.")
    expected_counts = _counts(photos, folders, skipped)
    if (not isinstance(plan["counts"], dict) or set(plan["counts"]) != set(expected_counts)
            or any(type(value) is not int for value in plan["counts"].values())
            or plan["counts"] != expected_counts):
        _invalid("Date plan summary counts are inconsistent.")
    if not isinstance(plan["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", plan["digest"]):
        _invalid("Date plan digest is invalid.")
    try:
        digest = _digest(plan)
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise PhotographyError("INVALID_DATE_PLAN", "Date plan must be finite UTF-8 JSON.") from exc
    if plan["digest"] != digest:
        _invalid("Date plan digest does not match its contents.")


def _stale(reason, **details):
    raise PhotographyError("DATE_PLAN_STALE", "Date plan inputs or targets changed; create a new plan.",
                           details={"reason": reason, **details})


def apply_date_plan(plan, confirmation, *, store):
    """Validate before writing, then atomically create targets and add only captured IDs."""
    _validate_plan(plan)
    plan = deepcopy(plan)
    if not isinstance(confirmation, str) or confirmation != plan["digest"]:
        raise PhotographyError("DATE_PLAN_CONFIRMATION_MISMATCH", "Confirm the exact date plan digest before applying.")
    with virtual_folders._write(store):
        album = store.album()
        if album["id"] != plan["album"]["id"]:
            raise PhotographyError("ALBUM_MISMATCH", "Date plan belongs to a different album.",
                                   details={"snapshot_album": plan["album"], "album": album})
        for expected in plan["photos"]:
            try:
                current = store.photo(expected["photo_id"])
            except PhotographyError as exc:
                if exc.code != "PHOTO_NOT_FOUND":
                    raise
                _stale("photo_missing", photo_id=expected["photo_id"])
            if fingerprint(_photo_input(current)) != fingerprint(expected):
                _stale("photo_input_changed", photo_id=expected["photo_id"])
        for target in plan["folders"]:
            if target["action"] == "create":
                if store.folder_by_name_key(target["name_key"]) is not None:
                    _stale("target_name_occupied", name=target["name"])
            else:
                try:
                    current = store.folder(target["folder_id"])
                except PhotographyError as exc:
                    if exc.code != "FOLDER_NOT_FOUND":
                        raise
                    _stale("target_missing", folder_id=target["folder_id"])
                if any(current[key] != target[key] for key in ("folder_id", "name", "name_key")):
                    _stale("target_identity_changed", folder_id=target["folder_id"])
        folders = []
        for target in plan["folders"]:
            folder_id = target["folder_id"]
            if target["action"] == "create":
                folder_id = virtual_folders.create_folder(target["name"], store=store)["folder"]["folder_id"]
            added = virtual_folders.add_photos(folder_id, target["photo_ids"], store=store)
            folders.append({
                "action": target["action"], "folder": added["folder"], "photo_ids": target["photo_ids"],
                "requested": target["requested"], "added": added["counts"]["added"],
                "already_present": added["counts"]["unchanged"], "expected_added": target["expected_added"],
                "expected_already_present": target["expected_already_present"],
            })
        return {
            "schema": "date-folder-apply-v1", "schema_version": 1,
            "album": album, "plan_digest": plan["digest"], "granularity": plan["granularity"],
            "photo_ids": plan["photo_ids"], "folders": folders, "skipped": plan["skipped"],
            "counts": {
                "selected": len(plan["photo_ids"]), "eligible": plan["counts"]["eligible"],
                "skipped": len(plan["skipped"]), "folders": len(folders),
                "created": sum(item["action"] == "create" for item in folders),
                "reused": sum(item["action"] == "reuse" for item in folders),
                "added": sum(item["added"] for item in folders),
                "already_present": sum(item["already_present"] for item in folders),
            },
        }
