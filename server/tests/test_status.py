"""The status tool: builds in game, installed and in the repo, and the live channel (Status.lua, tools/status.py)."""

import asyncio
import shutil

import pytest

from wow_mcp import app, game, install
from wow_mcp.tools import status
from wow_mcp.transport import slot_name

FOREVER = ("World of Warcraft: Forever 1.60.1 (build 70862, EU region, enUS client), a vanilla-era world on the "
           "modern client")


def build_file(addons):
    return addons / "WowMCP" / "Build.lua"


def get_status():
    return asyncio.run(status.get_status())


def test_up_to_date(live_game):
    with live_game():
        result = get_status()
    build = install.addon_build()
    assert result.model_dump() == {
        "game": FOREVER, "ok": True, "advice": [], "game_answered": True, "game_problem": None,
        "repo_build": build, "installed_build": build, "loaded_build": build,
        "live_updates": True, "live_problem": None, "slots_left": 300, "slot_count": 300,
        "last_update_sent": 0, "last_update_in_game": 0, "blocked_actions": [],
    }


def test_blocked_actions_are_reported(live_game):
    # The game raises the event inside the blocked call: the addon keeps it with the code path.
    setup = """
        C_Timer.After(1, function()
            local function Culprit() client.Fire("ADDON_ACTION_BLOCKED", "WowMCP", "CastSpellByName()") end
            Culprit()
        end)
    """
    with live_game(setup):
        result = get_status()
    [blocked] = result.blocked_actions
    assert (blocked.kind, blocked.addon, blocked.function) == ("ADDON_ACTION_BLOCKED", "WowMCP", "CastSpellByName()")
    assert blocked.at is not None and "Culprit" in blocked.stack, blocked.stack


def test_needs_reload(addons, live_game):
    # The game loaded an older install; the installer has since written the current one.
    current = build_file(addons).read_text()
    build_file(addons).write_text('local _, ns = ...\nns.BUILD = "0123456789ab"\n')
    with live_game():
        build_file(addons).write_text(current)
        result = get_status()
    assert result.loaded_build == "0123456789ab" and result.installed_build == result.repo_build
    assert result.advice == ["/reload in game: the installed addon code is newer than what the game has loaded."]
    assert not result.ok


def test_needs_install(addons, live_game, monkeypatch):
    monkeypatch.setattr(install, "addon_build", lambda: "fedcba987654")
    with live_game():
        result = get_status()
    assert result.loaded_build == result.installed_build != "fedcba987654"
    assert result.advice == [
        f'Run wow-mcp-install (from the server folder: uv run wow-mcp-install "{addons}"): the addon code in the '
        "server's checkout is newer than the installed copy; then /reload in game."
    ]


def test_needs_restart(addons, live_game):
    # Build.lua didn't exist when the game started, so the game never loaded it.
    current = build_file(addons).read_text()
    build_file(addons).write_text("local _, ns = ...\n")
    with live_game():
        build_file(addons).write_text(current)
        result = get_status()
    assert result.game_answered and result.loaded_build is None
    assert result.advice == ["Fully quit and restart WoW: the game hasn't loaded the addon's Build.lua (the game only "
                             "discovers new addon files at startup)."]


def test_few_slots_left(live_game):
    with live_game("for i = 1, 295 do client.loaded[string.format('WowMCP_S%03d', i)] = true end"):
        result = get_status()
    assert result.slots_left == 5
    assert result.advice == ["Only 5 live updates left this session; a /reload when convenient resets them."]


def test_game_not_answering(addons, monkeypatch):
    build = install.addon_build()
    # Not running: what the server knows, and why the game can't be asked.
    result = get_status()
    assert result.game == FOREVER and not result.game_answered and not result.ok
    assert result.game_problem == "World of Warcraft: Forever isn't running."
    assert result.advice == [] and result.installed_build == result.repo_build == build
    assert result.last_update_sent == 0 and result.loaded_build is None and result.slots_left is None

    # Running, but the addon doesn't answer (e.g. live updates off, or the addon not loaded).
    monkeypatch.setattr(game, "running_folders", lambda proc=None: {"_forever_"})
    monkeypatch.setattr(app, "START_TIMEOUT", 0.3)
    result = get_status()
    assert result.game_problem.startswith("The game didn't answer.")
    assert len(result.advice) == 1 and result.advice[0].startswith("Ask the player to type /mcp status in game.")

    # Slot addons missing on disk too: install first, then restart.
    shutil.rmtree(addons / slot_name(7))
    result = get_status()
    assert result.advice[0] == (
        f'Run wow-mcp-install (from the server folder: uv run wow-mcp-install "{addons}"): addon files are missing '
        "from the AddOns folder; then fully quit and restart WoW."
    )


