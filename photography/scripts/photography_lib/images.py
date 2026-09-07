from __future__ import annotations

import hashlib
import io
import os
import stat
import warnings
from pathlib import Path

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

from .config import Config, PhotographyError


EXIF_FIELDS = {
    271: "make", 272: "model", 274: "orientation", 306: "datetime",
    33434: "exposure_time", 33437: "f_number", 34855: "iso",
    36867: "datetime_original", 36881: "offset_time_original",
    37386: "focal_length", 42036: "lens_model",
}


def signature(info: os.stat_result) -> tuple:
    # On Windows/Python 3.12, path stat and fstat can give different ctime
    # semantics (creation vs metadata change). It is not a portable comparison.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").rstrip("\0")
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return str(value)


def srgb_on_white(oriented, icc, srgb, *, allow_invalid_icc=False):
    """Convert oriented pixels; an explicitly allowed ICC fallback returns its warning."""
    alpha = oriented.convert("RGBA").getchannel("A") if "A" in oriented.getbands() or "transparency" in oriented.info else None
    rgb = oriented.convert("RGB")
    warning = None
    if icc:
        try:
            color_input = oriented if oriented.mode in ("RGB", "CMYK", "LAB", "L") else rgb
            rgb = ImageCms.profileToProfile(color_input, ImageCms.ImageCmsProfile(io.BytesIO(icc)), srgb, outputMode="RGB")
        except (ImageCms.PyCMSError, OSError, ValueError):
            if not allow_invalid_icc:
                raise
            warning = "Embedded color profile could not be converted; preview assumes sRGB."
    if alpha is not None:
        background = Image.new("RGB", rgb.size, "white")
        background.paste(rgb, mask=alpha)
        rgb = background
    return rgb, warning


def _preview(source, config: Config) -> tuple[dict, bytes]:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(source) as image:
            if image.format not in {"JPEG", "PNG", "WEBP", "TIFF", "BMP"}:
                raise PhotographyError("UNSUPPORTED_IMAGE", "Unsupported image encoding.")
            if getattr(image, "n_frames", 1) != 1:
                raise PhotographyError("MULTIFRAME_UNSUPPORTED", "Animated and multi-page images are not supported in v0.1.")
            image.load()
            metadata = {"width": image.width, "height": image.height, "format": image.format,
                        "mode": image.mode, "exif": {}, "warnings": []}
            exif = image.getexif()
            exif_values = dict(exif)
            if 34665 in exif:
                try:
                    exif_values.update(exif.get_ifd(34665))
                except (OSError, ValueError, TypeError, KeyError):
                    metadata["warnings"].append("Some EXIF fields could not be read.")
            metadata["exif"] = {name: _value(exif_values[key]) for key, name in EXIF_FIELDS.items() if key in exif_values}
            oriented = ImageOps.exif_transpose(image)
            metadata.update(display_width=oriented.width, display_height=oriented.height)
            icc = image.info.get("icc_profile")
            srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
            rgb, warning = srgb_on_white(oriented, icc, srgb, allow_invalid_icc=True)
            metadata["color_handling"] = "converted_to_srgb" if icc and warning is None else "assumed_srgb"
            if warning is not None:
                metadata["warnings"].append(warning)
            rgb.thumbnail((config.thumbnail_size, config.thumbnail_size), Image.Resampling.LANCZOS)
            metadata.update(thumbnail_width=rgb.width, thumbnail_height=rgb.height)
            result = io.BytesIO()
            # Do not propagate source EXIF (including orientation) into the preview.
            rgb.save(result, "JPEG", quality=config.thumbnail_quality, icc_profile=srgb.tobytes())
            return metadata, result.getvalue()


def inspect_photo(path: Path, old: dict | None, config: Config, *, preview_reusable=False) -> tuple[dict, bytes | None]:
    """Hash and decode the same open file; reject ordinary mid-read changes."""
    try:
        before_path = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before_path.st_mode):
            raise PhotographyError("FILE_CHANGED_DURING_SCAN", "The photo is no longer a regular file.")
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            if signature(before_path) != signature(before):
                raise PhotographyError("FILE_CHANGED_DURING_SCAN", "The photo changed before it could be read.")
            digest = hashlib.file_digest(source, "sha256").hexdigest()
            reusable = (old is not None and old["content_version"] == digest
                        and old["thumbnail_profile"] == config.thumbnail_profile
                        and preview_reusable)
            source.seek(0)
            if reusable:
                metadata, thumbnail = dict(old["metadata"]), None
            else:
                metadata, thumbnail = _preview(source, config)
            after = path.stat(follow_symlinks=False)
            if signature(before) != signature(after):
                raise PhotographyError("FILE_CHANGED_DURING_SCAN", "The photo changed while it was being read; rescan it.")
        return {"content_version": digest, "size_bytes": before.st_size,
                "mtime_ns": before.st_mtime_ns, "metadata": metadata}, thumbnail
    except PhotographyError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning,
            SyntaxError, ValueError, TypeError) as exc:
        raise PhotographyError("IMAGE_DECODE_FAILED", str(exc)) from exc
    except OSError as exc:
        raise PhotographyError("FILE_READ_FAILED", str(exc)) from exc
