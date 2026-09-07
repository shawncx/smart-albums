"""Protect album data and publish complete exports without truncating target files."""
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import stat
import sys
from uuid import uuid4

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
            "-journal", "-wal", "-shm", ".image-embedding.lock", ".image-features.lock",
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


def _identity(info):
    return info.st_dev, info.st_ino


@dataclass(frozen=True)
class ExportTarget:
    path: Path
    parents: tuple[tuple[Path, tuple[int, int] | None], ...]
    replace_existing: bool

    def __fspath__(self):
        return str(self.path)

    def __str__(self):
        return str(self.path)


def prepare_export(output, config, store, suffixes):
    if isinstance(output, ExportTarget):
        if output.path.suffix.lower() not in suffixes:
            raise PhotographyError("INVALID_ARGUMENT", "Export requires an appropriate file extension.")
        return output
    path = export_path(output, config, store, suffixes)
    try:
        parents = []
        for parent in reversed(path.parents):
            try:
                info = parent.stat(follow_symlinks=False)
            except FileNotFoundError:
                info = None
            parents.append((parent, _identity(info) if info is not None else None))
        return ExportTarget(path, tuple(parents), path.exists())
    except OSError as exc:
        raise PhotographyError("INVALID_ARGUMENT", "Cannot inspect the export directory: " + str(exc)) from exc


def _windows_directory(path):
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    # Deny delete sharing for every ancestor so path-based publication cannot be redirected.
    handle = create(str(path), 0, 0x1 | 0x2, None, 3, 0x02000000 | 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        close = kernel.CloseHandle
        close.argtypes = (wintypes.HANDLE,)
        close.restype = wintypes.BOOL
        close(handle)
        raise


@contextmanager
def _export_directory(target):
    with ExitStack() as resources:
        parent_fd = None
        for path, expected in target.parents:
            name = path if os.name == "nt" or parent_fd is None else path.name
            try:
                descriptor = (_windows_directory(path) if os.name == "nt" else
                              os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd))
            except FileNotFoundError:
                if expected is not None:
                    raise PhotographyError("EXPORT_PATH_CHANGED", "An export directory disappeared; retry the export.")
                os.mkdir(name, dir_fd=parent_fd)
                descriptor = (_windows_directory(path) if os.name == "nt" else
                              os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd))
            resources.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            if (not stat.S_ISDIR(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    or expected is not None and _identity(info) != expected):
                raise PhotographyError("EXPORT_PATH_CHANGED", "An export directory changed; retry the export.")
            if os.name != "nt":
                parent_fd = descriptor
        yield parent_fd


def _publish_posix(temporary, destination, directory):
    import ctypes

    library = ctypes.CDLL(None, use_errno=True)
    name, flag = ("renameatx_np", 4) if sys.platform == "darwin" else ("renameat2", 1)
    rename = getattr(library, name, None)
    if rename is not None:
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(directory, os.fsencode(temporary), directory, os.fsencode(destination), flag) == 0:
            return
        error = ctypes.get_errno()
        if error not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP):
            raise OSError(error, os.strerror(error), destination)
    # Older systems may lack exclusive rename; hard-link publication is also no-clobber.
    os.link(temporary, destination, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
    os.unlink(temporary, dir_fd=directory)


def publish_new_file(temporary, destination, *, directory=None):
    """Publish without replacing an existing entry; optionally anchor relative names."""
    if os.name == "nt":
        os.rename(temporary, destination)
    elif directory is not None:
        _publish_posix(temporary, destination, directory)
    else:
        with ExitStack() as resources:
            destination = Path(destination).absolute()
            temporary = Path(temporary).absolute()
            if temporary.parent != destination.parent:
                raise ValueError("Exclusive publication requires files in the same directory.")
            descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            resources.callback(os.close, descriptor)
            _publish_posix(temporary.name, destination.name, descriptor)


def write_export(target, data):
    """Write a private file, then publish its directory entry through a pinned parent."""
    if not isinstance(target, ExportTarget) or not isinstance(data, bytes):
        raise PhotographyError("INVALID_ARGUMENT", "A prepared export target and byte payload are required.")
    try:
        with _export_directory(target) as directory:
            temporary_name = ".smart-albums-export-" + uuid4().hex + ".tmp"
            temporary = target.path.parent / temporary_name if directory is None else temporary_name
            destination = target.path if directory is None else target.path.name
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            identity = _identity(os.fstat(descriptor))
            published = False
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if _identity(os.stat(temporary, dir_fd=directory, follow_symlinks=False)) != identity:
                    raise PhotographyError("EXPORT_PATH_CHANGED", "The temporary export changed before publication.")
                if target.replace_existing:
                    os.replace(temporary, destination, src_dir_fd=directory, dst_dir_fd=directory)
                else:
                    publish_new_file(temporary, destination, directory=directory)
                published = True
                if directory is not None:
                    os.fsync(directory)
            finally:
                if not published:
                    try:
                        info = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        info = None
                    if info is not None and _identity(info) == identity:
                        os.unlink(temporary, dir_fd=directory)
    except FileExistsError as exc:
        raise PhotographyError("EXPORT_PATH_CHANGED", "The export destination appeared; it was not overwritten.") from exc
    except OSError as exc:
        raise PhotographyError("EXPORT_FAILED", "Cannot safely publish the export: " + str(exc)) from exc
