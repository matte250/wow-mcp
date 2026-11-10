"""Bags, equipped gear and the bank as last seen (Inventory.lua, tools/inventory.py)."""

import asyncio
import json
import time
import zlib
from datetime import datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wow_mcp import kinds
from wow_mcp.tools import inventory


def item(slot, item_id, name, count=1, quality="common", item_level=None, sell_price=None):
    return {"slot": slot, "item_id": item_id, "name": name, "count": count, "quality": quality,
            "item_level": item_level, "sell_price": sell_price}


def gear(slot, item_id, name, item_level, quality="common", durability=None, max_durability=None, count=None):
    return {"slot": slot, "item_id": item_id, "name": name, "quality": quality, "item_level": item_level,
            "durability": durability, "max_durability": max_durability, "count": count}


# The stub's character (stub/apis/inventory.lua).
BACKPACK = [
    item(1, 6948, "Hearthstone"),
    item(2, 2589, "Linen Cloth", count=7, sell_price=13),
    item(3, 4865, "Ruined Pelt", count=2, quality="poor", sell_price=5),
    item(4, 9789, "Raider's Legguards of the Bear", quality="uncommon", item_level=21, sell_price=304),
    item(5, 3164, None),  # not in the client's item cache yet
    item(6, 159, "Refreshing Spring Water", count=5, sell_price=1),
]
QUIVER = [item(1, 2512, "Rough Arrow", count=200), item(2, 2512, "Rough Arrow", count=143)]
EQUIPMENT = [
    gear("ammo", 2512, "Rough Arrow", 5, count=343),
    gear("shirt", 45, "Squire's Shirt", 1),
    gear("chest", 4243, "Fine Leather Tunic", 17, quality="uncommon", durability=70, max_durability=85),
    gear("legs", 2302, "Handstitched Leather Pants", 10, durability=0, max_durability=55),
    gear("main hand", 12282, "Worn Battleaxe", 2, durability=25, max_durability=25),
    gear("ranged", 2504, "Worn Shortbow", 2, durability=30, max_durability=35),
]


def test_inventory_with_bank_from_an_earlier_visit(live_game):
    seen = int(time.time()) - 3 * 86400
    # Saved variables from an earlier session: this character's bank, and another character's.
    setup = f"""
        WowMCPDB = {{ bank = {{
            [UnitName("player") .. "-" .. GetRealmName()] = {{ time = {seen}, bags = {{
                {{ bag = 6, size = 24, items = {{ {{ slot = 1, id = 2589, name = "Linen Cloth", count = 20, quality = 1, sell = 13 }} }} }},
                {{ bag = 7, size = 6, items = {{}} }},
            }} }},
            ["Someone-Else"] = {{ time = {seen}, bags = {{ {{ bag = 6, size = 24, items = {{}} }} }} }},
        }} }}
    """
    with live_game(setup):
        first = asyncio.run(inventory.get_inventory())
        result = asyncio.run(inventory.get_inventory(include_bank=True))

    assert first.bank is None, "only when asked for"
    assert first.bags[1].items[4].name is None
    dump = result.model_dump()
    assert dump["game"].startswith("World of Warcraft: Forever 1.60.1")
    assert dump["bags"] == [
        {"bag": -1, "name": "Keyring", "slots": 4, "free": 3, "special": True,
         "items": [item(1, 5396, "Key to Searing Gorge")]},
        {"bag": 0, "name": "Backpack", "slots": 16, "free": 10, "special": False,
         "items": BACKPACK[:4] + [item(5, 3164, "Discolored Worg Heart")] + BACKPACK[5:]},
        {"bag": 1, "name": "Small Brown Pouch", "slots": 6, "free": 6, "special": False, "items": []},
        {"bag": 2, "name": "Light Quiver", "slots": 8, "free": 6, "special": True, "items": QUIVER},
    ], "the item's data has loaded since the first request"
    assert dump["free_bag_slots"] == 16
    assert dump["equipment"] == EQUIPMENT
    assert dump["bank"] == {
        "seen_at": datetime.fromtimestamp(seen).astimezone(),
        "hours_ago": 72.0,
        "free_slots": 29,
        "bags": [
            {"bag": 6, "name": "Bank", "slots": 24, "free": 23, "special": False,
             "items": [item(1, 2589, "Linen Cloth", count=20, sell_price=13)]},
            {"bag": 7, "name": "Bank bag 1", "slots": 6, "free": 6, "special": False, "items": []},
        ],
    }


