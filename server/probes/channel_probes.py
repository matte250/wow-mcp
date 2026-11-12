"""Research probes: other ways for the server and the game to talk.

When these were written, the server reached the running game through load-on-demand slot addons and
sound "doorbells" (thousands of numbered files, read twice a second, each good once per launch), and
the game reached the server by opening files from a pool of 4096. The probes test other channels:
less discoverable, without per-launch limits, or both. What `stream` and `speed` found became the
bell (wow_mcp/bell.py). Run them with WoW at the character screen or in a quiet spot, one at a
time; each has a server half (here) and a game half (/mcp probe <name>).

    uv run --with fonttools --with numpy python probes/channel_probes.py <command>

prepare  With WoW closed (new files are only seen at startup): writes probe/ in the installed WowMCP
         folder: two valid silent sounds, a trigger, a font, a texture and three short tones.
files    Server to game, few files: does the game remember a file per path as written? Watches
         probe/; when the game opens go.wav, empties s1.wav and gives the font other glyph widths
         and the texture other proportions, and logs every probe file the game reads from disk.
         Game: /mcp probe files
listen   Game to server, no file opens: the addon plays short, very high, quiet tones; this records
         WoW's own audio stream (PipeWire, only WoW's) and reports which tones it heard, and when.
         Game: /mcp probe audio
stream   Server to game without a per-launch limit: small sounds are read once and remembered,
         but long or compressed ones may be streamed from disk on every play. Watches probe/;
         on go2.wav empties the long sounds, on go3.wav puts them back 3 seconds long. If the game
         reads them again each time, one file can signal again and again. Game: /mcp probe stream
font     Server to game, bigger data: a real font (a few letters of a system font) whose letter
         widths the server rewrites on go4.wav. Run `stream` for it too. Game: /mcp probe font
speed    How far the streamed channel stretches: what a streamed play costs, whether a streamed file
         rewritten very short is still read afresh, and how precisely the game can time how long it
         plays (each file's length as several bits). On each go2.wav read, rewrites the streamed
         files to the next length in SPEED_LENGTHS. Game: /mcp probe speed
chatlog  Game to server for what the player types: follows WoW's chat log (/chatlog in game) and
         prints each new line with the time it arrived, to see how soon the client writes it.
"""

import argparse
import ctypes
import json
import math
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

from wow_mcp import install
from wow_mcp.stream import IN_CLOSE_NOWRITE
from wow_mcp.transport import ADDON, SILENT_WAV

PROBE = "probe"
# Glyph advance widths (font units, 1000 per em): A-J tell the two versions apart, K-M test how
# finely the game measures (1000, 1001 and 1010 units).
GLYPHS = "ABCDEFGHIJKLM"
WIDTHS = {
    1: [100 * (i + 1) for i in range(10)] + [1000, 1001, 1010],
    2: [100 * (10 - i) + 50 for i in range(10)] + [1000, 1001, 1010],
}
TEXTURE = {1: (64, 16), 2: (16, 64)}
# Tones the addon plays for `listen`: two near the top of hearing, one plain control.
RATE = 48000
TONES = {"tone_a": 19000, "tone_b": 19600, "tone_c": 1000}
TONE_SECONDS, TONE_LEVEL = 0.08, 0.05
# Long silent sounds for `stream`, in the formats a game might stream rather than load: seconds as
# prepared, and after go3.wav.
STREAMS = {"big.wav": 60, "big.ogg": 60, "big.mp3": 60, "small.ogg": 2}
RESTORED_SECONDS = 3
SPEED_FILES = ("big.wav", "big.ogg", "big.mp3")
# Lengths for `speed`, in seconds: short ones to see if they still stream, and 0.25 five times
# to see how much the measured length varies.
SPEED_LENGTHS = [0.05, 0.1, 0.15, 0.2, 0.25, 0.25, 0.25, 0.25, 0.25, 0.3, 0.4, 0.5, 0.75, 1.0]
REAL_FONT = "/usr/share/fonts/Adwaita/AdwaitaMono-Regular.ttf"


