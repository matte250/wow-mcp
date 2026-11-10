"""Items in text from the AI: {item:<id>} and {item:<id>:<name>} shown with the item's icon and name
(Core.lua's ns.ItemText, used by every feature that shows AI text; wow_mcp/item_text.py).

Stub items (stub/apis/inventory.lua): Light Leather 2318, Coarse Thread 2320 and Medium Hide 4232 are
common (white), Ruined Leather Scraps 2934 poor (grey); Medium Leather 2319 takes half a second to load.
The bank holds 10 Light Leather. The player is in Durotar.
"""

import json
import sys
import typing

from pydantic.fields import FieldInfo

from wow_mcp import item_text, server
from wow_mcp.tools import areas, messages, plans, reminders, routes

# How a Lua test script hands control back to publish through the real tools.
PUBLISH_SCRIPT = """
import asyncio, json, sys
from wow_mcp import game
from wow_mcp.tools import areas, item_notes, map, messages, plans, reminders, routes
game.running_folders = lambda proc=None: set()  # the stub game waits for this script: it can't check item IDs
args = json.loads(sys.argv[1])
if "plan" in args:
    asyncio.run(plans.show_plan(args["plan"]["title"], [plans.Step(**s) for s in args["plan"]["steps"]]))
elif "message" in args:
    messages.show_message(**args["message"])
elif "route" in args:
    routes.show_route([routes.Stop(**s) for s in args["route"]["stops"]], title=args["route"]["title"], open_map=False)
elif "areas" in args:
    areas.highlight_areas([areas.Area(**a) for a in args["areas"]["areas"]], title=args["areas"]["title"],
                          open_map=False)
elif "reminder" in args:
    asyncio.run(reminders.set_reminder(**args["reminder"]))
elif "markers" in args:
    map.show_on_map([map.Marker(**m) for m in args["markers"]], open_map=False)
elif "notes" in args:
    item_notes.set_item_notes([item_notes.ItemNote(**n) for n in args["notes"]])
"""

PRELUDE = r"""
    local PREFIX = "|cff33ff99WowMCP|r "
    local WHITE, GREY = "|cffffffff", "|cff9d9d9d"
    local ICON = { [2318] = "|T134252:0|t ", [2319] = "|T134253:0|t ", [2934] = "|T134254:0|t ",
                   [2320] = "|T132889:0|t ", [4232] = "|T134255:0|t " }
    -- An item link as the game makes it (stub/apis/inventory.lua).
    local function link(id, name, color)
        return string.format("%s|Hitem:%d::::::::12:::::::::|h[%s]|h|r", color or WHITE, id, name)
    end
    local function chat() return table.concat(client.chat, "\n") end
    local function last() return client.chat[#client.chat] end
    local function has(text) return chat():find(text, 1, true) ~= nil end
"""


def lua_json(value):
    """JSON for client.Publish, which passes it through the shell in single quotes."""
    return json.dumps(value).replace("'", "\\u0027")


def publish(**what):
    return f"[[{lua_json(what)}]]"


