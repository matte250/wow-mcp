"""Receives what the addon streams as file opens (see addon/WowMCP/Stream.lua).

The addon "plays" empty files out/000.wav-out/fff.wav; each play makes the client open
the file, and inotify reports the opens here, in order. Each file name is a 12-bit
symbol, offset by a whitening sequence so that equal symbols in a row become different
files (the client opens a file at most once per frame). Nothing is drawn or heard in
game, and the game doesn't have to be on screen.

Two kinds of message come through: replies to requests (find_message) and posts the addon
sends unasked, such as /ai questions (find_posts, Receiver.receive_posts).
"""

import ctypes
import json
import os
import re
import select
import struct
import time
import zlib
from collections.abc import Container, Iterator
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"WM\x01"
HEADER = len(MAGIC) + 8  # magic, request id, body length
MAX_BODY = 1 << 22
POST_MAGIC = b"WM\x02"
POST_HEADER = len(POST_MAGIC) + 12  # magic, session token, post id, body length
MAX_POST = 1 << 13
# The most symbols an unfinished post can span; anything older can't be part of one.
POST_WINDOW = -(-(POST_HEADER + MAX_POST) * 8 // 12)
SYMBOL_FILE = re.compile(rb"([0-9a-f]{3})\.wav")

IN_OPEN = 0x20
IN_CLOSE_NOWRITE = 0x10
IN_Q_OVERFLOW = 0x4000
IN_IGNORED = 0x8000


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


def _encode(header: bytes, value: object) -> list[int]:
    body = zlib.compress(json.dumps(value).encode())
    symbols = bytes_to_symbols(header + struct.pack(">I", len(body)) + body)
    return [(s + w) % 4096 for s, w in zip(symbols, whitening(len(symbols)))]


def encode_message(request_id: int, value: object) -> list[int]:
    """The addon's reply format (as files opened), for tests."""
    return _encode(MAGIC + struct.pack(">I", request_id), value)


def encode_post(session: int, post_id: int, value: object) -> list[int]:
    """The addon's post format (as files opened), for tests."""
    return _encode(POST_MAGIC + struct.pack(">II", session, post_id), value)


def _messages(
    files: list[int], magic: bytes, header: int, max_body: int
) -> Iterator[tuple[int, tuple[int, ...], bytes | None]]:
    """(end, header fields, body) of each message with this magic in the opened files, oldest
    first; body is None while the message is incomplete. The fields are the header's numbers
    before the body length.

    Messages start on a symbol boundary, so try each position for the magic; that skips
    stray opens and other messages.
    """
    header_symbols = -(-header * 8 // 12)
    white = whitening(len(files))
    first = (bytes_to_symbols(magic)[0] + whitening(1)[0]) % 4096  # quick test before decoding

    def symbols(start: int, count: int) -> list[int]:
        return [(f - w) % 4096 for f, w in zip(files[start : start + count], white)]

    for start in range(len(files) - header_symbols + 1):
        if files[start] != first:
            continue
        head = symbols_to_bytes(symbols(start, header_symbols))[:header]
        if not head.startswith(magic):
            continue
        *fields, length = struct.unpack(f">{(header - len(magic)) // 4}I", head[len(magic) :])
        if length > max_body:
            continue
        end = start - (-(header + length) * 8 // 12)  # rounded up to whole symbols
        if end > len(files):
            yield end, tuple(fields), None
        else:
            yield end, tuple(fields), symbols_to_bytes(symbols(start, end - start))[header : header + length]


def _decode(body: bytes) -> object:
    return json.loads(zlib.decompress(body))


def find_message(files: list[int], request_id: int) -> object | None:
    """The decoded reply to request_id if it's complete in the opened files; None if not (yet).

    Skips stray opens, posts and other replies. Raises Corrupt if the reply is there but damaged.
    """
    for _, (reply_id,), body in _messages(files, MAGIC, HEADER, MAX_BODY):
        if reply_id != request_id:
            continue
        if body is None:
            return None
        try:
            return _decode(body)
        except (zlib.error, ValueError) as e:
            raise Corrupt(f"reply {request_id} arrived damaged: {e}") from e
    return None


@dataclass(frozen=True)
class Post:
    session: int  # token of the UI session that sent it (post ids restart after a /reload)
    id: int
    end: int  # position in the opened files just after it
    value: object = None
    error: str | None = None  # instead of a value, if it arrived damaged

    @property
    def key(self) -> tuple[int, int]:
        return self.session, self.id


def find_posts(files: list[int], handled: Container[tuple[int, int]] = ()) -> list[Post]:
    """Every complete post in the opened files, oldest first, except those whose key
    (session, id) is in `handled`. A damaged post comes with an error instead of a value."""
    posts = []
    for end, (session, post_id), body in _messages(files, POST_MAGIC, POST_HEADER, MAX_POST):
        if body is None or (session, post_id) in handled:
            continue
        try:
            posts.append(Post(session, post_id, end, value=_decode(body)))
        except (zlib.error, ValueError) as e:
            posts.append(Post(session, post_id, end, error=f"post {post_id} arrived damaged: {e}"))
    return posts


class Receiver:
    """Watches a folder of symbol files; use as a context manager around the request, or keep
    one open to receive posts."""

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
        self._handled: set[tuple[int, int]] = set()

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
            if mask & IN_IGNORED:
                raise StreamError("the watched folder was removed")
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

    def receive_posts(self, wait: float) -> list[Post]:
        """Posts that completed, waiting up to `wait` seconds for file opens. Each post comes
        once; the opens before it, and any too old to start a post, are let go."""
        if not self._read(wait):
            return []
        posts = find_posts(self.symbols, self._handled)
        self._handled.update(post.key for post in posts)
        del self.symbols[: max(len(self.symbols) - POST_WINDOW, *(post.end for post in posts), 0)]
        return posts
