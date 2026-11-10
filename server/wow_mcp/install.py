"""Install or update the WowMCP addon, its live-update slot addons and signal files.

WoW only discovers addon folders and files when it starts, so after this creates new
files the game must be fully restarted once. Re-running it to update the addon code is
safe: generated data is left alone.
"""

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

from wow_mcp.transport import ADDON, BELLS, INTERFACE, SLOT_COUNT, render_payload, slot_name

ADDON_SOURCE = Path(__file__).resolve().parents[2] / "addon" / ADDON
ADDONS_DIR_ENV = "WOW_MCP_ADDONS_DIR"


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


def install(addons_dir: Path) -> int:
    """Returns how many files the game hasn't seen yet (non-zero means restart WoW)."""
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

    (addon_dir / "Build.lua").write_text(f'local _, ns = ...\nns.BUILD = "{addon_build()}"\n', encoding="utf-8")

    empty_payload = render_payload({"seq": 0}).encode("utf-8")
    ensure(addon_dir / "Inbox.lua", empty_payload)
    ensure(addon_dir / "ctl" / "empty.wav", b"")
    for bell, count in BELLS.items():
        for n in range(count):
            ensure(addon_dir / bell / f"{n:04d}.wav", b"")
    for symbol in range(4096):  # see stream.py
        ensure(addon_dir / "out" / f"{symbol:03x}.wav", b"")
    for i in range(1, SLOT_COUNT + 1):
        slot_dir = addons_dir / slot_name(i)
        ensure(slot_dir / "Inbox.lua", empty_payload)
        toc = slot_dir / f"{slot_name(i)}.toc"
        if not toc.exists():
            created += 1
        toc.write_text(slot_toc(i), encoding="utf-8")
    return created


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

    created = install(addons_dir)
    print(f"Installed {ADDON} into {addons_dir}")
    if created:
        print(f"{created} new files: fully quit and restart WoW once so it can see them.")
    else:
        print("Addon code updated. /reload in game to pick up the changes.")


if __name__ == "__main__":
    main()
