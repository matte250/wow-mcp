"""Windows: reports each time another program opens a symbol file, like inotify's IN_OPEN on Linux
(stream.py uses it for what the addon streams as file opens).

Windows has no notification for a file being opened, but an oplock comes close: a program holding
an exclusive oplock on a file is told when another program opens it, and that open waits until the
holder lets go. So a watcher holds an oplock on every symbol file; on each break it notes the
file, lets go (the game's open goes on), and takes the oplock again. It needs no administrator
rights.

Only one handle can hold a file's oplock, while several processes listen at once (each MCP
server, the bridge). So the watcher is one background process per folder, started on demand
(`python -m wow_mcp.opens_windows <folder>`): it appends each open to a log file next to the
folder, which the listeners (Opens) follow. It exits when nobody has listened for a while.
"""

import contextlib
import ctypes
import hashlib
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

SYMBOLS = 4096
SYMBOL_FILE = "{:03x}.wav"
LOG_NAME = ".wowmcp-opens"  # in the folder's parent; two bytes per open
ERRORS_NAME = ".wowmcp-opens.err"
RECORD = struct.Struct("<H")

# The game's open can run on after the oplock was taken again, which breaks it a second time
# within a fraction of a millisecond. The addon reuses a file two frames later at the soonest
# (Stream.lua), so a break this soon after the file's last one is that same open.
SAME_OPEN = 0.001
IDLE_EXIT = 300.0  # seconds without listeners before the watcher exits
START_TIMEOUT = 15.0
POLL = 0.005  # how often a listener looks for new opens
ALIVE_EVERY = 0.5


class Unavailable(Exception):
    """The watcher can't run."""


class Lost(Exception):
    """The watcher was restarted: opens in between weren't seen."""


def _names(folder: Path) -> dict[str, str]:
    # "Local\": the names are per logon session. Listeners in another session (a service, a
    # scheduled task, an SSH login) don't see this session's watcher and start their own, which
    # gets no oplocks while this one runs and empties the log this one writes. Not handled: run
    # everything that listens to one game in the same session.
    key = hashlib.sha1(os.path.normcase(str(folder.resolve())).encode()).hexdigest()[:16]
    return {name: f"Local\\wow-mcp-opens-{key}-{name}" for name in ("instance", "ready", "users", "stop")}


def log_path(folder: Path) -> Path:
    return folder.parent / LOG_NAME


if sys.platform == "win32":
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _INVALID = wintypes.HANDLE(-1).value
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.CreateIoCompletionPort.restype = wintypes.HANDLE
    _k32.CreateIoCompletionPort.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_size_t, wintypes.DWORD]
    _k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    _k32.GetQueuedCompletionStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
                                               ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_void_p),
                                               wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CreateMutexW.restype = wintypes.HANDLE
    _k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.CreateEventW.restype = wintypes.HANDLE
    _k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.OpenEventW.restype = wintypes.HANDLE
    _k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.SetEvent.argtypes = [wintypes.HANDLE]
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    _FILE_READ_ATTRIBUTES = 0x80
    _SHARE_ALL = 0x7  # read, write, delete: the watcher never gets in anyone's way
    _OPEN_EXISTING = 3
    _FILE_FLAG_OVERLAPPED = 0x40000000
    _FSCTL_REQUEST_OPLOCK = 0x00090240
    _OPLOCK_READ_WRITE = 0x1 | 0x4  # exclusive: broken by any other open that reads or writes
    _OPLOCK_REQUEST = 0x1
    _ERROR_IO_PENDING = 997
    _ERROR_OPLOCK_NOT_GRANTED = 300
    _ERROR_ALREADY_EXISTS = 183
    _SYNCHRONIZE = 0x00100000
    _EVENT_MODIFY_STATE = 0x0002

    class _Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

    class _OplockRequest(ctypes.Structure):
        _fields_ = [("StructureVersion", wintypes.USHORT), ("StructureLength", wintypes.USHORT),
                    ("RequestedOplockLevel", wintypes.ULONG), ("Flags", wintypes.ULONG)]

    class _OplockReply(ctypes.Structure):
        _fields_ = [("StructureVersion", wintypes.USHORT), ("StructureLength", wintypes.USHORT),
                    ("OriginalOplockLevel", wintypes.ULONG), ("NewOplockLevel", wintypes.ULONG),
                    ("Flags", wintypes.ULONG), ("AccessMode", wintypes.ULONG), ("ShareMode", wintypes.USHORT)]

    def _exists(name: str) -> bool:
        handle = _k32.OpenEventW(_SYNCHRONIZE, False, name)
        if handle:
            _k32.CloseHandle(handle)
        return bool(handle)

    def _failed(what: str, path: str, error: int) -> Unavailable:
        return Unavailable(f"{what} {path}: {ctypes.FormatError(error).strip()} (Windows error {error})")

    def _oplock_taken(done: int, error: int, path: str) -> bool:
        """What a request for a file's oplock came to: True if it's held now, False if it can't be
        had yet (worth another try), and Unavailable if this file will never give one."""
        if not done and error == _ERROR_IO_PENDING:
            return True
        # Not granted: something else holds an oplock on the file. (The file merely being open
        # elsewhere is no obstacle: the oplock is granted, and breaks at the next open.)
        if done or error == _ERROR_OPLOCK_NOT_GRANTED:
            return False
        # Anything else stays that way, e.g. a disk or network share without oplocks.
        raise _failed("Windows gives no oplock on", path, error)


