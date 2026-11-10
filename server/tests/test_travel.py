"""Known flight paths (Travel.lua, tools/travel.py)."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wow_mcp.tools import travel

EARLIER = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)  # unix 1700000000

# Flight maps the character opened in an earlier session, saved the way Travel.lua saves them.
EARLIER_FLIGHT_MAPS = """
WowMCPDB = { travel = { [UnitName("player") .. "-" .. GetRealmName()] = {
    [1415] = { map = "Eastern Kingdoms", from = "Stormwind, Elwynn", at = 1700000000, nodes = {
        { name = "Stormwind, Elwynn", zone = "Stormwind City", x = 66.3, y = 62.1, state = "current" },
        { name = "Ironforge, Dun Morogh", zone = "Ironforge", x = 55.5, y = 47.8, state = "unreachable" },
    } },
    [1414] = { map = "Kalimdor", from = "Ratchet, The Barrens", at = 1699913600, nodes = {
        { name = "Ratchet, The Barrens", zone = "The Barrens", x = 63.1, y = 37.1, state = "current" },
    } },
} } }
"""

# Collect a kind's reply in a Lua test, without streaming it.
ASK = """
local ns = client.LoadAddon()
local function Ask(kind)
    local reply
    local send = ns.Stream.Send
    ns.Stream.Send = function(_, value) reply = value end
    ns.Answer(0, kind)
    ns.Stream.Send = send
    return reply
