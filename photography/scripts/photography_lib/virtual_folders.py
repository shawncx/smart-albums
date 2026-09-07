"""Manual static classification; folder writes never inspect photo pixels."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3
import unicodedata
from uuid import uuid4

from .config import PhotographyError
from .fingerprints import fingerprint
from .text import fold_text


def normalize_name(name):
    if (not isinstance(name, str) or not name.strip()
            or any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in name)):
        raise PhotographyError("INVALID_ARGUMENT", "Folder name must be nonblank text without control characters.")
    display_name = unicodedata.normalize("NFC", name.strip())
    return display_name, fold_text(display_name)


def _id(value, kind):
    if not isinstance(value, str) or not value.strip():
        raise PhotographyError("INVALID_ARGUMENT", f"{kind} ID must be nonblank text.")
    return value


def _ids(values, kind):
    if not isinstance(values, list):
        raise PhotographyError("INVALID_ARGUMENT", f"Use an array of {kind.lower()} IDs; [] is an empty selection.")
    for value in values:
        _id(value, kind)
    return list(dict.fromkeys(values))


def resolve_scope(*, store, folder_ids=None, folder_match=None):
    ids = sorted(_ids([] if folder_ids is None else folder_ids, "Folder"))
    if not ids:
        if folder_match is not None:
            raise PhotographyError("INVALID_ARGUMENT", "Folder match requires at least one folder ID.")
        return {"kind": "album"}, store.photos()
    if folder_match is not None and folder_match not in ("union", "intersection"):
        raise PhotographyError("INVALID_ARGUMENT", "Folder match must be union or intersection.")
    if len(ids) > 1 and folder_match is None:
        raise PhotographyError("INVALID_ARGUMENT", "Multiple folders require an explicit union or intersection.")
    match = folder_match or "union"
    folders = [store.folder(folder_id) for folder_id in ids]
    scope = {"kind": "virtual_folders", "match": match,
             "folders": [{"folder_id": folder["folder_id"], "name": folder["name"]} for folder in folders]}
    return scope, store.photos_in_folders(ids, match)


def _result(store, folder, counts):
    return {"album": store.album(), "folder": folder, "counts": counts,
            "model_calls": 0, "image_model_calls": 0}


@contextmanager
def _write(store):
    try:
        with store.transaction():
            yield
    except sqlite3.OperationalError as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 0xff
        busy = code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        raise PhotographyError("STORAGE_BUSY" if busy else "STORAGE_UNAVAILABLE",
                               f"Could not update virtual folders: {exc}") from exc


def _available_name(store, name_key, folder_id=None):
    existing = store.folder_by_name_key(name_key)
    if existing is not None and existing["folder_id"] != folder_id:
        raise PhotographyError("FOLDER_NAME_EXISTS", "A virtual folder with this normalized name already exists.")


def list_folders(*, store, query=None, limit=100, after=""):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise PhotographyError("INVALID_ARGUMENT", "Limit must be an integer between 1 and 1000.")
    if not isinstance(after, str):
        raise PhotographyError("INVALID_ARGUMENT", "The pagination cursor must be a folder ID.")
    if query is not None and (not isinstance(query, str) or not query.strip()):
        raise PhotographyError("INVALID_ARGUMENT", "Folder search text must not be blank.")
    with store.read_snapshot():
        folders = store.folders()
        if query is not None:
            folded = fold_text(query)
            folders = [folder for folder in folders if folded in folder["name_key"]]
        remaining = [folder for folder in folders if folder["folder_id"] > after]
        items = remaining[:limit]
        return {"album": store.album(), "items": items, "total": len(folders), "query": query, "limit": limit,
                "next_cursor": items[-1]["folder_id"] if len(remaining) > limit else None,
                "model_calls": 0, "image_model_calls": 0}


def create_folder(name, *, store, description=""):
    display_name, name_key = normalize_name(name)
    if not isinstance(description, str):
        raise PhotographyError("INVALID_ARGUMENT", "Folder description must be text.")
    with _write(store):
        _available_name(store, name_key)
        timestamp = datetime.now(timezone.utc).isoformat()
        folder_id = "folder_" + uuid4().hex
        store._create_folder({"folder_id": folder_id, "name": display_name, "name_key": name_key,
                              "description": description, "created_at": timestamp, "updated_at": timestamp})
        result = _result(store, store.folder(folder_id), {"created": 1})
    return result


def show_folder(folder_id, *, store, profile_id=None):
    from .image_embedding import embedding_status
    from .management import _profile

    _id(folder_id, "Folder")
    with store.read_snapshot():
        scope, members = resolve_scope(store=store, folder_ids=[folder_id])
        profile = _profile(store, profile_id)
        coverage = (embedding_status(members, store, profile)["counts"] if profile else
                    {"total": len(members), "not_configured": len(members)})
        result = _result(store, store.folder(folder_id), {})
        result.update(scope=scope, profile_id=fingerprint(profile) if profile else None,
                      embedding_configuration="configured" if profile else "not_configured",
                      coverage=coverage, coverage_scope="selected_folders",
                      original_verification="not_checked", preview_integrity="unchecked")
    return result


def rename_folder(folder_id, name, *, store):
    _id(folder_id, "Folder")
    display_name, name_key = normalize_name(name)
    with _write(store):
        folder = store.folder(folder_id)
        _available_name(store, name_key, folder_id)
        changed = int(folder["name"] != display_name)
        if changed:
            store._rename_folder(folder_id, display_name, name_key)
        result = _result(store, store.folder(folder_id), {"renamed": changed, "unchanged": 1 - changed})
    return result


def delete_folder(folder_id, *, store):
    _id(folder_id, "Folder")
    with _write(store):
        folder = store.folder(folder_id)
        store._delete_folder(folder_id)
        result = _result(store, folder, {"deleted": 1, "removed": folder["photo_count"]})
    return result


def _membership_counts(requested, unique, changed, action):
    counts = {"requested": requested, "unique": unique, "duplicates": requested - unique,
              "added": 0, "removed": 0, "unchanged": unique - changed}
    counts[action] = changed
    return counts


def _source_summary(selected):
    scope = selected["scope"]
    saved_scope = {"kind": scope["kind"]}
    if scope["kind"] == "virtual_folders":
        saved_scope.update(match=scope["match"], folders=[
            {"folder_id": folder["folder_id"], "name": folder["name"]} for folder in scope["folders"]])
    source = {"snapshot_id": selected["source_search"]["snapshot_id"],
              "query": selected["query"], "profile_id": selected["profile_id"],
              "scope": saved_scope, "coverage_scope": selected["coverage_scope"],
              "coverage": {key: selected["coverage"][key] for key in (
                  "ready", "missing", "stale", "invalid_input", "invalid_vector", "total")}}
    selection = {key: selected["selection"][key] for key in (
        "method", "candidate_count", "selected_count", "not_selected_count", "photo_ids",
        "scope", "automatic_classification")}
    return source, selection


def add_photos(folder_id, photo_ids, *, store, search_snapshot=None, query_snapshot=None):
    _id(folder_id, "Folder")
    ids = _ids(photo_ids, "Photo")
    if search_snapshot is not None and query_snapshot is not None:
        raise PhotographyError("INVALID_ARGUMENT", "Choose a search snapshot or a query snapshot, not both.")
    with _write(store):
        store.folder(folder_id)
        for photo_id in ids:
            store.photo(photo_id)
        selected = None
        if search_snapshot is not None:
            from .management import select_search_results

            selected = select_search_results(search_snapshot, ids, store=store)
        source_query = None
        if query_snapshot is not None:
            from .condition_search import select_results

            source_query = select_results(query_snapshot, ids, store=store)
        changed = store._add_folder_photos(folder_id, ids)
        result = _result(store, store.folder(folder_id),
                         _membership_counts(len(photo_ids), len(ids), changed, "added"))
        result["photo_ids"] = ids
        if selected is not None:
            result["source_search"], result["selection"] = _source_summary(selected)
        if source_query is not None:
            result["source_query"] = source_query
    return result


def remove_photos(folder_id, photo_ids, *, store):
    _id(folder_id, "Folder")
    ids = _ids(photo_ids, "Photo")
    with _write(store):
        store.folder(folder_id)
        for photo_id in ids:
            store.photo(photo_id)
        changed = store._remove_folder_photos(folder_id, ids)
        result = _result(store, store.folder(folder_id),
                         _membership_counts(len(photo_ids), len(ids), changed, "removed"))
        result["photo_ids"] = ids
    return result