# The watcher ----------------------------------------------------------------------------------


def watch(folder: Path) -> int:
    """Run the watcher for this folder until nobody listens any more. Returns an exit status."""
    names = _names(folder)
    instance = _k32.CreateMutexW(None, False, names["instance"])
    if not instance or ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        return 0  # another watcher has this folder
    stop = _k32.CreateEventW(None, True, False, names["stop"])

    port = _k32.CreateIoCompletionPort(_INVALID, None, 0, 1)
    paths = [str(folder / SYMBOL_FILE.format(symbol)) for symbol in range(SYMBOLS)]
    handles: list[int | None] = [None] * SYMBOLS
    overlapped = [_Overlapped() for _ in range(SYMBOLS)]
    replies = [_OplockReply() for _ in range(SYMBOLS)]
    request = _OplockRequest(1, ctypes.sizeof(_OplockRequest), _OPLOCK_READ_WRITE, _OPLOCK_REQUEST)

    def arm(symbol: int) -> bool:
        """Take the oplock on a symbol file; False while something else holds one on it.
        Raises Unavailable if the file can't be watched at all."""
        if handles[symbol] is None:
            handle = _k32.CreateFileW(paths[symbol], _FILE_READ_ATTRIBUTES, _SHARE_ALL, None, _OPEN_EXISTING,
                                      _FILE_FLAG_OVERLAPPED, None)
            if handle == _INVALID:
                raise _failed("can't open", paths[symbol], ctypes.get_last_error())
            _k32.CreateIoCompletionPort(handle, port, symbol, 0)
            handles[symbol] = handle
        ctypes.memset(ctypes.byref(overlapped[symbol]), 0, ctypes.sizeof(_Overlapped))
        done = _k32.DeviceIoControl(handles[symbol], _FSCTL_REQUEST_OPLOCK, ctypes.byref(request),
                                    ctypes.sizeof(request), ctypes.byref(replies[symbol]),
                                    ctypes.sizeof(_OplockReply), None, ctypes.byref(overlapped[symbol]))
        return _oplock_taken(done, ctypes.get_last_error(), paths[symbol])

    # Before the watcher says it's ready: a folder that can't be watched ends it here, with the reason.
    waiting = [symbol for symbol in range(SYMBOLS) if not arm(symbol)]
    log = os.open(log_path(folder), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND | os.O_BINARY)
    ctypes.WinDLL("winmm").timeBeginPeriod(1)  # so the 1 ms waits below are that short
    ready = _k32.CreateEventW(None, True, True, names["ready"])  # noqa: F841 (exists while this runs)

    last = [0.0] * SYMBOLS
    count, key, which = wintypes.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
    used = checked = time.monotonic()
    while True:
        _k32.GetQueuedCompletionStatus(port, ctypes.byref(count), ctypes.byref(key), ctypes.byref(which),
                                       1 if waiting else 500)
        opened = None
        if which.value:
            opened = key.value
            now = time.perf_counter()
            fresh = now - last[opened] >= SAME_OPEN
            last[opened] = now
            # Logged first, so listeners have it by the time the opening program carries on.
            if fresh:
                os.write(log, RECORD.pack(opened))
            # Closing the handle lets go of the oplock, and the game's open goes on.
            _k32.CloseHandle(handles[opened])
            handles[opened] = None
        # Whatever was opened before is taken again, unless something else holds an oplock on it.
        waiting = [symbol for symbol in waiting if not arm(symbol)]
        if opened is not None:
            waiting.append(opened)
            continue
        now = time.monotonic()
        if now - checked >= 1.0:
            checked = now
            if _k32.WaitForSingleObject(stop, 0) == 0 or not folder.is_dir():
                return 0
            if _exists(names["users"]):
                used = now
            elif now - used > IDLE_EXIT:
                return 0