def test_bank_seen_this_session(live_game):
    # The player opens the bank, deposits the pelts and closes it again.
    setup = """
        C_Timer.After(1, function()
            client.OpenBank()
            client.bags[6].slots[2], client.bags[0].slots[3] = client.bags[0].slots[3], nil
            client.Fire("BAG_UPDATE_DELAYED")
            client.CloseBank()
        end)
    """
    with live_game(setup):
        result = asyncio.run(inventory.get_inventory(include_bank=True))
    assert [i.name for i in result.bags[1].items] == [
        "Hearthstone", "Linen Cloth", "Raider's Legguards of the Bear", None, "Refreshing Spring Water"]
    bank = result.bank
    assert bank.hours_ago == 0 and abs(bank.seen_at.timestamp() - time.time()) < 10
    assert [(b.name, [(i.slot, i.name, i.count) for i in b.items]) for b in bank.bags] == [
        ("Bank", [(1, "Linen Cloth", 20), (2, "Ruined Pelt", 2), (3, "Light Leather", 10)]),
        ("Bank bag 1", [(2, "Ruined Pelt", 3)]),
    ]


def test_bank_is_saved_while_open(run_client):
    run_client(r"""
        local ns = client.LoadAddon()
        local reply
        local function Ask()
            ns.Stream.Send = function(_, value) reply = value end
            ns.Answer(2, ns.Kinds.ITEMS)
            return reply
        end
        local me = UnitName("player") .. "-" .. GetRealmName()

        -- Not seen yet, and unreadable away from the banker.
        assert(Ask().bank == false and WowMCPDB.bank == nil)
        client.Fire("BAG_UPDATE_DELAYED")
        assert(WowMCPDB.bank == nil)

        -- Opening it saves every tab, with the time.
        client.OpenBank()
        local saved = WowMCPDB.bank[me]
        assert(math.abs(saved.time - os.time()) <= 1)
        assert(#saved.bags == 2 and saved.bags[1].bag == 6 and saved.bags[1].size == 24 and saved.bags[2].bag == 7)
        assert(saved.bags[1].items[1].name == "Linen Cloth" and saved.bags[1].items[1].count == 20)
        assert(#Ask().bags == 4, "bank tabs aren't bags")

        -- Changes while it's open: after each batch of bag updates, and fresh on a request.
        client.bags[7].slots[1], client.bags[0].slots[2] = client.bags[0].slots[2], nil
        client.Fire("BAG_UPDATE_DELAYED")
        local tab = WowMCPDB.bank[me].bags[2]
        assert(#tab.items == 2 and tab.items[1].slot == 1 and tab.items[1].id == 2589)
        client.bags[6].slots[24] = { id = 2318, count = 5 }
        assert(#Ask().bank.bags[1].items == 3)

        -- After closing, the last view stays.
        client.CloseBank()
        client.bags[6].slots[1] = nil
        client.Fire("BAG_UPDATE_DELAYED")
        assert(#Ask().bank.bags[1].items == 3)

        -- Tabs not there yet when it opens: keep the old view until they arrive.
        local Fetch = C_Bank.FetchPurchasedBankTabData
        C_Bank.FetchPurchasedBankTabData = function() return {} end
        client.OpenBank()
        assert(#WowMCPDB.bank[me].bags[1].items == 3)
        C_Bank.FetchPurchasedBankTabData = Fetch
        client.Fire("BANK_TABS_CHANGED", Enum.BankType.Character)
        assert(#WowMCPDB.bank[me].bags[1].items == 2)
        client.CloseBank()

        -- Saved variables are account-wide: another character hasn't seen its bank.
        local Name = UnitName
        UnitName = function() return "Alt" end
        assert(Ask().bank == false)
        UnitName = Name

        -- /reload keeps it.
        wipe(client.tickers); wipe(client.timers); wipe(client.frames)
        ns = client.LoadAddon()
        assert(#Ask().bank.bags[1].items == 2)
    """)