def test_not_set_up(wow_root, monkeypatch):
    monkeypatch.delenv("WOW_MCP_ADDONS_DIR", raising=False)
    result = get_status()
    assert result.game == "unknown" and result.repo_build == install.addon_build()
    assert result.advice == ["Set WOW_MCP_ADDONS_DIR in this MCP server's configuration to the game's "
                             "Interface/AddOns folder."]

    empty = wow_root / "_forever_" / "Interface" / "AddOns"
    empty.mkdir(parents=True)
    monkeypatch.setenv("WOW_MCP_ADDONS_DIR", str(empty))
    monkeypatch.setattr(app, "_transport", None)
    result = get_status()
    assert result.game == FOREVER and result.game_problem == f"WowMCP is not installed in {empty}."
    assert result.advice == [f'Run wow-mcp-install (from the server folder: uv run wow-mcp-install "{empty}"): the '
                             "addon isn't installed; then fully quit and restart WoW."]


@pytest.mark.parametrize("reply, problem, advice", [
    ({}, "The addon in game doesn't report its status yet, so it runs older addon code.", [
        "/reload in game: the game is running older addon code.",
        "If get_status still can't read the addon's status after a /reload, fully quit and restart WoW.",
    ]),
    ({"errors": {"status": "boom"}}, "The game couldn't collect its status: boom", []),
])
def test_status_missing_from_reply(addons, monkeypatch, reply, problem, advice):
    async def request(kind):
        return reply, "the game"

    monkeypatch.setattr(status, "request", request)
    result = get_status()
    assert result.game == "the game" and result.game_answered and not result.ok
    assert result.game_problem == problem and result.advice == advice


def test_game_details_in_advice():
    found = status.Findings()
    status._check_game({"build": "b", "live": True, "slotsLeft": 0, "slotCount": 300, "slotProblem": "EXHAUSTED",
                        "seq": 4}, "b", 6, found)
    assert found.reload == "all live-update slots of this session are used, so updates wait for a /reload"
    assert found.notes == []

    found = status.Findings()
    status._check_game({"build": "b", "slotsLeft": 250, "slotProblem": "DISABLED", "seq": 3, "pendingLoad": True},
                       "b", 4, found)
    assert found.notes[0].startswith("Enable the WowMCP slot addons (WowMCP_S001-WowMCP_S300")
    assert found.notes[1] == "The latest update is waiting for the player to leave combat."

    found = status.Findings()
    status._check_game({"build": "b", "slotsLeft": 250, "slotProblem": "MISSING", "seq": 9}, "b", 2, found)
    assert found.restart == "the game can't find the live-update slot addons"
    assert found.reload == "the game has seen newer updates than this server sent, so it ignores new ones"


def test_probe_runs(run_client):
    # The research probes (/mcp probe files, audio) run to the end without their server halves,
    # and keep what they saw.
    run_client(r"""
        client.LoadAddon()
        client.Wait()
        local function chat() return table.concat(client.chat, "\n") end
        SlashCmdList.WOWMCP("probe")
        assert(chat():find("/mcp probe files, audio, stream or font", 1, true), chat())
        SlashCmdList.WOWMCP("probe files")
        client.Wait(8)  -- ticks of 0.5 s: the probe takes about 3 s
        assert(chat():find("Probe done.", 1, true) and not chat():find("Probe failed", 1, true), chat())
        local files = WowMCPDB.probes.files
        assert(#files.sounds == 8 and #files.fonts == 8 and #files.textures == 8 and files.sounds[2][1] == "lower case")
        SlashCmdList.WOWMCP("probe audio")
        client.Wait(8)
        SlashCmdList.WOWMCP("probe stream")
        client.Wait(30)  -- about 13 s
        local stream = WowMCPDB.probes.stream.files
        assert(stream["big.ogg"].prepared and stream["big.ogg"].emptied and stream["big.ogg"].restored, chat())
        SlashCmdList.WOWMCP("probe font")
        client.Wait(6)
        assert(WowMCPDB.probes.font.first and WowMCPDB.probes.font.newString, chat())
        assert(#WowMCPDB.probes.audio.played == 8 and not chat():find("Probe failed", 1, true), chat())
    """)