def font(version: int) -> bytes:
    """A small TrueType font: a box per glyph, advance widths from WIDTHS[version]."""
    from io import BytesIO

    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    def box(width):
        pen = TTGlyphPen(None)
        pen.moveTo((50, 0))
        pen.lineTo((50, 700))
        pen.lineTo((max(width - 50, 60), 700))
        pen.lineTo((max(width - 50, 60), 0))
        pen.closePath()
        return pen.glyph()

    names = [".notdef", "space"] + list(GLYPHS)
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(names)
    fb.setupCharacterMap({32: "space", **{ord(c): c for c in GLYPHS}})
    glyphs = {".notdef": box(500), "space": TTGlyphPen(None).glyph()}
    metrics = {".notdef": (500, 50), "space": (250, 0)}
    for c, width in zip(GLYPHS, WIDTHS[version]):
        glyphs[c] = box(width)
        metrics[c] = (width, 50)
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": f"WowMCPProbe{version}", "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=800, usWinAscent=800, usWinDescent=200)
    fb.setupPost()
    out = BytesIO()
    fb.save(out)
    return out.getvalue()


def tga(width: int, height: int) -> bytes:
    """An uncompressed 32-bit TGA, a plain colour."""
    header = struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, width, height, 32, 8)
    return header + bytes([40, 160, 220, 255]) * (width * height)


def silent(name: str, seconds: float) -> bytes:
    """Silence as a wav, ogg or mp3 (by the name's extension)."""
    if name.endswith(".wav"):
        samples = int(44100 * seconds)
        return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + 2 * samples, b"WAVE", b"fmt ", 16, 1, 1, 44100,
                           88200, 2, 16, b"data", 2 * samples) + bytes(2 * samples)
    codec, fmt = ("libvorbis", "ogg") if name.endswith(".ogg") else ("libmp3lame", "mp3")
    return subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t",
                           str(seconds), "-c:a", codec, "-f", fmt, "pipe:1"], capture_output=True, check=True).stdout


def real_font(version: int) -> bytes:
    """A few letters of a real TrueType font (the generated one was refused), widths from WIDTHS."""
    from io import BytesIO

    from fontTools import subset
    from fontTools.ttLib import TTFont

    font = TTFont(REAL_FONT)
    options = subset.Options()
    options.layout_features, options.name_IDs, options.hinting = [], ["*"], False
    sub = subset.Subsetter(options)
    sub.populate(text=GLYPHS + " ")
    sub.subset(font)
    cmap = font.getBestCmap()
    for c, width in zip(GLYPHS, WIDTHS[version]):
        glyph = cmap[ord(c)]
        font["hmtx"][glyph] = (width * font["head"].unitsPerEm // 1000, font["hmtx"][glyph][1])
    out = BytesIO()
    font.save(out)
    return out.getvalue()


def tone(frequency: float) -> bytes:
    """A short mono 16-bit wav: a sine with soft edges, so it doesn't click."""
    count = int(RATE * TONE_SECONDS)
    samples = bytearray()
    for i in range(count):
        envelope = math.sin(math.pi * i / count) ** 2
        value = TONE_LEVEL * envelope * math.sin(2 * math.pi * frequency * i / RATE)
        samples += struct.pack("<h", int(value * 32767))
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(samples), b"WAVE", b"fmt ", 16, 1, 1, RATE,
                         RATE * 2, 2, 16, b"data", len(samples))
    return header + bytes(samples)


def probe_dir(addons: Path) -> Path:
    return addons / ADDON / PROBE


def prepare(addons: Path, _args) -> None:
    folder = probe_dir(addons)
    folder.mkdir(exist_ok=True)
    (folder / "s1.wav").write_bytes(SILENT_WAV)
    (folder / "s2.wav").write_bytes(SILENT_WAV)
    (folder / "go.wav").write_bytes(b"")
    (folder / "f.ttf").write_bytes(font(1))
    (folder / "t.tga").write_bytes(tga(*TEXTURE[1]))
    for name, frequency in TONES.items():
        (folder / f"{name}.wav").write_bytes(tone(frequency))
    for name, seconds in STREAMS.items():
        (folder / name).write_bytes(silent(name, seconds))
    for trigger in ("go2.wav", "go3.wav", "go4.wav"):
        (folder / trigger).write_bytes(b"")
    (folder / "real.ttf").write_bytes(real_font(1))
    print(f"Wrote {', '.join(sorted(p.name for p in folder.iterdir()))} to {folder}")
    print("Start WoW now (the game only sees new files at startup).")


# --- files ----------------------------------------------------------------------------------------

def change(folder: Path) -> None:
    (folder / "s1.wav").write_bytes(b"")
    (folder / "f.ttf").write_bytes(font(2))
    (folder / "t.tga").write_bytes(tga(*TEXTURE[2]))


