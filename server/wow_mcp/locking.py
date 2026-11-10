"""Locks between processes on an open file: flock on Linux and macOS, a byte-range lock on Windows.

The lock is let go when the file is closed (or its process ends), on both.
"""

import os
import time
from typing import IO

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # not Windows
    msvcrt = None

# Windows locks a byte range and keeps other processes from reading it, so lock a byte far past
# anything the file holds: what's written in the file (the bridge's pid) stays readable.
_LOCK_BYTE = 1 << 30
_POLL = 0.05


def _windows_lock(fd: int, mode: int) -> None:
    at = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
    try:
        msvcrt.locking(fd, mode, 1)
    finally:
        os.lseek(fd, at, os.SEEK_SET)


def acquire(file: IO, blocking: bool = True) -> bool:
    """Take the exclusive lock on this file. False if another process holds it and `blocking` is
    off; otherwise waits for it."""
    if fcntl:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            return False
        return True
    if msvcrt:
        while True:
            try:
                _windows_lock(file.fileno(), msvcrt.LK_NBLCK)
                return True
            except OSError:
                if not blocking:
                    return False
                time.sleep(_POLL)
    return True  # no locks on this platform; locks within the process still apply


def release(file: IO) -> None:
    if fcntl:
        fcntl.flock(file, fcntl.LOCK_UN)
    elif msvcrt:
        _windows_lock(file.fileno(), msvcrt.LK_UNLCK)
