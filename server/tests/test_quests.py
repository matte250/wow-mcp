"""Quest completion and the quest log (Quests.lua, tools/quests.py)."""

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wow_mcp import kinds
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


def objective(text, fulfilled, required, finished):
    return {"text": text, "fulfilled": fulfilled, "required": required, "finished": finished}


def test_quest_log(live_game):
    with live_game():
        result = asyncio.run(quests.get_quest_log())
    assert result.game.startswith("World of Warcraft: Forever")
    redridge = {"header": "Redridge Mountains", "failed": False}
    assert [q.model_dump() for q in result.quests] == [
        {"quest_id": 20, "title": "Blackrock Menace", "level": 22, "tag": None, "ready_to_turn_in": False, **redridge,
         "objectives": [objective("Blackrock Champion slain: 7/15", 7, 15, False)]},
        {"quest_id": 122, "title": "Underbelly Scales", "level": 21, "tag": None, "ready_to_turn_in": True, **redridge,
         "objectives": [objective("Underbelly Whelp Scale: 6/6", 6, 6, True)]},
        {"quest_id": 19, "title": "Tharil'zun", "level": 26, "tag": "elite", "ready_to_turn_in": False, **redridge,
         "objectives": [objective("Tharil'zun's Head: 0/1", 0, 1, False)]},
        # Under a collapsed header, still listed.
        {"quest_id": 12, "title": "The People's Militia", "level": 14, "header": "Westfall", "tag": None,
         "ready_to_turn_in": False, "failed": False,
         "objectives": [objective("Defias Trapper slain: 15/15", 15, 15, True),
                        objective("Defias Smuggler slain: 7/15", 7, 15, False)]},
        {"quest_id": 65, "title": "The Defias Brotherhood", "level": 14, "header": "Westfall", "tag": None,
         "ready_to_turn_in": True, "failed": False, "objectives": []},
        {"quest_id": 309, "title": "Protecting the Shipment", "level": 15, "header": "Loch Modan", "tag": None,
         "ready_to_turn_in": False, "failed": True, "objectives": [objective("Protect Miran", 0, 1, False)]},
    ]


def test_quest_log_section_survives_hidden_objectives(run_client):
    run_client(r"""
        local ns = client.LoadAddon()
        -- The game won't give one quest's objectives (e.g. a secret value in restricted content).
        local get = C_QuestLog.GetQuestObjectives
        C_QuestLog.GetQuestObjectives = function(questID)
            if questID == 19 then error("secret") end
            return get(questID)
        end
        local reply
        ns.Stream.Send = function(_, value) reply = value end
        ns.Answer(1, ns.Kinds.QUESTS)
        assert(not reply.errors, "the section still works")
        local byID = {}
        for _, quest in ipairs(reply.quests.log) do byID[quest.id] = quest end
        assert(#reply.quests.log == 6)
        assert(byID[19].objectives == nil and byID[19].tag == "elite" and byID[19].level == 26)
        assert(byID[65].objectives and #byID[65].objectives == 0, "a quest without objectives has an empty list")
        assert(byID[309].failed == true and byID[20].failed == nil)
        assert(#reply.quests.completedGaps == 5)
    """)


def test_quest_log_from_replies(monkeypatch):
    replies = []

    async def fake_request(kind):
        assert kind == kinds.QUESTS
        return replies.pop(0), "test game"

    monkeypatch.setattr(quests, "request", fake_request)
    # The real client serializes an empty table as {}; a quest whose objectives the game hid has none.
    replies.append({"quests": {"completedGaps": {}, "log": [
        {"id": 65, "title": "The Defias Brotherhood", "ready": True, "level": 14, "header": "Westfall",
         "objectives": {}},
        {"id": 19, "title": "Tharil'zun", "ready": False, "level": 26, "tag": "elite"},
    ]}})
    result = asyncio.run(quests.get_quest_log())
    assert result.game == "test game"
    assert [(q.quest_id, q.objectives, q.tag, q.header) for q in result.quests] == [
        (65, [], None, "Westfall"), (19, None, "elite", None)]

    replies.append({"quests": {"completedGaps": {}, "log": {}}})
    assert asyncio.run(quests.get_quest_log()).quests == []

    # An addon loaded before this update (no /reload yet) sends the log without levels or objectives.
    replies.append({"quests": {"completedGaps": [5], "log": [{"id": 20, "title": "Blackrock Menace", "ready": False}]}})
    with pytest.raises(ToolError, match="/reload"):
        asyncio.run(quests.get_quest_log())

    replies.append({"errors": {"quests": "boom"}})
    with pytest.raises(ToolError, match="couldn't collect quests: boom"):
        asyncio.run(quests.get_quest_log())