def test_item_tokens(run_client):
    run_client(PRELUDE + r"""
        local ns = client.LoadAddon()
        client.Wait()

        -- The icon, then the game's name in the item's quality colour; a name given is only a fallback.
        assert(ns.ItemText("Craft 5 {item:2318}") == "Craft 5 " .. ICON[2318] .. WHITE .. "Light Leather|r")
        assert(ns.ItemText("{item:2934:Scraps} x3") == ICON[2934] .. GREY .. "Ruined Leather Scraps|r x3")
        assert(ns.ItemText("{item:2318:}") == ICON[2318] .. WHITE .. "Light Leather|r")

        -- Items the game doesn't know: the name given, else the token as written. Never an error.
        assert(ns.ItemText("{item:999999:Recipe: Mystery}") == "Recipe: Mystery")
        assert(ns.ItemText("{item:999999}") == "{item:999999}")
        for _, odd in ipairs({ "{item:abc}", "{item:12abc}", "{item:}", "{item:0}", "{item:99999999999999999999}",
                               "{ item:2318 }", "{item:2318", "item:2318", "{item:2318 }", "{item:-5}" }) do
            assert(ns.ItemText(odd) == odd, odd)
        end

        -- The only escape sequences are the ones made for items: the AI's are escaped, and a bar ends a token.
        assert(ns.ItemText("|cffff0000Red|r {item:2318}") == "||cffff0000Red||r " .. ICON[2318] .. WHITE .. "Light Leather|r")
        assert(ns.ItemText("{item:2318:|cffff0000Evil}") == "{item:2318:||cffff0000Evil}")
        assert(ns.ItemText("|Hitem:6948|h[Free]|h {item:999999:|T1:0|t}") == "||Hitem:6948||h[Free]||h {item:999999:||T1:0||t}")
        assert(ns.ItemText("|{item:2318}|") == "||" .. ICON[2318] .. WHITE .. "Light Leather|r||")
        assert(ns.ItemText("|T{item:2318}") == "||T" .. ICON[2318] .. WHITE .. "Light Leather|r")

        -- Text that's already escaped, inside the addon's own codes; an item's icon on its own.
        assert(ns.RenderItems("|cffaaaaaaNext: " .. ns.Escape("{item:2320}|") .. "|r")
            == "|cffaaaaaaNext: " .. ICON[2320] .. WHITE .. "Coarse Thread|r|||r")
        assert(ns.ItemIcon(2318) == ICON[2318] and ns.ItemIcon(999999) == "" and ns.ItemIcon(nil) == "")

        -- In chat: the game's item link after the icon. Inside one of the addon's own links, which can't
        -- hold another, it shows as it does outside chat.
        ns.Print("Take {item:2318} and {item:2934:Scraps}")
        assert(last() == PREFIX .. "Take " .. ICON[2318] .. link(2318, "Light Leather") .. " and "
            .. ICON[2934] .. link(2934, "Ruined Leather Scraps", GREY), last())
        ns.Print("|Haddon:WowMCP:route:1|h|cffffd100[Buy {item:2318}]|r|h, then {item:2318}")
        assert(last() == PREFIX .. "|Haddon:WowMCP:route:1|h|cffffd100[Buy " .. ICON[2318] .. WHITE
            .. "Light Leather|r]|r|h, then " .. ICON[2318] .. link(2318, "Light Leather"), last())
        ns.Print("||Hitem:1||h {item:2318:x||y}")
        assert(last() == PREFIX .. "||Hitem:1||h {item:2318:x||y}", last())
        ns.Print("Plain text stays as it was")
        assert(last() == PREFIX .. "Plain text stays as it was")
    """)


def test_items_that_load_later(run_client):
    run_client(PRELUDE + r"""
        local ns = client.LoadAddon()
        client.Wait()
        local redraws = 0
        ns.OnItemsLoaded(function() redraws = redraws + 1 end)

        -- Medium Leather isn't loaded yet: its icon shows at once, with the name given until it is.
        assert(ns.ItemText("{item:2319:Medium Leather}") == ICON[2319] .. "Medium Leather")
        assert(ns.ItemText("{item:2319}") == ICON[2319] .. "item 2319")

        -- A chat line naming it waits for it, and the lines after it wait their turn.
        local before = #client.chat
        ns.Print("Bank: {item:2319:Med. Leather}")
        ns.Print("Then the vendor")
        assert(#client.chat == before, chat())
        client.Tick()
        assert(#client.chat == before + 2, chat())
        assert(client.chat[before + 1] == PREFIX .. "Bank: " .. ICON[2319] .. link(2319, "Medium Leather"), chat())
        assert(client.chat[before + 2] == PREFIX .. "Then the vendor")

        -- What shows it is told once, to show it again with the game's name.
        assert(redraws == 1, redraws)
        assert(ns.ItemText("{item:2319}") == ICON[2319] .. WHITE .. "Medium Leather|r")

        -- One that takes longer than a second: the line shows anyway, with the name given and a link the
        -- game fills in when it's clicked.
        client.items[4232].uncached, client.items[4232].loadTime = true, 5
        ns.Print("Skin a {item:4232:Medium Hide}")
        client.Tick()
        assert(last() == PREFIX .. "Then the vendor")
        client.Tick()
        assert(last() == PREFIX .. "Skin a " .. ICON[4232] .. "|Hitem:4232::::::::::::::::|h[Medium Hide]|h", last())
        client.Wait(8)
        assert(redraws == 2 and ns.ItemText("{item:4232}") == ICON[4232] .. WHITE .. "Medium Hide|r")

        -- Items the game says don't exist: as written, as soon as it says so.
        ns.Print("{item:999999:Mystery Ore}, {item:888888}")
        client.Tick()
        assert(last() == PREFIX .. "Mystery Ore, {item:888888}", last())
        assert(redraws == 3)
    """)


