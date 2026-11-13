"""Install or update the WowMCP addon, its live-update slot addons and the files of its live channel.

WoW only discovers addon folders and files when it starts, so after this creates new
files the game must be fully restarted once. Re-running it to update the addon code is
safe: generated data is left alone.
"""

import argparse
import hashlib
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from wow_mcp import bell, junction_windows
from wow_mcp.transport import ADDON, INTERFACE, SLOT_COUNT, render_payload, slot_name, write_atomic

ADDON_SOURCE = Path(__file__).resolve().parents[2] / "addon" / ADDON
ADDONS_DIR_ENV = "WOW_MCP_ADDONS_DIR"
# The doorbells before the bell (bell.py): a sound file per signal, each good once per launch.
OLD_POOLS = ("sig", "req", "ctl")


def addon_build() -> str:
    """An ID of the addon source in this checkout; Build.lua in game carries the installed one."""
    digest = hashlib.sha256()
    for src in sorted(p for p in ADDON_SOURCE.iterdir() if p.is_file()):
        digest.update(src.name.encode() + b"\0" + src.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def slot_toc(i: int) -> str:
    return "\n".join([
        f"## Interface: {INTERFACE}",
        f"## Title: WowMCP slot {i:03d}",
        "## Notes: Live-update slot for WowMCP. Leave it enabled.",
        f"## Group: {ADDON}",
        "## LoadOnDemand: 1",
        f"## Dependencies: {ADDON}",
        "",
        "Inbox.lua",
        "",
    ])


def link_bell(folder: Path) -> int | None:
    """The junctions of the bell's link layout (bell.py), for Windows: one per bit, pointing at off/ when new,
    and chk at on/. Returns how many it made (new to the game: restart it), or None, with a warning, where the
    drive can't have junctions.

    The game may be running: a junction that's there keeps its target, unless it points outside this folder
    (the game was moved), when it's pointed back at this folder's on/ or off/."""
    made, here = 0, os.path.normcase(folder.absolute())
    try:
        for stem in (bell.LINK_CHECK, *bell.LINKS):
            link, default = folder / stem, bell.LINK_ON if stem == bell.LINK_CHECK else bell.LINK_OFF
            found = junction_windows.target(link)
            if found is None:
                junction_windows.create(link, folder / default)
                made += 1
            elif os.path.normcase(found.parent) != here or found.name not in (bell.LINK_ON, bell.LINK_OFF):
                kept = found.name in (bell.LINK_ON, bell.LINK_OFF) and stem != bell.LINK_CHECK
                junction_windows.point(link, folder / (found.name if kept else default))
    except OSError as error:
        print(f"Couldn't make the live channel's folder links ({error}). They need an NTFS drive; "
              "without them live updates don't reach the game on Windows.", file=sys.stderr)
        return None
    return made


@dataclass(frozen=True)
class Installed:
    created: int  # files the game hasn't seen yet: if any, restart WoW
    removed: int  # files of the old doorbells


def install(addons_dir: Path) -> Installed:
    created = 0

    def ensure(path: Path, data: bytes) -> None:
        nonlocal created
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        created += 1

    addon_dir = addons_dir / ADDON
    sources = {src.name for src in ADDON_SOURCE.iterdir() if src.is_file()}
    # Code files removed from the addon since the last install.
    for old in [*addon_dir.glob("*.lua"), *addon_dir.glob("*.xml")]:
        if old.name not in sources and old.name not in ("Inbox.lua", "Build.lua"):
            old.unlink()
    for src in ADDON_SOURCE.iterdir():
        if src.is_file() and src.name != "Inbox.lua":
            if not (addon_dir / src.name).exists():
                created += 1
            addon_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, addon_dir / src.name)

    build = f'local _, ns = ...\nns.BUILD = "{addon_build()}"\n'

    empty_payload = render_payload({"seq": 0}).encode("utf-8")
    ensure(addon_dir / "Inbox.lua", empty_payload)
    # The bell: every file empty but on.wav, whose silence valid files link to (an older one is made anew;
    # links to it keep theirs, which plays all the same).
    silence = bell.silence()
    for name in bell.FILES:
        ensure(addon_dir / bell.FOLDER / name, silence if name == bell.ON else b"")
    on = addon_dir / bell.FOLDER / bell.ON
    if on.stat().st_size != len(silence):
        write_atomic(on, silence)
    if sys.platform == "win32":
        # The files behind the junctions: written here and never again (the game holds them open).
        ensure(addon_dir / bell.FOLDER / bell.LINK_ON / bell.LINK_FILE, silence)
        ensure(addon_dir / bell.FOLDER / bell.LINK_OFF / bell.LINK_FILE, b"")
        links = link_bell(addon_dir / bell.FOLDER)
        if links is not None:
            created += links
            build += 'ns.BELL = "links"\n'
    (addon_dir / "Build.lua").write_text(build, encoding="utf-8")
    removed = 0
    for pool in OLD_POOLS:
        if (addon_dir / pool).is_dir():
            removed += sum(1 for _ in (addon_dir / pool).iterdir())
            shutil.rmtree(addon_dir / pool)
    for symbol in range(4096):  # see stream.py
        ensure(addon_dir / "out" / f"{symbol:03x}.wav", b"")
    for i in range(1, SLOT_COUNT + 1):
        slot_dir = addons_dir / slot_name(i)
        ensure(slot_dir / "Inbox.lua", empty_payload)
        toc = slot_dir / f"{slot_name(i)}.toc"
        if not toc.exists():
            created += 1
        toc.write_text(slot_toc(i), encoding="utf-8")
    return Installed(created, removed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "addons_dir",
        nargs="?",
        default=os.environ.get(ADDONS_DIR_ENV),
        help=f"the game's Interface/AddOns folder (default: ${ADDONS_DIR_ENV})",
    )
    args = parser.parse_args()
    if not args.addons_dir:
        parser.error(f"pass the Interface/AddOns folder or set {ADDONS_DIR_ENV}")
    addons_dir = Path(args.addons_dir).expanduser()
    if not addons_dir.is_dir():
        sys.exit(f"{addons_dir} is not a folder")

    done = install(addons_dir)
    print(f"Installed {ADDON} into {addons_dir}")
    if done.removed:
        print(f"Removed the {done.removed} files of the old doorbells ({', '.join(f'{p}/' for p in OLD_POOLS)}).")
    if done.created:
        print(f"{done.created} new files: fully quit and restart WoW once so it can see them.")
    else:
        print("Addon code updated. /reload in game to pick up the changes.")


if __name__ == "__main__":
    main()
