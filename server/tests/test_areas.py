"""Area highlights on the world map (Areas.lua, Areas.xml, tools/areas.py).

Stub geometry (stub/apis/map.lua): Durotar is 200 yards wide (world x 500-700, y 300-600),
Orgrimmar 80 (x 520-600, y 300-380), Kalimdor 1000. The canvas is 1002 x 668.
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from pydantic import ValidationError

from wow_mcp.tools import areas
from wow_mcp.transport import Transport

ADDON = Path(__file__).parents[2] / "addon" / "WowMCP"

# How a Lua test script hands control back to publish through the real tools.
PUBLISH_SCRIPT = """
import json, sys
from wow_mcp.tools import areas
args = json.loads(sys.argv[1])
if args.get("clear"):
    areas.clear_areas()
else:
    areas.highlight_areas([areas.Area(**a) for a in args["areas"]], title=args.get("title"))
"""

BOARS = (
    '{"areas":['
    '{"zone":"Durotar","x":50,"y":50,"radius":10,"label":"Boars","color":"red"},'
    '{"zone":"Orgrimmar","x":50,"y":50,"radius":5,"label":"Bank"},'
    '{"zone":"Atlantis","x":1,"y":1,"radius":1,"label":"Nope"}]}'
)

PRELUDE = f"""
    local BOARS = '{BOARS}'
    local AREA = "WowMCPAreaPinTemplate"
    local function near(a, b) return math.abs(a - b) < 1e-6 end
    local function chat() return table.concat(client.chat, "\\n") end
"""


@pytest.fixture
def run_areas(addons, run_client, tmp_path, monkeypatch):
    script = tmp_path / "publish_areas.py"
    script.write_text(PUBLISH_SCRIPT)
    monkeypatch.setenv("WOWMCP_PUBLISH", f"{sys.executable} {script}")
    return lambda lua: run_client(PRELUDE + lua)


def test_areas_on_the_map(run_areas):
    run_areas(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish(BOARS)
        client.Wait()
        assert(client.opened[1] == 1411, "map opened on the first area")

        -- Durotar: the boars at their own size and colour; the bank, 4 yards across in Orgrimmar,
        -- is too small here for a label.
        local pins = client.Pins(AREA)
        assert(#pins == 2)
        local boars, bank = pins[1], pins[2]
        assert(boars.x == 0.5 and boars.y == 0.5 and near(boars.width, 0.2 * 1002) and boars.width == boars.height)
        assert(boars.Label.text == "Boars" and boars.ignoreGlobalPinScale)
        assert(near(boars.Fill.color[1], 1) and near(boars.Fill.color[2], 0x4d / 255) and boars.Fill.color[4] == 0.3)
        assert(near(bank.x, 0.3) and near(bank.y, 40 / 300) and near(bank.width, 2 * 4 / 200 * 1002))
        assert(bank.Label.text == "" and near(bank.Fill.color[3], 1), "unlabelled, second palette colour")

        -- A light outline: a ring of lines on the circle's edge, in a lighter tint of its colour.
        local ring, r = boars.Outline, boars.width / 2
        assert(#ring == 48 and ring[1].from.point == "CENTER" and ring[1].from.relativeTo == boars)
        assert(near(ring[1].from.x, r) and near(ring[1].from.y, 0))
        assert(near(ring[13].from.x, 0) and near(ring[13].from.y, r), "a quarter of the way round")
        assert(near(ring[48].to.x, r) and near(ring[48].to.y, 0), "closed")
        assert(near(ring[1].color[2], 0x4d / 255 + (1 - 0x4d / 255) * 0.5) and ring[1].color[4] == 0.9)

        -- Zooming in keeps the label's size and the outline's thickness on screen.
        assert(boars.Label.scale == 1 and ring[1].thickness == 1.5)
        client.SetCanvasScale(2)
        assert(boars.Label.scale == 0.5 and ring[1].thickness == 0.75)

        -- Kalimdor: same ground, smaller circles.
        WorldMapFrame:SetMapID(1414)
        pins = client.Pins(AREA)
        assert(#pins == 2 and near(pins[1].x, 0.6) and near(pins[1].width, 2 * 20 / 1000 * 1002) and pins[1].Label.text == "")
        -- Orgrimmar: the boars' centre is off this map, the bank is its own.
        WorldMapFrame:SetMapID(1454)
        pins = client.Pins(AREA)
        assert(#pins == 1 and pins[1].Label.text == "Bank" and near(pins[1].width, 0.1 * 1002))
        WorldMapFrame:SetMapID(1415)
        assert(#client.Pins(AREA) == 0, "nothing on the other continent")

        local text = chat()
        assert(text:find("Highlighted on your map:", 1, true))
        assert(text:find("|cffff4d4dBoars|r Durotar 50.0, 50.0", 1, true), text)
        assert(text:find("Couldn't find these zones on the map:|r Atlantis", 1, true))

        client.Publish('{"clear":true}')
        client.Wait()
        assert(#client.Pins(AREA) == 0 and client.chat[#client.chat]:find("Area highlights cleared.", 1, true))
    """)