def files(addons: Path, _args) -> None:
    folder = probe_dir(addons)
    if not folder.is_dir():
        sys.exit(f"No {folder}: run `prepare` first (with WoW closed).")
    # Back to the first versions, so the probe can be run again.
    (folder / "s1.wav").write_bytes(SILENT_WAV)
    (folder / "f.ttf").write_bytes(font(1))
    (folder / "t.tga").write_bytes(tga(*TEXTURE[1]))
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_CLOEXEC)
    # Reads only: the game opens the files to read them, and closes them without writing. Our own
    # writes (prepare, the changes below) close with a write and aren't counted.
    if fd < 0 or libc.inotify_add_watch(fd, os.fsencode(folder), IN_CLOSE_NOWRITE) < 0:
        sys.exit(f"inotify failed: {os.strerror(ctypes.get_errno())}")
    print(f"Watching {folder}. Run /mcp probe files in game; Ctrl+C to stop.", flush=True)
    start = None
    while True:
        data = os.read(fd, 64 * 1024)
        at = 0
        while at < len(data):
            _wd, mask, _cookie, size = struct.unpack_from("iIII", data, at)
            name = data[at + 16:at + 16 + size].rstrip(b"\0").decode()
            at += 16 + size
            if not mask & IN_CLOSE_NOWRITE or not name:
                continue
            now = time.monotonic()
            start = start or now
            print(f"{now - start:7.3f}s  game read {name}", flush=True)
            if name == "go.wav":
                change(folder)
                print(f"{time.monotonic() - start:7.3f}s  -> s1.wav emptied, f.ttf version 2, t.tga "
                      f"{TEXTURE[2][0]}x{TEXTURE[2][1]}", flush=True)


# --- listen -----------------------------------------------------------------------------------------

