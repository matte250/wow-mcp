"""Routes on the world map and the navigator arrow (Routes.lua, Routes.xml, tools/routes.py).

Stub geometry (stub/apis/map.lua): Durotar spans world x 500-700, y 300-600, so 1% of its map is
2 yards across and 3 yards down; Orgrimmar spans x 520-600, y 300-380. The player starts in
Durotar at 50, 50 (world 600, 450), facing north.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from pydantic import ValidationError

from wow_mcp.tools import routes
from wow_mcp.transport import Transport

ADDON = Path(__file__).parents[2] / "addon" / "WowMCP"

# How a Lua test script hands control back to publish through the real tools.
PUBLISH_SCRIPT = """
import json, sys
from wow_mcp.tools import map, routes
args = json.loads(sys.argv[1])
if "markers" in args:
    map.show_on_map([map.Marker(**m) for m in args["markers"]])
elif args.get("clear"):
    routes.clear_route()
else:
    routes.show_route([routes.Stop(**s) for s in args["stops"]], title=args.get("title"),
                      loop=args.get("loop", False), open_map=args.get("open_map", True))
"""

# Stop 1 is 20 yards east of the player, stop 2 30 yards north of it, stop 3 in Orgrimmar.
TURN_INS = (
    '{"title":"Turn-ins","stops":['
    '{"zone":"Durotar","x":60,"y":50,"label":"Gornek","note":"In the den"},'
    '{"zone":"Durotar","x":60,"y":40,"label":"Kaltunk"},'
    '{"zone":"Orgrimmar","x":50,"y":50,"label":"Thrall"}]}'
)

PRELUDE = f"""
    local TURN_INS = '{TURN_INS}'
    local STOP, LINE = "WowMCPRouteStopPinTemplate", "WowMCPRouteLinePinTemplate"
    local function near(a, b) return math.abs(a - b) < 1e-6 end
    local function chat() return table.concat(client.chat, "\\n") end
    local function moveTo(map, x, y)
        client.player.map, client.player.x, client.player.y = map, x, y
        client.Tick()
    end
"""


@pytest.fixture
def run_route(addons, run_client, tmp_path, monkeypatch):
    script = tmp_path / "publish_routes.py"
    script.write_text(PUBLISH_SCRIPT)
    monkeypatch.setenv("WOWMCP_PUBLISH", f"{sys.executable} {script}")
    return lambda lua: run_client(PRELUDE + lua)


def test_route_on_the_map(run_route):
    run_route(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish(TURN_INS)
        client.Wait()

        -- Opened on the first stop, which gets the game's waypoint.
        assert(client.opened[1] == 1411 and client.waypoint.uiMapID == 1411)
        local wx, wy = client.waypoint.position:GetXY()
        assert(near(wx, 0.6) and near(wy, 0.5))

        -- Durotar: all three stops (Orgrimmar lies inside it), labels only on their own map.
        local stops, lines = client.Pins(STOP), client.Pins(LINE)
        assert(#stops == 3 and #lines == 2, #stops .. " stops, " .. #lines .. " lines")
        assert(stops[1].Number.text == "1" and stops[1].Label.text == "Gornek")
        assert(stops[3].Label.text == "" and near(stops[3].x, 0.3) and near(stops[3].y, 40 / 300))
        assert(stops[1].Fill.color[2] > 0.5 and stops[2].Fill.color[3] > 0.5, "current stop green, the rest blue")

        -- Lines run between the stops on the canvas (1002 x 668), 3 px wide at any zoom.
        local leg = lines[1].Fill
        assert(leg.from.relativeTo == WorldMapFrame:GetCanvas() and leg.from.point == "TOPLEFT" and leg.to.point == "TOPLEFT")
        assert(near(leg.from.x, 0.6 * 1002) and near(leg.from.y, -0.5 * 668))
        assert(near(leg.to.x, 0.6 * 1002) and near(leg.to.y, -0.4 * 668))
        assert(near(lines[2].Fill.to.x, 0.3 * 1002) and near(lines[2].Fill.to.y, -40 / 300 * 668))
        assert(leg.thickness == 3)
        client.SetCanvasScale(2)
        assert(leg.thickness == 1.5)

        -- Kalimdor shows the whole route; the other continent nothing.
        WorldMapFrame:SetMapID(1414)
        stops, lines = client.Pins(STOP), client.Pins(LINE)
        assert(#stops == 3 and #lines == 2 and stops[1].Label.text == "")
        assert(near(stops[1].x, 0.62) and near(stops[1].y, 0.45))
        WorldMapFrame:SetMapID(1415)
        assert(#client.Pins(STOP) == 0 and #client.Pins(LINE) == 0)

        -- Orgrimmar: only stop 3 is on the map, but the leg to it still comes in from off the edge.
        WorldMapFrame:SetMapID(1454)
        stops, lines = client.Pins(STOP), client.Pins(LINE)
        assert(#stops == 1 and stops[1].Label.text == "Thrall" and #lines == 1)
        assert(near(lines[1].Fill.from.x, (620 - 520) / 80 * 1002) and near(lines[1].Fill.from.y, -(420 - 300) / 80 * 668))

        local text = chat()
        assert(text:find("Turn-ins|r (3 stops)", 1, true), text)
        assert(text:find("|cff33ff33>|r 1. |Haddon:WowMCP:route:1|h|cffffd100[Gornek]|r|h Durotar 60.0, 50.0 |cffaaaaaa(20 yd)|r", 1, true), text)
    """)


