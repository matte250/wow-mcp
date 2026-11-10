"""Receives what the addon streams as file opens (see addon/WowMCP/Stream.lua).

The addon "plays" empty files out/000.wav-out/fff.wav; each play makes the client open
the file, and inotify reports the opens here, in order. Each file name is a 12-bit
symbol, offset by a whitening sequence so that equal symbols in a row become different
files (the client opens a file at most once per frame). Nothing is drawn or heard in
game, and the game doesn't have to be on screen.
"""

import ctypes
import json
import os
import re
import select
import struct
import time
import zlib
from pathlib import Path

MAGIC = b"WM\x01"
HEADER = len(MAGIC) + 8  # magic, request id, body length
MAX_BODY = 1 << 22
SYMBOL_FILE = re.compile(rb"([0-9a-f]{3})\.wav")

IN_OPEN = 0x20
IN_CLOSE_NOWRITE = 0x10
IN_Q_OVERFLOW = 0x4000


class StreamError(Exception):
    pass


class Timeout(StreamError):
    pass


class Corrupt(StreamError):
    pass


def whitening(count: int) -> list[int]:
    """Same sequence as Stream.lua: an LCG mod 4096 with full period."""
    values, r = [], 1
    for _ in range(count):
        values.append(r)
        r = (1597 * r + 51) % 4096
    return values


def symbols_to_bytes(symbols: list[int]) -> bytes:
    out = bytearray()
    for i in range(0, len(symbols) - 1, 2):
        a, b = symbols[i], symbols[i + 1]
        out += bytes((a >> 4, ((a & 0xF) << 4) | (b >> 8), b & 0xFF))
    if len(symbols) % 2:
        out.append(symbols[-1] >> 4)
    return bytes(out)


def bytes_to_symbols(data: bytes) -> list[int]:
    """The addon's encoding, for tests."""
    symbols = []
    for i in range(0, len(data), 3):
        chunk = data[i : i + 3] + b"\0\0"
        symbols.append((chunk[0] << 4) | (chunk[1] >> 4))
        if i + 1 < len(data):
            symbols.append(((chunk[1] & 0xF) << 8) | chunk[2])
    return symbols


def encode_message(request_id: int, value: object) -> list[int]:
    """The addon's message format (as files opened), for tests."""
    body = zlib.compress(json.dumps(value).encode())
    symbols = bytes_to_symbols(MAGIC + struct.pack(">II", request_id, len(body)) + body)
    return [(s + w) % 4096 for s, w in zip(symbols, whitening(len(symbols)))]


def find_message(files: list[int], request_id: int) -> object | None:
    """The decoded reply to request_id if it's complete in the opened files; None if not (yet).

    Messages start on a symbol boundary, so try each position for the magic; that skips
    stray opens and older replies. Raises Corrupt if the reply is there but damaged.
    """
    header_symbols = -(-HEADER * 8 // 12)
    white = whitening(len(files))

    def symbols(start: int, count: int) -> list[int]:
        return [(f - w) % 4096 for f, w in zip(files[start : start + count], white)]

    for start in range(len(files) - header_symbols + 1):
        head = symbols_to_bytes(symbols(start, header_symbols))[:HEADER]
        if not head.startswith(MAGIC):
            continue
        reply_id, length = struct.unpack(">II", head[len(MAGIC) :])
        if reply_id != request_id or length > MAX_BODY:
            continue
        needed = -(-(HEADER + length) * 8 // 12)
        if len(files) - start < needed:
            return None
        body = symbols_to_bytes(symbols(start, needed))[HEADER : HEADER + length]
        try:
            return json.loads(zlib.decompress(body))
        except (zlib.error, ValueError) as e:
            raise Corrupt(f"reply {request_id} arrived damaged: {e}") from e
    return None


class Receiver:
    """Watches a folder of symbol files; use as a context manager around the request."""

    def __init__(self, folder: Path):
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._fd = self._libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self._fd < 0:
            raise StreamError(f"inotify unavailable: {os.strerror(ctypes.get_errno())}")
        # Watching closes too keeps two opens of the same file from being merged into one event.
        if self._libc.inotify_add_watch(self._fd, os.fsencode(folder), IN_OPEN | IN_CLOSE_NOWRITE) < 0:
            os.close(self._fd)
            raise StreamError(f"can't watch {folder}: {os.strerror(ctypes.get_errno())}")
        self.symbols: list[int] = []

    def __enter__(self) -> "Receiver":
        return self

    def __exit__(self, *exc: object) -> None:
        os.close(self._fd)

    def _read(self, wait: float) -> bool:
        """Collect pending opens, waiting up to `wait` seconds; True if any arrived."""
        if not select.select([self._fd], [], [], max(wait, 0))[0]:
            return False
        try:
            data = os.read(self._fd, 1 << 20)
        except BlockingIOError:
            return False
        got = False
        pos = 0
        while pos < len(data):
            _, mask, _, length = struct.unpack_from("iIII", data, pos)
            name = data[pos + 16 : pos + 16 + length].rstrip(b"\0")
            pos += 16 + length
            if mask & IN_Q_OVERFLOW:
                raise Corrupt("too many file events at once; some were lost")
            if mask & IN_OPEN and (m := SYMBOL_FILE.fullmatch(name)):
                self.symbols.append(int(m.group(1), 16))
                got = True
        return got

    def receive(self, request_id: int, start_timeout: float, idle_timeout: float) -> object:
        """Wait for the reply: it must start within start_timeout and keep arriving without
        a gap longer than idle_timeout."""
        deadline = time.monotonic() + start_timeout
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise Timeout(f"no complete reply to request {request_id}")
            if self._read(deadline - now):
                value = find_message(self.symbols, request_id)
                if value is not None:
                    return value
                deadline = max(deadline, time.monotonic() + idle_timeout)