def wow_stream() -> tuple[str | None, str]:
    """WoW's audio output stream: its PipeWire serial and a description; None if it isn't playing."""
    objects = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, check=True).stdout)
    for o in objects:
        props = (o.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        who = " ".join(str(props.get(k, "")) for k in ("application.name", "application.process.binary", "node.name"))
        if "warcraft" in who.lower() or "wow" in who.lower():
            return str(props.get("object.serial")), who.strip()
    return None, ""


def listen(_addons: Path, args) -> None:
    import numpy as np

    serial, who = wow_stream()
    if not serial:
        sys.exit("WoW's audio stream isn't there: is WoW running with sound on? (pw-dump lists no WoW stream)")
    print(f"Recording WoW's audio ({who}, serial {serial}) for {args.seconds}s. Run /mcp probe audio in game.",
          flush=True)
    record = subprocess.Popen(["pw-record", "--raw", "--format", "f32", "--rate", str(RATE), "--channels", "1",
                               "--target", serial, "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    block = RATE // 100  # 10 ms
    window = np.hanning(block)
    freqs = np.fft.rfftfreq(block, 1 / RATE)
    bins = {name: int(np.argmin(np.abs(freqs - f))) for name, f in TONES.items()}
    start, last, heard = time.monotonic(), {}, 0
    try:
        while time.monotonic() - start < args.seconds:
            raw = record.stdout.read(block * 4)
            if len(raw) < block * 4:
                break
            samples = np.frombuffer(raw, dtype=np.float32)
            spectrum = np.abs(np.fft.rfft(samples * window)) / block
            noise = np.median(spectrum) + 1e-9
            for name, b in bins.items():
                level = spectrum[max(b - 1, 0):b + 2].max()
                now = time.monotonic() - start
                if level / noise > args.snr and now - last.get(name, -1) > 0.05:
                    heard += 1
                    print(f"{now:7.3f}s  heard {name} ({TONES[name]} Hz) at {20 * math.log10(level):.0f} dB, "
                          f"{level / noise:.0f}x the noise", flush=True)
                if level / noise > args.snr:
                    last[name] = now
    finally:
        record.terminate()
    print(f"Done: {heard} tone(s) heard.")


# --- stream (and font) -----------------------------------------------------------------------------

def stream(addons: Path, _args) -> None:
    folder = probe_dir(addons)
    if not (folder / "big.wav").exists():
        sys.exit(f"No long sounds in {folder}: run `prepare` first (with WoW closed).")
    for name, seconds in STREAMS.items():  # back to the start, so the probe can be run again
        (folder / name).write_bytes(silent(name, seconds))
    (folder / "real.ttf").write_bytes(real_font(1))
    restored = {name: silent(name, RESTORED_SECONDS) for name in STREAMS}
    second_font = real_font(2)
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_CLOEXEC)
    if fd < 0 or libc.inotify_add_watch(fd, os.fsencode(folder), IN_CLOSE_NOWRITE) < 0:
        sys.exit(f"inotify failed: {os.strerror(ctypes.get_errno())}")
    print(f"Watching {folder}. Run /mcp probe stream (or font) in game; Ctrl+C to stop.", flush=True)
    start = None
    while True:
        data = os.read(fd, 64 * 1024)
        at = 0
        while at < len(data):
            _wd, mask, _cookie, size = struct.unpack_from("iIII", data, at)
            name = data[at + 16:at + 16 + size].rstrip(b"\0").decode()
            at += 16 + size
            if not mask & IN_CLOSE_NOWRITE or not name:
                continue
            now = time.monotonic()
            start = start or now
            print(f"{now - start:7.3f}s  game read {name}", flush=True)
            if name == "go2.wav":
                for stream_name in STREAMS:
                    (folder / stream_name).write_bytes(b"")
                print(f"{time.monotonic() - start:7.3f}s  -> long sounds emptied", flush=True)
            elif name == "go3.wav":
                for stream_name, data_ in restored.items():
                    (folder / stream_name).write_bytes(data_)
                print(f"{time.monotonic() - start:7.3f}s  -> long sounds back, {RESTORED_SECONDS} s long", flush=True)
            elif name == "go4.wav":
                (folder / "real.ttf").write_bytes(second_font)
                print(f"{time.monotonic() - start:7.3f}s  -> real.ttf with other widths", flush=True)


# --- speed -------------------------------------------------------------------------------------------

def speed(addons: Path, _args) -> None:
    folder = probe_dir(addons)
    if not (folder / "big.wav").exists():
        sys.exit(f"No long sounds in {folder}: run `prepare` first (with WoW closed).")
    for name in SPEED_FILES:  # as streamed at the last launch: long
        (folder / name).write_bytes(silent(name, STREAMS[name]))
    versions = [{name: silent(name, length) for name in SPEED_FILES} for length in SPEED_LENGTHS]
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_CLOEXEC)
    if fd < 0 or libc.inotify_add_watch(fd, os.fsencode(folder), IN_CLOSE_NOWRITE) < 0:
        sys.exit(f"inotify failed: {os.strerror(ctypes.get_errno())}")
    print(f"Watching {folder}. Run /mcp probe speed in game; Ctrl+C to stop.", flush=True)
    start, step, reads = None, 0, {}
    while True:
        data = os.read(fd, 64 * 1024)
        at = 0
        while at < len(data):
            _wd, mask, _cookie, size = struct.unpack_from("iIII", data, at)
            name = data[at + 16:at + 16 + size].rstrip(b"\0").decode()
            at += 16 + size
            if not mask & IN_CLOSE_NOWRITE or not name:
                continue
            now = time.monotonic()
            start = start or now
            reads[name] = reads.get(name, 0) + 1
            if name != "go2.wav":
                continue
            if step < len(SPEED_LENGTHS):
                for file_name, content in versions[step].items():
                    (folder / file_name).write_bytes(content)
                print(f"{time.monotonic() - start:7.3f}s  step {step + 1}: files now {SPEED_LENGTHS[step]} s long "
                      f"(reads so far: {', '.join(f'{k} {v}' for k, v in sorted(reads.items()))})", flush=True)
            else:
                print(f"{now - start:7.3f}s  done (reads: {', '.join(f'{k} {v}' for k, v in sorted(reads.items()))})",
                      flush=True)
            step += 1


# --- chatlog ----------------------------------------------------------------------------------------

def chatlog(addons: Path, args) -> None:
    log = addons.parent.parent / "Logs" / "WoWChatLog.txt"
    print(f"Following {log}. In game: /chatlog to start logging, then type in a channel or whisper "
          f"yourself; Ctrl+C to stop.", flush=True)
    while not log.exists():
        time.sleep(0.2)
    with log.open("r", errors="replace") as f:
        f.seek(0, os.SEEK_END)
        start = time.monotonic()
        while time.monotonic() - start < args.seconds:
            line = f.readline()
            if line:
                print(f"{time.strftime('%H:%M:%S')} ({time.monotonic() - start:6.2f}s)  {line.rstrip()}", flush=True)
            else:
                time.sleep(0.05)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["prepare", "files", "stream", "speed", "listen", "chatlog"])
    parser.add_argument("--addons-dir", type=Path, default=os.environ.get(install.ADDONS_DIR_ENV))
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--snr", type=float, default=8.0, help="listen: how far above the noise counts as a tone")
    args = parser.parse_args()
    if not args.addons_dir:
        sys.exit(f"Pass --addons-dir or set {install.ADDONS_DIR_ENV}.")
    {"prepare": prepare, "files": files, "stream": stream, "speed": speed, "listen": listen, "chatlog": chatlog}[args.command](
        Path(args.addons_dir), args)


if __name__ == "__main__":
    main()
