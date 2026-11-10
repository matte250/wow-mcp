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

from wow_mcp.tools import plans
from wow_mcp.transport import Transport

# How a Lua test script hands control back to publish through the real tools.
PUBLISH_SCRIPT = """
import json, sys
from wow_mcp.tools import plans, routes
args = json.loads(sys.argv[1])
if "plan" in args:
    plans.show_plan(args["plan"]["title"], [plans.Step(**s) for s in args["plan"]["steps"]])
elif args.get("clear_plan"):
    plans.clear_plan()
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


def plan(title, *steps):
    return lua_json({"plan": {"title": title, "steps": list(steps)}})


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
        assert(last():find("/mcp plan [show, hide, next, prev, <n>, reset, clear]", 1, true))
        mcp("")
        assert(has("WowMCP|r /mcp plan [show, hide, next, prev, <n>, reset, clear] - the plan from your AI"))
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
    result = plans.show_plan("Gloves", [
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
    ])
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

    state = Transport(addons).load_state()
    bank = {"zone": "Orgrimmar", "x": 50.12, "y": 50, "label": "Bank", "note": None, "mapID": None}
    area = {"zone": "Durotar", "x": 55, "y": 45, "radius": 6, "label": "Spiders", "color": "cc66ff", "mapID": None}
    nothing = {"places": [], "loop": False, "areas": []}
    assert state["plan"] == {"id": state["seq"], "title": "Gloves", "steps": [
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
    assert state["plan"] == {"id": state["seq"], "steps": []}


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
        result = plans.show_plan("Silk", [
            plans.Step(text="Farm 2 Spider's Silk", places=[plans.Stop(zone="Durotar", x=60, y=50, label="Spiders")],
                       conditions=[plans.Have(have=3182, count=2, name="Spider's Silk")]),
            plans.Step(text="Show it to Gornek"),
        ])
        assert result.startswith("Sent plan #1 'Silk' with 2 steps"), result
        time.sleep(0.5)
        progress = run(plans.get_plan_progress())
        assert progress.game.startswith("World of Warcraft: Forever")
        assert (progress.active, progress.plan_id, progress.title, progress.current_step, progress.total_steps) == (
            True, 1, "Silk", 1, 2)
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
    plans.show_plan("Old", [plans.Step(text="One")])
    plans.show_plan("New", [plans.Step(text="Two")])

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
