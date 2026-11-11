"""Plans: a checklist window in game that ticks itself off (Plans.lua, the guide APIs in Routes.lua and
Areas.lua, tools/plans.py).

Stub geometry (stub/apis/map.lua): Durotar spans world x 500-700, y 300-600, so 1% of its map is 2 yards
across and 3 yards down; Orgrimmar spans x 520-600, y 300-380. The player starts in Durotar at 50, 50.
Stub game state: the bank holds 10 Light Leather (2318); the bags 7 Linen Cloth (2589); the character
is level 12 with Leatherworking 60 and Cooking 40, and has turned in quest 783.
"""

import asyncio
import json
import sys
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from wow_mcp import game as wow_game
from wow_mcp.tools import items, plans
from wow_mcp.transport import Transport

# How a Lua test script hands control back to publish through the real tools.
PUBLISH_SCRIPT = """
import asyncio, json, sys
from wow_mcp import game
from wow_mcp.tools import plans, routes
game.running_folders = lambda proc=None: set()  # the stub game waits for this script: it can't check item IDs
args = json.loads(sys.argv[1])
if "plan" in args:
    asyncio.run(plans.show_plan(args["plan"]["title"], [plans.Step(**s) for s in args["plan"]["steps"]],
                                tab=args["plan"].get("tab"), kind=args["plan"].get("kind")))
elif "clear_plan" in args:
    plans.clear_plan(args["clear_plan"] if isinstance(args["clear_plan"], str) else None)
elif "route" in args:
    routes.show_route([routes.Stop(**s) for s in args["route"]["stops"]], title=args["route"].get("title"),
                      open_map=False)
"""


def place(zone, x, y, label, **more):
    return {"zone": zone, "x": x, "y": y, "label": label, **more}


def step(text, *conditions, **more):
    return {"text": text, "conditions": list(conditions), **more}


def lua_json(value):
    """JSON for client.Publish, which passes it through the shell in single quotes."""
    return json.dumps(value).replace("'", "\\u0027")


def plan(title, *steps, **more):
    return lua_json({"plan": {"title": title, "steps": list(steps), **more}})


SILK_SPOTS = [place("Durotar", 60, 50, "Silk spot 1"), place("Durotar", 60, 40, "Silk spot 2"),
              place("Durotar", 50, 40, "Silk spot 3")]
GLOVES = plan(
    "Toughened Leather Gloves",
    step("Take 10 Light Leather out of the bank", {"have": 2318, "count": 10},
         places=[place("Orgrimmar", 50, 50, "Bank")]),
    step("Buy 2 Coarse Thread and 1 Salt from Duokna", {"have": 2320, "count": 2}, {"have": 4289},
         places=[place("Durotar", 52.3, 41.2, "Duokna")]),
    step("Farm 2 Spider's Silk", {"have": 3182, "count": 2, "name": "Spider's Silk"}, places=SILK_SPOTS, loop=True,
         areas=[{"zone": "Durotar", "x": 55, "y": 45, "radius": 6, "label": "Spiders"}]),
    step("Go to Orgrimmar", {"zone": "Orgrimmar"}),
    step("Learn Cured Light Hide from the trainer", {"recipe": 3816},
         places=[place("Orgrimmar", 20, 80, "Leatherworking trainer")]),
    step("Craft 2 Cured Light Hide", {"have": 4231, "count": 2, "name": "Cured Light Hide"}),
    step("Craft Toughened Leather Gloves", {"have": 4253, "name": "Toughened Leather Gloves"}),
)
TURN_INS = lua_json({"route": {"title": "Turn-ins", "stops": [
    place("Durotar", 60, 50, "Gornek"), place("Durotar", 60, 40, "Kaltunk"), place("Orgrimmar", 50, 50, "Thrall")]}})

PRELUDE = (
    f"local GLOVES = [[{GLOVES}]]\nlocal TURN_INS = [[{TURN_INS}]]\n"
    + r"""
    local TICK = "|TInterface\\Buttons\\UI-CheckBox-Check:0|t"
    local STOP, LINE, AREA = "WowMCPRouteStopPinTemplate", "WowMCPRouteLinePinTemplate", "WowMCPAreaPinTemplate"
    local function chat() return table.concat(client.chat, "\n") end
    local function last() return client.chat[#client.chat] end
    local function has(text) return chat():find(text, 1, true) ~= nil end
    -- Text without colour codes and item icons.
    local function plain(text)
        return (tostring(text or ""):gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|r", ""):gsub("|T%d+:0|t ", ""))
    end
    local function moveTo(map, x, y)
        client.player.map, client.player.x, client.player.y = map, x, y
        client.Tick()
    end
    -- The plan window's rows on show, and their texts.
    local function rows()
        local list = {}
        for _, row in ipairs(WowMCPPlanFrame.Rows) do
            if row:IsShown() then table.insert(list, row) end
        end
        return list
    end
    local function row(n)
        for _, r in ipairs(rows()) do if r.n == n then return r end end
    end
    local function progress(n) return plain(row(n).Progress.text) end
    local function nav() return WowMCPRouteNavigator end
    -- /reload: Lua state, frames and loaded slots reset; saved variables don't.
    local function Reload()
        for _, t in ipairs({ client.loaded, client.tickers, client.timers, client.frames, client.chat, client.sounds,
                             client.fontStrings, client.opened }) do
            wipe(t)
        end
        WorldMapFrame.providers = {}
        wipe(WorldMapFrame.pins)
        WowMCPRouteNavigator, WowMCPPlanFrame = nil, nil
        return client.LoadAddon()
    end
    local function noErrors() assert(not has("Plan error"), chat()) end
"""
)


@pytest.fixture
def game(addons, run_client, tmp_path, monkeypatch):
    script = tmp_path / "publish_plans.py"
    script.write_text(PUBLISH_SCRIPT)
    monkeypatch.setenv("WOWMCP_PUBLISH", f"{sys.executable} {script}")
    return lambda lua: run_client(PRELUDE + lua)