def test_areas_commands_and_reload(run_areas):
    run_areas(r"""
        client.LoadAddon()
        client.Wait()
        local mcp = SlashCmdList.WOWMCP
        mcp("areas")
        assert(client.chat[#client.chat]:find("No highlighted areas.", 1, true))
        client.Publish(BOARS)
        client.Wait()
        wipe(client.chat)
        mcp("areas")
        assert(chat():find("Boars", 1, true) and chat():find("Bank", 1, true))
        mcp("areas clear")
        assert(#client.Pins(AREA) == 0)

        -- Hidden stays hidden through a /reload, and nothing is announced again.
        wipe(client.loaded); wipe(client.tickers); wipe(client.timers); wipe(client.frames); wipe(client.chat); wipe(client.opened)
        WorldMapFrame.providers = {}; wipe(WorldMapFrame.pins)
        client.LoadAddon()
        assert(#client.Pins(AREA) == 0 and #client.chat == 0 and #client.opened == 0)

        -- New areas show again.
        client.Wait()
        client.Publish('{"title":"Kolkar","areas":[{"zone":"Durotar","x":40,"y":60,"radius":3,"label":"Camp","color":"#00FF00"}]}')
        client.Wait()
        local pins = client.Pins(AREA)
        assert(#pins == 1 and pins[1].Label.text == "Camp" and pins[1].Fill.color[2] == 1 and pins[1].Fill.color[1] == 0)
        assert(chat():find("Kolkar", 1, true))
        WorldMapFrame:SetMapID(1414)
        SlashCmdList.WOWMCP("areas show") -- the reloaded addon's handler
        assert(WorldMapFrame:GetMapID() == 1411)
    """)


def test_highlight_areas_payload(addons):
    result = areas.highlight_areas(
        [
            areas.Area(zone="Durotar", x=50, y=50.555, radius=10, label="Boars", color="Red"),
            areas.Area(zone="Orgrimmar", x=50, y=50, radius=2.5, label="Bank", color="#00FF88", map_id=1454),
            areas.Area(zone="Durotar", x=1, y=1, radius=1, label="Plain"),
        ],
        title="Boars",
    )
    assert "3 area(s)" in result and "Bank (Orgrimmar 50.0, 50.0, radius 2.5)" in result
    state = Transport(addons).load_state()
    assert state["areas"]["id"] == state["seq"] and state["areas"]["title"] == "Boars"
    assert state["areas"]["areas"] == [
        {"zone": "Durotar", "x": 50, "y": 50.55, "radius": 10, "label": "Boars", "color": "ff4d4d", "mapID": None},
        {"zone": "Orgrimmar", "x": 50, "y": 50, "radius": 2.5, "label": "Bank", "color": "00ff88", "mapID": 1454},
        {"zone": "Durotar", "x": 1, "y": 1, "radius": 1, "label": "Plain", "color": None, "mapID": None},
    ]
    areas.clear_areas()
    state = Transport(addons).load_state()
    assert state["areas"] == {"id": state["seq"], "areas": []}

    with pytest.raises(ValidationError, match="hex colour"):
        areas.Area(zone="Durotar", x=1, y=1, radius=1, label="Pink", color="pink")
    with pytest.raises(ValidationError):
        areas.Area(zone="Durotar", x=1, y=1, radius=0, label="Nothing")


def test_template_is_valid_xml():
    root = ET.parse(ADDON / "Areas.xml").getroot()
    assert [frame.get("name") for frame in root] == ["WowMCPAreaPinTemplate"]


def test_area_pins_in_combat_block_nothing(run_areas):
    # The map canvas sets each pin's mouse pass-through as it hands it out, which the game blocks for
    # addons in combat ("Interface action failed because of an AddOn"): opening the map in a fight is
    # fine, and outside combat the pins let right-clicks through as usual.
    run_areas(r"""
        client.LoadAddon()
        client.Wait()
        client.Publish(BOARS)
        client.Wait()
        assert(not client.blocked, "nothing blocked outside combat")
        local pin = WorldMapFrame.pins[1]
        assert(pin and pin.passThrough and pin.passThrough[1] == "RightButton", "right-clicks fall through to the map")
        client.combat = true
        WorldMapFrame:SetMapID(WorldMapFrame:GetMapID())
        assert(#WorldMapFrame.pins > 0 and not client.blocked, "blocked in combat: " .. tostring(client.blocked))
    """)
