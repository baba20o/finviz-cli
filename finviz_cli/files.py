"""Safe file handling shared by the client and the store: a lock between processes, and
writes that replace a file in one step (a reader never sees half of one)."""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path


class Busy(Exception):
    """The lock was not free within the time allowed."""


class Unavailable(Exception):
    """There can be no lock here: the folder cannot be written to."""


def _writable(folder: Path) -> bool:
    """Can a file be made in `folder`? Found out by making one: os.access says yes to every
    directory on Windows, whatever its permissions."""
    try:
        fd, name = tempfile.mkstemp(dir=folder, prefix=".probe.", suffix=".tmp")
    except OSError:
        return False
    os.close(fd)
    try:
        os.unlink(name)
    except OSError:
        pass
    return True


class FileLock:
    """An exclusive lock between processes on this machine, for short critical sections.

    The operating system releases it when the holder exits, so it cannot go stale. Entering
    either holds the lock or raises: `Busy` when it stayed taken (or the lock file could not
    be opened) for `timeout` seconds, `Unavailable` when the folder is not writable at all.
    It never reports a lock it does not hold; what to do without one is the caller's decision.
    """

    def __init__(self, path: Path, timeout: float = 10.0):
        self.path, self.timeout, self._fh = Path(path), timeout, None

    def _cannot_exist(self, exc: OSError) -> bool:
        """True when no amount of waiting will produce a lock here: the folder takes no new
        files, or opening the lock file was refused outright (its permissions, not another
        holder). On Windows both a refusal and a file busy elsewhere are PermissionError;
        they are told apart by the system's own code: 5 is "access denied", 32 and 33 are
        sharing and lock violations, which pass."""
        if not _writable(self.path.parent):
            return True
        if isinstance(exc, PermissionError) and getattr(exc, "winerror", 5) in (5, None):
            return True
        try:
            return self.path.exists() and not os.access(self.path, os.W_OK)
        except OSError:
            return True

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._fh = open(self.path, "a+b")
                    break
                except OSError as exc:
                    if self._cannot_exist(exc):
                        raise Unavailable(str(self.path.parent)) from None
                    if time.monotonic() >= deadline:        # a writable folder and file, yet it will not open
                        raise Busy(self.path.name) from None
                    threading.Event().wait(0.01)
            while True:
                try:
                    self._lock()
                    return self
                except OSError:
                    if time.monotonic() >= deadline:
                        raise Busy(self.path.name) from None
                    threading.Event().wait(0.01)           # a real wait, whatever a test did to time.sleep
        except BaseException:                              # an interrupt included: never leave the handle open
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            raise

    def _lock(self) -> None:
        if os.name == "nt":
            import msvcrt
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, *exc):
        if self._fh is None:
            return False
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None
        return False


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write next to the target, then swap it in: the target is always the old or the new file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(20):                  # Windows refuses while a reader has the target open
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                threading.Event().wait(0.05)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_text_atomic(path: Path, text: str) -> None:
    write_bytes_atomic(path, text.encode("utf-8"))
