"""Add exact photo selections from previously saved search snapshots."""
from __future__ import annotations

from .config import PhotographyError


def add_search_selection(snapshot, photo_ids, *, store, album_name):
    """Only add explicit IDs from the displayed snapshot. Never repeat the search."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("results"), list):
        raise PhotographyError("INVALID_ARGUMENT", "Use a saved search JSON result.")
    allowed = {r.get("photo_id") for r in snapshot["results"] if isinstance(r, dict)}
    if not photo_ids or not all(isinstance(i, str) and i in allowed for i in photo_ids):
        raise PhotographyError("INVALID_ARGUMENT", "Select non-empty photo IDs from this search result.")
    with store.transaction():
        album = store.create_album(album_name)
        result = store.change_members(album["album_id"], photo_ids)
    return {**result, "album_name": album["name"], "search_id": snapshot.get("search_id"),
            "visual_model_calls": 0, "local_model_calls": 0}