LEATHER = {
    "title": "Leatherworking 113 to 125",
    "steps": [
        {
            "text": "Bank: take out {item:2318:Light Leather} (37), {item:2319:Medium Leather} (18), "
                    "{item:2934:Ruined Leather Scraps} (16), {item:2320:Coarse Thread} (21) and the "
                    "{item:4232:Medium Hide}.",
            "places": [{"zone": "Orgrimmar", "x": 50, "y": 50, "label": "Bank: {item:2318}"}],
            "conditions": [{"have": 2318, "count": 37, "bank": True},
                           {"have": 2319, "count": 18, "bank": True, "name": "Medium Leather"}],
        },
        {"text": "Craft 5 {item:2318} from your 15 {item:2934}", "conditions": [{"have": 2318, "count": 42}]},
    ],
}


def test_plan_window(run_client, tmp_path, monkeypatch):
    script = tmp_path / "publish_items.py"
    script.write_text(PUBLISH_SCRIPT)
    monkeypatch.setenv("WOWMCP_PUBLISH", f"{sys.executable} {script}")
    run_client(PRELUDE + f"local LEATHER = {publish(plan=LEATHER)}\n" + r"""
        local function row(n)
            for _, r in ipairs(WowMCPPlanFrame.Rows) do if r:IsShown() and r.n == n then return r end end
        end
        client.LoadAddon()
        client.Wait()
        client.items[4232].uncached, client.items[4232].loadTime = true, 4 -- loads after the checks below
        client.Publish(LEATHER)
        client.Wait(5)

        -- The step as written, each item with its icon and the game's name in its quality colour; the Medium
        -- Hide is still loading, so it has the name given for now.
        assert(WowMCPPlanFrame.Title.text == "Leatherworking 113 to 125")
        local text = row(1).Text.text
        assert(text:find("Bank: take out " .. ICON[2318] .. WHITE .. "Light Leather|r (37), " .. ICON[2319] .. WHITE
            .. "Medium Leather|r (18), " .. ICON[2934] .. GREY .. "Ruined Leather Scraps|r (16), " .. ICON[2320] .. WHITE
            .. "Coarse Thread|r (21) and the " .. ICON[4232] .. "Medium Hide.|r", 1, true), text)
        assert(row(2).Text.text:find("Craft 5 " .. ICON[2318] .. WHITE .. "Light Leather|r from your 15 " .. ICON[2934]
            .. GREY .. "Ruined Leather Scraps|r", 1, true), row(2).Text.text)

        -- Progress on the items to have: the addon knows their IDs, so their icons come first.
        local progress = row(1).Progress.text
        assert(progress:find(ICON[2318] .. "Light Leather 10/37 (with bank)", 1, true), progress)
        assert(progress:find(ICON[2319] .. "Medium Leather 0/18 (with bank)", 1, true), progress)

        -- Hovering the step: its text, its place and its progress, with the items.
        local tip = {}
        GameTooltip.SetText = function(_, line) tip = { line } end
        GameTooltip.AddLine = function(_, line) table.insert(tip, line) end
        row(1).OnEnter(row(1))
        assert(tip[1]:find("1. Bank: take out " .. ICON[2318] .. WHITE .. "Light Leather|r (37)", 1, true) == 1, tip[1])
        assert(tip[2] == "Bank: " .. ICON[2318] .. WHITE .. "Light Leather|r - Orgrimmar 50.0, 50.0", tip[2])
        assert(tip[3] == ICON[2318] .. "Light Leather 10/37 (with bank)", tip[3])

        -- In chat, item links; a line waits up to a second for an item that's still loading.
        SlashCmdList.WOWMCP("plan")
        client.Wait(2)
        assert(has("1. Bank: take out " .. ICON[2318] .. link(2318, "Light Leather") .. " (37), "), chat())
        assert(has(" and the " .. ICON[4232] .. "|Hitem:4232::::::::::::::::|h[Medium Hide]|h."), chat())
        assert(has("2. Craft 5 " .. ICON[2318] .. link(2318, "Light Leather") .. " from your 15 " .. ICON[2934]
            .. link(2934, "Ruined Leather Scraps", GREY)), chat())

        -- Once the Medium Hide has loaded, the window has the game's name for it.
        client.Wait(6)
        assert(row(1).Text.text:find(ICON[4232] .. WHITE .. "Medium Hide|r.", 1, true), row(1).Text.text)
        assert(not has("Plan error"), chat())
    """)


