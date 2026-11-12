"""Any item's tooltip and stats by ID (Inventory.lua, tools/items.py).

The stub's items are in stub/apis/inventory.lua; its player is a level 12 hunter.
"""

import asyncio
import inspect

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import TypeAdapter, ValidationError

from wow_mcp import kinds
from wow_mcp.tools import items


def details(item_id, status="ok", name=None, quality=None, item_level=None, required_level=None, slot=None,
            type=None, subtype=None, armor=None, stats=None, tooltip=(), shown_in_red=(), binds=None,
            can_be_auctioned=None, held_bound=None, error=None):
    return {"item_id": item_id, "status": status, "name": name, "quality": quality, "item_level": item_level,
            "required_level": required_level, "slot": slot, "type": type, "subtype": subtype, "armor": armor,
            "stats": stats, "tooltip": list(tooltip), "shown_in_red": list(shown_in_red), "binds": binds,
            "can_be_auctioned": can_be_auctioned, "held_bound": held_bound, "error": error}


GLOVES = details(
    4253, name="Toughened Leather Gloves", quality="uncommon", item_level=30, required_level=25, slot="hands",
    type="Armor", subtype="Leather", armor=49, stats={"agility": 6, "attack power": 10, "strength": 4},
    tooltip=["Toughened Leather Gloves", "Binds when equipped", "Hands\tLeather", "49 Armor", "+4 Strength",
             "+6 Agility", "Requires Level 25", "Equip: +10 Attack Power.", "Sell Price: 5s 12c"],
    shown_in_red=["Requires Level 25"], binds="on equip", can_be_auctioned=True,
)
TUNIC = details(
    4243, name="Fine Leather Tunic", quality="uncommon", item_level=17, required_level=12, slot="chest",
    type="Armor", subtype="Leather", armor=59, stats={"stamina": 3},
    tooltip=["Fine Leather Tunic", "Chest\tLeather", "59 Armor", "+3 Stamina", "Requires Level 12",
             "Sell Price: 2s 20c"], binds="no", can_be_auctioned=True,
)
AXE = details(
    12282, name="Worn Battleaxe", quality="common", item_level=2, slot="two-hand", type="Weapon",
    subtype="Two-Handed Axes", stats={"damage per second": 3.1},
    tooltip=["Worn Battleaxe", "Two-Hand\tTwo-Handed Axes", "7 - 11 Damage\tSpeed 2.90", "(3.1 damage per second)",
             "Sell Price: 9c"], binds="no", can_be_auctioned=True,
)

# The player is in a fight when the next update arrives: it's applied when the fight ends, 20 s later.
FIGHT = """
    local fightEnds
    function InCombatLockdown()
        if not fightEnds then
            fightEnds = client.now + 20
            C_Timer.After(20, function() client.Fire("PLAYER_REGEN_ENABLED") end)
        end
        return client.now < fightEnds
    end
"""


@pytest.fixture
def replies(monkeypatch):
    """The itemDetails section of every reply the tool gets, in order."""
    seen = []
    ask = items.request

    async def recording(kind):
        reply, version = await ask(kind)
        seen.append(reply.get("itemDetails"))
        return reply, version

    monkeypatch.setattr(items, "request", recording)
    return seen


def get_item_details(ids):
    return asyncio.run(items.get_item_details(ids))


def test_items_the_player_doesnt_have(live_game, replies):
    with live_game():
        result = get_item_details([4253, 4243, 999999, 12282, 4253])
        asked = len(replies)
        # Asking again for the same items needs no new update: the game has the list.
        again = get_item_details([4253, 4243, 999999, 12282])

    assert result.game.startswith("World of Warcraft: Forever 1.60.1")
    assert [i.model_dump() for i in result.items] == [GLOVES, TUNIC, details(999999, "unknown"), AXE]
    assert again == result and len(replies) == asked + 1
    assert items.transport().load_state() == {
        "seq": 1, "req": asked + 1, "itemLookup": {"id": 1, "items": [4253, 4243, 999999, 12282]}}


