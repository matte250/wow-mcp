"""Quest completion and the quest log (Quests.lua, tools/quests.py)."""

import asyncio
import json
import zlib

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


def rewards(xp, copper, money, items=(), choices=(), status="ok"):
    return {"xp": xp, "money_copper": copper, "money": money, "reward_items": list(items),
            "reward_choices": list(choices), "rewards_status": status}


def item(item_id, name, count=1):
    return {"item_id": item_id, "name": name, "count": count}


def test_quest_log(live_game, monkeypatch):
    monkeypatch.setattr(quests, "LOAD_WAIT", 0.2)
    with live_game():
        result = asyncio.run(quests.get_quest_log())
    assert result.game.startswith("World of Warcraft: Forever")
    assert result.note is None
    redridge = {"header": "Redridge Mountains", "failed": False}
    # The People's Militia's rewards and the Belt of Vindication weren't loaded at first: the tool
    # asked again a moment later.
    assert [q.model_dump() for q in result.quests] == [
        {"quest_id": 20, "title": "Blackrock Menace", "level": 22, "tag": None, "ready_to_turn_in": False, **redridge,
         "objectives": [objective("Blackrock Champion slain: 7/15", 7, 15, False)],
         **rewards(1450, 1200, "12s 0c", choices=[item(2232, "Dark Runner Boots"), item(1282, "Sparkmetal Coif")])},
        {"quest_id": 122, "title": "Underbelly Scales", "level": 21, "tag": None, "ready_to_turn_in": True, **redridge,
         "objectives": [objective("Underbelly Whelp Scale: 6/6", 6, 6, True)], **rewards(1350, 0, "0c")},
        {"quest_id": 19, "title": "Tharil'zun", "level": 26, "tag": "elite", "ready_to_turn_in": False, **redridge,
         "objectives": [objective("Tharil'zun's Head: 0/1", 0, 1, False)],
         **rewards(2100, 2500, "25s 0c", choices=[item(3562, "Belt of Vindication"), item(1264, "Headbasher")])},
        # Under a collapsed header, still listed.
        {"quest_id": 12, "title": "The People's Militia", "level": 14, "header": "Westfall", "tag": None,
         "ready_to_turn_in": False, "failed": False,
         "objectives": [objective("Defias Trapper slain: 15/15", 15, 15, True),
                        objective("Defias Smuggler slain: 7/15", 7, 15, False)],
         **rewards(900, 750, "7s 50c", items=[item(929, "Healing Potion", 2)])},
        {"quest_id": 65, "title": "The Defias Brotherhood", "level": 14, "header": "Westfall", "tag": None,
         "ready_to_turn_in": True, "failed": False, "objectives": [], **rewards(450, 350, "3s 50c")},
        {"quest_id": 309, "title": "Protecting the Shipment", "level": 15, "header": "Loch Modan", "tag": None,
         "ready_to_turn_in": False, "failed": True, "objectives": [objective("Protect Miran", 0, 1, False)],
         "xp": None, "money_copper": None, "money": None, "reward_items": None, "reward_choices": None,
         "rewards_status": "hidden"},
    ]


def test_quest_rewards_load_later(run_client):
    """Until the client has loaded a quest's rewards they read as 0: the addon reports them as loading
    (never as no reward) and asks for them, so they're there when the server asks again."""
    run_client(r"""
        local ns = client.LoadAddon()
        local reply
        ns.Stream.Send = function(_, value) reply = value end
        local function Ask()
            ns.Answer(1, ns.Kinds.QUESTS)
            assert(not reply.errors, "the section works")
            local byID = {}
            for _, quest in ipairs(reply.quests.log) do byID[quest.id] = quest end
            return byID
        end

        local quests = Ask()
        local militia = quests[12]
        assert(militia.rewards == "loading" and militia.xp == nil and militia.money == nil and militia.items == nil)
        -- The XP and money came through; one item to choose from hasn't loaded its name yet.
        local tharil = quests[19]
        assert(tharil.rewards == "loading" and tharil.xp == 2100 and tharil.money == 2500)
        assert(tharil.choices[1].id == 3562 and tharil.choices[1].name == nil)
        assert(tharil.choices[2].id == 1264 and tharil.choices[2].name == "Headbasher")
        -- A quest that gives no money or items: known zeros and empty lists.
        local scales = quests[122]
        assert(scales.rewards == nil and scales.xp == 1350 and scales.money == 0)
        assert(#scales.items == 0 and #scales.choices == 0)
        -- The quest log shows no rewards for this one.
        assert(quests[309].rewards == "hidden" and quests[309].xp == nil)

        client.Wait(1)
        quests = Ask()
        militia = quests[12]
        assert(militia.rewards == nil and militia.xp == 900 and militia.money == 750)
        assert(#militia.items == 1 and militia.items[1].id == 929 and militia.items[1].count == 2
            and militia.items[1].name == "Healing Potion")
        assert(quests[19].rewards == nil and quests[19].choices[1].name == "Belt of Vindication")
        assert(quests[20].choices[1].count == nil, "a count of one is left out")
    """)


