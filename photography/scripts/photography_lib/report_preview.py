"""Preview HTML tied to saved identities, shared by local snapshot reports."""
import base64
import hashlib
import html

from .config import PhotographyError


def snapshot_preview(item, store):
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
        return '<img alt="已保存的照片预览" loading="lazy" src="data:image/jpeg;base64,' + base64.b64encode(data).decode("ascii") + '">'
    except (PhotographyError, KeyError) as exc:
        return '<p class="preview-error">预览不可用：' + html.escape(str(exc), quote=True) + "</p>"