def test_item_that_loads_on_a_retry(live_game, replies):
    setup = """
        client.items[4249] = { name = "Dark Leather Belt", quality = 2, level = 25, class = 4, subclass = 2,
            slot = "INVTYPE_WAIST", armor = 40, required = 20, uncached = true, loadTime = 20 }
    """
    with live_game(setup):
        result = get_item_details([4249])
    assert replies[0]["items"][0]["status"] == "loading" and len(replies) >= 2
    belt = result.items[0]
    assert (belt.status, belt.name, belt.armor, belt.slot, belt.shown_in_red) == (
        "ok", "Dark Leather Belt", 40, "waist", ["Requires Level 20"])


def test_answer_before_the_list_arrives(live_game, replies):
    # An earlier lookup, which the game has from Inbox.lua at login.
    items._publish([4243])
    with live_game(FIGHT):
        result = get_item_details([4253])
    assert [i.model_dump() for i in result.items] == [GLOVES]
    assert replies[0]["lookup"] == 1 and replies[-1]["lookup"] == 2 and len(replies) >= 2


def test_addon_lookups(run_client):
    run_client(r"""
        local ns = client.LoadAddon()
        local reply
        local function Ask()
            ns.Stream.Send = function(_, value) reply = value end
            ns.Answer(7, ns.Kinds.ITEM_DETAILS)
            return reply.itemDetails
        end
        local function Lookup(id, list) WowMCP_Receive({ seq = id, itemLookup = { id = id, items = list } }) end

        -- Nothing asked for yet.
        assert(Ask().lookup == nil and #Ask().items == 0)

        -- Bad payloads are ignored; lists are capped at 20 numbers.
        WowMCP_Receive({ seq = 1, itemLookup = "nope" })
        WowMCP_Receive({ seq = 2, itemLookup = { id = 2 } })
        assert(Ask().lookup == nil)
        local many = { "x" }
        for i = 1, 25 do many[#many + 1] = 4243 end
        Lookup(3, many)
        assert(Ask().lookup == 3 and #Ask().items == 20 and Ask().items[1].id == 4243)

        -- Uncached items start loading as soon as the list arrives.
        Lookup(4, { 3164, 4243 })
        assert(client.items[3164].loading and client.items[3164].requested)
        local first = Ask()
        assert(first.lookup == 4 and first.items[1].status == "loading" and first.items[1].type == "Quest")
        assert(first.items[2].status == nil and first.items[2].lines[1].left == "Fine Leather Tunic")
        client.Tick()
        local heart = Ask().items[1]
        assert(heart.status == nil and heart.name == "Discolored Worg Heart" and heart.stats == nil and heart.ilvl == nil)

        -- A tooltip that isn't ready yet counts as loading; one item failing doesn't stop the others.
        local GetItemByID = C_TooltipInfo.GetItemByID
        C_TooltipInfo.GetItemByID = function(id)
            if id == 4243 then return { lines = { { leftText = RETRIEVING_ITEM_INFO } } } end
            if id == 3164 then error("secret value") end
            return GetItemByID(id)
        end
        Lookup(5, { 4243, 3164, 4253 })
        local answer = Ask()
        assert(answer.items[1].status == "loading")
        assert(answer.items[2].status == "error" and answer.items[2].error:find("secret value", 1, true))
        assert(answer.items[3].status == nil and answer.items[3].armor == 49)
        C_TooltipInfo.GetItemByID = GetItemByID

        -- Armor from the tooltip when the stats don't have it; red lines; money lines.
        client.armorNotInStats = true
        local gloves = Ask().items[3]
        assert(gloves.armor == 49 and gloves.stats.RESISTANCE0_NAME == nil and gloves.stats.ITEM_MOD_AGILITY_SHORT == 6)
        assert(gloves.minLevel == 25 and gloves.slot == "INVTYPE_HAND" and gloves.subtype == "Leather")
        local red, price = {}, nil
        for _, line in ipairs(gloves.lines) do
            if line.red then red[#red + 1] = line.left end
            price = price or line.price
        end
        assert(#red == 1 and red[1] == "Requires Level 25" and price == 512)

        -- No such item: loading until the client says so, then unknown.
        Lookup(6, { 999999 })
        assert(Ask().items[1].status == "loading" and Ask().items[1].type == nil)
        client.Tick()
        assert(Ask().items[1].status == "unknown")
        -- Later updates carry the same lookup along; that doesn't start it over.
        WowMCP_Receive({ seq = 7, itemLookup = { id = 6, items = { 999999 } } })
        assert(Ask().items[1].status == "unknown")

        -- The bind type (Enum.ItemBind), and how many the player has, how many of them soulbound.
        Lookup(8, { 2589, 7613, 3164 })
        local cloth, pattern, heart = table.unpack(Ask().items)
        assert(cloth.bind == 0 and cloth.held == 7 and cloth.heldBound == 0)
        assert(pattern.bind == 1 and pattern.held == nil and pattern.heldBound == nil)
        assert(heart.bind == 0 and heart.held == 1 and heart.heldBound == 0)
    """)


