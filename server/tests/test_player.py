"""The player's location, character and professions (Player.lua, tools/player.py)."""

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wow_mcp import app, game
from wow_mcp.tools import player


def test_location_and_professions(live_game):
    with live_game("client.player = { map = 1454, x = 0.632, y = 0.451 }"):
        location = asyncio.run(player.get_player_location())
        professions = asyncio.run(player.get_professions())
    assert location.model_dump() == {
        "game": "World of Warcraft: Forever 1.60.1 (build 70862, EU region, enUS client), a vanilla-era world on "
        "the modern client",
        "zone": "Orgrimmar", "subzone": "Razor Hill", "continent": "Kalimdor", "x": 63.2, "y": 45.1,
        "map_id": 1454, "faction": "Alliance", "race": "Dwarf", "player_class": "Hunter", "level": 12,
        "in_instance": False,
    }
    assert [p.model_dump() for p in professions.professions] == [
        {"name": "Skinning", "rank": 87, "max_rank": 150, "kind": "primary"},
        {"name": "Leatherworking", "rank": 60, "max_rank": 75, "kind": "primary"},
        {"name": "Cooking", "rank": 40, "max_rank": 75, "kind": "secondary"},
    ]


def test_tool_errors(addons, monkeypatch):
    with pytest.raises(ToolError, match="isn't running"):
        asyncio.run(player.get_player_location())
    monkeypatch.setattr(game, "running_folders", lambda proc=None: {"_forever_"})
    monkeypatch.setattr(app, "START_TIMEOUT", 0.3)
    with pytest.raises(ToolError, match="didn't answer"):
        asyncio.run(player.get_professions())


def test_missing_section_explains_itself():
    assert app.section({"location": {}}, "location") == {}
    with pytest.raises(ToolError, match="couldn't collect professions: boom"):
        app.section({"errors": {"professions": "boom"}}, "professions")
    with pytest.raises(ToolError, match="doesn't send professions yet"):
        app.section({}, "professions")
