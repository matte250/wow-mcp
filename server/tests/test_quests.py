"""Quest completion and the quest log (Quests.lua, tools/quests.py)."""

import asyncio

from wow_mcp.tools import quests


def test_quest_status(live_game):
    with live_game():
        result = asyncio.run(quests.get_quest_status([783, 20, 122, 9999]))
    assert [q.model_dump() for q in result.quests] == [
        {"quest_id": 783, "completed": True, "in_quest_log": False, "ready_to_turn_in": None, "title": None},
        {"quest_id": 20, "completed": False, "in_quest_log": True, "ready_to_turn_in": False,
         "title": "Blackrock Menace"},
        {"quest_id": 122, "completed": False, "in_quest_log": True, "ready_to_turn_in": True,
         "title": "Underbelly Scales"},
        {"quest_id": 9999, "completed": False, "in_quest_log": False, "ready_to_turn_in": None, "title": None},
    ]
