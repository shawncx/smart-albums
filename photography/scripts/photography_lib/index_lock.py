"""Crash-released cross-process execution lock, independent of SQLite transactions."""
from contextlib import contextmanager
import os

from .config import PhotographyError


@contextmanager
def execution_lock(path, *, error_prefix="INDEX", component="image-index"):
    handle = None
    locked = False
    try:
        handle = path.open("a+b")
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PhotographyError(error_prefix + "_IN_PROGRESS",
                f"Another {component} executor owns this database. Wait for it to finish before resuming.") from exc
        locked = True
        yield
    except OSError as exc:
        raise PhotographyError(error_prefix + "_LOCK_UNAVAILABLE",
                               f"Cannot use the {component} execution lock: {exc}") from exc
    finally:
        if handle is not None:
            try:
                if locked:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