def test_quest_rewards_leave_the_selection_alone(run_client):
    """The reward functions read the quest selected in the log unless they're given a questID. The addon
    passes one, so every quest gets its own rewards and the player's selection stays as it was."""
    run_client(r"""
        local ns = client.LoadAddon()
        C_QuestLog.SetSelectedQuest(19)
        local reply
        ns.Stream.Send = function(_, value) reply = value end
        ns.Answer(1, ns.Kinds.QUESTS)
        local byID = {}
        for _, quest in ipairs(reply.quests.log) do byID[quest.id] = quest end
        assert(byID[20].xp == 1450 and byID[122].xp == 1350 and byID[19].xp == 2100)
        assert(byID[20].choices[1].id == 2232)
        assert(C_QuestLog.GetSelectedQuest() == 19)
    """)


def test_quest_rewards_survive_errors(run_client):
    run_client(r"""
        local ns = client.LoadAddon()
        -- The game won't give one quest's XP, nor another's items to choose from (e.g. a secret value).
        local xp, choice = GetQuestLogRewardXP, GetQuestLogChoiceInfo
        GetQuestLogRewardXP = function(questID)
            if questID == 20 then error("secret") end
            return xp(questID)
        end
        GetQuestLogChoiceInfo = function(index, questID)
            if questID == 19 then error("secret") end
            return choice(index, questID)
        end
        -- Nor does this client have HaveQuestRewardData: rewards that come through still count.
        HaveQuestRewardData = nil
        local reply
        ns.Stream.Send = function(_, value) reply = value end
        ns.Answer(1, ns.Kinds.QUESTS)
        assert(not reply.errors, "the section still works")
        assert(#reply.quests.log == 6)
        local byID = {}
        for _, quest in ipairs(reply.quests.log) do byID[quest.id] = quest end
        assert(byID[20].rewards == "unknown" and byID[20].xp == nil and byID[20].objectives[1].fulfilled == 7)
        assert(byID[19].rewards == "unknown" and byID[19].xp == 2100 and byID[19].choices == nil)
        assert(byID[122].rewards == nil and byID[122].xp == 1350)
        assert(byID[12].rewards == "loading" and byID[12].xp == nil, "all zeros: still not loaded")
    """)