def test_crafting_plan_from_start_to_finish(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish(GLOVES)
        client.Wait()

        -- The window: title, current/total, every step, the current one highlighted with its progress.
        local f = WowMCPPlanFrame
        assert(f and f:IsShown() and f.Title.text == "Toughened Leather Gloves" and f.Count.text == "1/7")
        assert(#rows() == 7 and plain(rows()[1].Text.text) == "1. Take 10 Light Leather out of the bank")
        assert(row(1).Current:IsShown() and not row(2).Current:IsShown() and not row(1).Check:IsShown())
        assert(progress(1) == "Light Leather 0/10", progress(1))
        assert(not row(2).Progress:IsShown(), "progress only under the current step")
        assert(has("New plan: |cffffffffToughened Leather Gloves|r (7 steps). /mcp plan lists them."))

        -- The navigator takes the player to the bank.
        assert(nav():IsShown() and nav().Title.text == "Step 1/7" and nav().Stop.text == "Bank")
        assert(client.waypoint.uiMapID == 1454 and #client.Pins(STOP) == 1)

        -- Out of order: thread and salt bought before the bank step is done tick step 2 off early.
        client.AddItem(2320, 2)
        client.Wait(2)
        assert(not row(2).Check:IsShown(), "salt is still missing")
        client.AddItem(4289, 1)
        client.Wait(2)
        assert(row(2).Check:IsShown() and f.Count.text == "1/7")
        assert(last() == "|cff33ff99WowMCP|r " .. TICK .. " Step 2/7 done: Buy 2 Coarse Thread and 1 Salt from Duokna", last())
        assert(client.sounds[#client.sounds] == 878)

        -- Taking the leather out of the bank finishes step 1; step 2 is done, so on to the silk.
        client.bags[6].slots[3] = nil
        client.AddItem(2318, 10)
        client.Wait(2)
        assert(has(TICK .. " Step 1/7 done: Take 10 Light Leather out of the bank |cffaaaaaaNext: 3. Farm 2 Spider's Silk|r"), chat())
        assert(f.Count.text == "3/7" and progress(3) == "Spider's Silk 0/2", progress(3))

        -- The farming loop: three spots, round and round, with the spiders' area shaded.
        assert(nav().Title.text == "Step 3/7  1/3" and nav().Stop.text == "Silk spot 1")
        assert(#client.Pins(STOP) == 3 and #client.Pins(LINE) == 3 and #client.Pins(AREA) == 1)
        moveTo(1411, 0.6, 0.5)
        moveTo(1411, 0.6, 0.4)
        moveTo(1411, 0.5, 0.4)
        assert(nav().Title.text == "Step 3/7  1/3", "back to the first spot")
        client.AddItem(3182, 1)
        client.Wait(2)
        assert(progress(3) == "Spider's Silk 1/2", progress(3))
        moveTo(1411, 0.6, 0.5)
        assert(nav().Title.text == "Step 3/7  2/3")
        client.AddItem(3182, 1)
        client.Wait(2)

        -- Enough silk: the area goes, and step 4 has no places, so the navigator and waypoint go too.
        assert(f.Count.text == "4/7" and #client.Pins(AREA) == 0 and not nav():IsShown() and client.waypoint == nil)
        assert(progress(4) == "Go to Orgrimmar")
        client.Enter(1454, 0.5, 0.5, "Orgrimmar", "Valley of Strength")
        client.Wait(2)
        assert(f.Count.text == "5/7" and nav():IsShown() and nav().Stop.text == "Leatherworking trainer")
        assert(progress(5) == "Learn: Cured Light Hide", progress(5))
        client.LearnRecipe(3816)
        client.Wait(2)
        assert(f.Count.text == "6/7" and progress(6) == "Cured Light Hide 0/2", progress(6))

        -- Crafting uses up the silk; the farming step stays done.
        client.AddItem(4231, 2)
        client.Wait(2)
        client.RemoveItem(3182, 2)
        client.RemoveItem(4231, 2)
        client.AddItem(4253, 1)
        client.Wait(2)
        assert(row(3).Check:IsShown() and row(6).Check:IsShown() and row(7).Check:IsShown())
        assert(f.Count.text == "|cff33ff33Done|r" and last() == "|cff33ff99WowMCP|r |cff33ff33Plan complete:|r Toughened Leather Gloves", last())
        assert(client.sounds[#client.sounds] == 8960)
        assert(not nav():IsShown() and client.waypoint == nil and #client.Pins(STOP) == 0 and #client.Pins(AREA) == 0)
        noErrors()
    """)


KOBOLDS = plan(
    "Kobold Camp Cleanup",
    step("Accept Kobold Camp Cleanup from Marshal McBride", {"quest_accepted": 900}),
    step("Kill 10 Kobold Vermin", {"quest_objective": 900, "index": 1}),
    step("Kill 3 Kobold Workers too", {"quest_complete": 900}),
    step("Turn it in", {"quest_turned_in": 900, "name": "Kobold Camp Cleanup"}),
    step("Turn in A Threat Within", {"quest_turned_in": 783}),
)


def test_quest_steps(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish([[""" + KOBOLDS + r"""]])
        client.Wait()
        local f = WowMCPPlanFrame
        -- Quest 783 was turned in long ago: that step ticks off at once, quietly.
        assert(row(5).Check:IsShown() and has("Already done: step 5.") and not has("Step 5/5 done"), chat())
        assert(progress(1) == "Accept: quest 900", progress(1))

        client.AcceptQuest(900, "Kobold Camp Cleanup", { { text = "Kobold Vermin slain", required = 10 },
            { text = "Kobold Worker slain", required = 3 } })
        client.Wait(2)
        assert(f.Count.text == "2/5" and progress(2) == "Kobold Vermin slain: 0/10", progress(2))
        client.Progress(900, 1, 4)
        client.Wait(2)
        assert(progress(2) == "Kobold Vermin slain: 4/10")
        client.Progress(900, 1, 10)
        client.Wait(2)
        assert(f.Count.text == "3/5")
        assert(progress(3) == "Complete: Kobold Camp Cleanup\n  Kobold Vermin slain: 10/10\n  Kobold Worker slain: 0/3", progress(3))
        client.Progress(900, 2, 3)
        client.Wait(2)
        assert(f.Count.text == "4/5" and progress(4) == "Turn in: Kobold Camp Cleanup", progress(4))

        -- The turn-in counts at once, before the game flags the quest completed.
        client.TurnInQuest(900)
        client.Tick()
        assert(not C_QuestLog.IsQuestFlaggedCompleted(900))
        assert(f.Count.text == "|cff33ff33Done|r" and has("Plan complete:|r Kobold Camp Cleanup"), chat())
        noErrors()
    """)


SKILLS = plan(
    "Leatherworking 75",
    step("Reach level 13", {"level": 13}),
    step("Leatherworking to 75", {"skill": "Leatherworking", "rank": 75}),
    step("Cooking to 50 on the side", {"skill": "cooking", "rank": 50}),
    step("Learn Light Armor Kit", {"recipe": "Light Armor Kit"}),
    step("Learn Cured Light Hide", {"recipe": 3816}),
    step("Know Light Leather", {"recipe": 2881}),
)


def test_level_skill_and_recipe_steps(game):
    game(r"""
        client.knownSpells[2881] = true
        client.LoadAddon()
        client.Wait()
        client.Publish([[""" + SKILLS + r"""]])
        client.Wait()
        local f = WowMCPPlanFrame
        assert(row(6).Check:IsShown() and has("Already done: step 6."), chat())
        assert(progress(1) == "Level 12/13", progress(1))

        -- On PLAYER_LEVEL_UP the game still says the old level; the event's level counts.
        client.LevelUp(13)
        client.Tick()
        assert(UnitLevel("player") == 12 and f.Count.text == "2/6", f.Count.text)
        assert(progress(2) == "Leatherworking 60/75", progress(2))

        -- Cooking (under a collapsed header in the Skills window) gets there first, out of order.
        client.SetSkill("Cooking", 50)
        client.Wait(2)
        assert(row(3).Check:IsShown() and f.Count.text == "2/6")
        assert(last():find("Step 3/6 done: Cooking to 50 on the side$"), last())
        client.SetSkill("Leatherworking", 75)
        client.Wait(2)
        assert(f.Count.text == "4/6" and progress(4) == "Learn: Light Armor Kit", progress(4))

        -- A recipe by name is known once the profession window has shown it (Knowledge.lua caches it).
        client.OpenProfession("Leatherworking")
        client.Wait(4)
        client.CloseProfession()
        assert(f.Count.text == "5/6" and progress(5) == "Learn: Cured Light Hide", progress(5))
        client.LearnRecipe(3816)
        client.Wait(2)
        assert(has("Plan complete:|r Leatherworking 75"))
        noErrors()
    """)


LEARNING = plan(
    "Dark Leather Belt",
    step("Leatherworking to 60", {"skill": "Leatherworking", "rank": 60}),
    step("Learn Cured Medium Hide and Dark Leather Belt", {"recipe": "Cured Medium Hide"},
         {"recipe": "dark leather belt"}),
    step("Learn Serpent Sting", {"recipe": "Serpent Sting"}),
)


@pytest.mark.parametrize("signal", ["event", "skillLine", "chat", "link"])
def test_recipes_by_name_count_once_learned(game, signal):
    # Any one of the signals of a learn is enough, without opening the profession window.
    game(f'local SIGNAL = "{signal}"\n' + r"""
        client.LoadAddon()
        client.Wait()
        client.Publish([[""" + LEARNING + r"""]])
        client.Wait()
        local f = WowMCPPlanFrame
        assert(f.Count.text == "2/3" and progress(2) == "Learn: Cured Medium Hide\nLearn: dark leather belt", progress(2))
        client.LearnRecipe(3817, SIGNAL)
        client.Tick()
        assert(progress(2) == "Known: Cured Medium Hide\nLearn: dark leather belt", progress(2))
        client.LearnRecipe(3766, SIGNAL)
        client.Tick()
        assert(row(2).Check:IsShown() and f.Count.text == "3/3", f.Count.text)

        -- A class spell counts for a recipe condition too (there's no NEW_RECIPE_LEARNED for it).
        client.LearnSpell(1978, SIGNAL == "event" and "skillLine" or SIGNAL)
        client.Tick()
        assert(has("Plan complete:|r Dark Leather Belt") and client.tradeSkill == nil, chat())
        noErrors()
    """)


LEARNED = plan(
    "Leatherworking recipes",
    step("Learn Cured Medium Hide", {"recipe": "Cured Medium Hide"}),
    step("Learn Dark Leather Belt", {"recipe": 3766}),
    step("Learn Cured Light Hide", {"recipe": "Cured Light Hide"}),
)


def test_learned_recipes_count_after_a_reload(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        -- One learn said only its name (the chat line), the other only its ID (the event).
        client.LearnRecipe(3817, "chat")
        client.LearnRecipe(3766, "event")
        client.Wait()
        Reload()
        client.Wait()
        wipe(client.knownSpells) -- as if the spellbook didn't know recipes: what was saved has to count
        client.Publish([[""" + LEARNED + r"""]])
        client.Wait()
        assert(has("Already done: steps 1, 2.") and progress(3) == "Learn: Cured Light Hide", chat())

        -- The profession window lists them now, instead of the saved learns.
        client.OpenProfession("Leatherworking")
        client.Wait(4)
        client.CloseProfession()
        assert(#WowMCPDB.knowledge.learned["Thorgrim-Firemaw"] == 0)
        wipe(client.chat)
        client.Publish([[""" + LEARNED + r"""]])
        client.Wait()
        assert(has("Already done: steps 1, 2."), chat())
        noErrors()
    """)


BELT = plan(
    "Dark Leather Belt",
    step("Craft a Dark Leather Belt", {"have": 4237, "name": "Dark Leather Belt"}),
    step("Keep a spare in the bank", {"have": 4237, "count": 2, "bank": True, "name": "Dark Leather Belt"}),
)


def test_have_with_another_items_id_counts_the_named_item(game):
    # The AI's ID is the Handstitched Leather Belt's.
    game(r"""
        local ns = client.LoadAddon()
        client.Wait()
        client.Publish([[""" + BELT + r"""]])
        client.Wait()
        local f = WowMCPPlanFrame
        local WRONG = " (item 4237 is Handstitched Leather Belt in this game; counted by name)"
        assert(progress(1) == "Dark Leather Belt 0/1" .. WRONG, progress(1))
        -- What the ID is doesn't count; the named item does.
        client.AddItem(4237, 1)
        client.Wait(2)
        assert(f.Count.text == "1/2", f.Count.text)
        client.AddItem(4249, 1)
        client.Wait(2)
        assert(row(1).Check:IsShown() and progress(2) == "Dark Leather Belt 1/2 (with bank)" .. WRONG, progress(2))

        -- The step's tooltip and get_plan_progress say so too.
        local lines = {}
        GameTooltip.AddLine = function(_, text) table.insert(lines, text) end
        row(2).OnEnter(row(2))
        assert(table.concat(lines, "\n"):find("Dark Leather Belt 1/2 (with bank)" .. WRONG, 1, true), table.concat(lines, "\n"))
        local reply
        ns.Stream.Send = function(_, value) reply = value end
        ns.Answer(0, ns.Kinds.STATE)
        assert(reply.plan.steps[2].progress[1].text == "Dark Leather Belt 1/2 (with bank)" .. WRONG)

        -- In the bank too.
        client.AddItem(4249, 1, 6)
        client.Wait(2)
        assert(has("Plan complete:|r Dark Leather Belt"), chat())
        noErrors()
    """)


POSITIONS = plan(
    "Errands",
    step("Talk to Gornek"),
    step("Walk to the well", {"at": {"zone": "Durotar", "x": 60, "y": 50}, "yards": 12},
         places=[place("Durotar", 60, 50, "Well")]),
    step("Go to Orgrimmar", {"zone": "Orgrimmar"}),
    step("Go to the Valley of Strength", {"subzone": "Valley of Strength"}),
)


def test_position_conditions_only_count_for_the_current_step(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        moveTo(1411, 0.6, 0.5) -- at the well already
        client.Publish([[""" + POSITIONS + r"""]])
        client.Wait(3)
        local f = WowMCPPlanFrame
        assert(f.Count.text == "1/4" and not row(2).Check:IsShown(), "the well only counts once it's the current step")
        assert(progress(1) == "Tick the box when done", progress(1))
        assert(not nav() or not nav():IsShown(), "step 1 has no places")

        -- Ticking step 1 off: step 2 is done right away (the player is at the well), then step 3 waits.
        local box = row(1).Box
        box.OnClick(box)
        client.Wait(2)
        assert(row(1).Check:IsShown() and row(2).Check:IsShown() and f.Count.text == "3/4", f.Count.text)
        assert(has(TICK .. " Step 1/4 done: Talk to Gornek |cffaaaaaaNext: 2. Walk to the well|r"), chat())
        assert(has(TICK .. " Step 2/4 done: Walk to the well |cffaaaaaaNext: 3. Go to Orgrimmar|r"), chat())

        -- Entering Orgrimmar in the Valley of Strength finishes step 3, then step 4 as it becomes current.
        client.Enter(1454, 0.5, 0.5, "Orgrimmar", "Valley of Strength")
        client.Wait(2)
        assert(has("Plan complete:|r Errands") and not nav():IsShown() and client.waypoint == nil)
        noErrors()
    """)


def test_farming_loop_until_the_count_is_reached(game):
    farm = plan(
        "Silk",
        step("Farm 3 Spider's Silk between the two camps", {"have": 3182, "count": 3},
             places=SILK_SPOTS[:2], loop=True,
             areas=[{"zone": "Durotar", "x": 60, "y": 45, "radius": 8, "label": "Spiders", "color": "purple"}]),
        step("Bring it to Gornek", places=[place("Durotar", 40, 40, "Gornek")]),
    )
    game(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish([[""" + farm + r"""]])
        client.Wait()
        -- Two spots make a loop (a route needs three): lines both ways, round and round.
        assert(nav().Title.text == "Step 1/2  1/2" and nav().Stop.text == "Silk spot 1")
        assert(#client.Pins(STOP) == 2 and #client.Pins(LINE) == 2)
        local area = client.Pins(AREA)[1]
        assert(area.Label.text == "Spiders" and math.abs(area.Fill.color[1] - 0.8) < 0.01, "the step's area, in purple")
        for lap = 1, 3 do
            moveTo(1411, 0.6, 0.5)
            assert(nav().Title.text == "Step 1/2  2/2", lap)
            client.AddItem(3182, 1)
            moveTo(1411, 0.6, 0.4)
            client.Tick()
            if lap < 3 then
                assert(nav().Title.text == "Step 1/2  1/2" and progress(1) == "Spider's Silk " .. lap .. "/3", progress(1))
            end
        end
        -- The third silk: on to Gornek.
        assert(WowMCPPlanFrame.Count.text == "2/2" and nav().Title.text == "Step 2/2" and nav().Stop.text == "Gornek")
        assert(#client.Pins(STOP) == 1 and #client.Pins(LINE) == 0 and #client.Pins(AREA) == 0)
        assert(client.waypoint.uiMapID == 1411 and math.abs(client.waypoint.position.x - 0.4) < 1e-6)
        noErrors()
    """)


CHORES = plan(
    "Chores",
    step("Say hello to Gornek"),
    step("Pet the cat", {"manual": True}),
    step("Get 1 Light Leather", {"have": 2318}),
    step("Keep 5 Linen Cloth", {"have": 2589, "count": 5}),
)


def test_clicking_and_manual_steps(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish([[""" + CHORES + r"""]])
        client.Wait()
        local f = WowMCPPlanFrame
        assert(row(4).Check:IsShown() and has("Already done: step 4."), chat())
        assert(progress(1) == "Tick the box when done")
        for _, r in ipairs(rows()) do r.OnEnter(r); r.OnLeave(r) end -- tooltips

        -- Clicking a step makes it the current one.
        local r3 = row(3)
        r3.OnMouseUp(r3, "LeftButton")
        assert(f.Count.text == "3/4" and row(3).Current:IsShown() and progress(3) == "Light Leather 0/1", progress(3))

        -- Right-click ticks a step off by hand; the box does too.
        local r1 = row(1)
        r1.OnMouseUp(r1, "RightButton")
        assert(row(1).Check:IsShown() and last() == "|cff33ff99WowMCP|r " .. TICK .. " Step 1/4 done: Say hello to Gornek", last())
        assert(WowMCPDB.plan.done[1] == "click" and f.Count.text == "3/4")
        r1.OnMouseUp(r1, "LeftButton")
        assert(f.Count.text == "3/4", "clicking a done step doesn't undo it")
        local box = row(2).Box
        box.OnClick(box)
        assert(row(2).Check:IsShown())

        -- Un-ticking a step the game ticked: it becomes current and stays un-ticked while its items are
        -- still there; once they've gone and come back it ticks itself off again.
        local r4 = row(4)
        r4.OnMouseUp(r4, "RightButton")
        client.Wait(3)
        assert(not row(4).Check:IsShown() and f.Count.text == "4/4" and has("Step 4/4 marked not done: Keep 5 Linen Cloth"))
        assert(progress(4) == "Linen Cloth 5/5\nUn-ticked by hand: tick the box when done", progress(4))
        client.RemoveItem(2589, 7)
        client.Wait(2)
        assert(progress(4) == "Linen Cloth 0/5", progress(4))
        client.AddItem(2589, 5)
        client.Wait(2)
        assert(row(4).Check:IsShown() and f.Count.text == "3/4", "back to the first step left")
        assert(has("Step 4/4 done: Keep 5 Linen Cloth |cffaaaaaaNext: 3. Get 1 Light Leather|r"), chat())
        client.AddItem(2318, 1)
        client.Wait(2)
        assert(has("Plan complete:|r Chores"))
        noErrors()
    """)


ERRANDS = lua_json({"route": {"title": "Errands", "stops": [
    place("Durotar", 40, 40, "Mailbox"), place("Durotar", 45, 40, "Inn")]}})
BANK_RUN = plan(
    "Bank run",
    step("Go to the bank", {"at": {"zone": "Orgrimmar", "x": 50, "y": 50}}, places=[place("Orgrimmar", 50, 50, "Bank")]),
    step("Take out 10 Light Leather", {"have": 2318, "count": 10}),
)


def test_plan_and_routes_share_the_navigator(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish(TURN_INS)
        client.Wait()
        assert(nav().Title.text == "Turn-ins  1/3")

        -- A plan replaces the route: the navigator, waypoint and map follow the plan's step.
        client.Publish([[""" + BANK_RUN + r"""]])
        client.Wait()
        assert(has("Your route is hidden while the plan guides you; /mcp route show brings it back."))
        assert(nav().Title.text == "Step 1/2" and nav().Stop.text == "Bank" and client.waypoint.uiMapID == 1454)
        local pins = client.Pins(STOP)
        assert(#pins == 1 and pins[1].Label.text == "" and #client.Pins(LINE) == 0, "Orgrimmar lies inside Durotar's map")
        SlashCmdList.WOWMCP("route")
        assert(has("|cffffffffStep 1/2|r (1 stops)") and has("1. |Haddon:WowMCP:guide:1|h|cffffd100[Bank]|r|h Orgrimmar 50.0, 50.0"), chat())

        -- A newer route shows over the plan; hiding it brings the plan's directions back, and so on.
        client.Publish([[""" + ERRANDS + r"""]])
        client.Wait()
        assert(nav().Title.text == "Errands  1/2" and client.waypoint.uiMapID == 1411 and #client.Pins(STOP) == 2)
        assert(WowMCPPlanFrame.Count.text == "1/2")
        SlashCmdList.WOWMCP("route stop")
        assert(nav().Title.text == "Step 1/2" and client.waypoint.uiMapID == 1454)
        SlashCmdList.WOWMCP("route show")
        assert(nav().Title.text == "Errands  1/2" and client.waypoint.uiMapID == 1411)
        client.itemRef("addon:WowMCP:guide:1")
        assert(nav().Title.text == "Step 1/2" and client.waypoint.uiMapID == 1454, "a guide link brings it in front")
        client.itemRef("addon:WowMCP:route:2")
        assert(nav().Title.text == "Errands  2/2")

        -- Once the route is done, the plan's directions take over again.
        moveTo(1411, 0.45, 0.4)
        assert(has("Route complete: reached Inn, the last stop."))
        assert(nav():IsShown() and nav().Title.text == "Step 1/2" and client.waypoint.uiMapID == 1454)
        moveTo(1454, 0.5, 0.5)
        client.Wait(2)
        assert(has("Step 1/2 done: Go to the bank") and WowMCPPlanFrame.Count.text == "2/2")
        assert(not nav():IsShown() and client.waypoint == nil, "step 2 has no places")

        -- Without a plan, routes work as before.
        client.Publish('{"clear_plan":true}')
        client.Wait()
        assert(has("Plan cleared.") and not WowMCPPlanFrame:IsShown())
        client.Publish(TURN_INS)
        client.Wait()
        assert(nav():IsShown() and nav().Title.text == "Turn-ins  1/3" and #client.Pins(STOP) == 3)
        noErrors()
    """)


FARM = plan(
    "Silk",
    step("Keep 5 Linen Cloth", {"have": 2589, "count": 5}),
    step("Farm 2 Spider's Silk", {"have": 3182, "count": 2}, places=SILK_SPOTS, loop=True,
         areas=[{"zone": "Durotar", "x": 55, "y": 45, "radius": 6, "label": "Spiders"}]),
    step("Hand it in"),
)


def test_reload_restores_the_plan_quietly(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish([[""" + FARM + r"""]])
        client.Wait()
        moveTo(1411, 0.6, 0.5)
        client.AddItem(3182, 1)
        client.Wait(2)
        local f = WowMCPPlanFrame
        assert(f.Count.text == "2/3" and nav().Title.text == "Step 2/3  2/3")
        f.Collapse.OnClick(f.Collapse)
        assert(#rows() == 1 and rows()[1].n == 2 and f.Collapse.normalTexture:find("PlusButton"))
        f.OnDragStart(f)
        f:SetPoint("CENTER", UIParent, "CENTER", 100, 50)
        f.OnDragStop(f)
        local waypoint = client.waypoint

        Reload()
        f = WowMCPPlanFrame
        assert(f and f:IsShown() and #client.chat == 0 and #client.sounds == 0, chat())
        assert(f.Count.text == "2/3" and #rows() == 1 and progress(2) == "Spider's Silk 1/2", progress(2))
        local point = f.points[1]
        assert(point[1] == "CENTER" and point[4] == 100 and point[5] == 50)
        assert(nav():IsShown() and nav().Title.text == "Step 2/3  2/3" and client.waypoint == waypoint)
        assert(#client.Pins(STOP) == 3 and #client.Pins(AREA) == 1)

        -- The step's areas are listed with the others, and hiding them sticks.
        SlashCmdList.WOWMCP("areas")
        assert(has("|cffffffffPlan step 2|r") and has("Spiders|r Durotar 55.0, 45.0"), chat())
        SlashCmdList.WOWMCP("areas clear")
        assert(#client.Pins(AREA) == 0)
        Reload()
        f = WowMCPPlanFrame
        assert(#client.Pins(AREA) == 0 and #client.Pins(STOP) == 3)

        -- Done stays done after the reload; the plan carries on.
        client.RemoveItem(2589, 7)
        client.AddItem(3182, 1)
        client.Wait(2)
        assert(WowMCPDB.plan.done[1] == "auto" and f.Count.text == "3/3")

        -- Hidden and removed plans stay that way.
        SlashCmdList.WOWMCP("plan hide")
        Reload()
        assert(not WowMCPPlanFrame and (not nav() or not nav():IsShown()) and #client.Pins(AREA) == 0)
        SlashCmdList.WOWMCP("plan show")
        assert(WowMCPPlanFrame:IsShown())
        SlashCmdList.WOWMCP("plan clear")
        Reload()
        SlashCmdList.WOWMCP("plan")
        assert(not WowMCPPlanFrame and last():find("No plan. Ask your AI for one!", 1, true))
        noErrors()
    """)


TEN = plan("Ten steps", *[step(f"Step number {n}") for n in range(1, 11)])


def test_plan_commands_and_window(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        local mcp = SlashCmdList.WOWMCP
        mcp("plan")
        assert(last():find("No plan. Ask your AI for one!", 1, true))
        client.Publish([[""" + TEN + r"""]])
        client.Wait()
        local f = WowMCPPlanFrame

        -- Eight steps at a time; the mouse wheel scrolls.
        assert(#rows() == 8 and rows()[1].n == 1 and f.Later:IsShown() and f.Later.text == "2 more steps below")
        assert(not f.Earlier:IsShown())
        f.OnMouseWheel(f, -1)
        assert(rows()[1].n == 2 and f.Earlier.text == "1 step above" and f.Later.text == "1 more step below")
        local r = rows()[1]
        r.OnMouseWheel(r, -5)
        assert(rows()[1].n == 3 and not f.Later:IsShown(), "over a step too")

        mcp("plan")
        assert(has("|cffffffffTen steps|r (step 1/10)") and has("|cff33ff33>|r 1. Step number 1") and has("WowMCP|r 2. Step number 2"))
        mcp("plan next")
        assert(last():find(TICK .. " Step 1/10 done: Step number 1 |cffaaaaaaNext: 2. Step number 2|r", 1, true), last())
        assert(WowMCPDB.plan.done[1] == "click" and f.Count.text == "2/10" and rows()[1].n == 1, "follows the current step again")
        mcp("plan next")
        mcp("plan prev")
        assert(f.Count.text == "2/10" and not WowMCPDB.plan.done[2] and last():find("Step 2/10: Step number 2", 1, true))
        mcp("plan 7")
        assert(f.Count.text == "7/10" and rows()[1].n == 3 and row(7).Current:IsShown())
        mcp("plan 99")
        assert(last():find("No step 99. Type /mcp plan to list them.", 1, true))

        -- Collapsed: just the current step.
        f.Collapse.OnClick(f.Collapse)
        assert(#rows() == 1 and rows()[1].n == 7 and not f.Earlier:IsShown() and not f.Later:IsShown())
        f.Collapse.OnClick(f.Collapse)
        assert(#rows() == 8)

        -- Hide (the close button too) and show.
        mcp("plan hide")
        assert(not f:IsShown() and WowMCPDB.plan.hidden and last():find("Plan hidden;", 1, true))
        mcp("plan show")
        assert(f:IsShown() and f.Count.text == "7/10")
        f.Close.OnClick(f.Close)
        assert(not f:IsShown())
        mcp("plan")
        assert(has("(step 7/10) - hidden, /mcp plan show brings it back"))
        mcp("plan show")

        mcp("plan reset")
        assert(f.Count.text == "1/10" and next(WowMCPDB.plan.done) == nil and has("Plan progress reset."))
        mcp("plan PREV")
        assert(last():find("Already at the first step.", 1, true))
        mcp("plan bogus")
        assert(last():find("/mcp plan [show, hide, next, prev, <n>, reset, clear, tabs, tab <n or name>, name <text>, archive, archived, restore <n or name>, size <width> [steps]]", 1, true))
        mcp("")
        assert(has("WowMCP|r /mcp plan [show, hide, next, prev, <n>, reset, clear, tabs, tab <n or name>, name <text>, archive, archived, restore <n or name>, size <width> [steps]] - the plan from your AI"))
        mcp("plan clear")
        assert(not f:IsShown() and last():find("Plan removed. Ask your AI for a new one!", 1, true))
        mcp("plan next")
        assert(last():find("No plan. Ask your AI for one!", 1, true))
        noErrors()
    """)


def test_odd_data_doesnt_stop_the_plan(game):
    game(r"""
        local ns = client.LoadAddon()
        client.Wait()
        -- The game won't give one item's count (e.g. a secret value): that condition says so, the
        -- rest of the plan carries on, and the error is shown once.
        local count = C_Item.GetItemCount
        C_Item.GetItemCount = function(item, ...)
            if item == 3182 then error("secret value") end
            return count(item, ...)
        end
        -- Straight from a payload, past the server's checks.
        WowMCP_Receive({ seq = 1000, plan = { id = 1000, title = "Odd | plan", steps = {
            { text = "Farm silk", conditions = { { have = 3182, count = 1 } } },
            { text = "Level up", conditions = { { level = 13 } } },
            "not a step",
            { text = "Odd conditions", conditions = { "junk", { bogus = 1 }, { have = "abc" }, { at = 5 }, { zone = "Atlantis" } },
              places = { { zone = "Atlantis", x = 1, y = 1 } } },
        } } })
        client.Wait(4)
        local f = WowMCPPlanFrame
        assert(f.Title.text == "Odd || plan" and #rows() == 3 and progress(1) == "Couldn't check this here", progress(1))
        assert(has("Couldn't find these zones on the map:|r ?, Atlantis"), chat())
        client.LevelUp(13)
        client.Wait(4)
        assert(row(2).Check:IsShown())
        local errors = 0
        for _, line in ipairs(client.chat) do
            if line:find("Plan error:|r", 1, true) then errors = errors + 1 end
        end
        assert(errors == 1 and has("secret value"), chat())

        -- The step with conditions the addon can't use never ticks itself off.
        ns.Plans.GoTo(3)
        client.Wait(2)
        assert(f.Count.text == "3/3" and not row(3).Check:IsShown())
        assert(progress(3):find("A condition this addon doesn't know", 1, true), progress(3))
        assert(progress(3):find("Go to Atlantis", 1, true), progress(3))
    """)


def test_show_plan_payload(addons):
    stop = plans.Stop(zone="Orgrimmar", x=50.123, y=50, label="Bank")
    spiders = plans.Area(zone="Durotar", x=55, y=45, radius=6, label="Spiders", color="purple")
    result = run(plans.show_plan("Gloves", [
        plans.Step(text="Take out the leather", places=[stop], conditions=[plans.Have(have=2318, count=10, bank=True)]),
        plans.Step(text="Farm silk", places=[stop, stop], loop=True, areas=[spiders],
                   conditions=[{"have": 3182, "count": 2, "name": "Spider's Silk"}]),
        plans.Step(text="Quests and places", conditions=[
            {"at": {"zone": "Durotar", "x": 60, "y": 50, "map_id": 1411}, "yards": 20}, {"zone": "Orgrimmar"},
            {"subzone": "Valley of Strength"}, {"quest_accepted": 900}, {"quest_objective": 900, "index": 2},
            {"quest_complete": 900}, {"quest_turned_in": 900, "name": "Kobolds"},
        ]),
        plans.Step(text="Grow", conditions=[{"level": 13}, {"skill": "Leatherworking", "rank": 75}, {"recipe": 3816},
                                            {"recipe": "Light Armor Kit"}]),
        plans.Step(text="Say hi"),
        plans.Step(text="Pet the cat", conditions=[{"manual": True}]),
    ]))
    assert result.startswith(
        "Sent plan #1 'Gloves' with 6 steps to the game:\n"
        "1. Take out the leather [done when: 10 x item 2318 (bags + bank)]\n"
        "2. Farm silk [done when: 2 x Spider's Silk]\n"
        "3. Quests and places [done when: within 20 yd of Durotar 60.0, 50.0; in Orgrimmar; in Valley of Strength; "
        "quest 900 accepted; objective 2 of quest 900 done; quest 900 ready to turn in; quest 900 turned in]\n"
        "4. Grow [done when: level 13; Leatherworking 75; recipe 3816 known; recipe Light Armor Kit known]\n"
        "5. Say hi [done when: ticked by the player]\n"
    ), result
    assert "2 step(s) have nothing the game can check, so the player ticks them off." in result
    # The game isn't running, so the silk's ID couldn't be checked.
    assert result.endswith("Its item IDs weren't checked against the game. The game isn't running, so it will show "
                           "when the player next logs in."), result

    state = Transport(addons).load_state()
    bank = {"zone": "Orgrimmar", "x": 50.12, "y": 50, "label": "Bank", "note": None, "mapID": None}
    area = {"zone": "Durotar", "x": 55, "y": 45, "radius": 6, "label": "Spiders", "color": "cc66ff", "mapID": None}
    nothing = {"places": [], "loop": False, "areas": []}
    # One plan, in the tab every plan goes to unless it names another; its steps train a profession.
    assert "It's in the tab 'Plan' (Leatherworking icon)." in result
    assert state["plan"]["id"] == state["seq"] and len(state["plan"]["list"]) == 1
    assert state["plan"]["list"][0] == {"id": state["seq"], "tab": "Plan", "kind": "profession",
                                        "profession": "Leatherworking", "title": "Gloves", "steps": [
        {"text": "Take out the leather", "places": [bank], "loop": False, "areas": [],
         "conditions": [{"have": 2318, "count": 10, "bank": True}]},
        {"text": "Farm silk", "places": [bank, bank], "loop": True, "areas": [area],
         "conditions": [{"have": 3182, "count": 2, "bank": False, "name": "Spider's Silk"}]},
        {"text": "Quests and places", **nothing, "conditions": [
            {"at": {"zone": "Durotar", "x": 60, "y": 50, "mapID": 1411}, "yards": 20}, {"zone": "Orgrimmar"},
            {"subzone": "Valley of Strength"}, {"questAccepted": 900}, {"questObjective": 900, "index": 2},
            {"questComplete": 900}, {"questTurnedIn": 900, "name": "Kobolds"}]},
        {"text": "Grow", **nothing, "conditions": [{"level": 13}, {"skill": "Leatherworking", "rank": 75},
                                                   {"recipe": 3816}, {"recipe": "Light Armor Kit"}]},
        {"text": "Say hi", **nothing, "conditions": []},
        {"text": "Pet the cat", **nothing, "conditions": [{"manual": True}]},
    ]}
    # The game gets it as Lua, with keys it can read.
    inbox = (addons / "WowMCP" / "Inbox.lua").read_text()
    assert "questTurnedIn = 900," in inbox and "mapID = 1411," in inbox

    assert plans.clear_plan() == "Cleared the plan."
    state = Transport(addons).load_state()
    assert state["plan"] == {"id": state["seq"], "list": []}


def test_show_plan_sends_the_npcs_of_a_step(addons):
    run(plans.show_plan("Kobolds", [
        plans.Step(text="Accept it from Marshal McBride", npcs=[plans.Npc(id=197, name="Marshal McBride")]),
        plans.Step(text="Kill kobolds", npcs=[{"id": 6, "name": "Kobold Vermin"}, {"id": 80, "display_id": 373}]),
        plans.Step(text="Hand it in"),
    ]))
    steps = Transport(addons).load_state()["plan"]["list"][0]["steps"]
    # As the addon reads them, without what wasn't given; a step without NPCs has no key at all.
    assert steps[0]["npcs"] == [{"id": 197, "name": "Marshal McBride"}]
    assert steps[1]["npcs"] == [{"id": 6, "name": "Kobold Vermin"}, {"id": 80, "display": 373}]
    assert "npcs" not in steps[2]
    inbox = (addons / "WowMCP" / "Inbox.lua").read_text()
    assert inbox.count("npcs = {") == 2 and "display = 373," in inbox and 'name = "Marshal McBride",' in inbox

    with pytest.raises(ValidationError, match="greater than 0"):
        plans.Step(text="Nobody", npcs=[{"id": 0}])
    with pytest.raises(ValidationError, match="at most 4 items"):
        plans.Step(text="A crowd", npcs=[{"id": n} for n in range(1, 6)])


def test_show_plan_checks_item_ids_in_game(live_game):
    with live_game(seconds=20):
        with pytest.raises(ToolError) as error:
            run(plans.show_plan("Belt", [
                plans.Step(text="Make the belt", conditions=[{"have": 4237, "name": "Dark Leather Belt"}]),
                plans.Step(text="Farm silk", conditions=[{"have": 3182, "count": 2, "name": "spiders silk"}]),
                plans.Step(text="Find the thing", conditions=[{"have": 999999, "name": "Thing"}]),
                plans.Step(text="Bank one", conditions=[{"have": 4237, "bank": True, "name": "Dark Leather Belt"},
                                                       {"have": 2318}]),
            ]))
        sent = plans.transport().load_state()
        result = run(plans.show_plan("Belt", [
            plans.Step(text="Make the belt", conditions=[{"have": 4249, "name": "Dark Leather Belt"}])]))
        state = plans.transport().load_state()
    assert str(error.value) == (
        "Nothing was sent: these 'have' conditions have another item's ID in this game.\n"
        "step 1: item 4237 is Handstitched Leather Belt, not Dark Leather Belt\n"
        "step 3: item 999999 (Thing) isn't in this game\n"
        "step 4: item 4237 is Handstitched Leather Belt, not Dark Leather Belt\n"
        "Fix the IDs and send the plan again: get_known_recipes has the item each known recipe makes, get_inventory "
        "and get_vendor_items have the items the player has or has seen for sale, and get_item_details looks up any ID."
    )
    # One lookup per plan, for the named items only; the corrected plan went out after its own.
    assert "plan" not in sent and sent["itemLookup"]["items"] == [4237, 3182, 999999]
    assert state["itemLookup"] == {"id": 2, "items": [4249]} and state["plan"]["id"] == 3
    assert result.startswith("Sent plan #3 'Belt'") and "checked" not in result, result


def test_show_plan_when_the_game_cant_check_items(addons, monkeypatch):
    belt = [plans.Step(text="Make the belt", conditions=[{"have": 4249, "name": "Dark Leather Belt"}])]

    async def look_up(ids):
        raise AssertionError("asked the game")

    monkeypatch.setattr(plans, "look_up", look_up)
    # Not running: sent as it is.
    result = run(plans.show_plan("Belt", belt))
    assert result.endswith("Its item IDs weren't checked against the game. The game isn't running, so it will show "
                           "when the player next logs in."), result

    # Running, but no named 'have' conditions: nothing to ask.
    monkeypatch.setattr(wow_game, "running_folders", lambda proc=None: {"_forever_"})
    leather = [plans.Step(text="Take it out", conditions=[{"have": 2318, "bank": True}])]
    result = run(plans.show_plan("Leather", leather))
    assert result.endswith("get_plan_progress shows where they are."), result

    # No answer (e.g. a long fight): sent anyway.
    async def look_up(ids):
        raise ToolError("The game didn't answer.")

    monkeypatch.setattr(plans, "look_up", look_up)
    result = run(plans.show_plan("Belt", belt))
    assert result.endswith("Its item IDs weren't checked against the game: The game didn't answer."), result
    assert [p["title"] for p in Transport(addons).load_state()["plan"]["list"]] == ["Belt"]

    # Still loading after the retries.
    async def look_up(ids):
        return items.ItemDetailsList(game="WoW", items=[items._details(4249, {"status": "loading"})])

    monkeypatch.setattr(plans, "look_up", look_up)
    assert run(plans.show_plan("Belt", belt)).endswith("Item 4249 couldn't be checked against the game.")


@pytest.mark.parametrize("condition, error", [
    ({"have": 1, "level": 2}, "A condition needs exactly one of these keys: have, at, zone"),
    ({}, "A condition needs exactly one of these keys"),
    ({"have": 1, "colour": "red"}, "Extra inputs are not permitted"),
    ({"manual": False}, "Input should be True"),
    ({"at": {"zone": "Durotar", "x": 101, "y": 1}}, "less than or equal to 100"),
    ({"quest_objective": 5}, "Field required"),
])
def test_conditions_are_checked(condition, error):
    with pytest.raises(ValidationError, match=error):
        plans.Step(text="Do it", conditions=[condition])


def run(coro):
    return asyncio.run(coro)


def test_plan_tools_live(live_game, tmp_path):
    loot = tmp_path / "loot"
    setup = f"""
        -- The player loots the silk once the test says so.
        C_Timer.NewTicker(0.5, function()
            local f = io.open([[{loot}]])
            if f and not client.looted then
                f:close()
                client.looted = true
                client.AddItem(3182, 2)
            end
        end)
    """
    with live_game(setup, seconds=20):
        result = run(plans.show_plan("Silk", [
            plans.Step(text="Farm 2 Spider's Silk", places=[plans.Stop(zone="Durotar", x=60, y=50, label="Spiders")],
                       conditions=[plans.Have(have=3182, count=2, name="Spider's Silk")]),
            plans.Step(text="Show it to Gornek"),
        ]))
        # Update 1 asked the game about the silk's ID.
        assert result.startswith("Sent plan #2 'Silk' with 2 steps"), result
        assert "checked" not in result
        time.sleep(0.5)
        progress = run(plans.get_plan_progress())
        assert progress.game.startswith("World of Warcraft: Forever")
        assert (progress.tab, progress.renamed_to, progress.kind, progress.note) == ("Plan", None, "general", None)
        assert [t.model_dump() for t in progress.tabs] == [{
            "plan_id": 2, "tab": "Plan", "renamed_to": None, "kind": "general", "title": "Silk", "shown": True,
            "archived": False, "steps_done": 0, "total_steps": 2, "complete": False,
        }]
        assert (progress.active, progress.plan_id, progress.title, progress.current_step, progress.total_steps) == (
            True, 2, "Silk", 1, 2)
        assert progress.steps[0].model_dump() == {
            "step": 1, "text": "Farm 2 Spider's Silk", "done": False, "ticked_by_hand": False, "current": True,
            "needs_click": False, "progress": [{"kind": "have", "progress": "Spider's Silk 0/2", "met": False}],
        }

        loot.write_text("")
        for _ in range(30):
            progress = run(plans.get_plan_progress())
            if progress.steps[0].done:
                break
            time.sleep(0.2)
        assert progress.current_step == 2 and progress.steps_done == 1 and progress.steps[0].progress == []
        assert progress.steps[1].model_dump() == {
            "step": 2, "text": "Show it to Gornek", "done": False, "ticked_by_hand": False, "current": True,
            "needs_click": True, "progress": [],
        }
        assert not progress.complete and progress.note is None

        plans.clear_plan()
        time.sleep(0.5)
        progress = run(plans.get_plan_progress())
        assert not progress.active and progress.steps == []
        assert progress.note == "There's no plan in game. show_plan sends one."


def test_plan_progress_notes(addons, monkeypatch):
    reply = {}

    async def request(kind):
        return reply, "World of Warcraft: Forever 1.60.1"

    monkeypatch.setattr(plans, "request", request)
    run(plans.show_plan("Old", [plans.Step(text="One")]))
    run(plans.show_plan("New", [plans.Step(text="Two")]))

    reply["plan"] = {"none": True}
    assert "hasn't received the plan yet" in run(plans.get_plan_progress()).note
    reply["plan"] = {"none": True, "cleared": True}
    assert "The player removed the plan" in run(plans.get_plan_progress()).note
    reply["plan"] = {"id": 1, "title": "Old", "current": 1, "complete": False, "hidden": True,
                     "problem": "secret value", "steps": [{"done": False, "needsClick": True, "progress": {}}]}
    progress = run(plans.get_plan_progress())
    assert progress.hidden and progress.steps[0].text is None and progress.steps[0].needs_click
    assert "still shows an older plan" in progress.note and "couldn't be checked in game: secret value" in progress.note

    del reply["plan"]
    with pytest.raises(ToolError, match="doesn't send plan yet"):
        run(plans.get_plan_progress())


@pytest.fixture
def answers(addons, monkeypatch):
    """The game's answer to get_plan_progress: a dict a test fills in ('plan' is the plan section)."""
    reply = {}

    async def request(kind):
        return reply, "World of Warcraft: Forever 1.60.1"

    monkeypatch.setattr(plans, "request", request)
    return reply


def sent(addons):
    """The server's plans and the id of the last plan payload."""
    state = Transport(addons).load_state()
    return state["plan"]["list"], state["plan"]["id"]


def test_plan_progress_lists_the_tabs(addons, answers):
    quests = [plans.Step(text="Accept it", conditions=[{"quest_accepted": 900}]), plans.Step(text="Hand it in")]
    run(plans.show_plan("Kobold quests", quests, tab="Quests"))
    run(plans.show_plan("Hunter rotation", [plans.Step(text="Serpent Sting first")], tab="Rotation", kind="class"))
    (first, second), payload = sent(addons)

    # The player named the open tab 'Shots' and archived the other. The tab keeps the name the tools know.
    game_tabs = [
        {"id": first["id"], "tab": "Quests", "kind": "questing", "title": "Kobold quests", "steps": 2, "done": 1,
         "complete": False, "archived": True, "shown": False},
        {"id": second["id"], "tab": "Rotation", "name": "Shots", "kind": "class", "title": "Hunter rotation",
         "steps": 1, "done": 0, "complete": False, "shown": True},
    ]
    answers["plan"] = {"id": second["id"], "tab": "Rotation", "name": "Shots", "kind": "class",
                       "title": "Hunter rotation", "current": 1, "complete": False, "hidden": False,
                       "steps": [{"done": False, "needsClick": True, "progress": {}}], "tabs": game_tabs,
                       "seen": payload}
    progress = run(plans.get_plan_progress())
    assert (progress.active, progress.plan_id, progress.tab, progress.renamed_to, progress.kind) == (
        True, second["id"], "Rotation", "Shots", "class")
    assert progress.steps[0].text == "Serpent Sting first" and progress.note is None
    assert [t.model_dump() for t in progress.tabs] == [
        {"plan_id": first["id"], "tab": "Quests", "renamed_to": None, "kind": "questing", "title": "Kobold quests",
         "shown": False, "archived": True, "steps_done": 1, "total_steps": 2, "complete": False},
        {"plan_id": second["id"], "tab": "Rotation", "renamed_to": "Shots", "kind": "class",
         "title": "Hunter rotation", "shown": True, "archived": False, "steps_done": 0, "total_steps": 1,
         "complete": False},
    ]
    # The name the tools take is the one that addresses the plan, whatever the game calls the tab.
    answers["plan"]["tab"] = answers["plan"]["tabs"][1]["tab"] = "Shots"
    progress = run(plans.get_plan_progress())
    assert progress.tab == "Rotation" and progress.tabs[1].tab == "Rotation"
    assert plans.clear_plan(progress.tab) == "Cleared the plan in the tab 'Rotation'."


def test_plan_progress_when_whats_left_is_archived(addons, answers):
    """No plan open, the game up to date: the player archived what's left. Not 'type /reload'."""
    run(plans.show_plan("Old", [plans.Step(text="One")], tab="Old"))
    (old,), payload = sent(addons)
    archived = {"id": old["id"], "tab": "Old", "kind": "general", "title": "Old", "steps": 1, "done": 0,
                "archived": True, "shown": False}

    answers["plan"] = {"none": True, "archived": True, "tabs": [archived], "seen": payload}
    progress = run(plans.get_plan_progress())
    assert not progress.active and progress.note.startswith("The player archived their plans"), progress.note
    assert [(t.tab, t.archived, t.shown) for t in progress.tabs] == [("Old", True, False)]
    # The tabs say so too.
    answers["plan"] = {"none": True, "tabs": [archived], "seen": payload}
    assert run(plans.get_plan_progress()).note.startswith("The player archived their plans")
    # Unless a plan sent since hasn't arrived: that one would open.
    run(plans.show_plan("New", [plans.Step(text="Two")], tab="New"))
    assert "hasn't received the plan yet" in run(plans.get_plan_progress()).note


def test_plan_progress_says_when_the_game_is_behind(addons, answers):
    run(plans.show_plan("Quests", [plans.Step(text="One")], tab="Quests"))
    (quests,), payload = sent(addons)
    shows = {"id": quests["id"], "tab": "Quests", "kind": "general", "title": "Quests", "current": 1,
             "complete": False, "hidden": False, "steps": [{"done": False, "needsClick": True, "progress": {}}],
             "tabs": [{"id": quests["id"], "tab": "Quests", "kind": "general", "title": "Quests", "steps": 1,
                       "done": 0, "shown": True}],
             "seen": payload}
    answers["plan"] = shows
    assert run(plans.get_plan_progress()).note is None

    # A second tab that hasn't arrived: the game still shows the first, and doesn't list the new one.
    run(plans.show_plan("Leather", [plans.Step(text="Two")], tab="Leather"))
    progress = run(plans.get_plan_progress())
    assert progress.active and [t.tab for t in progress.tabs] == ["Quests"]
    assert "hasn't received the latest change to the player's plans" in progress.note and "/reload" in progress.note
    # Once it has, there's nothing to say.
    shows["seen"] = sent(addons)[1]
    assert run(plans.get_plan_progress()).note is None

    # A plan sent to the same tab that hasn't arrived: the game shows the one it replaced.
    run(plans.show_plan("Quests again", [plans.Step(text="Three")], tab="Quests"))
    progress = run(plans.get_plan_progress())
    assert "still shows an older plan" in progress.note and progress.steps[0].text is None

    # clear_plan() that hasn't arrived: the game still shows a plan the server no longer has.
    plans.clear_plan()
    progress = run(plans.get_plan_progress())
    assert progress.active and progress.plan_id == quests["id"]
    assert "still shows a plan that was cleared" in progress.note and "/reload" in progress.note
    # An addon that doesn't say what it has received: nothing can be said about a cleared plan.
    del shows["seen"]
    assert run(plans.get_plan_progress()).note is None


def test_plans_the_player_removed_stop_counting(addons, answers, monkeypatch):
    one = [plans.Step(text="One")]
    for n in range(1, 11):
        run(plans.show_plan(f"Plan {n}", one, tab=f"Tab {n}"))
    listed, payload = sent(addons)
    ids = [p["id"] for p in listed]
    inbox = addons / "WowMCP" / "Inbox.lua"

    # The game isn't running, so it can't be asked: the limit stands, and the error says what would help.
    with pytest.raises(ToolError, match="already has 10 plans .*removed in game still count.*get_plan_progress"):
        run(plans.show_plan("Eleventh", one, tab="Eleven"))

    # The player removed two in game (the x on their tabs): the game says which, and the server forgets them.
    answers["plan"] = {"none": True, "cleared": True, "tabs": {}, "removed": [ids[0], ids[4], 999], "seen": payload}
    before = inbox.read_bytes()
    progress = run(plans.get_plan_progress())
    assert "The player removed the plan" in progress.note
    listed, same = sent(addons)
    assert [p["tab"] for p in listed] == [f"Tab {n}" for n in (2, 3, 4, 6, 7, 8, 9, 10)] and same == payload
    assert inbox.read_bytes() == before, "nothing is sent for it"

    # There's room again, and what's sent next doesn't have them.
    result = run(plans.show_plan("Eleventh", one, tab="Eleven"))
    assert "the player's tabs are now: Tab 2, Tab 3, Tab 4, Tab 6, Tab 7, Tab 8, Tab 9, Tab 10, Eleven." in result
    now = inbox.read_text(encoding="utf-8")
    assert 'tab = "Tab 1",' not in now and 'tab = "Tab 5",' not in now and 'tab = "Tab 2",' in now

    # At the limit show_plan asks the game itself, when it can.
    run(plans.show_plan("Twelfth", one, tab="Twelve"))
    monkeypatch.setattr(plans, "delivery_note", lambda: "")
    with pytest.raises(ToolError, match=r"already has 10 plans \(tabs: 'Tab 2', .*clear_plan\(tab=\.\.\.\) first\.$"):
        run(plans.show_plan("Thirteenth", one, tab="Thirteen"))
    answers["plan"]["removed"] = [ids[1]]
    result = run(plans.show_plan("Thirteenth", one, tab="Thirteen"))
    assert "the player's tabs are now: Tab 3, Tab 4, Tab 6, Tab 7, Tab 8, Tab 9, Tab 10, Eleven, Twelve, Thirteen." \
        in result


@pytest.mark.parametrize("title, conditions, given, expected", [
    # A profession's skill or a recipe: a profession plan; quests: a questing plan.
    ("To 75", [{"skill": "leatherworking", "rank": 75}], {}, ("profession", "Leatherworking")),
    ("Gloves", [{"recipe": 3770}], {}, ("profession", None)),
    ("Kobolds", [{"quest_accepted": 900}], {}, ("questing", None)),
    # A skill that isn't a profession says nothing about the plan.
    ("Westfall", [{"quest_accepted": 900}, {"skill": "Riding", "rank": 75}], {}, ("questing", None)),
    ("Train swords", [{"skill": "Swords", "rank": 100}], {}, ("general", None)),
    # A profession's skill among quests and other skills still makes it that profession's plan.
    ("Ores", [{"quest_accepted": 900}, {"skill": "Riding", "rank": 75}, {"skill": "Mining", "rank": 75}], {},
     ("profession", "Mining")),
    # The title names a profession only as a word of its own.
    ("Undermining the Defias", [{"quest_accepted": 900}], {}, ("questing", None)),
    ("Disenchanting your greens", [], {}, ("general", None)),
    ("First aid 1 to 75", [], {}, ("profession", "First Aid")),
    ("Mining: copper", [{"quest_accepted": 900}], {}, ("profession", "Mining")),
    # A profession given without a kind makes it a profession plan; a kind given stands.
    ("Farm Rugged Leather", [{"have": 8170}], {"profession": "Leatherworking"}, ("profession", "Leatherworking")),
    ("Farm Rugged Leather", [{"have": 8170}], {"profession": "Leatherworking", "kind": "general"}, ("general", None)),
])
def test_a_plans_kind_is_worked_out_from_its_steps(addons, title, conditions, given, expected):
    run(plans.show_plan(title, [plans.Step(text="Do it", conditions=[c]) for c in conditions]
                        or [plans.Step(text="Do it")], **given))
    (plan_sent,), _ = sent(addons)
    assert (plan_sent["kind"], plan_sent.get("profession")) == expected


# Several plans, each under its own tab --------------------------------------------------------


def test_plans_go_to_tabs(addons):
    one = [plans.Step(text="One")]
    quests = [plans.Step(text="Accept it", conditions=[{"quest_accepted": 900}])]
    train = [plans.Step(text="Train", conditions=[{"skill": "Leatherworking", "rank": 75}])]

    def tabs():
        return [(p["tab"], p["kind"], p.get("profession"), p["title"]) for p in
                Transport(addons).load_state()["plan"]["list"]]

    # A plan without a tab name goes to the tab 'Plan' and replaces the one there; its kind is worked out.
    result = run(plans.show_plan("First", one))
    assert "tab" not in result, result
    run(plans.show_plan("Kobolds", quests))
    assert tabs() == [("Plan", "questing", None, "Kobolds")]

    # A tab name adds a plan next to it; the profession comes from the skill its steps train, or the title.
    result = run(plans.show_plan("To 75", train, tab="  Leather   work "))
    assert "It's in the tab 'Leather work' (Leatherworking icon); the player's tabs are now: Plan, Leather work." \
        in result, result
    run(plans.show_plan("Tailoring 1 to 75", one, tab="Cloth"))
    run(plans.show_plan("Hunter rotation", one, tab="Rotation", kind="class"))
    run(plans.show_plan("Mounts", one, tab="Other", kind="profession", profession="Riding"))
    assert tabs() == [
        ("Plan", "questing", None, "Kobolds"), ("Leather work", "profession", "Leatherworking", "To 75"),
        ("Cloth", "profession", "Tailoring", "Tailoring 1 to 75"), ("Rotation", "class", None, "Hunter rotation"),
        ("Other", "profession", "Riding", "Mounts"),
    ]

    # The name of an existing tab (any case) replaces that plan where it is, with a new id.
    before = Transport(addons).load_state()["plan"]["list"][1]["id"]
    run(plans.show_plan("To 100", train, tab="leather WORK"))
    state = Transport(addons).load_state()
    assert tabs()[1] == ("leather WORK", "profession", "Leatherworking", "To 100") and len(tabs()) == 5
    assert state["plan"]["list"][1]["id"] == state["seq"] > before

    # There's room for ten, counting the ones the player archived.
    for n in range(6, 11):
        run(plans.show_plan(f"Plan {n}", one, tab=f"Tab {n}"))
    with pytest.raises(ToolError, match=r"already has 10 plans \(tabs: 'Plan', 'leather WORK', .*clear_plan"):
        run(plans.show_plan("Eleventh", one, tab="Eleven"))
    assert len(tabs()) == 10
    run(plans.show_plan("Sixth again", one, tab="tab 6"))
    for n in range(7, 11):
        plans.clear_plan(f"Tab {n}")

    # Removing one tab's plan, or all of them.
    with pytest.raises(ToolError, match="No plan in a tab 'Nope'; the player's tabs: 'Plan', "):
        plans.clear_plan("Nope")
    assert plans.clear_plan("cloth") == "Cleared the plan in the tab 'Cloth'."
    assert [t[0] for t in tabs()] == ["Plan", "leather WORK", "Rotation", "Other", "tab 6"]
    assert plans.clear_plan() == "Cleared the 5 plans."
    assert tabs() == []


def test_a_plan_from_before_tabs_keeps_its_place(addons):
    """The state of a server from before there were tabs holds one plan; it becomes the tab 'Plan'."""
    transport = Transport(addons)
    old = {"id": 1, "title": "Old", "steps": [{"text": "One", "places": [], "loop": False, "areas": [],
                                              "conditions": []}]}
    transport.publish(lambda state, seq: state.update(plan=old))
    run(plans.show_plan("Quests", [plans.Step(text="Two")], tab="Quests"))
    sent = transport.load_state()["plan"]["list"]
    assert [(p["id"], p["tab"], p["title"]) for p in sent] == [(1, "Plan", "Old"), (2, "Quests", "Quests")]
    assert sent[0]["steps"] == old["steps"]


QUESTS_TAB = plan("Kobold quests", step("Accept Kobold Camp Cleanup", {"quest_accepted": 900}), step("Hand it in"),
                  tab="Quests")
LEATHER_TAB = plan("Leatherworking 75", step("Train", {"skill": "Leatherworking", "rank": 75}), tab="Leather")
CLASS_TAB = plan("Hunter rotation", step("Serpent Sting first"), step("Then Arcane Shot"), tab="Rotation",
                 kind="class")
NOTES_TAB = plan("Things to do", step("Feed the pet"), tab="Notes")


def test_plans_under_tabs(game):
    game(f"local QUESTS, LEATHER, CLASS, NOTES = [[{QUESTS_TAB}]], [[{LEATHER_TAB}]], [[{CLASS_TAB}]], [[{NOTES_TAB}]]\n"
         + r"""
        client.LoadAddon()
        client.Wait()
        local function tabs()
            local list = {}
            for _, tab in ipairs(WowMCPPlanFrame.Tabs) do
                if tab:IsShown() then table.insert(list, tab) end
            end
            return list
        end
        local function names()
            local list = {}
            for i, tab in ipairs(tabs()) do list[i] = (tab.open and "*" or "") .. plain(tab.Name.text) end
            return table.concat(list, ",")
        end
        local function icons()
            local list = {}
            for i, tab in ipairs(tabs()) do list[i] = tab.Icon.texture:match("[^\\]+$") end
            return table.concat(list, ",")
        end
        local function mcp(command) SlashCmdList.WOWMCP(command) end

        -- One plan: one tab, with the icon of its kind.
        client.Publish(QUESTS)
        client.Wait()
        local f = WowMCPPlanFrame
        assert(names() == "*Quests" and icons() == "AvailableQuestIcon" and plain(f.Title.text) == "Kobold quests")

        -- Each new plan gets a tab next to the others and is the one shown. A profession has its own
        -- icon, a class guide the player's class, anything else a note.
        client.Publish(LEATHER)
        client.Wait()
        client.Publish(CLASS)
        client.Wait()
        client.Publish(NOTES)
        client.Wait()
        assert(names() == "Quests,Leather,Rotation,*Notes", names())
        assert(icons() == "AvailableQuestIcon,Trade_LeatherWorking,ClassIcon_HUNTER,INV_Misc_Note_01", icons())
        assert(plain(f.Title.text) == "Things to do" and f.Count.text == "1/1")
        assert(has("New plan: |cffffffffThings to do|r (1 steps)."))
        -- They share the window's width, each right of the one before, on top of the window.
        local first, second = tabs()[1], tabs()[2]
        assert(first.width == second.width and first.width * 4 <= 300)
        assert(first.points[1][1] == "BOTTOMLEFT" and first.points[1][3] == "TOPLEFT" and first.points[1][4] == 0)
        assert(second.points[1][4] > first.width)

        -- Clicking a tab shows its plan, which goes on where it was.
        first.OnMouseUp(first)
        assert(names() == "*Quests,Leather,Rotation,Notes" and plain(f.Title.text) == "Kobold quests")
        assert(f.Count.text == "1/2" and WowMCPDB.plan.id == WowMCPDB.plans[WowMCPDB.plan.id].id)
        assert(last():find("Plan: |cffffffffQuests|r (step 1/2).", 1, true), last())
        client.AcceptQuest(900)
        client.Wait(2)
        assert(f.Count.text == "2/2")

        -- By number or by name; what happened meanwhile ticks off when its tab is opened.
        mcp("plan tab 3")
        assert(names() == "Quests,Leather,*Rotation,Notes" and f.Count.text == "1/2")
        mcp("plan next")
        mcp("plan tab leather")
        assert(names() == "Quests,*Leather,Rotation,Notes" and f.Count.text == "1/1")
        client.SetSkill("Leatherworking", 75)
        mcp("plan tab QUESTS")
        assert(f.Count.text == "2/2")
        mcp("plan tab 2")
        assert(plain(f.Count.text) == "Done", f.Count.text)
        mcp("plan tab 9")
        assert(last():find("No such plan. /mcp plan tabs lists them.", 1, true))

        -- The player can name a tab; the list says what each plan is.
        mcp("plan tab rotation")
        mcp("plan name Hunter things, all of them!")
        assert(names() == "Quests,Leather,*Hunter things, all o,Notes", names())
        mcp("plan name Shots")
        mcp("plan tabs")
        assert(has("1. |cffffffffQuests|r (Questing): Kobold quests"), chat())
        assert(has("2. |cffffffffLeather|r (Leatherworking): Leatherworking 75"))
        assert(has("|cff33ff33>|r 3. |cffffffffShots|r (Hunter): Hunter rotation"))
        assert(has("4. |cffffffffNotes|r (General): Things to do"))

        -- A /reload keeps the tabs, the open one, its name and everyone's progress.
        Reload()
        f = WowMCPPlanFrame
        assert(names() == "Quests,Leather,*Shots,Notes" and f.Count.text == "2/2", names())
        mcp("plan tab 1")
        assert(f.Count.text == "2/2")
        assert(not has("New plan"), chat())

        -- A plan sent to a tab's name replaces that plan, in place.
        client.Publish(QUESTS)
        client.Wait()
        assert(names() == "*Quests,Leather,Shots,Notes" and f.Count.text == "2/2" and has("New plan: |cffffffffKobold quests|r"))

        -- Removing the open plan removes its tab and opens another; the AI removing one does the same.
        mcp("plan clear")
        assert(names() == "*Leather,Shots,Notes", names())
        assert(has("Plan removed: |cffffffffQuests|r.") and plain(f.Title.text) == "Leatherworking 75")
        client.Publish('{"clear_plan": "Leather"}')
        client.Wait()
        assert(names() == "*Shots,Notes" and plain(f.Title.text) == "Hunter rotation", names())
        -- Hiding hides the tabs with the window; opening a tab brings it back.
        mcp("plan hide")
        assert(not f:IsShown())
        mcp("plan tab notes")
        assert(f:IsShown() and names() == "Shots,*Notes")

        -- All of them gone.
        client.Publish('{"clear_plan": true}')
        client.Wait()
        assert(not f:IsShown() and has("Plan cleared."))
        mcp("plan tabs")
        assert(last():find("No plan. Ask your AI for one!", 1, true))
        noErrors()
    """)


def test_a_plan_without_a_kind_gets_its_icon_from_its_steps(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        -- As a server from before there were tabs sends it: one plan, no tab or kind.
        WowMCP_Receive({ seq = 1000, plan = { id = 1000, title = "Old style", steps = {
            { text = "Get to 75", conditions = { { skill = "Mining", rank = 75 } } },
        } } })
        client.Wait(2)
        local tab = WowMCPPlanFrame.Tabs[1]
        assert(tab:IsShown() and tab.open and tab.Name.text:find("Plan", 1, true))
        assert(tab.Icon.texture == "Interface\\Icons\\Trade_Mining", tab.Icon.texture)
        WowMCP_Receive({ seq = 1001, plan = { id = 1001, title = "Quests", steps = {
            { text = "Hand it in", conditions = { { questTurnedIn = 900 } } },
        } } })
        client.Wait(2)
        assert(not WowMCPPlanFrame.Tabs[2] and tab.Icon.texture == "Interface\\GossipFrame\\AvailableQuestIcon")
        assert(WowMCPDB.plan.id == 1001 and WowMCPDB.plans[1000] == nil)
        noErrors()
    """)


def test_closing_a_tab_archives_or_removes_its_plan(game):
    game(f"local QUESTS, LEATHER, CLASS = [[{QUESTS_TAB}]], [[{LEATHER_TAB}]], [[{CLASS_TAB}]]\n" + r"""
        client.LoadAddon()
        client.Wait()
        local function mcp(command) SlashCmdList.WOWMCP(command) end
        local function tabs()
            local list = {}
            for _, tab in ipairs(WowMCPPlanFrame.Tabs) do
                if tab:IsShown() then table.insert(list, tab) end
            end
            return list
        end
        local function names()
            local list = {}
            for i, tab in ipairs(tabs()) do list[i] = (tab.open and "*" or "") .. plain(tab.Name.text) end
            return table.concat(list, ",")
        end
        local function shelf() return WowMCPPlanFrame.Shelf end
        local function archived()
            local list = {}
            for _, row in ipairs(shelf() and shelf().List:IsShown() and shelf().List.Rows or {}) do
                if row:IsShown() then table.insert(list, row) end
            end
            return list
        end
        client.Publish(QUESTS)
        client.Wait()
        client.Publish(LEATHER)
        client.Wait()
        client.Publish(CLASS)
        client.Wait()
        local f = WowMCPPlanFrame
        assert(names() == "Quests,Leather,*Rotation" and not shelf())

        -- Progress on a plan, then its tab's x: here that archives it (in game it asks: archive or remove).
        mcp("plan next")
        assert(f.Count.text == "2/2")
        local x = tabs()[3].Close
        x.OnMouseUp(x)
        assert(names() == "*Quests,Leather", names())
        assert(has("Plan archived: |cffffffffRotation|r. The box at the end of the tabs, or /mcp plan restore, brings it back."))
        assert(plain(f.Title.text) == "Kobold quests" and WowMCPDB.plans[WowMCPDB.plan.id] == WowMCPDB.plan)

        -- The archive button appears at the end of the tabs, which make room for it.
        assert(shelf():IsShown() and plain(shelf().Count.text) == "1" and #archived() == 0)
        local last_tab = tabs()[2]
        assert(last_tab.points[1][4] + last_tab.width <= 300 - shelf().width)
        -- The x of a tab that isn't open leaves the open one alone.
        x = tabs()[2].Close
        x.OnMouseUp(x)
        assert(names() == "*Quests" and plain(shelf().Count.text) == "2")

        -- Clicking the button lists the archived plans; clicking one brings it back, where it was.
        shelf().OnMouseUp(shelf())
        assert(#archived() == 2 and plain(archived()[1].Name.text) == "Leather  Leatherworking 75", archived()[1].Name.text)
        assert(archived()[2].Icon.texture == "Interface\\Icons\\ClassIcon_HUNTER")
        archived()[2].OnMouseUp(archived()[2], "LeftButton")
        assert(names() == "Quests,*Rotation" and f.Count.text == "2/2" and plain(f.Title.text) == "Hunter rotation", names())
        assert(#archived() == 0 and plain(shelf().Count.text) == "1")

        -- By command, and it all survives a /reload.
        mcp("plan archive")
        assert(names() == "*Quests")
        mcp("plan archived")
        assert(has("1. |cffffffffLeather|r (Leatherworking): Leatherworking 75"))
        assert(has("2. |cffffffffRotation|r (Hunter): Hunter rotation"))
        Reload()
        f = WowMCPPlanFrame
        assert(names() == "*Quests" and plain(shelf().Count.text) == "2")
        mcp("plan restore rotation")
        assert(names() == "Quests,*Rotation" and f.Count.text == "2/2")
        mcp("plan restore 5")
        assert(last():find("No such archived plan. /mcp plan archived lists them.", 1, true))

        -- With every plan archived the window stays, small, for the archive button.
        mcp("plan archive")
        mcp("plan archive")
        assert(f:IsShown() and names() == "" and plain(f.Title.text) == "No plan open" and plain(shelf().Count.text) == "3")
        assert(not nav() or not nav():IsShown())
        mcp("plan next")
        assert(last():find("No plan. Ask your AI for one!", 1, true))
        mcp("plan hide")
        assert(not f:IsShown())
        mcp("plan show")
        assert(f:IsShown())
        Reload()
        f = WowMCPPlanFrame
        assert(f:IsShown() and names() == "" and plain(shelf().Count.text) == "3")

        -- Right-clicking an archived plan removes it for good; bringing the last one back opens it.
        shelf().OnMouseUp(shelf())
        archived()[1].OnMouseUp(archived()[1], "RightButton")
        assert(has("Plan removed: |cffffffffQuests|r.") and plain(shelf().Count.text) == "2")
        mcp("plan restore 1")
        assert(names() == "*Leather" and plain(f.Title.text) == "Leatherworking 75" and plain(shelf().Count.text) == "1")
        -- A new plan for an archived plan's tab replaces it: nothing left in the archive.
        client.Publish(CLASS)
        client.Wait()
        assert(names() == "Leather,*Rotation" and not shelf():IsShown() and f.Count.text == "1/2")
        noErrors()
    """)


def test_a_question_about_a_plan_thats_gone_does_nothing(game):
    game(f"local QUESTS, LEATHER, CLASS = [[{QUESTS_TAB}]], [[{LEATHER_TAB}]], [[{CLASS_TAB}]]\n" + r"""
        client.UsePopups()
        client.LoadAddon()
        client.Wait()
        local function tabs()
            local list = {}
            for _, tab in ipairs(WowMCPPlanFrame.Tabs) do
                if tab:IsShown() then table.insert(list, tab) end
            end
            return list
        end
        local function names()
            local list = {}
            for i, tab in ipairs(tabs()) do list[i] = (tab.open and "*" or "") .. plain(tab.Name.text) end
            return table.concat(list, ",")
        end
        local function x(n) tabs()[n].Close.OnMouseUp(tabs()[n].Close) end
        client.Publish(QUESTS)
        client.Wait()
        client.Publish(LEATHER)
        client.Wait()
        client.Publish(CLASS)
        client.Wait()
        assert(names() == "Quests,Leather,*Rotation")

        -- The x on a tab asks: archive or remove. Nothing happens until the player answers.
        x(1)
        local asked = client.popup
        assert(asked.which == "WOWMCP_CLOSE_PLAN" and asked.text == "Quests" and names() == "Quests,Leather,*Rotation")

        -- Meanwhile the AI sends another plan to that tab: the question goes with the plan it was about.
        client.Publish(QUESTS)
        client.Wait()
        assert(client.popup == nil and names() == "*Quests,Leather,Rotation", names())
        -- An answer that still comes (the old plan's id) touches no other plan: not the one now open.
        local dialog = StaticPopupDialogs.WOWMCP_CLOSE_PLAN
        dialog.OnAlt(nil, asked.data)
        assert(last():find("That plan is gone: your AI replaced or removed it.", 1, true), last())
        dialog.OnAccept(nil, asked.data)
        assert(names() == "*Quests,Leather,Rotation" and not has("Plan removed") and not has("Plan archived"), chat())
        assert(not WowMCPPlanFrame.Shelf or not WowMCPPlanFrame.Shelf:IsShown())

        -- The same when the AI removes the plan asked about; a question about another plan stays.
        x(2)
        client.Publish('{"clear_plan": "Rotation"}')
        client.Wait()
        assert(client.popup and client.popup.text == "Leather" and names() == "*Quests,Leather")
        client.Publish('{"clear_plan": "Leather"}')
        client.Wait()
        assert(client.popup == nil and names() == "*Quests")

        -- Answered in time, it does what was asked: to the plan asked about, open or not.
        client.Publish(LEATHER)
        client.Wait()
        client.Publish(CLASS)
        client.Wait()
        x(1)
        client.AnswerPopup("alt")
        assert(names() == "Leather,*Rotation" and has("Plan removed: |cffffffffQuests|r."), names())
        x(1)
        client.AnswerPopup("accept")
        assert(names() == "*Rotation" and has("Plan archived: |cffffffffLeather|r."), names())
        noErrors()
    """)


def test_the_game_reports_its_tabs_and_what_the_player_removed(game):
    game(f"local QUESTS, LEATHER, CLASS = [[{QUESTS_TAB}]], [[{LEATHER_TAB}]], [[{CLASS_TAB}]]\n" + r"""
        local ns = client.LoadAddon()
        client.Wait()
        local function mcp(command) SlashCmdList.WOWMCP(command) end
        local function report() return ns.Plans.Progress() end
        local ids = {}
        for i, sent in ipairs({ QUESTS, LEATHER, CLASS }) do
            client.Publish(sent)
            client.Wait()
            ids[i] = WowMCPDB.plan.id
        end
        local quests, leather, class = ids[1], ids[2], ids[3]

        -- The open plan and every tab, by the names the AI sent; the last payload received.
        local r = report()
        assert(r.id == class and r.tab == "Rotation" and r.name == nil and r.kind == "class" and r.removed == nil)
        assert(r.seen == class and WowMCPDB.planSeen == class)
        assert(#r.tabs == 3 and r.tabs[1].tab == "Quests" and r.tabs[2].kind == "profession" and r.tabs[3].shown)

        -- A tab the player names keeps the AI's name too: that's the one its tools know.
        mcp("plan name Shots")
        r = report()
        assert(r.tab == "Rotation" and r.name == "Shots", r.tab)
        assert(r.tabs[3].tab == "Rotation" and r.tabs[3].name == "Shots" and r.tabs[1].name == nil)

        -- An archived plan is still listed; a removed one isn't, but its id is reported.
        local x = WowMCPPlanFrame.Tabs[2].Close
        x.OnMouseUp(x)
        mcp("plan clear")
        r = report()
        assert(r.id == quests and #r.tabs == 2 and r.tabs[2].id == leather and r.tabs[2].archived and not r.tabs[2].shown)
        assert(#r.removed == 1 and r.removed[1] == class)

        -- The AI removes the open plan: what's left is archived, and that's what the game says (it
        -- has received everything: a /reload wouldn't change a thing).
        client.Publish('{"clear_plan": "Quests"}')
        client.Wait()
        r = report()
        assert(r.none and r.archived == true and r.cleared == nil and r.id == nil)
        assert(#r.tabs == 1 and r.tabs[1].archived and r.removed[1] == class)
        assert(r.seen == WowMCPDB.planSeen and r.seen > class)

        -- Back from the archive and removed: nothing left but what was removed.
        mcp("plan restore 1")
        assert(report().id == leather and report().archived == nil)
        mcp("plan clear")
        r = report()
        assert(r.none and r.cleared == true and r.archived == nil and #r.tabs == 0 and #r.removed == 2)
        noErrors()
    """)


def test_dragging_the_corner_resizes_until_let_go(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish([[""" + TEN + r"""]])
        client.Wait()
        local f = WowMCPPlanFrame
        local grip = f.Grip
        local function held() return grip.OnUpdate ~= nil and grip.Corner.texture:find("Down") ~= nil end

        -- Held by the left button, the window follows the cursor from its top left corner: 60 px to
        -- the right is 60 wider, 66 px up three steps fewer.
        client.Mouse(500, 400, true)
        grip.OnMouseDown(grip, "RightButton")
        assert(not held())
        grip.OnMouseDown(grip, "LeftButton")
        assert(held() and f.points[1][1] == "TOPLEFT" and f.points[1][3] == "BOTTOMLEFT")
        client.Mouse(560, 400 + 66, true)
        client.Tick()
        assert(f.width == 360 and #rows() == 5, f.width)
        grip.OnMouseUp(grip, "LeftButton")
        assert(not held() and grip.Corner.texture:find("Up"))
        client.Mouse(900, 100, false)
        client.Tick()
        assert(f.width == 360 and #rows() == 5)

        -- The window goes while the button is held (the AI cleared the plans): the drag is over, and
        -- doesn't pick up again when the window is back.
        grip.OnMouseDown(grip, "LeftButton")
        assert(held())
        f.OnHide(f)
        assert(not held())
        grip.OnMouseDown(grip, "LeftButton")
        grip.OnHide(grip)
        assert(not held())
        -- Let go without the grip being told: it stops by itself, where it was.
        grip.OnMouseDown(grip, "LeftButton")
        client.Mouse(300, 300, false)
        client.Tick()
        assert(not held() and f.width == 360 and #rows() == 5, f.width)
        assert(WowMCPDB.planWindow.width == 360 and WowMCPDB.planWindow.rows == 5)
        noErrors()
    """)


def test_the_window_can_be_resized(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        local function mcp(command) SlashCmdList.WOWMCP(command) end
        client.Publish([[""" + TEN + r"""]])
        client.Wait()
        local f = WowMCPPlanFrame
        assert(f.width == 300 and #rows() == 8 and rows()[1].width == 284 and rows()[1].Text.width == 266)
        assert(f.Grip.points[1][1] == "BOTTOMRIGHT")

        -- Dragging the corner ends up here: wider, and fewer steps at once.
        local ns = select(2, ...)
        WowMCPPlanFrame.Grip.OnMouseDown(WowMCPPlanFrame.Grip) -- no cursor in the stub: nothing happens
        mcp("plan size 420 5")
        assert(last():find("The plan window is 420 wide and shows up to 5 steps.", 1, true), last())
        assert(f.width == 420 and #rows() == 5 and rows()[1].width == 404 and rows()[1].Text.width == 386)
        assert(f.Title.width == 420 - 16 - 80)
        assert(f.Later:IsShown() and f.Later.text == "5 more steps below", f.Later.text)
        -- The tabs share the new width.
        assert(f.Tabs[1].width == 120)
        -- The mouse wheel still scrolls to the last step.
        for _ = 1, 20 do f.OnMouseWheel(f, -1) end
        assert(rows()[5].n == 10 and #rows() == 5)

        -- Only the width; and no narrower, wider, fewer or more than is useful.
        mcp("plan size 250")
        assert(f.width == 250 and #rows() == 5)
        mcp("plan size 50 1")
        assert(f.width == 220 and #rows() == 2)
        mcp("plan size 5000 99")
        assert(f.width == 600 and #rows() == 10)
        mcp("plan size huge")
        assert(last():find("/mcp plan size <width> [steps shown]", 1, true))

        -- It's kept over a /reload, and can be reset.
        mcp("plan size 380 6")
        Reload()
        f = WowMCPPlanFrame
        assert(f.width == 380 and #rows() == 6, f.width)
        mcp("plan size reset")
        assert(f.width == 300 and #rows() == 8 and WowMCPDB.planWindow.width == nil)
        noErrors()
    """)


WHO = plan(
    "Kobold Camp Cleanup",
    step("Accept Kobold Camp Cleanup from Marshal McBride", npcs=[{"id": 197, "name": "Marshal McBride"}]),
    step("Kill Kobold Vermin and Workers", npcs=[{"id": 6, "name": "Kobold Vermin"}, {"id": 257, "name": "Kobold Worker"}]),
    step("Look for the others", npcs=[
        {"id": 999999, "name": "Nobody"}, {"id": 40, "name": "Kobold Miner"},
        {"id": 888888, "name": "Old Kobold", "display_id": 373}, {"id": 475, "name": "Kobold Tunneler"}]),
    step("Hand it in"),
)


def test_npcs_beside_a_tooltip_near_the_screen_edge(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish([[""" + WHO + r"""]])
        client.Wait()
        local function boxes()
            local list = {}
            for _, frame in ipairs(client.frames) do
                if frame.SetCreature and frame.parent:IsShown() then table.insert(list, frame.parent) end
            end
            return list
        end
        local function at(box) return box.points[1][4], box.points[1][5] end
        UIParent.GetWidth = function() return 1920 end
        UIParent.GetEffectiveScale = function() return 1 end
        GameTooltip.GetEffectiveScale = function() return 1 end
        local function tooltipAt(left, right)
            GameTooltip.GetLeft = function() return left end
            GameTooltip.GetRight = function() return right end
        end

        -- Room on the right: there, as before.
        tooltipAt(300, 600)
        row(2).OnEnter(row(2))
        local first, second = table.unpack(boxes())
        local holder = first.parent
        assert(holder.points[1][1] == "TOPLEFT" and holder.points[1][3] == "TOPRIGHT")
        row(2).OnLeave(row(2))

        -- The plan on the right of the screen: the models go to the left of the tooltip, nearest first.
        tooltipAt(1500, 1800)
        row(2).OnEnter(row(2))
        first, second = table.unpack(boxes())
        local point = holder.points[1]
        assert(point[1] == "TOPRIGHT" and point[2] == GameTooltip and point[3] == "TOPLEFT" and point[4] == -4)
        assert(holder.width == 2 * 130 + 4, holder.width)
        assert(at(first) == 134 and at(second) == 0, "the first next to the tooltip")
        row(2).OnLeave(row(2))

        -- No room for a row on either side: one above the other on the roomier side, never on top of each other.
        tooltipAt(200, 1800)
        row(2).OnEnter(row(2))
        first, second = table.unpack(boxes())
        assert(holder.points[1][1] == "TOPRIGHT" and holder.width == 130 and holder.height == 2 * (170 + 16) + 4)
        local x1, y1 = at(first)
        local x2, y2 = at(second)
        assert(x1 == 0 and y1 == 0 and x2 == 0 and y2 == -(170 + 16 + 4), x2 .. "," .. y2)
        noErrors()
    """)


def test_npc_models_keep_going_while_the_window_updates(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish([[""" + WHO + r"""]])
        client.Wait()
        local asked = 0
        for _, frame in ipairs(client.frames) do
            if frame.SetCreature then error("no models before a step is hovered") end
        end
        row(2).OnEnter(row(2))
        local models = {}
        for _, frame in ipairs(client.frames) do
            if frame.SetCreature then
                table.insert(models, frame)
                local set = frame.SetCreature
                frame.SetCreature = function(self, ...) asked = asked + 1 return set(self, ...) end
            end
        end
        assert(#models == 2)
        -- The window checks its steps every second; the step under the mouse stays shown, so it isn't
        -- told the mouse left and came back, and its creatures aren't asked for again.
        local hidden = 0
        local hide = row(2).Hide
        row(2).Hide = function(self) hidden = hidden + 1 return hide(self) end
        client.Wait(5)
        assert(hidden == 0 and row(2):IsShown(), "the hovered row was hidden " .. hidden .. " times")
        -- Entering the same step again doesn't restart them either.
        row(2).OnEnter(row(2))
        assert(asked == 0, asked .. " creatures asked for again")
        noErrors()
    """)


def test_hovering_a_step_shows_its_npcs(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        client.unseenCreatures[999999], client.unseenCreatures[888888] = true, true
        client.Publish([[""" + WHO + r"""]])
        client.Wait()
        -- The model frames there are, and the ones to be seen (in their box), left to right.
        local function seen(frame) return frame.parent:IsShown() and frame.parent:GetAlpha() > 0 end
        local function models(shown)
            local list = {}
            for _, frame in ipairs(client.frames) do
                if frame.SetCreature and (not shown or seen(frame)) then table.insert(list, frame) end
            end
            return list
        end
        local function name(model) return model.parent.Name.text end
        local function left(model) return model.parent.points[1][4] end
        local lines
        GameTooltip.SetText = function() lines = {} end
        GameTooltip.AddLine = function(_, text) table.insert(lines, text) end
        local function tip() return table.concat(lines, "\n") end
        local function enter(n) row(n).OnEnter(row(n)) end
        local function leave(n) row(n).OnLeave(row(n)) end

        -- Nothing until a step is hovered: not even the frames.
        assert(#models() == 0)

        -- A step with two NPCs: both beside the tooltip, side by side, named; the tooltip names them too.
        enter(2)
        local shown = models(true)
        assert(#shown == 2 and shown[1].creature == 6 and shown[2].creature == 257)
        assert(name(shown[1]) == "Kobold Vermin" and name(shown[2]) == "Kobold Worker")
        -- In one frame beside the tooltip, which is kept on screen as a whole.
        local holder = shown[1].parent.parent
        local point = holder.points[1]
        assert(point[1] == "TOPLEFT" and point[2] == GameTooltip and point[3] == "TOPRIGHT" and point[4] == 4)
        assert(shown[1].parent.points[1][2] == holder and holder:IsShown())
        assert(left(shown[1]) == 0 and left(shown[2]) == 130 + 4 and shown[1].width == 130 and shown[1].height == 170)
        assert(shown[1].parent.Back.color[4] > 0.5, "on a dark background")
        assert(tip():find("NPC: Kobold Vermin\nNPC: Kobold Worker", 1, true), tip())

        -- Only while it's hovered.
        leave(2)
        assert(#models(true) == 0 and not holder:IsShown())

        -- A step without NPCs shows none; the same frames serve every step.
        enter(4)
        assert(#models(true) == 0 and not tip():find("NPC:", 1, true), tip())
        leave(4)
        enter(1)
        shown = models(true)
        assert(#shown == 1 and shown[1].creature == 197 and name(shown[1]) == "Marshal McBride" and #models() == 2)
        enter(2) -- straight on to the next step
        assert(#models(true) == 2 and models(true)[1].creature == 6 and #models() == 2)
        leave(2)

        -- Three at most. A creature the client can't draw leaves no empty box (the tooltip still names
        -- it), unless its display ID can be drawn.
        enter(3)
        shown = models(true)
        assert(#models() == 3 and #shown == 2, #shown)
        assert(shown[1].creature == 40 and left(shown[1]) == 0 and name(shown[1]) == "Kobold Miner")
        assert(shown[2].creature == 888888 and shown[2].display == 373 and left(shown[2]) == 134)
        assert(tip():find("NPC: Nobody\nNPC: Kobold Miner\nNPC: Old Kobold\nNPC: Kobold Tunneler", 1, true), tip())
        -- It may only have needed loading, which a hidden model might never do: its box is shown, but
        -- can't be seen. Once it's loaded it joins the others.
        local late = models()[1]
        assert(late.creature == 999999 and late.parent:IsShown() and late.parent:GetAlpha() == 0)
        client.unseenCreatures[999999] = nil
        late.OnModelLoaded(late)
        shown = models(true)
        assert(#shown == 3 and shown[1].creature == 999999 and left(shown[1]) == 0 and left(shown[3]) == 268)
        leave(3)
        assert(not late.parent:IsShown(), "no box waits once the mouse left")
        late.OnModelLoaded(late)
        assert(#models(true) == 0 and not late.parent:IsShown(), "not after the mouse left")
        -- One that never loads is never seen, and gone with the mouse.
        client.unseenCreatures[999999] = true
        enter(3)
        assert(#models(true) == 2 and late.parent:IsShown() and late.parent:GetAlpha() == 0)
        leave(3)
        assert(not late.parent:IsShown())

        -- They go with the window.
        enter(2)
        assert(#models(true) == 2)
        SlashCmdList.WOWMCP("plan hide")
        assert(#models(true) == 0)
        SlashCmdList.WOWMCP("plan show")
        assert(#models(true) == 0)

        -- Straight from a payload, past the server's checks: what isn't an NPC is left out.
        WowMCP_Receive({ seq = 1000, plan = { id = 1000, title = "Odd", steps = {
            { text = "Odd NPCs", npcs = { "junk", { id = "abc" }, { name = "No ID" }, { id = 12, name = 5 }, { id = -1 } } },
            { text = "No list", npcs = "junk" },
        } } })
        client.Wait(2)
        enter(1)
        shown = models(true)
        assert(#shown == 1 and shown[1].creature == 12 and name(shown[1]) == "" and not tip():find("NPC:", 1, true))
        enter(2)
        assert(#models(true) == 0)
        noErrors()
    """)
