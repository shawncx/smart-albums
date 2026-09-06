"""Read saved observations without provider initialization or image uploads."""
from __future__ import annotations

from pathlib import Path
from .analysis_schema import fingerprint
from .config import PhotographyError


def source_status(photo):
    try:
        info = Path(photo["original_path"]).stat()
    except OSError:
        return "unavailable"
    if (info.st_size, info.st_mtime_ns) != (photo["size_bytes"], photo["mtime_ns"]):
        return "changed"
    return "available" if photo["state"] == "available" else "rescan_required"


def saved_observation(photo, store, analysis_config=None, *, check_source=True):
    records = store.analysis_records(photo["photo_id"])
    config_id = fingerprint(analysis_config.profile()) if analysis_config else None
    preview_error = None
    try:
        meta = store.thumbnail(photo["photo_id"], include_data=False)
        if (meta["content_version"], meta["profile"]) != (photo["content_version"], photo["thumbnail_profile"]):
            raise PhotographyError("INVALID_PREVIEW", "Preview version differs from the indexed photo.")
    except PhotographyError as exc:
        meta = None
        preview_error = exc.to_dict()
    matching = [r for r in records if meta and r["content_version"] == photo["content_version"]
                and r["thumbnail_profile"] == photo["thumbnail_profile"]
                and r["input_image_hash"] == meta["image_hash"]]
    if config_id:
        selected = next((r for r in matching if r["analysis_config_id"] == config_id), None)
    else:
        selected = next(iter(matching), None)
    state = "never_analyzed" if not records else "cached" if selected and config_id else "saved" if selected else "needs_update"
    reason = None if selected or not records else "analysis_config_changed" if matching else "photo_or_preview_changed"
    if preview_error and records:
        reason = "preview_unavailable"
    entry = {"photo_id": photo["photo_id"], "relative_path": photo["relative_path"],
             "status": state, "reason": reason, "source_status": source_status(photo) if check_source else "not_checked",
             "preview_error": preview_error, "preview_integrity": "not_checked",
             "analysis_id": selected["analysis_id"] if selected else None,
             "latest_analysis_id": records[0]["analysis_id"] if records else None,
             "latest_failure": store.latest_analysis_failure(photo["photo_id"])}
    # Display a saved historical result even if it is no longer current.
    return entry, selected or next(iter(records), None)


def analysis_status(photos, store, analysis_config=None):
    entries = [saved_observation(photo, store, analysis_config)[0] for photo in photos]
    counts = {s: sum(e["status"] == s for e in entries)
              for s in ("never_analyzed", "saved", "cached", "needs_update")}
    counts.update(total=len(entries), source_unavailable=sum(e["source_status"] != "available" for e in entries),
                  preview_unavailable=sum(bool(e["preview_error"]) for e in entries),
                  latest_failed=sum(bool(e["latest_failure"]) for e in entries))
    return {"counts": counts, "items": entries, "model_calls": 0,
            "analysis_config_id": fingerprint(analysis_config.profile()) if analysis_config else None,
            "scope_note": "Saved indexed versions; image integrity is checked before analysis, not by this metadata query."}