def main() -> None:
    if sys.platform != "win32":
        sys.exit("wow_mcp.opens_windows only runs on Windows; Linux uses inotify (stream.py).")
    if len(sys.argv) != 2:
        sys.exit("usage: python -m wow_mcp.opens_windows <folder of symbol files>")
    try:
        status = watch(Path(sys.argv[1]))
    except (Unavailable, OSError) as e:
        sys.exit(str(e))  # one line, which the listener that started this watcher reports (_start)
    sys.exit(status)


# Listeners ------------------------------------------------------------------------------------


def _start(folder: Path) -> None:
    """Start the watcher for this folder (if it isn't running) and wait until it reports opens."""
    names = _names(folder)
    if _exists(names["ready"]):
        return
    # A console without a window, not no console at all: a venv's python.exe starts the real one as its child,
    # and a console program whose parent has no console gets a new window of its own, which pops up over the game.
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    package = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [package, os.environ.get("PYTHONPATH")]))}
    folder = Path(os.path.abspath(folder))
    command = [sys.executable, "-m", "wow_mcp.opens_windows", str(folder)]
    with open(folder.parent / ERRORS_NAME, "w") as errors:
        # In the addon's folder, not the caller's: Windows can't remove or rename a folder that a
        # running program works in, and the watcher outlives whoever started it.
        options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors, env=env, close_fds=True,
                       cwd=str(folder.parent))
        try:
            # Outside the job of whoever started this process, so it outlives an agent session.
            process = subprocess.Popen(command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **options)
        except OSError:  # the job doesn't allow that
            process = subprocess.Popen(command, creationflags=flags, **options)
    deadline = time.monotonic() + START_TIMEOUT
    while not _exists(names["ready"]):
        # It exits at once if another one is already starting: wait for that one.
        if process.poll() not in (None, 0) or time.monotonic() > deadline:
            detail = ""
            with contextlib.suppress(OSError):
                detail = (folder.parent / ERRORS_NAME).read_text(errors="replace").strip()
            last = detail.splitlines()[-1] if detail else "it didn't start in time"
            raise Unavailable(f"can't watch {folder} for file opens: {last}")
        time.sleep(0.02)


def stop(folder: Path, wait: float = 5.0) -> None:
    """Stop this folder's watcher, if one runs (tests, before removing the folder)."""
    names = _names(folder)
    handle = _k32.OpenEventW(_EVENT_MODIFY_STATE, False, names["stop"])
    if not handle:
        return
    _k32.SetEvent(handle)
    _k32.CloseHandle(handle)
    deadline = time.monotonic() + wait
    while _exists(names["stop"]) and time.monotonic() < deadline:
        time.sleep(0.05)


class Opens:
    """The symbol files in a folder as they're opened, from the moment this is made."""

    def __init__(self, folder: Path):
        if not folder.is_dir():
            raise Unavailable(f"can't watch {folder}: no such folder")
        self.folder = folder
        self._names = _names(folder)
        # Its existence tells the watcher that someone listens.
        self._user = _k32.CreateEventW(None, True, False, self._names["users"])
        self._file: int | None = None
        try:
            _start(folder)
            self._follow()
        except BaseException:
            self.close()
            raise

    def _follow(self) -> None:
        if self._file is not None:
            os.close(self._file)
        self._file = os.open(log_path(self.folder), os.O_RDONLY | os.O_BINARY)
        self._at = os.fstat(self._file).st_size
        self._checked = time.monotonic()

    def close(self) -> None:
        if self._file is not None:
            os.close(self._file)
            self._file = None
        if self._user:
            _k32.CloseHandle(self._user)
            self._user = None

    def read(self, wait: float) -> list[int]:
        """The files opened since the last call (as symbol numbers), waiting up to `wait` seconds
        for the first. Raises Lost if the watcher had to be restarted."""
        deadline = time.monotonic() + max(wait, 0)
        while True:
            os.lseek(self._file, self._at, os.SEEK_SET)
            data = os.read(self._file, 1 << 20)
            data = data[: len(data) & ~1]
            if data:
                self._at += len(data)
                return [symbol for (symbol,) in RECORD.iter_unpack(data)]
            now = time.monotonic()
            if now - self._checked >= ALIVE_EVERY:
                self._checked = now
                if not _exists(self._names["ready"]) or os.fstat(self._file).st_size < self._at:
                    _start(self.folder)
                    self._follow()
                    raise Lost("the file watcher was restarted; some file opens may have been missed")
            if now >= deadline:
                return []
            time.sleep(min(POLL, deadline - now))


if __name__ == "__main__":
    main()
