"""Goals: background targets that track themselves and point out chances to make progress (Goals.lua, the
goal condition and exports in Plans.lua, tools/goals.py).

Stub geometry (stub/apis/map.lua): Durotar spans world x 500-700, y 300-600, so 1% of its map is 2 yards across
and 3 yards down; Orgrimmar spans x 520-600, y 300-380. The player starts in Durotar at 50, 50, with
Leatherworking 60 and no Medium Hide (4232), Salt (4289) or Fine Thread (2321) in their bags.
"""

import asyncio
import json
import sys
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from wow_mcp.tools import goals, plans
from wow_mcp.transport import Transport

# How a Lua test script hands control back to publish through the real tools.
PUBLISH_SCRIPT = """
import asyncio, json, sys
from wow_mcp import game
from wow_mcp.tools import goals, plans
game.running_folders = lambda proc=None: set()  # the stub game waits for this script: it can't check item IDs
args = json.loads(sys.argv[1])
if "goals" in args:
    goals.set_goals([goals.Goal(**g) for g in args["goals"]], replace=args.get("replace", False))
elif "clear_goals" in args:
    goals.clear_goals(**args["clear_goals"])
elif "plan" in args:
    asyncio.run(plans.show_plan(args["plan"]["title"], [plans.Step(**s) for s in args["plan"]["steps"]]))
"""


def lua_json(value):
    """JSON for client.Publish, which passes it through the shell in single quotes."""
    return json.dumps(value).replace("'", "\\u0027")


HIDES = {
    "id": "medium-hide", "title": "6 Medium Hide", "target": {"have": 4232, "count": 6, "name": "Medium Hide"},
    "zones": [{"zone": "Durotar", "note": "Beasts here skin into Medium Hide"}],
    "mobs": [{"name": "Mottled Boar", "note": "skin"}, {"name": "Durotar Tiger", "npc_id": 3121, "note": "skin"}],
    "note": "Skin everything while questing",
}
SALT = {"id": "salt", "title": "3 Salt", "target": {"have": 4289, "count": 3, "name": "Salt"}}
SILK = {
    "id": "silk", "title": "2 Spider's Silk", "target": {"have": 3182, "count": 2},
    "zones": [{"zone": "Durotar"}],
    "areas": [{"zone": "Durotar", "x": 60, "y": 40, "radius": 5, "label": "Spiders here drop Spider's Silk"}],
}
LW = {"id": "lw-75", "title": "Leatherworking 75", "target": {"skill": "Leatherworking", "rank": 75}}
BELT_PLAN = {"plan": {"title": "Leatherworking 60 to 75", "steps": [
    {"text": "Buy 2 Fine Thread", "conditions": [{"have": 2321, "count": 2, "name": "Fine Thread"}]},
    {"text": "Goal: 6 Medium Hide", "conditions": [{"goal": "medium-hide"}]},
    {"text": "Craft until 75", "conditions": [{"skill": "Leatherworking", "rank": 75}]},
]}}

PRELUDE = (
    f"local HIDES = [[{lua_json({'goals': [HIDES]})}]]\n"
    f"local ALL = [[{lua_json({'goals': [HIDES, SALT, SILK, LW]})}]]\n"
    f"local BELT_PLAN = [[{lua_json(BELT_PLAN)}]]\n"
    + r"""
    local TICK = "|TInterface\\Buttons\\UI-CheckBox-Check:0|t"
    -- Text without colour codes.
    -- Text as read: no colours, icons or link codes (Core.lua renders {item:...} as icon plus link).
    local function plain(text)
        return (tostring(text or ""):gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|r", ""):gsub("|T%d+:0|t ", "")
            :gsub("|Hitem:[^|]*|h%[(.-)%]|h", "%1"))
    end
    local function chat() return plain(table.concat(client.chat, "\n")) end
    local function has(text) return chat():find(text, 1, true) ~= nil end
    local function count(text)
        local n = 0
        for _, line in ipairs(client.chat) do
            if plain(line):find(text, 1, true) then n = n + 1 end
        end
        return n
    end
    local function last() return plain(client.chat[#client.chat]) end
    local function rows()
        local list = {}
        for _, row in ipairs(WowMCPGoalsFrame.Rows) do
            if row:IsShown() then table.insert(list, row) end
        end
        return list
    end
    local function row(n) return rows()[n] end
    local function anchor(f) return f.points[#f.points] end
    local function moveTo(map, x, y)
        client.player.map, client.player.x, client.player.y = map, x, y
        client.Wait(4)
    end
    local function mobLines(name, npc)
        local lines = {}
        for i, line in ipairs(client.HoverUnit(name, npc)) do
            if i > 1 then table.insert(lines, plain(line.text)) end
        end
        return lines
    end
    -- /reload: Lua state, frames and loaded slots reset; saved variables don't.
    local function Reload()
        for _, t in ipairs({ client.loaded, client.tickers, client.timers, client.frames, client.chat, client.sounds,
                             client.fontStrings, client.opened, client.tooltipPostCalls }) do
            wipe(t)
        end
        WorldMapFrame.providers = {}
        wipe(WorldMapFrame.pins)
        WowMCPRouteNavigator, WowMCPPlanFrame, WowMCPGoalsFrame = nil, nil, nil
        return client.LoadAddon()
    end
    local function noErrors() assert(not has("Goal error") and not has("Plan error"), chat()) end
"""
)


