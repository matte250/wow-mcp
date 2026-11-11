"""Works out which version of WoW the player is on, so agents look up the right data.

Two sources, both outside the game: the install's .build.info (installed version of each
game flavour) and the running processes (which flavour is being played right now).
"""

import ctypes
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Install folder -> Battle.net product code, for the flavours this knows by name.
FOLDER_PRODUCTS = {
    "_retail_": "wow",
    "_ptr_": "wowt",
    "_xptr_": "wowxptr",
    "_beta_": "wow_beta",
    "_forever_": "wowf",
    "_classic_era_": "wow_classic_era",
    "_classic_era_ptr_": "wow_classic_era_ptr",
    "_anniversary_": "wow_anniversary",
    "_classic_": "wow_classic",
    "_classic_ptr_": "wow_classic_ptr",
    "_classic_beta_": "wow_classic_beta",
}

EXPANSIONS = {
    1: "Vanilla", 2: "The Burning Crusade", 3: "Wrath of the Lich King", 4: "Cataclysm",
    5: "Mists of Pandaria", 6: "Warlords of Draenor", 7: "Legion", 8: "Battle for Azeroth",
    9: "Shadowlands", 10: "Dragonflight", 11: "The War Within", 12: "Midnight",
}

RUNNING_EXE = re.compile(r"\\World of Warcraft\\(_[a-z_]+_)\\Wow[a-z]*\.exe", re.IGNORECASE)


@dataclass
class Game:
    folder: str
    version: str | None = None  # e.g. "1.60.1"
    build: str | None = None
    locale: str | None = None
    region: str | None = None

    @property
    def name(self) -> str:
        product = FOLDER_PRODUCTS.get(self.folder, "")
        major = int(self.version.split(".")[0]) if self.version else None
        expansion = EXPANSIONS.get(major) if major else None
        test = " (test realm)" if product.endswith(("ptr", "beta", "wowt")) else ""
        if product == "wowf":
            name = "World of Warcraft: Forever"
        elif product.startswith("wow_classic_era"):
            name = "World of Warcraft Classic Era (vanilla)"
        elif product.startswith("wow_anniversary"):
            name = f"World of Warcraft Classic Anniversary ({expansion or 'unknown expansion'})"
        elif product.startswith("wow_classic"):
            name = f"World of Warcraft Classic ({expansion or 'unknown expansion'})"
        elif product in ("wow", "wowt", "wowxptr", "wow_beta"):
            name = f"World of Warcraft ({expansion or 'retail'})"
        else:
            name = f"World of Warcraft ({self.folder.strip('_')})"
        return name + test

    def describe(self) -> str:
        details = [f"build {self.build}"] if self.build else []
        if self.region:
            details.append(f"{self.region} region")
        if self.locale:
            details.append(f"{self.locale} client")
        version = f" {self.version}" if self.version else ""
        text = f"{self.name}{version}" + (f" ({', '.join(details)})" if details else "")
        if FOLDER_PRODUCTS.get(self.folder) == "wowf":
            text += ", a vanilla-era world on the modern client"
        return text


def read_build_info(root: Path) -> dict[str, dict[str, str]]:
    """Product code -> fields of its row in <root>/.build.info."""
    try:
        lines = (root / ".build.info").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    if not lines:
        return {}
    columns = [c.split("!")[0] for c in lines[0].split("|")]
    rows = {}
    for line in lines[1:]:
        fields = dict(zip(columns, line.split("|")))
        if fields.get("Product"):
            rows[fields["Product"]] = fields
    return rows


def installed_game(root: Path, folder: str) -> Game:
    game = Game(folder)
    row = read_build_info(root).get(FOLDER_PRODUCTS.get(folder, ""))
    if row:
        version = row.get("Version", "")
        game.version, _, game.build = version.rpartition(".") if version.count(".") >= 3 else (version, "", "")
        game.build = game.build or None
        tags = row.get("Tags", "")
        if m := re.search(r"\b([a-z]{2}[A-Z]{2}) text\?", tags):
            game.locale = m.group(1)
        if m := re.search(r"\b(US|EU|KR|TW|CN)\?", tags):
            game.region = m.group(1)
    return game


def _linux_commands(proc: Path) -> list[str]:
    """The command line of every running process."""
    commands = []
    try:
        entries = list(proc.iterdir())
    except OSError:
        return commands
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            commands.append((entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace"))
        except OSError:
            continue
    return commands


def _windows_commands() -> list[str]:
    """The program file of every running process."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    size = 4096
    while True:
        pids = (wintypes.DWORD * size)()
        used = wintypes.DWORD()
        if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(used)):
            return []
        count = used.value // ctypes.sizeof(wintypes.DWORD)
        if count < size:
            break
        size *= 2
    commands = []
    name = ctypes.create_unicode_buffer(32768)
    for pid in pids[:count]:
        process = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            continue
        length = wintypes.DWORD(len(name))
        if kernel32.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(length)):
            commands.append(name.value)
        kernel32.CloseHandle(process)
    return commands


def running_folders(proc: Path | None = None) -> set[str]:
    """Install folders (e.g. "_forever_") of the WoW clients running right now: from /proc on
    Linux (where the game's Windows path is in its command line), from the process list on
    Windows. `proc` is a /proc folder to read instead."""
    if proc is None and sys.platform == "win32":
        commands = _windows_commands()
    else:
        commands = _linux_commands(proc or Path("/proc"))
    return {m.group(1).lower() for command in commands if (m := RUNNING_EXE.search(command))}


@dataclass
class Situation:
    installed: Game  # the flavour the WowMCP addon is installed in
    running: list[Game]

    @property
    def mismatch(self) -> Game | None:
        """A running client the addon isn't installed in, when the addon's own isn't running."""
        if self.running and all(g.folder != self.installed.folder for g in self.running):
            return self.running[0]
        return None


def situation(addons_dir: Path, proc: Path | None = None) -> Situation:
    # <root>/<_flavour_>/Interface/AddOns
    flavour_dir = addons_dir.resolve().parents[1]
    root = flavour_dir.parent
    running = [installed_game(root, folder) for folder in sorted(running_folders(proc))]
    return Situation(installed_game(root, flavour_dir.name), running)
