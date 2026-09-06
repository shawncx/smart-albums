from __future__ import annotations

import os
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
    state_dir: Path
    thumbnail_size: int = 1024
    thumbnail_quality: int = 85
    extensions: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp")

    def __post_init__(self):
        object.__setattr__(self, "state_dir", Path(self.state_dir).expanduser().resolve())
        if not 64 <= self.thumbnail_size <= 4096:
            raise PhotographyError("INVALID_CONFIG", "Thumbnail size must be between 64 and 4096.")
        if not 1 <= self.thumbnail_quality <= 95:
            raise PhotographyError("INVALID_CONFIG", "JPEG quality must be between 1 and 95.")

    @classmethod
    def from_env(cls, state_dir: str | Path | None = None, **kwargs) -> Config:
        location = state_dir or os.environ.get("PHOTOGRAPHY_STATE_DIR") or Path.home() / ".photography-skill"
        return cls(Path(location), **kwargs)

    @property
    def thumbnail_profile(self) -> str:
        # Version covers orientation, color conversion, resampling and encoding.
        return f"preview-v1-srgb-{self.thumbnail_size}-q{self.thumbnail_quality}"


def path_key(path: Path | str) -> str:
    return os.path.normcase(str(path))
