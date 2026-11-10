"""Money, XP, hearthstone and /played (Stats.lua, tools/stats.py)."""

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wow_mcp import kinds
from wow_mcp.tools import stats


def test_character_stats(live_game):
    with live_game():
        result = asyncio.run(stats.get_character_stats())
    data = result.model_dump()
    total, this_level = data.pop("played_total_seconds"), data.pop("played_this_level_seconds")
    played = data.pop("played")
    assert data == {
        "game": "World of Warcraft: Forever 1.60.1 (build 70862, EU region, enUS client), a vanilla-era world on "
        "the modern client",
        "money": "12g 34s 56c", "money_copper": 123456, "level": 12, "max_level": 60,
        "xp": 4200, "xp_max": 8800, "xp_to_level": 4600, "rested_xp": 3000, "resting": False, "xp_disabled": False,
        "hearthstone": "Thelsamar",
    }
    # The stub's clock runs fast, so /played has grown by an unknown amount since login.
    assert 200000 < total < 210000 and this_level - 3000 == total - 200000
    assert played.startswith("2d 7h ") and played.endswith(" this level")


def test_played_is_asked_once_and_quietly(run_client):
    run_client(r"""
        local ns = client.LoadAddon()
        local sent
        ns.Stream.Send = function(_, value) sent = value end
        local function Stats() ns.Answer(1, ns.Kinds.STATE) return sent.stats end

        assert(Stats().played == nil, "unknown until the game answers")
        client.Wait(12) -- asked 5 s after login, answered 0.5 s later (total 200005, level 3005 at 5.5 s)
        assert(client.played.requests == 1)
        assert(#client.chat == 0, "the addon's own request doesn't print /played: " .. table.concat(client.chat, "\n"))
        assert(ChatFrame1:IsEventRegistered("TIME_PLAYED_MSG"), "the chat frame gets /played back")
        assert(not ChatFrame2:IsEventRegistered("TIME_PLAYED_MSG"), "a frame that didn't show /played still doesn't")

        -- Answers add the time since the game's answer instead of asking again.
        client.Wait(4)
        local played = Stats().played
        assert(played.total == 200007 and played.level == 3007, played.total .. " " .. played.level)
        assert(client.played.requests == 1)

        -- The player's own /played still prints, and refreshes what we know.
        RequestTimePlayed()
        client.Wait(1)
        assert(table.concat(client.chat, "\n"):find("Total time played: 200008", 1, true))

        -- A level up starts the level's clock again.
        client.Fire("PLAYER_LEVEL_UP", 13)
        client.Wait(2)
        played = Stats().played
        assert(played.total == 200009 and played.level == 1, played.total .. " " .. played.level)

        -- At the level cap there's no XP to report.
        UnitLevel = function() return 60 end
        local stats = Stats()
        assert(stats.level == 60 and stats.maxLevel == 60 and stats.xp == nil and stats.rested == nil)
        assert(stats.money == 123456 and stats.hearth == "Thelsamar")
    """)


def test_chat_gets_played_back_without_an_answer(run_client):
    run_client(r"""
        RequestTimePlayed = function() client.played.requests = client.played.requests + 1 end
        client.LoadAddon()
        client.Wait(12)
        assert(client.played.requests == 1 and not ChatFrame1:IsEventRegistered("TIME_PLAYED_MSG"))
        client.Wait(20)
        assert(ChatFrame1:IsEventRegistered("TIME_PLAYED_MSG"))
    """)


def fake_game(monkeypatch, reply):
    async def request(kind):
        assert kind == kinds.STATE
        return reply, "WoW"

    monkeypatch.setattr(stats, "request", request)


def test_level_cap_and_unknown_played(monkeypatch):
    fake_game(monkeypatch, {"stats": {"money": 5, "level": 60, "maxLevel": 60, "hearth": "Booty Bay", "resting": True}})
    result = asyncio.run(stats.get_character_stats())
    assert result.model_dump() == {
        "game": "WoW", "money": "5c", "money_copper": 5, "level": 60, "max_level": 60, "xp": None, "xp_max": None,
        "xp_to_level": None, "rested_xp": None, "resting": True, "xp_disabled": False, "hearthstone": "Booty Bay",
        "played_total_seconds": None, "played_this_level_seconds": None, "played": None,
    }

    fake_game(monkeypatch, {"errors": {"stats": "attempt to call a nil value"}})
    with pytest.raises(ToolError, match="couldn't collect stats"):
        asyncio.run(stats.get_character_stats())


def test_formatting():
    assert [stats.format_money(c) for c in (0, 99, 150, 10000, 1234567)] == ["0c", "99c", "1s 50c", "1g 0s 0c",
                                                                           "123g 45s 67c"]
    assert [stats.format_duration(s) for s in (59, 3700, 90061)] == ["0m", "1h 1m", "1d 1h 1m"]
