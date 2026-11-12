"""The frames of the streamed channel, from the server to the running game (the "bell"; addon/WowMCP/Core.lua
reads them, transport.py writes them).

The client reads a long sound from disk every time it's played and keeps nothing of it; a short one it reads
once and keeps for the rest of the launch; an empty file never plays (measured live, probes/channel_probes.py).
So a file in bell/ that's either 8 seconds of silence or empty is a bit the server can set as often as it
likes, and the game reads it by playing it and asking C_Sound.IsPlaying a moment later. A valid file is a hard
link to bell/on.wav, so setting a bit writes no data.

A frame is 24 such bits (00.wav-23.wav) between two markers that carry its parity, which flips with every
frame: lead.wav, set first, and seal.wav, set last. The game reads seal.wav first and lead.wav last, so a
frame read while the server writes the next shows two different markers and is read again (a seqlock). The 24
bits are 3 of type, 16 of payload and a CRC-5 over those and the parity, for anything torn some other way.

The game confirms each frame it takes with a receipt over the stream (stream.py: its bits and parity, and
whether it has seen a marker go from valid to empty yet), and the server writes the next frame only after that.
"""

import struct
from dataclasses import dataclass

FOLDER = "bell"
DATA_BITS = 24
TYPE_BITS, PAYLOAD_BITS = 3, 16
CRC_SHIFT = TYPE_BITS + PAYLOAD_BITS
ON, OFF, LEAD, SEAL = "on.wav", "off.wav", "lead.wav", "seal.wav"
DATA_FILES = [f"{i:02d}.wav" for i in range(DATA_BITS)]
FILES = [ON, OFF, LEAD, SEAL, *DATA_FILES]  # what the installer makes; on.wav is the only valid one at first

# In a receipt, above the frame's 24 bits: its parity, and whether the game has seen the files change.
PARITY = 1 << DATA_BITS
VERIFIED = 1 << (DATA_BITS + 1)
FRAME_MASK = (1 << (DATA_BITS + 1)) - 1

# Frame types (Core.lua's TYPES): what the payload is.
MORE = 0  # the next two bytes of a LONG message
DATA = 1  # new data in the slots: the payload's seq (mod 2^16); the game loads a slot
REQUEST = 2  # a request for game data: kind (3 bits), request id (13); the game answers over the stream
ACK = 3  # the bridge has /ai question (post id mod 16, 4 bits), and may stay quiet this many seconds (12)
PROGRESS = 4  # how the question the bridge works on comes along: step (5 bits, STEP_CODES), tokens (11)
TEST = 5  # nothing but a change of the files, for the game to see them read afresh (the handshake)
LONG = 6  # a message longer than a frame: kind (4 bits), length in bytes (12); its bytes follow in MOREs

REQUEST_IDS = 1 << 13
QUESTION_IDS = 16
MAX_QUIET = (1 << 12) - 1
MAX_LONG = (1 << 12) - 1

CRC_POLY, CRC_INIT = 0x05, 0x1F  # CRC-5-USB, x^5 + x^2 + 1; with this start no all-empty or all-valid frame passes

SILENCE_SECONDS = 8


def silence(seconds: int = SILENCE_SECONDS, rate: int = 44100) -> bytes:
    """on.wav: silence as the probes had it streamed, 16-bit mono at 44.1 kHz. They found 3 s streamed and 2 s
    kept, so 8 s is far from the edge whether the client goes by length or by size. About 700 KB, on disk once."""
    samples = rate * seconds
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + 2 * samples, b"WAVE", b"fmt ", 16, 1, 1, rate, 2 * rate,
                       2, 16, b"data", 2 * samples) + bytes(2 * samples)


def crc(message: int, bits: int = CRC_SHIFT + 1) -> int:
    """CRC-5 of the frame's parity, type and payload (most significant bit first), as Core.lua computes it."""
    value = CRC_INIT
    for i in reversed(range(bits)):
        top = value >> 4
        value = (value << 1) & 0x1F
        if top ^ (message >> i & 1):
            value ^= CRC_POLY
    return value


@dataclass(frozen=True)
class Frame:
    type: int
    payload: int = 0

    def bits(self, parity: int) -> int:
        """The 24 data bits of this frame written with this parity: type, payload, CRC (lowest bit: 00.wav)."""
        body = self.type | self.payload << TYPE_BITS
        return body | crc(parity << CRC_SHIFT | body) << CRC_SHIFT


def decode(bits: int, parity: int) -> Frame | None:
    """The frame in 24 data bits read with this parity; None if the CRC doesn't match (torn)."""
    body = bits & (1 << CRC_SHIFT) - 1
    if bits >> CRC_SHIFT != crc(parity << CRC_SHIFT | body):
        return None
    return Frame(body & (1 << TYPE_BITS) - 1, body >> TYPE_BITS)


def data(seq: int) -> Frame:
    return Frame(DATA, seq % (1 << PAYLOAD_BITS))


def request(kind: int, request_id: int) -> Frame:
    return Frame(REQUEST, kind | request_id % REQUEST_IDS << 3)


def ack(question: int, quiet: float) -> Frame:
    return Frame(ACK, question % QUESTION_IDS | min(MAX_QUIET, max(0, round(quiet))) << 4)


def progress(step: int, tokens: int | None) -> Frame:
    return Frame(PROGRESS, step | tokens_code(tokens) << 5)


def test(n: int) -> Frame:
    """The handshake's frames: two in a row always differ, both ways, in the data files too."""
    return Frame(TEST, 0xAAAA if n % 2 else 0x5555)


def long(kind: int, payload: bytes) -> list[Frame]:
    """A message longer than a frame: its kind and length, then its bytes, two to a frame."""
    if not 0 <= kind < 16 or len(payload) > MAX_LONG:
        raise ValueError(f"a long message is a kind below 16 and at most {MAX_LONG} bytes")
    padded = payload + b"\0" * (len(payload) % 2)
    return [Frame(LONG, kind | len(payload) << 4),
            *(Frame(MORE, padded[i] << 8 | padded[i + 1]) for i in range(0, len(padded), 2))]


UNCOUNTED = (1 << 11) - 1


def tokens_code(count: int | None) -> int:
    """Tokens written, in 11 bits, exact as the game shows them up to 99.9k: below 1000 as they are, then in
    hundreds, rounded as Chat.lua rounds them ("3.4k"); then in ten thousands up to 660k. None: not counted."""
    if count is None:
        return UNCOUNTED
    count = max(0, int(count))
    if count < 1000:
        return count
    hundreds = (count + 50) // 100
    if hundreds < 1000:
        return 990 + hundreds
    return min(UNCOUNTED - 1, 1980 + max(10, count // 10000))


def tokens_count(code: int) -> int | None:
    """What tokens_code says, as the game reads it."""
    if code >= UNCOUNTED:
        return None
    if code < 1000:
        return code
    return (code - 990) * 100 if code < 1990 else (code - 1980) * 10000