def test_what_items_bind_to(live_game):
    # The player wore their Raider's Legguards, which bind when equipped, and one of two pairs of the gloves (the
    # other is in the bank, as last seen). The pattern in their bags bound when they bought it.
    setup = """
        client.items[9789].bind = "Binds when equipped"
        client.bags[0].slots[4].bound = true
        client.bags[1].slots[1] = { id = 4253, bound = true }
        client.bags[1].slots[2] = { id = 7613 }
        WowMCPDB = { bank = { [UnitName("player") .. "-" .. GetRealmName()] = { time = 1700000000, bags = {
            { bag = 6, size = 24, items = { { slot = 1, id = 4253, quality = 2 } } } } } } }
    """
    with live_game(setup):
        result = get_item_details([7613, 7114, 7560, 5085, 5350, 9789, 4253, 2589])
    found = {i.item_id: (i.binds, i.can_be_auctioned, i.held_bound) for i in result.items}
    assert found == {
        7613: ("on pickup", False, True),
        7114: ("on pickup", False, None),
        7560: ("on pickup", False, None),
        5085: ("quest item", False, None),
        5350: ("no", False, None),  # conjured
        9789: ("on equip", False, True),
        4253: ("on equip", True, False),
        2589: ("no", True, False),
    }
    assert "Binds when picked up" in result.items[0].tooltip and "Conjured Item" in result.items[4].tooltip


@pytest.fixture
def fake_game(addons, monkeypatch):
    """The tool against scripted replies: answer(lookup) gives each reply's itemDetails."""
    monkeypatch.setattr(items, "LIST_WAIT", 0)
    monkeypatch.setattr(items, "LOAD_WAIT", 0)
    game = {"asked": 0, "answer": None}

    async def fake_request(kind):
        assert kind == kinds.ITEM_DETAILS
        game["asked"] += 1
        lookup = items.transport().load_state().get("itemLookup")
        return {"itemDetails": game["answer"](lookup)}, "WoW"

    monkeypatch.setattr(items, "request", fake_request)
    return game


def test_retries(fake_game):
    def seq():
        return items.transport().load_state()["seq"]

    # The list arrives late: the first two answers are for no lookup yet, then it's there.
    late = iter([None, None])
    fake_game["answer"] = lambda lookup: {"lookup": next(late, lookup["id"]), "items": [{"id": 1, "name": "A"}]}
    assert get_item_details([1]).items[0].name == "A" and fake_game["asked"] == 3 and seq() == 1

    # The same list again: no new update.
    fake_game["asked"] = 0
    fake_game["answer"] = lambda lookup: {"lookup": lookup["id"], "items": [{"id": 1, "name": "A"}]}
    get_item_details([1, 1])
    assert fake_game["asked"] == 1 and seq() == 1

    # ...unless the game doesn't have it after all (it answers an older one): then it's sent again.
    items._publish([1])
    fake_game["asked"] = 0
    fake_game["answer"] = lambda lookup: {"lookup": 1 if lookup["id"] == 2 else lookup["id"], "items": []}
    get_item_details([1])
    assert fake_game["asked"] == 2 and seq() == 3

    # Another session's newer lookup replaced ours: ours is sent again.
    fake_game["asked"] = 0
    answers = iter([99])
    fake_game["answer"] = lambda lookup: {"lookup": next(answers, lookup["id"]), "items": []}
    get_item_details([2])
    assert fake_game["asked"] == 2 and seq() == 5

    # It never arrives (e.g. a long fight).
    fake_game["asked"] = 0
    fake_game["answer"] = lambda lookup: {"items": {}}
    with pytest.raises(ToolError, match="hasn't received the list of items yet"):
        get_item_details([3])
    assert fake_game["asked"] == items.LIST_RETRIES + 1

    # Still loading after every retry: reported as loading.
    fake_game["asked"] = 0
    fake_game["answer"] = lambda lookup: {"lookup": lookup["id"], "items": [
        {"id": 4, "status": "loading", "type": "Armor", "slot": "INVTYPE_FEET"}, {"id": 5, "name": "B"}]}
    result = get_item_details([4, 5])
    assert fake_game["asked"] == items.LOAD_RETRIES + 1
    assert result.items[0].model_dump() == details(4, "loading", type="Armor", slot="feet")
    assert result.items[1].name == "B"


