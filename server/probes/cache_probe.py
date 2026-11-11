"""Research probe: does the game remember files per path as written, or per file?

Two of the channel's limits come from the client remembering files it has read: a sound file that
was once valid stays valid for the whole process, and a slot addon loads once per UI session. If
that memory is keyed on the path as written, other spellings of the same path (case, slashes,
"." segments) read the file afresh, and a few files could carry updates without limit. The same
for fonts: an addon can load a font from its folder and measure its glyphs, so a font the server
rewrites could carry bigger data.

    uv run --with fonttools python probes/cache_probe.py prepare   # with WoW closed: new files are only seen at startup
    uv run --with fonttools python probes/cache_probe.py watch     # while /mcp probe runs in game

`prepare` writes probe/ in the installed WowMCP folder: two valid silent sounds (s1, s2), a trigger
(go.wav, empty), a small font (f.ttf) and a texture (t.tga). In game, /mcp probe plays and loads
them, then "plays" go.wav. `watch` sees that open and changes the files: s1 becomes empty, the
font's glyphs get other widths, the texture other proportions. The game then reads them again
under other spellings, and reports what it got; `watch` logs every time the game opens a probe
file from disk, which shows directly which spellings were read afresh.
"""

import argparse
import ctypes
import os
import struct
import sys
import time
from pathlib import Path

from wow_mcp import install
from wow_mcp.stream import IN_CLOSE_NOWRITE, IN_OPEN
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


def probe_dir(addons: Path) -> Path:
    return addons / ADDON / PROBE


def prepare(addons: Path) -> None:
    folder = probe_dir(addons)
    folder.mkdir(exist_ok=True)
    (folder / "s1.wav").write_bytes(SILENT_WAV)
    (folder / "s2.wav").write_bytes(SILENT_WAV)
    (folder / "go.wav").write_bytes(b"")
    (folder / "f.ttf").write_bytes(font(1))
    (folder / "t.tga").write_bytes(tga(*TEXTURE[1]))
    print(f"Wrote {', '.join(sorted(p.name for p in folder.iterdir()))} to {folder}")
    print("Start WoW (the game only sees new files at startup), then run `watch` and /mcp probe in game.")


def change(folder: Path) -> None:
    (folder / "s1.wav").write_bytes(b"")
    (folder / "f.ttf").write_bytes(font(2))
    (folder / "t.tga").write_bytes(tga(*TEXTURE[2]))


def watch(addons: Path) -> None:
    folder = probe_dir(addons)
    if not folder.is_dir():
        sys.exit(f"No {folder}: run `prepare` first (with WoW closed).")
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_CLOEXEC)
    if fd < 0 or libc.inotify_add_watch(fd, os.fsencode(folder), IN_OPEN | IN_CLOSE_NOWRITE) < 0:
        sys.exit(f"inotify failed: {os.strerror(ctypes.get_errno())}")
    print(f"Watching {folder}. Run /mcp probe in game; Ctrl+C to stop.", flush=True)
    start = None
    while True:
        data = os.read(fd, 64 * 1024)
        at = 0
        while at < len(data):
            _wd, mask, _cookie, size = struct.unpack_from("iIII", data, at)
            name = data[at + 16:at + 16 + size].rstrip(b"\0").decode()
            at += 16 + size
            if not mask & IN_OPEN:
                continue
            now = time.monotonic()
            start = start or now
            print(f"{now - start:7.3f}s  game opened {name}", flush=True)
            if name == "go.wav":
                change(folder)
                print(f"{time.monotonic() - start:7.3f}s  -> s1.wav emptied, f.ttf version 2, t.tga "
                      f"{TEXTURE[2][0]}x{TEXTURE[2][1]}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["prepare", "watch"])
    parser.add_argument("--addons-dir", type=Path, default=os.environ.get(install.ADDONS_DIR_ENV))
    args = parser.parse_args()
    if not args.addons_dir:
        sys.exit(f"Pass --addons-dir or set {install.ADDONS_DIR_ENV}.")
    {"prepare": prepare, "watch": watch}[args.command](Path(args.addons_dir))


if __name__ == "__main__":
    main()