def test_navigator_follows_the_player(run_route):
    run_route(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish(TURN_INS)
        client.Wait()
        local nav = WowMCPRouteNavigator
        assert(nav and nav:IsShown())
        assert(nav.Title.text == "Turn-ins  1/3" and nav.Stop.text == "Gornek" and nav.Distance.text == "20 yd")
        -- Facing north, the stop is due east: the arrow turns a quarter clockwise.
        assert(nav.Arrow.shown and nav.Arrow.atlas == "Navigation-Tracked-Arrow")
        assert(near(nav.Arrow.rotation, -math.pi / 2), nav.Arrow.rotation)
        client.player.facing = 3 * math.pi / 2 -- east
        client.Tick()
        assert(near(nav.Arrow.rotation, 0))
        client.player.facing = 0

        -- Within 12 yards: on to stop 2, with its waypoint.
        moveTo(1411, 0.595, 0.5)
        assert(nav.Title.text == "Turn-ins  2/3" and nav.Stop.text == "Kaltunk")
        assert(client.waypoint.uiMapID == 1411 and near(select(2, client.waypoint.position:GetXY()), 0.4))
        assert(chat():find("Reached Gornek. Next: 2. |Haddon:WowMCP:route:2|h", 1, true), chat())
        local stops = client.Pins(STOP)
        assert(stops[1].Fill.color[1] == 0.45, "visited stops turn grey")

        -- 13 yards out doesn't count; then stop 3 is up north-west, 100 yards away.
        moveTo(1411, 0.6, 0.4 + 13 / 300)
        assert(nav.Title.text == "Turn-ins  2/3" and nav.Distance.text == "13 yd")
        moveTo(1411, 0.6, 0.4)
        assert(nav.Title.text == "Turn-ins  3/3" and nav.Distance.text == "100 yd")
        assert(near(nav.Arrow.rotation, math.atan(60, 80)), nav.Arrow.rotation)
        assert(client.waypoint.uiMapID == 1454)

        -- In Orgrimmar (another map, same continent) the last stop completes the route.
        moveTo(1454, 0.5, 0.5)
        assert(not nav:IsShown())
        assert(client.waypoint == nil, "the route's waypoint is removed when it's done")
        assert(client.chat[#client.chat]:find("Route complete: reached Thrall, the last stop.", 1, true))
        for _, pin in ipairs(client.Pins(STOP)) do assert(pin.Fill.color[1] == 0.45) end
    """)


def test_no_position_or_facing(run_route):
    run_route(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish(TURN_INS)
        client.Wait()
        local nav = WowMCPRouteNavigator

        -- In an instance there is no map position and no facing.
        client.player.facing = false
        moveTo(nil, 0, 0)
        assert(nav:IsShown() and nav.Distance.text == "no position here" and not nav.Arrow.shown)

        -- Facing alone missing: the distance still shows.
        moveTo(1411, 0.5, 0.5)
        assert(nav.Distance.text == "20 yd" and not nav.Arrow.shown)

        -- A stop on another continent: no distance or arrow, and no arriving there by accident.
        client.player.facing = 0
        client.Publish('{"stops":[{"zone":"Stormwind City","x":50,"y":50,"label":"Bank"},{"zone":"Durotar","x":1,"y":1,"label":"Far"}]}')
        client.Wait()
        assert(nav.Title.text == "Route  1/2" and nav.Distance.text == "Stormwind City (another continent)")
        assert(not nav.Arrow.shown)
    """)


def test_loop_wraps_around(run_route):
    run_route(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish('{"title":"Peacebloom","loop":true,"stops":['
            .. '{"zone":"Durotar","x":60,"y":50,"label":"A"},'
            .. '{"zone":"Durotar","x":60,"y":40,"label":"B"},'
            .. '{"zone":"Durotar","x":50,"y":40,"label":"C"}]}')
        client.Wait()
        assert(#client.Pins(LINE) == 3, "the loop closes")
        assert(chat():find("Peacebloom|r (3 stops, loop)", 1, true))
        local nav = WowMCPRouteNavigator
        moveTo(1411, 0.6, 0.5)
        moveTo(1411, 0.6, 0.4)
        moveTo(1411, 0.5, 0.4)
        assert(nav.Title.text == "Peacebloom  1/3" and nav:IsShown(), nav.Title.text)
        moveTo(1411, 0.6, 0.5)
        assert(nav.Title.text == "Peacebloom  2/3")
        -- A loop doesn't skip ahead while you stand at a stop you chose yourself.
        SlashCmdList.WOWMCP("route prev")
        client.Wait()
        assert(nav.Title.text == "Peacebloom  1/3")
        SlashCmdList.WOWMCP("route prev")
        assert(nav.Title.text == "Peacebloom  3/3", "prev wraps too")
    """)


def test_route_commands(run_route):
    run_route(r"""
        client.LoadAddon()
        client.Wait()
        local mcp = SlashCmdList.WOWMCP
        mcp("route")
        assert(chat():find("No route. Ask your AI for one!", 1, true))
        client.Publish(TURN_INS)
        client.Wait()
        local nav = WowMCPRouteNavigator

        mcp("route next")
        assert(nav.Title.text == "Turn-ins  2/3" and client.waypoint.position.y == 0.4)
        mcp("route 3")
        assert(nav.Title.text == "Turn-ins  3/3" and client.waypoint.uiMapID == 1454)
        -- Past the last stop the route is complete: navigator and waypoint go away.
        mcp("route next")
        assert(client.chat[#client.chat]:find("Route complete.", 1, true))
        assert(not nav:IsShown() and client.waypoint == nil)
        local chatLines = #client.chat
        mcp("route next")
        assert(client.chat[#client.chat]:find("Route complete.", 1, true) and #client.chat == chatLines, "said once")
        mcp("route 9")
        assert(client.chat[#client.chat]:find("No stop 9.", 1, true))
        mcp("route PREV")
        assert(nav:IsShown() and nav.Title.text == "Turn-ins  3/3", "back to the last stop")
        mcp("route PREV")
        assert(nav.Title.text == "Turn-ins  2/3")
        mcp("route bogus")
        assert(client.chat[#client.chat]:find("/mcp route [show, next, prev, <n>, stop]", 1, true))

        -- The navigator: right-click for the next stop, shift-right-click for the previous one.
        nav.OnMouseUp(nav, "RightButton")
        assert(nav.Title.text == "Turn-ins  3/3")
        client.shift = true
        nav.OnMouseUp(nav, "RightButton")
        client.shift = false
        assert(nav.Title.text == "Turn-ins  2/3")
        nav.OnDragStart(nav)
        nav:SetPoint("CENTER", UIParent, "CENTER", 10, 20)
        nav.OnDragStop(nav)

        -- Stop hides it all, show brings it back where it was and opens the map there.
        mcp("route stop")
        assert(not nav:IsShown() and #client.Pins(STOP) == 0 and #client.Pins(LINE) == 0)
        assert(client.waypoint == nil)
        client.Tick()
        assert(nav.Title.text == "Turn-ins  2/3", "a hidden navigator doesn't update")
        mcp("route")
        assert(chat():find("hidden, /mcp route show brings it back", 1, true))
        wipe(client.opened)
        mcp("route show")
        assert(nav:IsShown() and #client.Pins(STOP) == 3 and client.waypoint.position.y == 0.4)
        assert(WorldMapFrame:GetMapID() == 1411)

        -- Map markers alongside: both sets of chat links work, and clearing the markers leaves the
        -- route and its waypoint alone.
        client.Publish('{"markers":[{"zone":"Durotar","x":40,"y":40,"label":"Vendor"}]}')
        client.Wait()
        assert(#client.Pins("WowMCPPinTemplate") == 1 and #client.Pins(STOP) == 3)
        client.itemRef("addon:WowMCP:nav:1")
        assert(client.waypoint.position.x == 0.4)
        client.itemRef("addon:WowMCP:route:1")
        assert(nav.Title.text == "Turn-ins  1/3" and client.waypoint.position.x == 0.6)
        mcp("clear")
        assert(#client.Pins("WowMCPPinTemplate") == 0 and #client.Pins(STOP) == 3)
        assert(client.waypoint and client.waypoint.position.x == 0.6)
        assert(WowMCPDB.routeNavigator.x == 10 and WowMCPDB.routeNavigator.point == "CENTER")
    """)


def test_reload_and_clear(run_route):
    run_route(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish(TURN_INS)
        client.Wait()
        moveTo(1411, 0.6, 0.5)
        assert(WowMCPRouteNavigator.Title.text == "Turn-ins  2/3")

        -- /reload: Lua state resets, saved variables don't. The route comes back quietly where it was.
        wipe(client.loaded); wipe(client.tickers); wipe(client.timers); wipe(client.frames); wipe(client.chat); wipe(client.opened)
        WorldMapFrame.providers = {}; wipe(WorldMapFrame.pins)
        WowMCPRouteNavigator = nil
        client.LoadAddon()
        local nav = WowMCPRouteNavigator
        assert(nav and nav:IsShown() and nav.Title.text == "Turn-ins  2/3")
        assert(#client.Pins(STOP) == 3 and #client.chat == 0 and #client.opened == 0)

        client.Wait()
        client.Publish('{"clear":true}')
        client.Wait()
        assert(not nav:IsShown() and #client.Pins(STOP) == 0 and #client.Pins(LINE) == 0)
        assert(client.waypoint == nil and client.chat[#client.chat]:find("Route cleared.", 1, true))

        -- Unknown zones are reported; the rest of the route still works.
        client.Publish('{"stops":[{"zone":"Atlantis","x":1,"y":1,"label":"Nope"},{"zone":"Durotar","x":70,"y":50,"label":"Gornek"}]}')
        client.Wait()
        assert(chat():find("Couldn't find these zones on the map:|r Atlantis", 1, true))
        assert(nav:IsShown() and nav.Title.text == "Route  1/1" and #client.Pins(LINE) == 0)
    """)


def test_show_route_payload(addons):
    result = routes.show_route(
        [
            routes.Stop(zone="Durotar", x=60.123, y=50, label="Gornek", note="In the den"),
            routes.Stop(zone="Orgrimmar", x=50, y=50, label="Thrall", map_id=1454),
        ],
        title="Turn-ins",
        loop=True,
    )
    assert "2 stops (looping)" in result and "Gornek (Durotar 60.1, 50.0) -> Thrall" in result
    state = Transport(addons).load_state()
    assert state["routes"] == {
        "id": state["seq"], "title": "Turn-ins", "loop": True, "openMap": True,
        "stops": [
            {"zone": "Durotar", "x": 60.12, "y": 50, "label": "Gornek", "note": "In the den", "mapID": None},
            {"zone": "Orgrimmar", "x": 50, "y": 50, "label": "Thrall", "note": None, "mapID": 1454},
        ],
    }
    routes.clear_route()
    state = Transport(addons).load_state()
    assert state["routes"] == {"id": state["seq"], "stops": []}

    with pytest.raises(ValidationError):
        routes.Stop(zone="Durotar", x=101, y=50, label="Off the map")


def test_templates_are_valid_xml():
    root = ET.parse(ADDON / "Routes.xml").getroot()
    names = {frame.get("name") for frame in root}
    assert names == {"WowMCPRouteStopPinTemplate", "WowMCPRouteLinePinTemplate"}


def test_navigator_fits_long_stop_names(run_route):
    run_route(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish(TURN_INS)
        client.Wait()
        local nav = WowMCPRouteNavigator
        local shortWidth, shortHeight = nav.width, nav.height
        assert(shortWidth == 180 and shortHeight >= 58, shortWidth .. "x" .. shortHeight)

        -- A longer name widens it; one too long for the widest navigator wraps onto two lines.
        client.Publish('{"title":"Errands","stops":[{"zone":"Durotar","x":60,"y":50,"label":"Finespindles Leather Goods"},'
            .. '{"zone":"Durotar","x":60,"y":40,"label":"Kaltunk"}]}')
        client.Wait()
        assert(nav.Stop.text == "Finespindles Leather Goods" and nav.width > shortWidth and nav.width < 360)
        assert(nav.Stop:GetStringHeight() == 12, "fits on one line")

        client.Publish('{"title":"Errands","stops":[{"zone":"Durotar","x":60,"y":50,'
            .. '"label":"Finespindles Leather Goods and Skinning, Great Forge"},'
            .. '{"zone":"Durotar","x":60,"y":40,"label":"Kaltunk"}]}')
        client.Wait()
        assert(nav.width == 360 and nav.Stop.width == 360 - 58 - 10)
        assert(nav.Stop:GetStringHeight() == 24 and nav.height > shortHeight, "wrapped onto two lines")
    """)
