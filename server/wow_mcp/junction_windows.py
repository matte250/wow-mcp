"""Directory junctions on Windows, for the bell's link layout (transport.py's Bell, install.py).

A junction is a folder that points at another folder. Windows resolves it on every open of a path through it,
and a file opened that way holds nothing on the junction itself. So the server can re-point one while the
game has the file behind it open, which it can't do to the file: the client opens a streamed sound shared for
reading only and keeps it about 2 s after a play (measured live), and a file that's looked at more often than
that can never be replaced. Re-pointing is one FSCTL, with no moment where the path doesn't resolve.

Junctions need NTFS but no special rights (unlike symbolic links).
"""

import ctypes
import os
import struct
import sys
from pathlib import Path

if sys.platform == "win32":
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                          ctypes.c_void_p]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_GENERIC_WRITE = 0x40000000
_SHARE_ALL = 0x7
_OPEN_EXISTING = 3
_OPEN_REPARSE_POINT, _BACKUP_SEMANTICS = 0x00200000, 0x02000000
_FSCTL_SET_REPARSE_POINT = 0x000900A4
_MOUNT_POINT = 0xA0000003  # the reparse tag of a junction


def _buffer(target: Path) -> bytes:
    """REPARSE_DATA_BUFFER of a junction to `target` (absolute)."""
    printed = str(target)
    substitute = ("\\??\\" + printed).encode("utf-16-le")
    shown = printed.encode("utf-16-le")
    names = substitute + b"\0\0" + shown + b"\0\0"
    body = struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(shown)) + names
    return struct.pack("<IHH", _MOUNT_POINT, len(body), 0) + body


def point(link: Path, target: Path) -> None:
    """Make the junction `link` point at the folder `target`; `link` is a junction already, or an empty folder
    (which becomes one). Raises OSError."""
    handle = _kernel32.CreateFileW(str(link), _GENERIC_WRITE, _SHARE_ALL, None, _OPEN_EXISTING,
                                   _OPEN_REPARSE_POINT | _BACKUP_SEMANTICS, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        data, returned = _buffer(target.absolute()), wintypes.DWORD()
        if not _kernel32.DeviceIoControl(handle, _FSCTL_SET_REPARSE_POINT, data, len(data), None, 0,
                                         ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _kernel32.CloseHandle(handle)


def target(link: Path) -> Path | None:
    """Where the junction `link` points, or None if it isn't a link."""
    try:
        found = os.readlink(link)
    except (OSError, ValueError):
        return None
    return Path(found[4:] if found.startswith("\\\\?\\") else found)


def create(link: Path, to: Path) -> None:
    """A new junction `link` to the folder `to`."""
    link.mkdir()
    try:
        point(link, to)
    except OSError:
        link.rmdir()
        raise