def test_full_quest_log_stays_compact(run_client, tmp_path):
    """A full log (25 quests, each with objectives, an item and four to choose from) should still
    stream in about a second."""
    out = tmp_path / "reply.json"
    run_client(r"""
        local words = { "Defias", "Kobold", "Gnoll", "Murloc", "Raptor", "Spider", "Ogre", "Bandit", "Wolf", "Bear",
            "Crate", "Letter", "Tusk", "Pelt", "Scale", "Head", "Ring", "Boots", "Gloves", "Cloak", "Belt", "Staff",
            "Shield", "Hammer", "Blade", "Report", "Supplies", "Shipment", "Menace", "Threat", "Elder", "Captain" }
        local zones = { "Elwynn Forest", "Westfall", "Redridge Mountains", "Duskwood", "Wetlands" }
        local function Word(n) return words[n % #words + 1] end
        wipe(client.questLog); wipe(client.questObjectives); wipe(client.questRewards)
        for zone = 1, 5 do
            table.insert(client.questLog, { title = zones[zone], isHeader = true, questID = 0, level = 0, difficultyLevel = 0 })
            for q = 1, 5 do
                local n = zone * 5 + q
                local questID = 1000 + n * 37
                table.insert(client.questLog, { title = "The " .. Word(n) .. " " .. Word(n * 3) .. " " .. Word(n * 7),
                    isHeader = false, questID = questID, level = 10 + n, difficultyLevel = 10 + n })
                local objectives = {}
                for o = 1, n % 3 + 1 do
                    objectives[o] = { text = Word(n + o) .. " " .. Word(n * o) .. " slain: " .. o .. "/" .. (o + 9),
                        finished = false, numFulfilled = o, numRequired = o + 9 }
                end
                client.questObjectives[questID] = objectives
                local function Item(k)
                    local itemID = 20000 + n * 11 + k
                    client.items[itemID] = { name = Word(n + k) .. " " .. Word(n * k + 5) .. " of the " .. Word(n * 13 + k),
                        quality = 2, level = 10 + n, class = 4 }
                    return { id = itemID, count = k == 0 and 5 or nil }
                end
                local choices = {}
                for k = 1, 4 do choices[k] = Item(k) end
                client.questRewards[questID] = { xp = 500 + n * 73, money = n * 517, items = { Item(0) }, choices = choices }
            end
        end

        local ns = client.LoadAddon()
        local reply
        ns.Stream.Send = function(_, value) reply = value end
        ns.Answer(1, ns.Kinds.QUESTS)
        local last = reply.quests.log[25]
        assert(#reply.quests.log == 25 and last.rewards == nil and #last.choices == 4 and last.items[1].count == 5)
        local f = assert(io.open("OUT", "w"))
        f:write(C_EncodingUtil.SerializeJSON(reply))
        f:close()
    """.replace("OUT", str(out)))
    body = out.read_bytes()
    assert len(json.loads(body)["quests"]["log"]) == 25
    assert len(zlib.compress(body, 9)) < 3000, len(zlib.compress(body, 9))


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
    monkeypatch.setattr(quests, "LOAD_WAIT", 0)
    # The real client serializes an empty table as {}; a quest whose objectives the game hid has none.
    replies.append({"quests": {"completedGaps": {}, "log": [
        {"id": 65, "title": "The Defias Brotherhood", "ready": True, "level": 14, "header": "Westfall",
         "objectives": {}, "xp": 450, "money": 12345, "items": {}, "choices": {}},
        {"id": 19, "title": "Tharil'zun", "ready": False, "level": 26, "tag": "elite", "rewards": "unknown"},
    ]}})
    result = asyncio.run(quests.get_quest_log())
    assert result.game == "test game" and result.note is None
    assert [(q.quest_id, q.objectives, q.tag, q.header) for q in result.quests] == [
        (65, [], None, "Westfall"), (19, None, "elite", None)]
    assert [(q.xp, q.money, q.money_copper, q.reward_items, q.reward_choices, q.rewards_status)
            for q in result.quests] == [(450, "1g 23s 45c", 12345, [], [], "ok"),
                                        (None, None, None, None, None, "unknown")]

    replies.append({"quests": {"completedGaps": {}, "log": {}}})
    assert asyncio.run(quests.get_quest_log()).quests == []

    # Rewards still loading: asked again until they're there or the retries run out. Never 0.
    loading = {"id": 12, "title": "The People's Militia", "ready": False, "level": 14, "rewards": "loading"}
    loaded = {"id": 12, "title": "The People's Militia", "ready": False, "level": 14, "xp": 900, "money": 750,
              "items": [{"id": 929, "count": 2}], "choices": {}}
    replies.extend([{"quests": {"log": [loading]}}, {"quests": {"log": [loaded]}}])
    quest = asyncio.run(quests.get_quest_log()).quests[0]
    assert (quest.xp, quest.money, quest.rewards_status) == (900, "7s 50c", "ok") and not replies
    assert quest.reward_items == [quests.RewardItem(item_id=929, name=None, count=2)]
    replies.extend([{"quests": {"log": [loading]}}] * (quests.LOAD_RETRIES + 1))
    quest = asyncio.run(quests.get_quest_log()).quests[0]
    assert (quest.xp, quest.money_copper, quest.rewards_status) == (None, None, "loading") and not replies

    # An addon loaded before rewards were added (no /reload yet): the log, and a note.
    replies.append({"quests": {"log": [{"id": 20, "title": "Blackrock Menace", "ready": False, "level": 22,
                                        "objectives": {}}]}})
    result = asyncio.run(quests.get_quest_log())
    assert result.quests[0].rewards_status == "unknown" and result.quests[0].xp is None
    assert "/reload" in result.note

    # An addon loaded before objectives were added sends the log without levels or objectives.
    replies.append({"quests": {"completedGaps": [5], "log": [{"id": 20, "title": "Blackrock Menace", "ready": False}]}})
    with pytest.raises(ToolError, match="/reload"):
        asyncio.run(quests.get_quest_log())

    replies.append({"errors": {"quests": "boom"}})
    with pytest.raises(ToolError, match="couldn't collect quests: boom"):
        asyncio.run(quests.get_quest_log())
