"""Research probes: other ways for the server and the game to talk.

Today the server reaches the running game through load-on-demand slot addons and sound
"doorbells" (thousands of numbered files, read twice a second), and the game reaches the server by
opening files from a pool of 4096. The probes test other channels: less discoverable, without
per-launch limits, or both. Run them with WoW at the character screen or in a quiet spot, one at a
time; each has a server half (here) and a game half (/mcp probe <name>).

    uv run --with fonttools --with numpy --with evdev python probes/channel_probes.py <command>

prepare  With WoW closed (new files are only seen at startup): writes probe/ in the installed WowMCP
         folder: two valid silent sounds, a trigger, a font, a texture and three short tones.
files    Server to game, few files: does the game remember a file per path as written? Watches
         probe/; when the game opens go.wav, empties s1.wav and gives the font other glyph widths
         and the texture other proportions, and logs every probe file the game reads from disk.
         Game: /mcp probe files
pad      Server to game, no files at all: a virtual gamepad whose spare axes carry numbers. Creates
         it, then steps an axis WoW doesn't use for movement through values. Game: /mcp probe pad
         (turns gamepad support on for the probe, and back off).
listen   Game to server, no file opens: the addon plays short, very high, quiet tones; this records
         WoW's own audio stream (PipeWire, only WoW's) and reports which tones it heard, and when.
         Game: /mcp probe audio
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


# --- pad --------------------------------------------------------------------------------------------

def pad(_addons: Path, args) -> None:
    """A virtual gamepad. Its sticks and triggers stay at rest; spare axes (throttle, rudder) step
    through values the probe in game reads back from GetDeviceRawState. --xbox makes it look like an
    Xbox 360 pad, which games recognise more readily but may drop spare axes for."""
    from evdev import AbsInfo, UInput, ecodes as e

    stick = AbsInfo(value=0, min=-32768, max=32767, fuzz=0, flat=0, resolution=0)
    trigger = AbsInfo(value=0, min=0, max=255, fuzz=0, flat=0, resolution=0)
    spare = AbsInfo(value=0, min=0, max=65535, fuzz=0, flat=0, resolution=0)
    axes = [(e.ABS_X, stick), (e.ABS_Y, stick), (e.ABS_RX, stick), (e.ABS_RY, stick),
            (e.ABS_Z, trigger), (e.ABS_RZ, trigger), (e.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)),
            (e.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0))]
    if not args.xbox:
        axes += [(e.ABS_THROTTLE, spare), (e.ABS_RUDDER, spare)]
    buttons = [e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST, e.BTN_TL, e.BTN_TR, e.BTN_SELECT,
               e.BTN_START, e.BTN_MODE, e.BTN_THUMBL, e.BTN_THUMBR]
    ids = dict(vendor=0x045E, product=0x028E, version=0x110) if args.xbox else dict(vendor=0x1209, product=0x7E57)
    name = "Xbox 360 Controller" if args.xbox else "WowMCP Probe Pad"
    device = UInput({e.EV_KEY: buttons, e.EV_ABS: axes}, name=name, bustype=e.BUS_USB, **ids)
    # On an Xbox pad the right trigger carries the numbers, kept below where the game counts it
    # as pressed; otherwise the spare throttle axis.
    axis, values = (e.ABS_RZ, [0, 8, 16, 24, 32, 40, 48]) if args.xbox else \
        (e.ABS_THROTTLE, [0, 1, 2, 1000, 1001, 30000, 65535])
    print(f"Virtual pad '{name}' created. Run /mcp probe pad in game now (it reads for 15 seconds).", flush=True)
    time.sleep(3)
    start = time.monotonic()
    try:
        while time.monotonic() - start < args.seconds:
            for value in values:
                device.write(e.EV_ABS, axis, value)
                device.syn()
                print(f"{time.monotonic() - start:6.2f}s  axis {e.ABS[axis]} = {value}", flush=True)
                time.sleep(0.5)
    finally:
        device.close()
        print("Virtual pad removed.")


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
    parser.add_argument("command", choices=["prepare", "files", "pad", "listen", "chatlog"])
    parser.add_argument("--addons-dir", type=Path, default=os.environ.get(install.ADDONS_DIR_ENV))
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--xbox", action="store_true", help="pad: look like an Xbox 360 controller")
    parser.add_argument("--snr", type=float, default=8.0, help="listen: how far above the noise counts as a tone")
    args = parser.parse_args()
    if not args.addons_dir:
        sys.exit(f"Pass --addons-dir or set {install.ADDONS_DIR_ENV}.")
    {"prepare": prepare, "files": files, "pad": pad, "listen": listen, "chatlog": chatlog}[args.command](
        Path(args.addons_dir), args)


if __name__ == "__main__":
    main()