end
"""


def path(name, zone, x, y, faction=None):
    return {"name": name, "zone": zone, "x": x, "y": y, "faction": faction}


def test_world_map_and_earlier_flight_maps(live_game):
    with live_game(EARLIER_FLIGHT_MAPS):
        result = asyncio.run(travel.get_flight_paths())
    assert result.game.startswith("World of Warcraft: Forever")
    # The Alliance player knows Ratchet and Stormwind; the Horde flight masters aren't listed.
    # Ironforge is only on the flight map from the earlier session.
    assert [p.model_dump() for p in result.known] == [
        path("Ratchet, The Barrens", "The Barrens", 63.1, 37.1, "Neutral"),
        path("Stormwind, Elwynn", "Stormwind City", 66.3, 62.1, "Alliance"),
        path("Ironforge, Dun Morogh", "Ironforge", 55.5, 47.8),
    ]
    assert [p.model_dump() for p in result.not_yet_discovered] == [
        path("Sentinel Hill, Westfall", "Westfall", 56.6, 52.6, "Alliance"),
    ]
    assert [m.model_dump() for m in result.flight_maps_seen] == [
        {"continent": "Eastern Kingdoms", "flight_master": "Stormwind, Elwynn", "seen_at": EARLIER,
         "unreachable": ["Ironforge, Dun Morogh"]},
        {"continent": "Kalimdor", "flight_master": "Ratchet, The Barrens", "seen_at": EARLIER - timedelta(days=1),
         "unreachable": []},
    ]
    assert result.note is None


def test_flight_map_opened_this_session(live_game):
    # The world map data can't be read, so the flight map opened at Stormwind is all there is.
    setup = EARLIER_FLIGHT_MAPS + """
        client.taxi.known = { [2] = true, [8] = true, [80] = true }
        client.taxi.unreachable = { [8] = true }
        client.taxi.worldMap = "broken"
        C_Timer.After(1, function() client.OpenFlightMap(2) end)
        C_Timer.After(2, function() client.CloseFlightMap() end)
    """
    with live_game(setup):
        result = asyncio.run(travel.get_flight_paths())
    assert [p.model_dump() for p in result.known] == [
        path("Stormwind, Elwynn", "Stormwind City", 66.3, 62.1),
        path("Sentinel Hill, Westfall", "Westfall", 56.6, 52.6),
        path("Ratchet, The Barrens", "The Barrens", 63.1, 37.1),
    ]
    assert result.not_yet_discovered == []
    eastern, kalimdor = result.flight_maps_seen
    assert eastern.flight_master == "Stormwind, Elwynn" and eastern.seen_at > EARLIER
    assert eastern.unreachable == ["Sentinel Hill, Westfall"]
    assert kalimdor.seen_at == EARLIER - timedelta(days=1)
    assert "world map data wasn't readable (" in result.note and "secret value" in result.note


def test_continent_maps_without_nodes_ask_the_zones(run_client):
    # A Horde character, and continent maps that list no flight points: each zone is asked, and a
    # flight master on several zone maps is listed once, on the first.
    run_client(ASK + r"""
        function UnitFactionGroup() return "Horde", "Horde" end
        client.taxi.known = { [23] = true, [80] = true }
        client.taxi.worldMap = "zones only"
        local paths = Ask(ns.Kinds.TRAVEL).flightPaths
        assert(#paths.known == 2 and #paths.undiscovered == 1, #paths.known .. " " .. #paths.undiscovered)
        local orgrimmar, ratchet = paths.known[1], paths.known[2]
        assert(orgrimmar.name == "Orgrimmar, Durotar" and orgrimmar.zone == "Durotar" and orgrimmar.faction == "Horde")
        assert(orgrimmar.x == 28.0 and orgrimmar.y == 17.0, orgrimmar.x .. ", " .. orgrimmar.y)
        assert(ratchet.zone == "The Barrens" and ratchet.x == 63.1 and ratchet.faction == "Neutral")
        assert(paths.undiscovered[1].name == "Crossroads, The Barrens" and paths.undiscovered[1].faction == "Horde")
        assert(next(paths.flightMaps) == nil and not paths.worldMapProblem)
    """)


def test_flight_map_cached_per_character(run_client):
    run_client(ASK + r"""
        client.taxi.known = { [2] = true, [8] = true }
        client.taxi.oldFlightMap = true -- only the older API answers: no positions
        client.OpenFlightMap(2)
        client.CloseFlightMap()
        local maps = Ask(ns.Kinds.TRAVEL).flightPaths.flightMaps
        assert(#maps == 1 and maps[1].map == "Eastern Kingdoms" and maps[1].from == "Stormwind, Elwynn")
        assert(math.abs(maps[1].at - time()) <= 1)
        local stormwind, sentinel = maps[1].nodes[1], maps[1].nodes[2]
        assert(stormwind.name == "Stormwind, Elwynn" and stormwind.state == "current" and not stormwind.zone)
        assert(sentinel.name == "Sentinel Hill, Westfall" and sentinel.state == "reachable" and #maps[1].nodes == 2)

        -- Opening it again replaces it; with the newer API it has positions.
        client.taxi.oldFlightMap = false
        client.OpenFlightMap(2)
        maps = Ask(ns.Kinds.TRAVEL).flightPaths.flightMaps
        assert(#maps == 1 and maps[1].nodes[2].zone == "Westfall" and maps[1].nodes[2].x == 56.6)

        -- Another character on the account has its own.
        function UnitName() return "Alt", nil end
        assert(next(Ask(ns.Kinds.TRAVEL).flightPaths.flightMaps) == nil)
        assert(next(WowMCPDB.travel["Thorgrim-" .. GetRealmName()]) ~= nil)
    """)


def test_problems_are_reported(run_client):
    run_client(ASK + r"""
        C_TaxiMap.GetAllTaxiNodes = function() error("no flight master") end
        client.OpenFlightMap(2)
        client.taxi.worldMap = "broken"
        local paths = Ask(ns.Kinds.TRAVEL).flightPaths
        assert(paths.flightMapProblem:find("no flight master", 1, true), paths.flightMapProblem)
        assert(paths.worldMapProblem:find("secret value", 1, true) and not paths.known)
    """)


def test_reply_shapes(monkeypatch):
    async def request(kind):
        return reply, "WoW"

    monkeypatch.setattr(travel, "request", request)
    # Empty Lua tables arrive as {}.
    reply = {"flightPaths": {"known": {}, "undiscovered": {}, "flightMaps": {}}}
    result = asyncio.run(travel.get_flight_paths())
    assert (result.known, result.not_yet_discovered, result.flight_maps_seen, result.note) == ([], [], [], None)

    reply = {"flightPaths": {"flightMapProblem": "boom", "flightMaps": [
        {"at": 1700000000, "nodes": [{"name": "Ratchet, The Barrens", "state": "unreachable"}, {}]},
    ]}}
    result = asyncio.run(travel.get_flight_paths())
    assert [p.name for p in result.known] == ["Ratchet, The Barrens"]
    assert result.flight_maps_seen[0].continent is None and result.flight_maps_seen[0].seen_at == EARLIER
    assert result.note == "The last flight map couldn't be cached: boom"

    reply = {"errors": {"flightPaths": "attempt to index nil"}}
    with pytest.raises(ToolError, match="couldn't collect flightPaths: attempt to index nil"):
        asyncio.run(travel.get_flight_paths())


def test_client_that_never_flags_undiscovered(live_game):
    """Forever's world map marks no flight master undiscovered, so it only gives locations;
    what's known then comes from the flight maps seen."""
    with live_game("client.taxi.noDiscoveryFlag = true"):
        fresh = asyncio.run(travel.get_flight_paths())
    assert fresh.known == [] and fresh.not_yet_discovered == []
    masters = [p.name for p in fresh.flight_masters]
    assert "Stormwind, Elwynn" in masters and "Ratchet, The Barrens" in masters
    assert {p.faction for p in fresh.flight_masters} <= {"Alliance", "Neutral"}, "no Horde flight masters"
    assert "talk to any flight master" in fresh.note

    with live_game("client.taxi.noDiscoveryFlag = true\n" + EARLIER_FLIGHT_MAPS):
        later = asyncio.run(travel.get_flight_paths())
    assert [p.name for p in later.known] == ["Stormwind, Elwynn", "Ironforge, Dun Morogh", "Ratchet, The Barrens"]
    assert later.flight_masters and later.note is None
