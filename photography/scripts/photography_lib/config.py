from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


class PhotographyError(Exception):
    def __init__(self, code: str, message: str, scan_id: str | None = None, *, details=None):
        super().__init__(message)
        self.code, self.scan_id = code, scan_id
        self.details = details

    def to_dict(self) -> dict:
        result = {"code": self.code, "message": str(self)}
        if self.scan_id:
            result["scan_id"] = self.scan_id
        if self.details is not None:
            result["details"] = self.details
        return result


@dataclass(frozen=True)
class Config:
    database_path: Path
    model_cache_root: Path | None = None
    thumbnail_size: int = 1024
    thumbnail_quality: int = 85
    extensions: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp")

    def __post_init__(self):
        if self.database_path is None or not str(self.database_path).strip():
            raise PhotographyError("DATABASE_REQUIRED", "Explicitly select an absolute album database path.")
        database_path = Path(self.database_path).expanduser()
        if not database_path.is_absolute():
            raise PhotographyError("INVALID_DATABASE_PATH", "The album database path must be absolute.")
        if database_path.suffix.casefold() not in (".sqlite", ".sqlite3", ".db"):
            raise PhotographyError("INVALID_DATABASE_PATH", "Use a .sqlite, .sqlite3, or .db album file.")
        object.__setattr__(self, "database_path", database_path.resolve())
        cache_root = self.model_cache_root if self.model_cache_root is not None else default_model_cache_root()
        object.__setattr__(self, "model_cache_root", Path(cache_root).expanduser().resolve())
        if not 64 <= self.thumbnail_size <= 4096:
            raise PhotographyError("INVALID_CONFIG", "Thumbnail size must be between 64 and 4096.")
        if not 1 <= self.thumbnail_quality <= 95:
            raise PhotographyError("INVALID_CONFIG", "JPEG quality must be between 1 and 95.")

    @classmethod
    def from_env(cls, database_path: str | Path | None = None,
                 model_cache_root: str | Path | None = None, **kwargs) -> Config:
        if database_path is None or not str(database_path).strip():
            raise PhotographyError("DATABASE_REQUIRED", "Explicitly select an absolute album database path.")
        if model_cache_root is None:
            model_cache_root = os.environ.get("SMART_ALBUMS_MODEL_CACHE_DIR") or None
        return cls(Path(database_path), model_cache_root=model_cache_root, **kwargs)

    @property
    def thumbnail_profile(self) -> str:
        # Version covers orientation, color conversion, resampling and encoding.
        return f"preview-v1-srgb-{self.thumbnail_size}-q{self.thumbnail_quality}"


def path_key(path: Path | str) -> str:
    return os.path.normcase(str(path))


def default_model_cache_root() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return root / "SmartAlbums" / "models"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "SmartAlbums" / "models"
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return root / "smart-albums" / "models"