def test_full_bags_and_bank_stay_compact(run_client, tmp_path):
    """Every slot of four 16-slot bags and a full vanilla bank (24 slots and six 16-slot bags)
    holds a different item: the reply should still stream in a second or two."""
    out = tmp_path / "reply.json"
    run_client(r"""
        local words = { "Linen", "Wool", "Silk", "Heavy", "Light", "Leather", "Scale", "Copper", "Bronze", "Iron",
            "Healing", "Mana", "Minor", "Lesser", "Greater", "Band", "Boots", "Gloves", "Cloak", "Tunic", "Pants",
            "Belt", "Bracers", "Raptor", "Wolf", "Spider", "Scroll", "Stamina", "Bear", "Eagle", "Monkey", "Owl" }
        local n = 0
        local function Fill(bag, size)
            client.bags[bag] = { size = size, slots = {} }
            for slot = 1, size do
                n = n + 1
                local id = 20000 + n * 7
                client.items[id] = { name = words[n % #words + 1] .. " " .. words[n * 7 % #words + 1] .. " of the "
                    .. words[n * 13 % #words + 1], quality = n % 5, level = 10 + n % 50, class = n % 2 * 2 + 2, sell = n * 37 % 5000 }
                client.bags[bag].slots[slot] = { id = id, count = n % 3 == 0 and 20 or nil }
            end
        end
        client.bags[-1] = nil
        for bag = 0, 4 do Fill(bag, 16) end
        Fill(6, 24)
        for bag = 7, 12 do Fill(bag, 16) end

        local ns = client.LoadAddon()
        client.OpenBank()
        local reply
        ns.Stream.Send = function(_, value) reply = value end
        ns.Answer(2, ns.Kinds.ITEMS)
        assert(#reply.bank.bags == 7 and #reply.bank.bags[7].items == 16)
        local f = assert(io.open("OUT", "w"))
        f:write(C_EncodingUtil.SerializeJSON(reply))
        f:close()
    """.replace("OUT", str(out)))
    body = out.read_bytes()
    assert len(json.loads(body)["bags"]) == 5
    assert len(zlib.compress(body, 9)) < 5000, len(zlib.compress(body, 9))


def test_reply_quirks(monkeypatch):
    """The real client sends empty Lua tables as {}; unknown values don't break the tool."""
    reply = {
        "bags": [{"bag": 0, "size": 16, "name": "Backpack", "items": {}},
                 {"bag": 3, "size": 10, "items": [{"slot": 4, "id": 999, "quality": 12}]}],
        "equipment": {},
        "bank": {"time": int(time.time()), "bags": [{"bag": 15, "size": 98, "items": {}}]},
    }

    async def fake_request(kind):
        assert kind == kinds.ITEMS
        return reply, "WoW"

    monkeypatch.setattr(inventory, "request", fake_request)
    result = asyncio.run(inventory.get_inventory(include_bank=True))
    assert [(b.name, b.free, len(b.items)) for b in result.bags] == [("Backpack", 16, 0), (None, 9, 1)]
    assert result.bags[1].items[0].model_dump() == item(4, 999, None, quality=None)
    assert result.equipment == [] and result.free_bag_slots == 25
    assert [(b.name, b.free) for b in result.bank.bags] == [("Warband bank tab 1", 98)]

    reply["bank"] = False
    assert asyncio.run(inventory.get_inventory(include_bank=True)).bank is None, "never seen"
    del reply["bank"]
    reply["errors"] = {"bank": "boom"}
    assert asyncio.run(inventory.get_inventory()).bank is None
    with pytest.raises(ToolError, match="couldn't collect bank: boom"):
        asyncio.run(inventory.get_inventory(include_bank=True))
