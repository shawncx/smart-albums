"""Standalone read-only reports for management-snapshot-v1, never legacy selection."""
from __future__ import annotations

import base64
import hashlib
import html
import json

from .config import PhotographyError
from .exports import export_path
from .management import SCHEMA


def _text(value):
    return html.escape(str(value), quote=True)


def _json(value):
    return _text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _preview(item, store):
    from .thumbnails import stored_preview

    try:
        identity = (item.get("content_version"), item.get("thumbnail_profile"), item.get("input_image_hash"))
        if not all(isinstance(value, str) and value for value in identity):
            raise PhotographyError("INVALID_PREVIEW", "This snapshot has no matching saved preview identity.")
        photo = store.photo(item["photo_id"])
        if (photo.get("content_version"), photo.get("thumbnail_profile")) != identity[:2]:
            raise PhotographyError("PHOTO_CHANGED", "Photo changed since this snapshot; no replacement preview is shown.")
        thumbnail = store.thumbnail(item["photo_id"], include_data=False)
        if (thumbnail["content_version"], thumbnail["profile"], thumbnail["image_hash"]) != identity:
            raise PhotographyError("PHOTO_CHANGED", "Preview changed since this snapshot; no replacement preview is shown.")
        data = stored_preview(photo, store)
        if (thumbnail.get("mime_type") != "image/jpeg" or len(data) != thumbnail.get("size_bytes")
                or hashlib.sha256(data).hexdigest() != identity[2]):
            raise PhotographyError("INVALID_PREVIEW", "Stored preview failed its snapshot integrity check.")
        return '<img alt="Saved photo preview" src="data:image/jpeg;base64,' + base64.b64encode(data).decode("ascii") + '">'
    except (PhotographyError, KeyError) as exc:
        return '<p class="preview-error">Preview unavailable: ' + _text(exc) + "</p>"


def management_report(snapshot, output, *, config, store):
    target = export_path(output, config, store, (".html",))
    if (not isinstance(snapshot, dict) or snapshot.get("schema") != SCHEMA
            or snapshot.get("mode") not in ("metadata", "semantic")
            or snapshot.get("target") not in ("albums", "photos")):
        raise PhotographyError("INVALID_ARGUMENT", "Expected a management-snapshot-v1 snapshot.")
    semantic = snapshot["mode"] == "semantic"
    items = snapshot.get("results" if semantic else "items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise PhotographyError("INVALID_ARGUMENT", "Snapshot items must be a list of records.")
    cards = []
    with store.read_snapshot():
        for rank, item in enumerate(items, 1):
            if snapshot["target"] == "albums":
                cover = item.get("cover")
                image = _preview(cover, store) if isinstance(cover, dict) else "<p>No saved cover preview.</p>"
                title = item.get("name", item.get("album_id", "Album"))
            else:
                image = _preview(item, store)
                title = item.get("relative_path", item.get("filename", item.get("photo_id", "Photo")))
            score = '<p class="score">Rank ' + str(rank) + " · cosine " + _text(item.get("score")) + "</p>" if semantic else ""
            cards.append("<article>" + image + '<div class="body"><h2>' + _text(title) + "</h2>" + score
                         + "<details><summary>Saved metadata and index status</summary><pre>" + _json(item)
                         + "</pre></details></div></article>")
    heading = "Smart Albums · " + str(snapshot.get("view", "management"))
    if snapshot.get("album"):
        heading += " · " + str(snapshot["album"].get("name", ""))
    header = '<header><h1>' + _text(heading) + "</h1>"
    if "query" in snapshot:
        header += "<p>Query: " + _text(snapshot["query"]) + "</p>"
    header += "<p>Saved read-only snapshot · " + _text(snapshot.get("created_at", "")) + "</p>"
    header += "<p>Index configuration: " + _text(snapshot.get("index_configuration")) + "</p>"
    header += ("<p>Metadata, ranking and coverage are historical, not a live view. Original files were not checked. "
               "Preview bytes are validated when this report is generated; changed previews are not substituted. "
               "Cosine similarity is not a probability. This page does not change albums or indexes.</p>")
    if "coverage" in snapshot:
        header += "<h2>Index coverage of the entire selected scope</h2><pre>" + _json(snapshot["coverage"]) + "</pre>"
    header += "</header>"
    page = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>Smart Albums · Management snapshot</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f3ed;color:#243d45;font:15px/1.6 system-ui}
header,main,footer{max-width:1280px;margin:auto;padding:24px}h1{font-size:28px}h2{font-size:18px;overflow-wrap:anywhere}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px}
article{background:white;border:1px solid #dce1d9;border-radius:12px;overflow:hidden}
img{width:100%;height:280px;object-fit:contain;background:#e6e9e2}.body{padding:20px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.preview-error{padding:20px;color:#8a3b20}
.score{color:#406b67}details{margin-top:16px}
</style></head><body>"""
    page += header + "<main>" + ("".join(cards) or "<p>No results in this snapshot.</p>") + "</main>"
    page += "<footer><details><summary>Complete saved JSON snapshot</summary><pre>" + _json(snapshot)
    page += "</pre></details></footer></body></html>"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    return str(target)
