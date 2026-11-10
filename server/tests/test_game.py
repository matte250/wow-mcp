"""Working out the game version (game.py) and what agents are told about it (app.py)."""

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import RUNNING_FOLDERS
from wow_mcp import app, game
from wow_mcp.tools import map, player


def fake_proc(tmp_path, *cmdlines: str):
    proc = tmp_path / "proc"
    for pid, cmdline in enumerate(cmdlines, start=100):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode())
    (proc / "self").mkdir(parents=True, exist_ok=True)
    return proc


FOREVER_CMDLINE = r"C:\Program Files (x86)\World of Warcraft\_forever_\Wow.exe -launcherlogin -uid wowf"


ANNIVERSARY_CMDLINE = r"C:\Program Files (x86)\World of Warcraft\_anniversary_\WowClassic.exe -launcherlogin -uid wow_anniversary"


def test_game_names(wow_root):
    assert game.installed_game(wow_root, "_retail_").describe() == \
        "World of Warcraft (Midnight) 12.1.5 (build 70783, EU region, enUS client)"
    assert game.installed_game(wow_root, "_anniversary_").name == "World of Warcraft Classic Anniversary (The Burning Crusade)"
    assert game.installed_game(wow_root, "_classic_").name == "World of Warcraft Classic (Mists of Pandaria)"
    assert game.installed_game(wow_root, "_classic_era_").describe() == "World of Warcraft Classic Era (vanilla)"


def test_running_folders(tmp_path):
    proc = fake_proc(tmp_path, FOREVER_CMDLINE, "/usr/bin/bash -c sleep", ANNIVERSARY_CMDLINE)
    (proc / "999").mkdir()  # a process that exited: no cmdline
    assert game.running_folders(proc) == {"_forever_", "_anniversary_"}


def test_instructions_follow_the_running_client(addons, tmp_path, monkeypatch):
    situation = game.situation
    monkeypatch.setattr(game, "running_folders", RUNNING_FOLDERS)  # undo the fixture's stub

    def with_processes(name, *cmdlines):
        proc = fake_proc(tmp_path / name, *cmdlines)
        monkeypatch.setattr(game, "situation", lambda folder: situation(folder, proc))

    with_processes("none")
    assert app.game_note().startswith("The player plays World of Warcraft: Forever 1.60.1 (build 70862")
    assert app.mismatch_error() is None

    with_processes("forever", FOREVER_CMDLINE)
    assert app.game_note().startswith("The player is playing World of Warcraft: Forever 1.60.1")

    with_processes("anniversary", ANNIVERSARY_CMDLINE)
    note = app.game_note()
    assert note.startswith("The player is playing World of Warcraft Classic Anniversary (The Burning Crusade) 2.5.6")
    assert "only installed for World of Warcraft: Forever" in note
    with pytest.raises(ToolError, match="running game is World of Warcraft Classic Anniversary"):
        asyncio.run(player.get_player_location())
    result = map.show_on_map([map.Marker(zone="Durotar", x=1, y=1, label="x")])
    assert "But: The running game is World of Warcraft Classic Anniversary" in result