def test_messages_and_reminders(run_client, tmp_path, monkeypatch):
    script = tmp_path / "publish_items.py"
    script.write_text(PUBLISH_SCRIPT)
    monkeypatch.setenv("WOWMCP_PUBLISH", f"{sys.executable} {script}")
    message = publish(message={"text": "Sell your {item:2934} and skin a {item:4232:Medium Hide}", "style": "both"})
    reminder = publish(reminder={"text": "Sell your {item:2934}", "zone": "Durotar"})
    run_client(PRELUDE + f"local MESSAGE, REMINDER = {message}, {reminder}\n" + r"""
        client.LoadAddon()
        client.Wait()
        client.items[4232].uncached, client.items[4232].loadTime = true, 3
        client.Publish(MESSAGE)
        client.Wait()

        -- On screen at once: the icons, and the name given while the Medium Hide loads.
        local center = client.CenterText()
        assert(center[1] == "Sell your " .. ICON[2934] .. GREY .. "Ruined Leather Scraps|r and skin a " .. ICON[4232]
            .. "Medium Hide", center[1])
        -- In chat within a second: item links, the one still loading with the name given.
        client.Wait(2)
        assert(last() == "|cff33ff99[Claude]|r Sell your " .. ICON[2934] .. link(2934, "Ruined Leather Scraps", GREY)
            .. " and skin a " .. ICON[4232] .. "|Hitem:4232::::::::::::::::|h[Medium Hide]|h", last())
        -- Once it has loaded, the screen shows the game's name.
        client.Wait(4)
        assert(client.CenterText()[1]:find(ICON[4232] .. WHITE .. "Medium Hide|r", 1, true), client.CenterText()[1])

        -- A reminder: when it's set and when it fires (on screen and in chat).
        client.Publish(REMINDER)
        client.Wait(4)
        local scraps = ICON[2934] .. link(2934, "Ruined Leather Scraps", GREY)
        assert(has(PREFIX .. "Reminder set: Sell your " .. scraps .. " |cffaaaaaa(in Durotar)|r"), chat())
        assert(has("[Claude]|r |cffffd100Reminder:|r Sell your " .. scraps), chat())
        center = client.CenterText()
        assert(center[#center] == "|cffffd100Reminder:|r Sell your " .. ICON[2934] .. GREY .. "Ruined Leather Scraps|r",
            center[#center])
    """)