def test_reply_quirks(fake_game):
    """Empty Lua tables arrive as {}; escape sequences are formatting; odd values don't break the tool."""
    fake_game["answer"] = lambda lookup: {"lookup": lookup["id"], "items": [
        {"id": 1, "name": "Ring", "quality": 4, "slot": "INVTYPE_FINGER", "stats": {}, "lines": [
            {"left": "|cffa335eeRing|r"}, {"left": "", "right": ""}, {"left": "Unique"},
            {"left": "|TInterface\\Icons\\gem:0|t Red Socket"}, {"left": "Use: a ||b|nc", "red": True},
            {"left": "Sell Price:", "price": 10203}, {"left": "|Hitem:5|h[Linked]|h text"}, "junk",
            {"left": "|cnRED_FONT_COLOR:Classes: Rogue|r", "red": True},
        ]},
        {"id": 2, "status": "weird", "lines": {}},
        {"id": 3, "status": "error", "error": "secret value"},
        {"id": 4, "slot": "INVTYPE_NEWTHING", "type": "", "stats": {"RESISTANCE4_NAME": 5}},
    ]}
    result = get_item_details([1, 2, 3, 4, 5])
    ring, weird, failed, new, missing = result.items
    assert ring.tooltip == ["Ring", "Unique", "Red Socket", "Use: a |b\nc", "Sell Price: 1g 2s 3c", "[Linked] text",
                            "Classes: Rogue"]
    assert ring.shown_in_red == ["Use: a |b\nc", "Classes: Rogue"]
    assert (ring.quality, ring.slot, ring.stats) == ("epic", "finger", None)
    assert (weird.status, weird.error, weird.tooltip) == ("error", "unexpected status 'weird'", [])
    assert (failed.status, failed.error) == ("error", "secret value")
    assert (new.status, new.slot, new.type, new.stats) == ("ok", "newthing", None, {"frost resistance": 5})
    assert (missing.status, missing.error) == ("error", "The game didn't report this item.")


def test_binds_from_the_tooltip(fake_game):
    """An addon loaded before it sent the bind type: the tooltip's binding line says. Lines that keep an item off
    the auction house whatever it binds to; the player's own copies bound already."""
    def item(n, bind=None, *lines, **more):
        return {"id": n, "name": f"Item {n}", "lines": [{"left": text} for text in (f"Item {n}", *lines)],
                **({"bind": bind} if bind is not None else {}), **more}

    fake_game["answer"] = lambda lookup: {"lookup": lookup["id"], "items": [
        item(1, None, "Binds when picked up"),
        item(2, None, "Quest Item"),
        item(3, None, "|cffffffffBinds when equipped|r"),
        item(4, None, "Soulbound", "Binds when equipped"),
        item(5, None, "Warbound"),
        item(6, 0, "Conjured Item"),
        item(7, 8),
        item(8, 3, "Binds when used", held=4, heldBound=4),
        item(9, 2, held=3, heldBound=1),
        item(10, 6),  # Enum.ItemBind's unused values
        item(11),
        {"id": 12, "status": "loading", "held": 2},
    ]}
    result = get_item_details(list(range(1, 13)))
    assert [(i.binds, i.can_be_auctioned, i.held_bound) for i in result.items] == [
        ("on pickup", False, None),
        ("quest item", False, None),
        ("on equip", True, None),
        ("on equip", False, None),
        ("warband", False, None),
        ("no", False, None),
        ("warband", False, None),
        ("on use", False, True),
        ("on equip", True, False),
        (None, None, None),
        (None, None, None),
        (None, None, False),
    ]


def test_item_ids_are_checked():
    ids = TypeAdapter(inspect.signature(items.get_item_details).parameters["item_ids"].annotation)
    assert ids.validate_python([4253, 1]) == [4253, 1]
    for bad in ([], [0], list(range(1, items.MAX_ITEMS + 2))):
        with pytest.raises(ValidationError):
            ids.validate_python(bad)
