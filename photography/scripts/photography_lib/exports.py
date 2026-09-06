"""Protect album data without imposing a database/source directory layout."""
from pathlib import Path
import stat

from .config import PhotographyError
from .source_paths import absolute_candidate, relative_candidate


def _path_candidates(absolute_path, relative_path, database):
    absolute = absolute_candidate(absolute_path)
    if absolute is not None:
        yield absolute
    relative = relative_candidate(relative_path, database)
    if relative is not None:
        yield relative


def export_path(output, config, store, suffixes):
    try:
        requested = Path(output).expanduser().absolute()
        target = requested.resolve()
        if requested.suffix.lower() not in suffixes or target.suffix.lower() not in suffixes:
            raise PhotographyError("INVALID_ARGUMENT", "Export requires an appropriate file extension.")
        database = Path(store.database_path).resolve()
        protected = [database] + [Path(str(database) + suffix) for suffix in (
            "-journal", "-wal", "-shm", ".image-embedding.lock",
        )]
        cache = Path(config.model_cache_root).expanduser().absolute()
        if any(path.is_relative_to(root) for path in (requested, target) for root in (cache, cache.resolve())):
            raise PhotographyError("INVALID_ARGUMENT", "Export must not overwrite the local model cache.")
        for photo in store.photos():
            protected.extend(_path_candidates(photo["original_absolute_path"], photo["original_relative_path"], database))
        for path in protected:
            if target == path.resolve() or requested == path.absolute():
                raise PhotographyError("INVALID_ARGUMENT", "Export must not overwrite the database, sidecars or originals.")

        # A multiply linked target can alias any protected file, including a model cache file.
        # Refuse it without opening or statting every registered original.
        try:
            info = target.stat()
        except FileNotFoundError:
            info = None
        if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1):
            raise PhotographyError("INVALID_ARGUMENT", "Export requires a regular target without multiple hard links.")
        if target.suffix.lower() in (".jpg", ".jpeg"):
            for row in store.db.execute("SELECT DISTINCT source_absolute_path,source_relative_path FROM scans"):
                for source in _path_candidates(row[0], row[1], database):
                    if target.is_relative_to(source.resolve()) or requested.is_relative_to(source.absolute()):
                        raise PhotographyError("INVALID_ARGUMENT", "JPEG exports must be outside saved scan source directories.")
        return target
    except (OSError, ValueError, RuntimeError) as exc:
        raise PhotographyError("INVALID_ARGUMENT", "Cannot safely resolve this export target: " + str(exc)) from exc