def test_routes_areas_markers_and_notes(run_client, tmp_path, monkeypatch):
    script = tmp_path / "publish_items.py"
    script.write_text(PUBLISH_SCRIPT)
    monkeypatch.setenv("WOWMCP_PUBLISH", f"{sys.executable} {script}")
    route = publish(route={"title": "Errands", "stops": [
        {"zone": "Durotar", "x": 60, "y": 50, "label": "Buy {item:2320:Coarse Thread}", "note": "And {item:2318}"},
        {"zone": "Durotar", "x": 60, "y": 40, "label": "Kaltunk"}]})
    area = publish(areas={"title": "Where {item:2934} drops", "areas": [
        {"zone": "Durotar", "x": 50, "y": 50, "radius": 10, "label": "{item:2934:Scraps}", "color": "red"}]})
    markers = publish(markers=[{"zone": "Durotar", "x": 52, "y": 41, "label": "Sells {item:2320}"}])
    notes = publish(notes=[{"item_id": 2589, "text": "Keep for {item:2320}"}])
    run_client(PRELUDE + f"local ROUTE, AREA, MARKERS, NOTES = {route}, {area}, {markers}, {notes}\n" + r"""
        client.LoadAddon()
        client.Wait()
        client.Publish(ROUTE)
        client.Wait()

        -- The navigator, sized to what shows: colour codes take no room, an icon is as wide as the line is high.
        local nav = WowMCPRouteNavigator
        local thread = ICON[2320] .. WHITE .. "Coarse Thread|r"
        assert(nav.Stop.text == "Buy " .. thread, nav.Stop.text)
        assert(nav.width == 58 + 7 * #"Buy  Coarse Thread" + 12 + 10, nav.width)
        -- The stop on the map, and in chat inside its own link (so without an item link).
        local stop = client.Pins("WowMCPRouteStopPinTemplate")[1]
        assert(stop.Label.text == "Buy " .. thread)
        assert(has("1. |Haddon:WowMCP:route:1|h|cffffd100[Buy " .. thread .. "]|r|h Durotar 60.0, 50.0"), chat())

        client.Publish(AREA)
        client.Wait()
        local scraps = ICON[2934] .. GREY .. "Ruined Leather Scraps|r"
        assert(client.Pins("WowMCPAreaPinTemplate")[1].Label.text == scraps)
        assert(has(PREFIX .. "|cffffffffWhere " .. ICON[2934] .. link(2934, "Ruined Leather Scraps", GREY) .. " drops|r"),
            chat())
        assert(has(PREFIX .. "|cffff4d4d" .. ICON[2934] .. link(2934, "Ruined Leather Scraps", GREY) .. "|r Durotar 50.0, 50.0"),
            chat())

        client.Publish(MARKERS)
        client.Wait()
        assert(client.Pins("WowMCPPinTemplate")[1].Label.text == "Sells " .. thread)
        assert(has("|Haddon:WowMCP:nav:1|h|cffffd100[Sells " .. thread .. "]|r|h"), chat())

        client.Publish(NOTES)
        client.Wait()
        local lines = client.ShowTooltip({ type = Enum.TooltipDataType.Item, id = 2589, lines = { { leftText = "Linen Cloth" } } })
        assert(lines[2].text == "|cff33ff99AI:|r Keep for " .. thread, lines[2].text)
    """)


def test_markup_matches_the_addon(run_client):
    """The server's idea of an item token (wow_mcp/item_text.py) is the addon's."""
    samples = ["{item:2318}", "{item:2318:Light Leather}", "{item:2318:Pattern: Thing}", "{item:2318:}", "{item:abc}",
               "{item:12abc}", "{item:2318:a|b}", "{item:2318:a{b}", "{Item:2318}", "{item: 2318}", "{item:2318 }",
               "{item:2318:two\nlines}"]
    table = ", ".join(f'{{ {json.dumps(s)}, {str(bool(item_text.ITEM.fullmatch(s))).lower()} }}' for s in samples)
    run_client(f"local SAMPLES = {{ {table} }}\n" + r"""
        local ns = client.LoadAddon()
        client.Wait()
        for _, sample in ipairs(SAMPLES) do
            local shown = ns.ItemText(sample[1]) ~= ns.Escape(sample[1])
            assert(shown == sample[2], sample[1])
        end
    """)


def described(fn, param):
    hint = typing.get_type_hints(fn, include_extras=True)[param]
    return next(m.description for m in hint.__metadata__ if isinstance(m, FieldInfo))


def test_agents_are_told(addons):
    assert item_text.GUIDANCE in server.create_server().instructions
    for model, field in [(routes.Stop, "label"), (routes.Stop, "note"), (areas.Area, "label"), (plans.Step, "text")]:
        assert item_text.HINT in model.model_fields[field].description, (model, field)
    for fn, param in [(messages.show_message, "text"), (plans.show_plan, "title"), (routes.show_route, "title"),
                      (areas.highlight_areas, "title"), (reminders.set_reminder, "text")]:
        assert item_text.HINT in described(fn, param), fn


def test_shorten_keeps_items_whole():
    text = "Bring " + "{item:2318:Light Leather} " * 3
    assert item_text.shorten(text, 200) == text
    assert item_text.shorten(text, 40) == "Bring {item:2318:Light Leather} …"
    assert item_text.shorten(text, 32) == "Bring {item:2318:Light Leather}…"
    assert item_text.shorten(text, 20) == "Bring …"
    assert item_text.shorten("x" * 50, 10) == "x" * 9 + "…"
