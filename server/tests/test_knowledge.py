"""Recipes known and vendors seen (Knowledge.lua, tools/knowledge.py)."""

import asyncio
from datetime import datetime, timezone

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wow_mcp.tools import knowledge

EARLIER = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)  # unix 1700000000

# Cooking, cached when the window was open in an earlier session, saved the way Knowledge.lua saves it.
EARLIER_RECIPES = """
WowMCPDB = { knowledge = { recipes = { [UnitName("player") .. "-" .. GetRealmName()] = { Cooking = {
    name = "Cooking", rank = 40, maxRank = 75, at = 1700000000, recipes = {
        { id = 2538, name = "Charred Wolf Meat", color = "green", reagents = { { item = 2672, count = 1 } } },
    } } } } } }
"""

EARLIER_VENDOR = """
WowMCPDB = { knowledge = { vendors = { {
    name = "Innkeeper Gryshka", npc = 6929, zone = "Orgrimmar", subzone = "Valley of Strength", mapID = 1454,
    x = 54.1, y = 68.4, at = 1700000000, items = { { id = 159, name = "Refreshing Spring Water", price = 25 } },
} } } }
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


def reagent(name, item_id, count):
    return {"name": name, "item_id": item_id, "count": count}


def test_recipes_from_this_and_an_earlier_session(live_game):
    setup = EARLIER_RECIPES + """
        C_Timer.After(1, function() client.OpenProfession("Leatherworking") end)
        C_Timer.After(3, function() client.CloseProfession() end)
    """
    with live_game(setup):
        everything = asyncio.run(knowledge.get_known_recipes())
        leather = asyncio.run(knowledge.get_known_recipes("leather"))
        alchemy = asyncio.run(knowledge.get_known_recipes("Alchemy"))
    assert everything.game.startswith("World of Warcraft: Forever")
    cooking, leatherworking = everything.professions
    assert cooking.model_dump() == {
        "profession": "Cooking", "rank": 40, "max_rank": 75, "seen_at": EARLIER,
        "recipes": [{"name": "Charred Wolf Meat", "recipe_id": 2538, "color": "green",
                     "reagents": [reagent(None, 2672, 1)]}],
    }
    assert (leatherworking.profession, leatherworking.rank, leatherworking.max_rank) == ("Leatherworking", 60, 75)
    assert leatherworking.seen_at > EARLIER
    # Learned recipes only, by name.
    assert [r.model_dump() for r in leatherworking.recipes] == [
        {"name": "Handstitched Leather Boots", "recipe_id": 2149, "color": "yellow",
         "reagents": [reagent("Light Leather", 2318, 2), reagent("Coarse Thread", 2320, 1)]},
        {"name": "Light Armor Kit", "recipe_id": 2152, "color": "orange",
         "reagents": [reagent("Light Leather", 2318, 1)]},
        {"name": "Light Leather", "recipe_id": 2881, "color": "gray", "reagents": [reagent("Light Hide", 783, 3)]},
    ]
    assert everything.note is None
    assert leather.professions == [leatherworking]
    assert alchemy.professions == []
    assert "No recipes cached for Alchemy on this character (cached: Cooking, Leatherworking)" in alchemy.note


def test_vendors_newest_first(live_game):
    setup = EARLIER_VENDOR + """
        client.player = { map = 1411, x = 0.523, y = 0.412 }
        C_Timer.After(1, function() client.OpenMerchant({ name = "Duokna", npc = 3158, items = {
            { id = 2678, price = 10 },
            { id = 4289, price = 50, stack = 5 },
            { id = 4470, price = 38, stock = 2 },
            { id = 5976, price = 0, locked = true, costs = { { 2, "Honor Token" } } },
        } }) end)
        C_Timer.After(3, function() client.CloseMerchant() end)
    """
    with live_game(setup):
        everything = asyncio.run(knowledge.get_vendor_items())
        razor_hill = asyncio.run(knowledge.get_vendor_items("razor"))
        gryshka = asyncio.run(knowledge.get_vendor_items("GRYSHKA"))
        stormwind = asyncio.run(knowledge.get_vendor_items("Stormwind"))
    duokna, innkeeper = everything.vendors
    assert duokna.seen_at > EARLIER
    assert duokna.model_dump(exclude={"seen_at", "items"}) == {
        "name": "Duokna", "npc_id": 3158, "zone": "Durotar", "subzone": "Razor Hill", "x": 52.3, "y": 41.2,
        "map_id": 1411,
    }
    base = {"stack": 1, "stock": None, "other_cost": None, "can_buy": True}
    # Salt, Simple Wood and the tabard weren't loaded when the window opened; their names came later.
    assert [i.model_dump() for i in duokna.items] == [
        {**base, "name": "Mild Spices", "item_id": 2678, "price": 10},
        {**base, "name": "Salt", "item_id": 4289, "price": 50, "stack": 5},
        {**base, "name": "Simple Wood", "item_id": 4470, "price": 38, "stock": 2},
        {**base, "name": "Guild Tabard", "item_id": 5976, "price": 0, "other_cost": "2 Honor Token", "can_buy": False},
    ]
    assert innkeeper.model_dump() == {
        "name": "Innkeeper Gryshka", "npc_id": 6929, "zone": "Orgrimmar", "subzone": "Valley of Strength",
        "x": 54.1, "y": 68.4, "map_id": 1454, "seen_at": EARLIER,
        "items": [{**base, "name": "Refreshing Spring Water", "item_id": 159, "price": 25}],
    }
    assert everything.note is None
    assert razor_hill.vendors == [duokna] and gryshka.vendors == [innkeeper]
    assert stormwind.vendors == [] and "No vendor matching 'Stormwind' seen yet" in stormwind.note


def test_recipes_are_the_characters_own(run_client):
    run_client(ASK + r"""
        -- Someone else's profession linked in chat isn't cached.
        client.OpenProfession("Leatherworking", { linked = "Grubnash" })
        client.Wait()
        client.CloseProfession()
        assert(next(Ask(ns.Kinds.KNOWLEDGE).recipes.professions) == nil)

        -- Without GetAllRecipeIDs, the window's own list (learned recipes by default) is used.
        C_TradeSkillUI.GetAllRecipeIDs = nil
        client.itemNames[2672] = "Stringy Wolf Meat"
        client.OpenProfession("Cooking")
        client.Wait()
        client.CloseProfession()
        local cooking = Ask(ns.Kinds.KNOWLEDGE).recipes.professions[1]
        assert(cooking.name == "Cooking" and cooking.rank == 40 and math.abs(cooking.at - time()) <= 1)
        assert(#cooking.recipes == 1 and cooking.recipes[1].color == "green")
        -- The reagent's name wasn't loaded during the scan, but is by the time it's asked for.
        assert(cooking.recipes[1].reagents[1].name == "Stringy Wolf Meat")

        -- Another character on the account has its own recipes.
        function UnitName() return "Alt", nil end
        assert(next(Ask(ns.Kinds.KNOWLEDGE).recipes.professions) == nil)
    """)


def test_vendors_are_capped(run_client):
    run_client(ASK + r"""
        local function Visit(name)
            client.OpenMerchant({ name = name, npc = 3000, items = { { id = 159, price = 25 } } })
            client.CloseMerchant()
        end
        for i = 1, 22 do Visit("Vendor " .. i) end
        local vendors = Ask(ns.Kinds.KNOWLEDGE).vendors.vendors
        assert(#vendors == 20 and vendors[1].name == "Vendor 22" and vendors[20].name == "Vendor 3")
        -- A visit again moves the vendor to the front.
        Visit("Vendor 5")
        Visit("Vendor 1")
        vendors = Ask(ns.Kinds.KNOWLEDGE).vendors.vendors
        assert(#vendors == 20 and vendors[1].name == "Vendor 1" and vendors[2].name == "Vendor 5")
        assert(vendors[3].name == "Vendor 22" and vendors[20].name == "Vendor 4")
        for i, vendor in ipairs(vendors) do
            for j = i + 1, #vendors do assert(vendor.name ~= vendors[j].name) end
        end
        -- Vendors are the same for every character.
        local UnitNameBase = UnitName
        function UnitName(unit) if unit == "player" then return "Alt", nil end return UnitNameBase(unit) end
        assert(#Ask(ns.Kinds.KNOWLEDGE).vendors.vendors == 20)
    """)


def test_problems_are_reported(run_client):
    run_client(ASK + r"""
        C_TradeSkillUI.GetRecipeInfo = function() error("secret value") end
        client.OpenProfession("Leatherworking")
        client.Wait()
        C_MerchantFrame.GetItemInfo = function() error("no merchant") end
        client.OpenMerchant({ name = "Duokna", npc = 3158, items = { { id = 2678, price = 10 } } })
        local reply = Ask(ns.Kinds.KNOWLEDGE)
        assert(reply.recipes.problem:find("secret value", 1, true), reply.recipes.problem)
        assert(reply.vendors.problem:find("no merchant", 1, true), reply.vendors.problem)
    """)


def test_reply_shapes(monkeypatch):
    async def request(kind):
        return reply, "WoW"

    monkeypatch.setattr(knowledge, "request", request)
    # Empty Lua tables arrive as {}.
    reply = {"recipes": {"professions": {}}, "vendors": {"vendors": {}, "problem": "boom"}}
    recipes = asyncio.run(knowledge.get_known_recipes())
    assert recipes.professions == []
    assert "No recipes cached for any profession on this character (cached: none)" in recipes.note
    vendors = asyncio.run(knowledge.get_vendor_items())
    assert vendors.vendors == []
    assert vendors.note.endswith("Reading the last vendor failed: boom")

    reply = {"recipes": {"professions": [{"name": "Mining", "at": 1700000000, "recipes": [
        {"id": 2657, "name": "Smelt Copper", "reagents": {}},
    ]}]}}
    mining = asyncio.run(knowledge.get_known_recipes("mining")).professions[0]
    smelt = mining.recipes[0]
    assert (mining.rank, mining.seen_at, smelt.color, smelt.reagents) == (None, EARLIER, None, [])

    reply = {}
    with pytest.raises(ToolError, match="doesn't send vendors yet"):
        asyncio.run(knowledge.get_vendor_items())