@pytest.fixture
def game(addons, run_client, tmp_path, monkeypatch):
    script = tmp_path / "publish_goals.py"
    script.write_text(PUBLISH_SCRIPT)
    monkeypatch.setenv("WOWMCP_PUBLISH", f"{sys.executable} {script}")
    return lambda lua: run_client(PRELUDE + lua)


def test_goal_tracks_itself_and_completes(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish(HIDES)
        client.Wait(5) -- the goal, and its first look at the zone a tick later
        assert(has("New goal: 6 Medium Hide (Medium Hide 0/6)"), chat())

        -- The window: on its own (there's no plan), one row with the count.
        local f = WowMCPGoalsFrame
        assert(f and f:IsShown() and f.Title.text == "Goals" and plain(f.Count.text) == "0/1 done")
        assert(anchor(f)[2] == UIParent and #rows() == 1)
        assert(plain(row(1).Text.text) == "6 Medium Hide" and plain(row(1).Progress.text) == "0/6" and not row(1).Check:IsShown())

        -- Two hides looted close together: one line.
        client.AddItem(4232, 1)
        client.Tick()
        client.AddItem(4232, 1)
        client.Wait(4)
        assert(count("Goal: Medium Hide") == 1 and last() == "WowMCP Goal: Medium Hide 2/6", chat())
        assert(plain(row(1).Progress.text) == "2/6")
        client.AddItem(4232, 3)
        client.Wait(4)
        assert(last() == "WowMCP Goal: Medium Hide 5/6", last())

        -- Done: a sound and a line; it stays done, and shown, after the hides are crafted with.
        client.AddItem(4232, 1)
        client.Wait(4)
        assert(last() == "WowMCP Goal complete: 6 Medium Hide" and client.sounds[#client.sounds] == 23404, chat())
        assert(not has("Medium Hide 6/6"), "completion replaces the progress line")
        assert(row(1).Check:IsShown() and plain(row(1).Progress.text) == "Done" and plain(f.Count.text) == "1/1 done")
        local lines = #client.chat
        client.RemoveItem(4232, 6)
        client.Wait(4)
        assert(#client.chat == lines and row(1).Check:IsShown() and WowMCPDB.goals.state["medium-hide"].done)

        -- Selling some doesn't say anything either (only progress is told).
        noErrors()
    """)


def test_plan_step_waits_for_a_goal_and_the_window_docks_under_the_plan(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish(ALL)
        client.Wait(4)
        local g = WowMCPGoalsFrame
        assert(anchor(g)[2] == UIParent and #rows() == 4)
        client.Publish(BELT_PLAN)
        client.Wait(4)

        -- Docked under the plan window while that's shown.
        local p = WowMCPPlanFrame
        local a = anchor(g)
        assert(a[1] == "TOPLEFT" and a[2] == p and a[3] == "BOTTOMLEFT", tostring(a[2]))

        -- The step waiting for the goal shows its progress, and ticks off when the goal is done.
        client.AddItem(2321, 2)
        client.Wait(4)
        assert(p.Count.text == "2/3", p.Count.text)
        local progress
        for _, r in ipairs(p.Rows) do
            if r:IsShown() and r.n == 2 then progress = plain(r.Progress.text) end
        end
        assert(progress == "Goal: 6 Medium Hide - Medium Hide 0/6", progress)
        client.AddItem(4232, 6)
        client.Wait(4)
        assert(p.Count.text == "3/3" and has(TICK .. " Step 2/3 done: Goal: 6 Medium Hide"), chat())
        assert(has("Goal complete: 6 Medium Hide"))
        -- The skill goal and the plan's last step finish together.
        client.SetSkill("Leatherworking", 75)
        client.Wait(4)
        assert(has("Goal complete: Leatherworking 75") and has("Plan complete: Leatherworking 60 to 75"), chat())

        -- Dragging the goals while docked moves the plan window, which saves where it went.
        g.OnDragStart(g)
        assert(p.moving and not g.moving)
        p:SetPoint("CENTER", UIParent, "CENTER", 40, 30)
        g.OnDragStop(g)
        assert(not p.moving and WowMCPDB.planWindow.point == "CENTER" and WowMCPDB.planWindow.x == 40)

        -- With the plan hidden, the goals stand alone; dragging them then saves their own position.
        SlashCmdList.WOWMCP("plan hide")
        client.Wait(3)
        a = anchor(g)
        assert(g:IsShown() and a[2] == UIParent and a[1] == "TOPLEFT" and a[4] == 24, tostring(a[2]))
        g.OnDragStart(g)
        assert(g.moving)
        g:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 300, -200)
        g.OnDragStop(g)
        assert(WowMCPDB.goalsWindow.x == 300 and WowMCPDB.goalsWindow.y == -200)
        SlashCmdList.WOWMCP("plan show")
        client.Wait(3)
        assert(anchor(g)[2] == p)
        SlashCmdList.WOWMCP("plan hide")
        client.Wait(3)
        a = anchor(g)
        assert(a[2] == UIParent and a[4] == 300 and a[5] == -200)
        noErrors()
    """)


def test_zones_and_areas_point_out_chances_once_per_visit(game):
    game(r"""
        client.LoadAddon()
        client.Enter(1454, 0.5, 0.5, "Orgrimmar", "Valley of Strength")
        client.Wait()
        client.Publish(ALL)
        client.Wait(4)
        assert(not has("Goal: Beasts"), "not in Durotar")

        -- Entering Durotar: the hides' zone. The silk has an area there, so it waits for that.
        client.Enter(1411, 0.5, 0.5, "Durotar", "Razor Hill")
        client.Wait(3)
        assert(count("Goal: Beasts here skin into Medium Hide (0/6)") == 1 and client.sounds[#client.sounds] == 3175, chat())
        assert(not has("Goal: Spiders"))
        moveTo(1411, 0.6, 0.4)
        assert(count("Goal: Spiders here drop Spider's Silk (0/2)") == 1, chat())

        -- Walking around the zone says nothing more, in the area or out of it.
        moveTo(1411, 0.5, 0.5)
        moveTo(1411, 0.6, 0.4)
        client.Enter(1411, 0.6, 0.41, "Durotar", "Spiders' Den")
        client.Wait(2)
        assert(count("Goal: Beasts") == 1 and count("Goal: Spiders") == 1, chat())

        -- A new visit to the zone points them out again.
        client.Enter(1454, 0.5, 0.5, "Orgrimmar", "Valley of Strength")
        client.Wait(2)
        client.Enter(1411, 0.6, 0.4, "Durotar", "Spiders' Den")
        client.Wait(3)
        assert(count("Goal: Beasts") == 2 and count("Goal: Spiders") == 2, chat())

        -- Done goals don't.
        client.AddItem(4232, 6)
        client.Wait(3)
        client.Enter(1454, 0.5, 0.5, "Orgrimmar", "Valley of Strength")
        client.Wait(2)
        client.Enter(1411, 0.5, 0.5, "Durotar", "Razor Hill")
        client.Wait(3)
        assert(count("Goal: Beasts") == 2, chat())
        noErrors()
    """)


def test_mobs_get_a_tooltip_line(game):
    game(r"""
        local ns = client.LoadAddon()
        client.Wait()
        client.Publish(HIDES)
        client.Wait(4)
        local lines = mobLines("Mottled Boar")
        assert(#lines == 1 and lines[1] == "Skin for Medium Hide: goal 0/6", lines[1])
        -- By NPC ID too (another name in another client language); by name case-insensitively.
        lines = mobLines("Tigre de Durotar", 3121)
        assert(#lines == 1 and lines[1] == "Skin for Medium Hide: goal 0/6")
        assert(#mobLines("mottled boar") == 1 and #mobLines("Kul Tiras Sailor") == 0 and #mobLines("Durotar Tiger", 9999) == 1)

        client.AddItem(4232, 2)
        client.Wait(4)
        assert(mobLines("Mottled Boar")[1] == "Skin for Medium Hide: goal 2/6")

        -- A secret name (restricted content) leaves the tooltip as it was.
        local tooltip = client.ShowTooltip({ type = Enum.TooltipDataType.Unit, guid = 5, lines = { { leftText = setmetatable({}, {
            __index = function() error("secret value") end }) } } })
        assert(#tooltip == 1)

        -- Muted or done goals add nothing.
        SlashCmdList.WOWMCP("goals mute 1")
        assert(#mobLines("Mottled Boar") == 0)
        SlashCmdList.WOWMCP("goals unmute 1")
        assert(#mobLines("Mottled Boar") == 1)
        client.AddItem(4232, 4)
        client.Wait(4)
        assert(#mobLines("Mottled Boar") == 0)
        noErrors()
    """)


def test_vendors_selling_what_goals_and_the_plan_need(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish(ALL)
        client.Wait(4)
        client.Publish(BELT_PLAN)
        client.Wait(4)
        local bombus = { name = "Bombus", npc = 3077, items = { { id = 4289, price = 50 }, { id = 2321, price = 100 },
            { id = 159, price = 25 }, { id = 4289, price = 50 } } }
        local before = #client.chat
        client.OpenMerchant(bombus)
        client.Wait(3)
        assert(count("Bombus sells Salt, your goal needs 3.") == 1, chat())
        assert(count("Bombus sells Fine Thread, your plan needs 2.") == 1, chat())
        assert(#client.chat == before + 2 and client.sounds[#client.sounds] == 3175, "nothing else is said")

        -- Once per visit; the next visit says it again, with what's still needed.
        client.Wait(4)
        assert(count("Bombus sells") == 2)
        client.CloseMerchant()
        client.AddItem(4289, 1)
        client.Wait(4)
        client.OpenMerchant(bombus)
        client.Wait(3)
        assert(count("Bombus sells Salt, your goal needs 2.") == 1, chat())

        -- Bought: nothing to say.
        client.AddItem(4289, 2)
        client.AddItem(2321, 2)
        client.CloseMerchant()
        client.Wait(4)
        client.OpenMerchant(bombus)
        client.Wait(3)
        assert(count("Bombus sells") == 4, chat())
        noErrors()
    """)


def test_quiet_in_combat_and_instances(game):
    game(r"""
        client.LoadAddon()
        client.Enter(1454, 0.5, 0.5, "Orgrimmar", "Valley of Strength")
        client.Wait()
        client.Publish(HIDES)
        client.Wait(4)

        -- Progress in a fight waits until the fight is over.
        client.EnterCombat()
        client.AddItem(4232, 1)
        client.Wait(4)
        assert(not has("Goal: Medium Hide 1/6"))
        client.AddItem(4232, 1)
        client.Wait(2)
        client.LeaveCombat()
        assert(count("Goal: Medium Hide") == 1 and last() == "WowMCP Goal: Medium Hide 2/6", chat())

        -- A zone entered mid-fight is pointed out after it, if the player is still there...
        client.EnterCombat()
        client.Enter(1411, 0.5, 0.5, "Durotar", "Razor Hill")
        client.Wait(3)
        assert(not has("Goal: Beasts"))
        client.LeaveCombat()
        assert(count("Goal: Beasts here skin into Medium Hide (2/6)") == 1, chat())
        -- ...but not if they've left it, or the fight took long.
        client.Enter(1454, 0.5, 0.5, "Orgrimmar", "Valley of Strength")
        client.Wait(2)
        client.EnterCombat()
        client.Enter(1411, 0.5, 0.5, "Durotar", "Razor Hill")
        client.Wait(2)
        client.Enter(1454, 0.5, 0.5, "Orgrimmar", "Valley of Strength")
        client.Wait(2)
        client.LeaveCombat()
        client.Wait(2)
        client.Enter(1411, 0.5, 0.5, "Durotar", "Razor Hill")
        client.EnterCombat()
        client.Wait(130)
        client.LeaveCombat()
        client.Wait(2)
        assert(count("Goal: Beasts") == 1, chat())

        -- In a dungeon: nothing, not even completion, until the player is out.
        client.EnterInstance("Ragefire Chasm")
        client.Wait(3)
        client.AddItem(4232, 4)
        client.Wait(4)
        assert(not has("Goal complete") and not has("Medium Hide 6/6"), chat())
        assert(WowMCPDB.goals.state["medium-hide"].done, "but it's tracked")
        local sounds = #client.sounds
        client.LeaveInstance(1454, 0.5, 0.5, "Orgrimmar", "Valley of Strength")
        client.Wait(2)
        assert(last() == "WowMCP Goal complete: 6 Medium Hide" and client.sounds[#client.sounds] == 23404, chat())
        assert(#client.sounds == sounds + 1)
        noErrors()
    """)


def test_goal_commands_and_mute(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        local mcp = SlashCmdList.WOWMCP
        mcp("goals")
        assert(last() == "WowMCP No goals. Ask your AI for some!")
        client.Publish(ALL)
        client.Wait(4)
        mcp("goals")
        assert(has("Goals (4)") and has("1. 6 Medium Hide - Medium Hide 0/6"), chat())
        assert(has("4. Leatherworking 75 - Leatherworking 60/75"), chat())

        -- Muted: no pings, in the list as muted.
        mcp("goals mute 1")
        assert(last():find("Muted goal: 6 Medium Hide", 1, true) and WowMCPDB.goals.state["medium-hide"].muted)
        assert(plain(row(1).Text.text) == "6 Medium Hide (muted)")
        client.AddItem(4232, 2)
        client.Enter(1454, 0.5, 0.5, "Orgrimmar", "Valley of Strength")
        client.Wait(2)
        client.Enter(1411, 0.5, 0.5, "Durotar", "Razor Hill")
        client.Wait(4)
        assert(not has("Medium Hide 2/6") and not has("Goal: Beasts"), chat())
        -- The list's right-click unmutes (and mutes); by id works too.
        local r = row(1)
        r.OnMouseUp(r, "RightButton")
        assert(not WowMCPDB.goals.state["medium-hide"].muted and last():find("Unmuted goal: 6 Medium Hide", 1, true))
        mcp("goals mute salt")
        assert(WowMCPDB.goals.state.salt.muted)
        mcp("goals unmute 2")
        mcp("goals mute 9")
        assert(last():find("No goal 9. /mcp goals lists them.", 1, true))

        -- The tooltip says what the goal is, where, and how to mute it.
        r.OnEnter(r)
        local tip = table.concat(client.tooltip.lines, "\n")
        assert(tip:find("6 Medium Hide", 1, true) and tip:find("Medium Hide 2/6", 1, true))
        assert(tip:find("In Durotar: Beasts", 1, true), tip)
        assert(tip:find("Mottled Boar (skin)", 1, true) and tip:find("Right-click: mute", 1, true), tip)

        -- Clear: the done ones, one, or all.
        mcp("goals clear")
        assert(last():find("No done goals to clear", 1, true))
        client.AddItem(4232, 4)
        client.Wait(4)
        mcp("goals clear")
        assert(last():find("Goal removed: 6 Medium Hide", 1, true) and #rows() == 3)
        mcp("goals clear 1")
        assert(last():find("Goal removed: 3 Salt", 1, true) and #rows() == 2)
        mcp("goals hide")
        assert(not WowMCPGoalsFrame:IsShown() and last():find("Goals hidden", 1, true))
        mcp("goals")
        assert(has("Goals (2) - hidden, /mcp goals show brings them back"))
        mcp("goals show")
        assert(WowMCPGoalsFrame:IsShown())
        WowMCPGoalsFrame.Collapse.OnClick(WowMCPGoalsFrame.Collapse)
        assert(#rows() == 0 and WowMCPGoalsFrame:IsShown() and plain(WowMCPGoalsFrame.Count.text) == "0/2 done")
        WowMCPGoalsFrame.Collapse.OnClick(WowMCPGoalsFrame.Collapse)
        assert(#rows() == 2)
        mcp("goals clear all")
        assert(last():find("Removed 2 goals.", 1, true) and not WowMCPGoalsFrame:IsShown())
        mcp("goals bogus")
        assert(last():find("/mcp goals [show, hide, mute <n>, unmute <n>, clear [<n>, all]]", 1, true))
        noErrors()
    """)


def test_new_goals_open_the_list(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        local mcp = SlashCmdList.WOWMCP
        client.Publish([[""" + lua_json({"goals": [HIDES]}) + r"""]])
        client.Wait(4)
        assert(WowMCPGoalsFrame:IsShown() and #rows() == 1)
        -- Hidden, and collapsed: the AI's next new goal shows the list again, open.
        WowMCPGoalsFrame.Collapse.OnClick(WowMCPGoalsFrame.Collapse)
        mcp("goals hide")
        assert(not WowMCPGoalsFrame:IsShown())
        client.Publish([[""" + lua_json({"goals": [SALT]}) + r"""]])
        client.Wait(4)
        assert(WowMCPGoalsFrame:IsShown() and #rows() == 2, "new goals open the list")
        assert(not WowMCPDB.goalsWindow.hidden and not WowMCPDB.goalsWindow.collapsed)
        -- The same goals sent again, nothing new: the player's choice stands.
        mcp("goals hide")
        client.Publish([[""" + lua_json({"goals": [SALT]}) + r"""]])
        client.Wait(4)
        assert(not WowMCPGoalsFrame:IsShown(), "nothing new: stays hidden")
        -- Nor does a new goal that's already done.
        client.AddItem(4289, 3)
        client.Publish([[""" + lua_json({"goals": [{**SALT, "id": "salt-again"}]}) + r"""]])
        client.Wait(4)
        assert(has("Goal already done: 3 Salt") and not WowMCPGoalsFrame:IsShown(), chat())
        noErrors()
    """)


def test_reload_keeps_goals_as_they_were(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish(ALL)
        client.Wait(4)
        client.AddItem(4232, 6)
        client.Wait(4)
        SlashCmdList.WOWMCP("goals mute 2")
        local g = WowMCPGoalsFrame
        g.OnDragStart(g)
        g:SetPoint("CENTER", UIParent, "CENTER", -100, 80)
        g.OnDragStop(g)
        g.Collapse.OnClick(g.Collapse)
        client.RemoveItem(4232, 6)

        Reload()
        client.Wait(4)
        g = WowMCPGoalsFrame
        assert(#client.chat == 0 and #client.sounds == 0, chat())
        local a = anchor(g)
        assert(g:IsShown() and a[1] == "CENTER" and a[4] == -100 and a[5] == 80 and #rows() == 0)
        g.Collapse.OnClick(g.Collapse)
        assert(row(1).Check:IsShown() and plain(row(1).Progress.text) == "Done", "done stays done without the hides")
        assert(plain(row(2).Text.text) == "3 Salt (muted)")
        -- Progress after the reload is told as usual, from where it was.
        client.AddItem(3182, 1)
        client.Wait(4)
        assert(last() == "WowMCP Goal: Spider's Silk 1/2", chat())

        -- A goal the AI changes starts over (and stays muted); the others keep their state.
        client.Publish([[""" + lua_json({"goals": [{**SALT, "title": "5 Salt", "target": {"have": 4289, "count": 5}}]}) + r"""]])
        client.Wait(4)
        local state = WowMCPDB.goals.state
        assert(has("New goal: 5 Salt (Salt 0/5)") and state.salt.muted and state["medium-hide"].done, chat())
        assert(not has("New goal: 6 Medium Hide"))
        noErrors()
    """)


def test_item_markup_shows(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish([[""" + lua_json({"goals": [{**HIDES, "title": "{item:4232} x6"}]}) + r"""]])
        client.Wait(4)
        -- Core.lua's item markup: the icon and the game's name in the list, on mob tooltips and in chat,
        -- where it's an item link.
        assert(plain(row(1).Text.text) == "Medium Hide x6" and row(1).Text.text:find("|T134255:0|t", 1, true),
            row(1).Text.text)
        assert(mobLines("Mottled Boar")[1] == "Skin for Medium Hide: goal 0/6")
        assert(has("New goal: Medium Hide x6"), chat())
        SlashCmdList.WOWMCP("goals")
        client.Tick()
        assert(has("1. Medium Hide x6 - Medium Hide 0/6"), chat())
        assert(table.concat(client.chat, "\n"):find("|Hitem:4232:", 1, true), "no item link in chat")
        noErrors()
    """)


def test_set_goals_payload(addons):
    result = goals.set_goals([goals.Goal(**HIDES), goals.Goal(**LW)])
    assert result.startswith(
        "Set 2 goal(s); 2 in game now:\n"
        "- medium-hide '6 Medium Hide': done when 6 x Medium Hide; pings in Durotar; on Mottled Boar (skin); on "
        "Durotar Tiger (skin); at vendors and the auction house selling it\n"
        "- lw-75 'Leatherworking 75': done when Leatherworking 75; pings nowhere in particular"
    ), result
    state = Transport(addons).load_state()
    first = state["seq"]
    assert state["goals"] == {"id": first, "goals": [
        {"id": "medium-hide", "title": "6 Medium Hide", "target": {"have": 4232, "count": 6, "bank": False, "name": "Medium Hide"},
         "zones": [{"zone": "Durotar", "note": "Beasts here skin into Medium Hide"}], "areas": [],
         "mobs": [{"name": "Mottled Boar", "npc": None, "note": "skin"}, {"name": "Durotar Tiger", "npc": 3121, "note": "skin"}],
         "note": "Skin everything while questing", "rev": first},
        {"id": "lw-75", "title": "Leatherworking 75", "target": {"skill": "Leatherworking", "rank": 75}, "zones": [],
         "areas": [], "mobs": [], "note": None, "rev": first},
    ]}
    inbox = (addons / "WowMCP" / "Inbox.lua").read_text()
    assert 'id = "medium-hide",' in inbox and "npc = 3121," in inbox

    # Merged by id: an unchanged goal keeps its rev (and its progress in game), a changed one gets a new one.
    goals.set_goals([goals.Goal(**HIDES), goals.Goal(**{**LW, "target": {"skill": "Leatherworking", "rank": 80}}),
                     goals.Goal(**SILK)])
    state = Transport(addons).load_state()
    revs = {g["id"]: g["rev"] for g in state["goals"]["goals"]}
    assert revs == {"medium-hide": first, "lw-75": state["seq"], "silk": state["seq"]}
    assert state["goals"]["goals"][2]["areas"] == [
        {"zone": "Durotar", "x": 60, "y": 40, "radius": 5, "label": "Spiders here drop Spider's Silk", "mapID": None}]

    goals.set_goals([goals.Goal(**SALT)], replace=True)
    assert [g["id"] for g in Transport(addons).load_state()["goals"]["goals"]] == ["salt"]

    with pytest.raises(ToolError, match="Each goal needs its own id"):
        goals.set_goals([goals.Goal(**SALT), goals.Goal(**SALT)])
    many = [goals.Goal(**{**SALT, "id": f"salt-{n}"}) for n in range(10)]
    with pytest.raises(ToolError, match="That makes 11 goals; at most 10"):
        goals.set_goals(many)


@pytest.mark.parametrize("goal, error", [
    ({**SALT, "id": "Salt Goal"}, "String should match pattern"),
    ({**SALT, "target": {"zone": "Durotar"}}, "A goal's target needs exactly one of these keys: have, skill"),
    ({**SALT, "target": {"goal": "salt"}}, "A goal's target needs exactly one of these keys"),
    ({**SALT, "target": {"have": 4289, "skill": "Cooking"}}, "A goal's target needs exactly one"),
    ({**SALT, "mobs": [{"name": "Boar", "colour": "red"}]}, "Extra inputs are not permitted"),
    ({**SALT, "areas": [{"zone": "Durotar", "x": 50, "y": 50}]}, "label"),
])
def test_goals_are_checked(goal, error):
    with pytest.raises(ValidationError, match=error):
        goals.Goal(**goal)


def test_plans_wait_for_goals_that_are_set(addons):
    step = plans.Step(text="Goal: 6 Medium Hide", conditions=[{"goal": "medium-hide"}])
    with pytest.raises(ToolError, match="Step 1 waits for goal 'medium-hide', which isn't set: call set_goals first"):
        asyncio.run(plans.show_plan("Belts", [step]))
    goals.set_goals([goals.Goal(**HIDES)])
    result = asyncio.run(plans.show_plan("Belts", [step, plans.Step(text="Craft")]))
    assert "1. Goal: 6 Medium Hide [done when: goal 'medium-hide' done]" in result
    assert "1 step(s) have nothing the game can check" in result
    (sent,) = plans.sent_plans(Transport(addons).load_state())
    assert sent["steps"][0]["conditions"] == [{"goal": "medium-hide"}]
    with pytest.raises(ValidationError, match="String should match pattern"):
        plans.Step(text="x", conditions=[{"goal": "Medium Hide"}])

    # Clearing a goal a step waits for says so.
    result = goals.clear_goals(ids=["medium-hide"])
    assert result.startswith("Removed 1 goal(s): medium-hide. The plan's step(s) 1 waited for them"), result
    with pytest.raises(ToolError, match="No goal 'nope'"):
        goals.clear_goals(ids=["nope"])
    with pytest.raises(ToolError, match="Give the ids"):
        goals.clear_goals()
    goals.set_goals([goals.Goal(**SALT), goals.Goal(**LW)])
    assert goals.clear_goals(all=True) == "Removed 2 goal(s): salt, lw-75."
    assert Transport(addons).load_state()["goals"]["goals"] == []


def run(coro):
    return asyncio.run(coro)


def test_get_goals_notes(addons, monkeypatch):
    reply = {}

    async def request(kind):
        return reply, "World of Warcraft: Forever 1.60.1"

    monkeypatch.setattr(goals, "request", request)
    goals.set_goals([goals.Goal(**HIDES)])
    reply["goals"] = {"id": 0, "goals": {}}
    progress = run(goals.get_goals())
    assert progress.goals == [] and "hasn't received the newest goals" in progress.note and "no goals in game" in progress.note
    sent = Transport(addons).load_state()["goals"]["id"]
    reply["goals"] = {"id": sent, "hidden": True, "problem": "secret value", "goals": [
        {"id": "medium-hide", "rev": sent, "met": True, "done": 1767225600, "progress": "Medium Hide 6/6", "muted": True},
        {"id": "old", "met": False, "progress": "Salt 0/3", "cleared": True},
    ]}
    progress = run(goals.get_goals())
    assert progress.hidden and progress.note == "A goal couldn't be checked in game: secret value"
    hides, old = progress.goals
    assert (hides.title, hides.target, hides.done, hides.met_now, hides.muted) == (
        "6 Medium Hide", "6 x Medium Hide", True, True, True)
    assert hides.done_at.year == 2026 and (old.title, old.target, old.cleared) == (None, None, True)
    del reply["goals"]
    with pytest.raises(ToolError, match="doesn't send goals yet"):
        run(goals.get_goals())


def test_goal_tools_live(live_game, tmp_path):
    loot = tmp_path / "loot"
    setup = f"""
        -- The player skins once the test says so.
        C_Timer.NewTicker(0.5, function()
            local f = io.open([[{loot}]])
            if f and not client.looted then
                f:close()
                client.looted = true
                client.AddItem(4232, 6)
            end
        end)
    """
    with live_game(setup, seconds=20):
        result = goals.set_goals([goals.Goal(**HIDES), goals.Goal(**LW)])
        assert result.startswith("Set 2 goal(s); 2 in game now"), result
        time.sleep(0.5)
        progress = run(goals.get_goals())
        assert progress.game.startswith("World of Warcraft: Forever") and progress.note is None
        assert [g.model_dump(exclude={"done_at"}) for g in progress.goals] == [
            {"id": "medium-hide", "title": "6 Medium Hide", "target": "6 x Medium Hide", "progress": "Medium Hide 0/6",
             "done": False, "met_now": False, "muted": False, "cleared": False},
            {"id": "lw-75", "title": "Leatherworking 75", "target": "Leatherworking 75",
             "progress": "Leatherworking 60/75", "done": False, "met_now": False, "muted": False, "cleared": False},
        ]

        loot.write_text("")
        for _ in range(30):
            progress = run(goals.get_goals())
            if progress.goals[0].done:
                break
            time.sleep(0.2)
        hides = progress.goals[0]
        assert hides.done and hides.met_now and hides.progress == "Medium Hide 6/6" and hides.done_at is not None

        goals.clear_goals(ids=["medium-hide"])
        time.sleep(0.5)
        progress = run(goals.get_goals())
        assert [g.id for g in progress.goals] == ["lw-75"]
