"""Gear upgrades (the upgrade finder in Auction.lua, the full scan's random suffixes, tools/upgrades.py).

The player is a level 25 Shaman (stub/apis/upgrades.lua): cloth, leather and shields, maces, staves and two-handed
maces; no mail before 40, no daggers, bows or two-handed axes. They wear a Fine Leather Tunic (59 armor, +3 stamina),
Handstitched Leather Pants (24 armor), a one-handed mace with a buckler, and two rings; nothing on head, hands, waist
or feet. On offer (stats made up, prices per item):
- the auction house's last full scan: gloves of every kind (leather, mail, a Paladin-only pair, ones for levels 27 and
  28, a 50g pair, cloth), fur boots, a cloth robe, mail armor, a staff, a two-handed mace, a dagger, a buckler, a
  ring, a bow and Raider's Legguards, which come with random suffixes;
- quest rewards in the log: Blackrock Menace lets them choose Dark Runner Boots or a Sparkmetal Coif, Tharil'zun the
  Belt of Vindication (which the client loads later) or Headbasher, a two-handed axe;
- Gerik Koen in Stormwind sells a leather belt, a mail belt and one they can't buy yet.
"""

import asyncio
import json
import time
import zlib

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wow_mcp import kinds, online_auctions
from wow_mcp.online_auctions import Listing
from wow_mcp.tools import items, upgrades
from wow_mcp.transport import to_lua

SHAMAN = to_lua(upgrades._payload_weights(upgrades.WEIGHTS["SHAMAN"][None][1]))
CASTER = to_lua(upgrades._payload_weights(upgrades.CASTER))

WORLD = r"""
client.CheckYields()
client.Shaman(25)
local function Gear(id, name, loc, class, subclass, required, extra)
    local item = { name = name, quality = 2, level = required + 5, class = class, subclass = subclass, slot = loc,
        required = required, sell = 100 }
    for k, v in pairs(extra or {}) do item[k] = v end
    client.items[id] = item
end
local STR, AGI, STA, INT, SPI, AP = "ITEM_MOD_STRENGTH_SHORT", "ITEM_MOD_AGILITY_SHORT", "ITEM_MOD_STAMINA_SHORT",
    "ITEM_MOD_INTELLECT_SHORT", "ITEM_MOD_SPIRIT_SHORT", "ITEM_MOD_ATTACK_POWER_SHORT"
-- Worn
Gear(80001, "Heavy Spiked Mace", "INVTYPE_WEAPON", 2, 4, 15, { damage = { 10, 19, 2.5 } })
Gear(80002, "Battered Buckler", "INVTYPE_SHIELD", 4, 6, 13, { armor = 250 })
Gear(80003, "Copper Band", "INVTYPE_FINGER", 4, 0, 10, { stats = { [STA] = 2 } })
Gear(80004, "Silver Band", "INVTYPE_FINGER", 4, 0, 15, { stats = { [STR] = 3, [STA] = 2 } })
client.equipment = {
    [5] = { id = 4243, durability = 70, maxDurability = 85 }, [7] = { id = 2302, durability = 40, maxDurability = 55 },
    [11] = { id = 80003 }, [12] = { id = 80004 },
    [16] = { id = 80001, durability = 40, maxDurability = 40 }, [17] = { id = 80002, durability = 50, maxDurability = 50 },
}
-- On the auction house
Gear(80011, "Chainmail Gloves", "INVTYPE_HAND", 4, 3, 22, { armor = 90, stats = { [STR] = 8 } })
Gear(80012, "Gauntlets of the Righteous", "INVTYPE_HAND", 4, 2, 20, { armor = 50, stats = { [STR] = 20 }, classes = { "Paladin" } })
Gear(80013, "Gloves of the Future", "INVTYPE_HAND", 4, 2, 28, { armor = 60, stats = { [STR] = 12 } })
Gear(80014, "Wolfhide Gloves", "INVTYPE_HAND", 4, 2, 27, { armor = 55, stats = { [STR] = 5, [STA] = 5 } })
Gear(80015, "Gilded Gauntlets", "INVTYPE_HAND", 4, 2, 24, { armor = 50, stats = { [STR] = 15 } })
Gear(80016, "Silk Gloves", "INVTYPE_HAND", 4, 1, 20, { armor = 20, stats = { [INT] = 5, [SPI] = 3 } })
Gear(80017, "Fur Boots", "INVTYPE_FEET", 4, 2, 18, { armor = 50, stats = { [STA] = 2 } })
Gear(80041, "Robe of Power", "INVTYPE_ROBE", 4, 1, 22, { armor = 30, stats = { [INT] = 10 } })
Gear(80040, "Chainmail Armor", "INVTYPE_CHEST", 4, 3, 20, { armor = 180, stats = { [STR] = 6 } })
Gear(80030, "Oaken War Staff", "INVTYPE_2HWEAPON", 2, 10, 24, { damage = { 30, 46, 3.2 } })
Gear(80031, "Bonecrusher", "INVTYPE_2HWEAPON", 2, 5, 22, { damage = { 20, 31, 3.0 } })
Gear(80032, "Keen Dagger", "INVTYPE_WEAPON", 2, 15, 20, { damage = { 15, 25, 1.6 } })
Gear(80033, "Iron Buckler", "INVTYPE_SHIELD", 4, 6, 20, { armor = 400, stats = { [STA] = 2 } })
Gear(80005, "Jade Ring", "INVTYPE_FINGER", 4, 0, 20, { stats = { [STR] = 4, [STA] = 3 } })
client.items[9789].random = true -- Raider's Legguards: 72 armor, and a random suffix
local PRICES = { [4253] = 9500, [80011] = 3000, [80012] = 5000, [80013] = 10000, [80014] = 9000, [80015] = 500000,
    [80016] = 2000, [80017] = 4000, [80041] = 7000, [80040] = 6000, [80030] = 20000, [80031] = 15000, [80032] = 8000,
    [80033] = 6000, [80005] = 10000, [2504] = 1500, [9789] = 5000, [2589] = 10 }
local ids = {}
for id in pairs(PRICES) do ids[#ids + 1] = id end
client.scannedAt = time() - 2 * 3600
client.Scanned(ids, function(id) return PRICES[id] end, client.scannedAt)
-- Quest rewards
client.items[2232].armor, client.items[2232].stats, client.items[2232].required = 60, { [AGI] = 4 }, 20
client.items[1282].armor, client.items[1282].required = 80, 22
local belt = client.items[3562]
belt.armor, belt.stats, belt.required, belt.loadTime = 45, { [STR] = 5 }, 22, 1
-- A vendor seen an hour ago
Gear(80020, "Reinforced Leather Belt", "INVTYPE_WAIST", 4, 2, 20, { armor = 40, stats = { [STA] = 3 } })
Gear(80021, "Laminated Scale Belt", "INVTYPE_WAIST", 4, 3, 20, { armor = 80, stats = { [STR] = 5 } })
Gear(80022, "Champion's Girdle", "INVTYPE_WAIST", 4, 2, 20, { armor = 60, stats = { [STR] = 30 } })
WowMCPDB = WowMCPDB or {}
WowMCPDB.knowledge = { vendors = { { name = "Gerik Koen", npc = 5512, zone = "Stormwind City", subzone = "Trade District",
    x = 64.2, y = 60.5, at = time() - 3600, items = {
        { id = 80020, price = 2500 }, { id = 80021, price = 1800 }, { id = 80022, price = 3000, locked = true },
        { id = 159, price = 25, stack = 5 },
    } } } }
"""

HELPERS = r"""
local SHAMAN, CASTER = SHAMAN_WEIGHTS, CASTER_WEIGHTS
local ns = client.LoadAddon()
client.Wait()
local seq = 1000
-- A new payload, as the server publishes it: its seq.
local function Receive(payload)
    seq = seq + 1
    payload.seq = seq
    WowMCP_Receive(payload)
    return seq
end
-- The game's upgrades section now.
local function Ask()
    local reply
    ns.Stream.Send = function(_, value) reply = value end
    ns.Answer(7, ns.Kinds.ITEM_DETAILS)
    return reply.upgrades
end
-- What the game answers for a query, once its search is done.
local function Find(q)
    q.id, q.at, q.weights = seq + 1, time(), q.weights or SHAMAN
    Receive({ upgradeQuery = q })
    for _ = 1, 400 do
        local answer = Ask()
        assert(answer.query == q.id, "answers another query")
        if answer.status ~= "working" then
            assert(answer.status == "done", tostring(answer.error))
            return answer
        end
        client.Tick()
    end
    error("the search never finished")
end
-- The item IDs per slot: "hands=80015,4253,80014 legs=9789", slots without any left out.
local function Ids(answer)
    local parts = {}
    for _, slot in ipairs(answer.slots) do
        local ids = {}
        for _, item in ipairs(slot.items) do ids[#ids + 1] = item.id .. (item.suffix and (":" .. item.suffix) or "") end
        if ids[1] then parts[#parts + 1] = slot.slot .. "=" .. table.concat(ids, ",") end
    end
    return table.concat(parts, " ")
end
local function Slot(answer, name)
    for _, slot in ipairs(answer.slots) do
        if slot.slot == name then return slot end
    end
end
local function Item(answer, id)
    for _, slot in ipairs(answer.slots) do
        for _, item in ipairs(slot.items) do
            if item.id == id then return item end
        end
    end
end
local function Write(path, value)
    local f = assert(io.open(path, "w"))
    f:write(C_EncodingUtil.SerializeJSON(value))
    f:close()
end
""".replace("SHAMAN_WEIGHTS", SHAMAN).replace("CASTER_WEIGHTS", CASTER)


@pytest.fixture
def game(run_client):
    return lambda lua, world=WORLD: run_client(world + HELPERS + lua)


EVERYTHING = ("head=1282 chest=80041 hands=80015,4253,80014 waist=3562,80020 legs=9789 feet=2232,80017 finger=80005 "
              "main hand=80030,80031 off hand=80033")


def test_what_the_character_can_use(game):
    game(f"""
        local answer = Find({{}})
        assert(Ids(answer) == "{EVERYTHING}", Ids(answer))
        -- Not there: mail (gloves, armor, a vendor's belt), the Paladin's gauntlets, gloves for level 28, the dagger,
        -- the bow, Headbasher (a two-handed axe), the vendor's belt they can't buy yet.
        assert(answer.level == 25 and answer.unusable == 1 and answer.notLoaded == 0, answer.unusable)
        -- Every slot asked for is answered, also those with nothing better.
        assert(#answer.slots == 15 and Slot(answer, "neck") and #Slot(answer, "neck").items == 0)
        -- What they wear, with stats, for the server to compare.
        local worn = {{}}
        for _, e in ipairs(answer.equipped) do worn[#worn + 1] = e.slot .. ":" .. e.id end
        assert(table.concat(worn, " ") == "5:4243 7:2302 11:80003 12:80004 16:80001 17:80002", table.concat(worn, " "))
        assert(answer.equipped[1].armor == 59 and answer.equipped[1].stats.ITEM_MOD_STAMINA_SHORT == 3)

        -- Only the slots asked for.
        assert(Ids(Find({{ slots = {{ "hands", "feet" }} }})) == "hands=80015,4253,80014 feet=2232,80017")
        local some = Find({{ slots = {{ "hands" }}, perSlot = 1 }})
        assert(#some.slots == 1 and Ids(some) == "hands=80015", Ids(some))

        -- Mail is fine once they can wear it.
        client.armorSkills[#client.armorSkills + 1] = {{ skillID = 413, name = "Mail", rank = 1, maxRank = 1 }}
        local mail = Find({{ slots = {{ "hands", "chest", "waist" }} }})
        assert(Ids(mail) == "chest=80040,80041 hands=80015,4253,80011 waist=80021,3562,80020", Ids(mail))
    """)


def test_levels_and_prices(game):
    game("""
        -- Two levels ahead by default: the level 27 gloves are in, the level 28 ones not; three ahead, they are.
        local hands = Slot(Find({ slots = { "hands" }, perSlot = 5 }), "hands")
        assert(#hands.items == 4 and hands.items[3].id == 80014 and hands.items[3].req == 27)
        local ahead = Find({ slots = { "hands" }, perSlot = 5, levelAhead = 3 })
        assert(Ids(ahead) == "hands=80015,4253,80013,80014,80016", Ids(ahead))
        local now = Find({ slots = { "hands" }, perSlot = 5, levelAhead = 0 })
        assert(Ids(now) == "hands=80015,4253,80016", Ids(now))

        -- At most 1g each: no Gilded Gauntlets; quest rewards are free, so they stay.
        local cheap = Find({ maxPrice = 10000 })
        assert(Ids(cheap) == "head=1282 chest=80041 hands=4253,80014,80016 waist=3562,80020 legs=9789 feet=2232,80017 "
            .. "finger=80005 off hand=80033", Ids(cheap))
        assert(Item(cheap, 4253).offers[1].price == 9500)
    """)


def test_each_source(game):
    game("""
        -- The auction house: the full scan's price and when it was taken.
        local auction = Find({ sources = { "auction" }, slots = { "hands", "feet", "waist" } })
        assert(Ids(auction) == "hands=80015,4253,80014 feet=80017", Ids(auction))
        local gloves = Item(auction, 4253)
        assert(gloves.name == "Toughened Leather Gloves" and gloves.sub == "Leather" and gloves.req == 25)
        assert(gloves.armor == 49 and gloves.stats.ITEM_MOD_AGILITY_SHORT == 6 and gloves.vs[1] == 10)
        local offer = gloves.offers[1]
        assert(offer.src == "auction" and offer.price == 9500 and offer.listed == 1 and offer.at == client.scannedAt)

        -- Quest rewards: free, with the quest; the belt the client loads a moment later is there too.
        local quests = Find({ sources = { "quests" } })
        assert(Ids(quests) == "head=1282 waist=3562 feet=2232", Ids(quests))
        local boots = Item(quests, 2232).offers[1]
        assert(boots.src == "quest" and boots.quest == "Blackrock Menace" and boots.questID == 20 and boots.choice == 2)
        assert(boots.price == nil and Item(quests, 3562).offers[1].quest == "Tharil'zun")

        -- Vendors: who sells it, where, and for how much.
        local vendors = Find({ sources = { "vendors" } })
        assert(Ids(vendors) == "waist=80020", Ids(vendors))
        local belt = Item(vendors, 80020).offers[1]
        assert(belt.src == "vendor" and belt.vendor == "Gerik Koen" and belt.price == 2500)
        assert(belt.zone == "Stormwind City" and belt.subzone == "Trade District" and belt.x == 64.2)

        -- An item from several: free first, then the cheapest.
        WowMCPDB.knowledge.vendors[1].items[5] = { id = 2232, price = 1200 }
        local both = Item(Find({ slots = { "feet" } }), 2232)
        assert(#both.offers == 2 and both.offers[1].src == "quest" and both.offers[2].price == 1200)
    """)


def test_scores_follow_the_weights(game):
    game("""
        -- The melee Shaman's weights: strength and weapon DPS first. A two-hander replaces mace and buckler.
        local melee = Find({ slots = { "hands", "main hand", "off hand", "finger" } })
        assert(Ids(melee) == "hands=80015,4253,80014 finger=80005 main hand=80030,80031 off hand=80033", Ids(melee))
        local staff = Item(melee, 80030)
        assert(staff.vs[1] == 16 and staff.vs[2] == 17 and staff.stats.ITEM_MOD_DAMAGE_PER_SECOND_SHORT == 11.875)
        -- A ring is compared with the weaker one.
        assert(Item(melee, 80005).vs[1] == 11 and #Item(melee, 80005).vs == 1)

        -- A caster's: the silk gloves come first, and no two-handed mace.
        local caster = Find({ slots = { "hands", "main hand" }, weights = CASTER })
        assert(Ids(caster) == "hands=80016,80014,80015", Ids(caster))

        -- With a two-hander on, off hands aren't compared.
        client.equipment[16], client.equipment[17] = { id = 80031 }, nil
        client.Fire("PLAYER_EQUIPMENT_CHANGED", 16, false)
        local two = Find({ slots = { "main hand", "off hand" } })
        assert(Ids(two) == "main hand=80030", Ids(two))
        assert(Slot(two, "off hand").twoHand == true and #Slot(two, "off hand").items == 0)
    """)


def test_items_asked_about(game):
    game("""
        -- "Is X an upgrade?": listed whatever it scores, with why the character can't use it if they can't.
        local answer = Find({ sources = {}, items = { 80017, 80011, 2589, 80003, 999999 } })
        assert(Ids(answer) == "hands=80011 feet=80017 finger=80003", Ids(answer))
        local mail = Item(answer, 80011)
        assert(mail.asked and mail.cant == "Mail" and #mail.offers == 0)
        assert(Item(answer, 80017).asked and Item(answer, 80017).cant == nil)
        -- The ring they already wear isn't better than the weaker of the two.
        assert(Item(answer, 80003).asked and Item(answer, 80003).vs[1] == 11)
        assert(table.concat(answer.notGear, ",") == "2589,999999")
        -- Asked about with the sources: in the list with its price, even past the slot's per-slot count.
        local hands = Find({ slots = { "hands" }, perSlot = 1, items = { 80016 } })
        assert(Ids(hands) == "hands=80015,80016", Ids(hands))
        assert(Item(hands, 80016).offers[1].price == 2000)
    """)


def test_random_suffixes(game):
    """The full scan keeps each suffix's cheapest price, from the auctions' links; each is a candidate of its own."""
    game(r"""
        client.suffixes[1103] = { name = " of the Monkey", stats = { ITEM_MOD_AGILITY_SHORT = 3, ITEM_MOD_STAMINA_SHORT = 3 } }
        client.suffixes[1104] = { name = " of the Owl", stats = { ITEM_MOD_INTELLECT_SHORT = 3, ITEM_MOD_SPIRIT_SHORT = 3 } }
        client.ah.listings[9789] = { results = {
            { buyout = 12000, quantity = 1, suffix = 1102 }, { buyout = 15000, quantity = 1, suffix = 1102 },
            { buyout = 8000, quantity = 1, suffix = 1103 }, { buyout = 5000, quantity = 1, suffix = 1104 },
        } }
        client.ah.listings[4253] = { results = { { buyout = 9000, quantity = 1 } } }
        -- Bandit Cinch: random suffixes, but its data loads late, so the scan has no links for it at first.
        client.items[80050] = { name = "Bandit Cinch", quality = 2, level = 25, class = 4, subclass = 2,
            slot = "INVTYPE_WAIST", armor = 30, required = 20, random = true }
        client.ah.listings[80050] = { results = { { buyout = 3000, quantity = 1, suffix = 1103 } } }
        client.ah.missingInfo[80050] = true
        WowMCPDB.auction = nil
        client.OpenAuctionHouse()
        for _ = 1, 40 do
            if WowMCPDB.auction.Firemaw.current == 1 then break end
            client.Tick()
        end
        local realm = WowMCPDB.auction.Firemaw
        assert(realm.current == 1, "no scan")
        -- Per suffix, cheapest first; gear without suffixes has none; one whose links weren't there is marked.
        assert(realm.variants[9789] == "1;1104,5000,1;1103,8000,1;1102,12000,2", realm.variants[9789])
        assert(realm.variants[4253] == nil and realm.variants[4232] == nil)
        assert(realm.variants[80050] == "1;?", tostring(realm.variants[80050]))

        -- Each suffix with its own stats and price; the Bear's strength beats the Monkey's agility.
        local answer = Find({ slots = { "legs", "waist" }, sources = { "auction" }, perSlot = 5 })
        assert(Ids(answer) == "waist=80050 legs=9789:1102,9789:1103,9789:1104", Ids(answer))
        local bear, owl = Slot(answer, "legs").items[1], Slot(answer, "legs").items[3]
        assert(bear.name == "Raider's Legguards of the Bear" and bear.offers[1].price == 12000 and bear.offers[1].listed == 2)
        assert(bear.stats.ITEM_MOD_STRENGTH_SHORT == 3 and bear.armor == 72 and not bear.random)
        assert(owl.name == "Raider's Legguards of the Owl" and owl.offers[1].price == 5000 and owl.stats.ITEM_MOD_INTELLECT_SHORT == 3)
        -- The cinch: no suffix known, so as it is, marked random, at the cheapest price of any.
        local cinch = Item(answer, 80050)
        assert(cinch.random and cinch.suffix == nil and cinch.offers[1].random and cinch.offers[1].price == 3000)

        -- Once its data is there, the scan is read again and the cinch has its suffix too.
        for _ = 1, 10 do client.Tick() end
        assert(realm.variants[80050] == "1;1103,3000,1", tostring(realm.variants[80050]))
        local again = Find({ slots = { "waist" }, sources = { "auction" } })
        assert(Ids(again) == "waist=80050:1103" and Item(again, 80050).name == "Bandit Cinch of the Monkey", Ids(again))

        -- From a scan without suffixes kept (before they were), the tooltip still says it's random.
        realm.variants[9789] = nil
        local old = Find({ slots = { "legs" }, sources = { "auction" } })
        assert(Ids(old) == "legs=9789" and Item(old, 9789).random and Item(old, 9789).offers[1].price == 5000, Ids(old))
        -- Worn with a suffix, its stats count.
        client.equipment[7] = { id = 9789, suffix = 1103 }
        local worn
        for _, e in ipairs(Find({ slots = { "legs" } }).equipped) do
            if e.slot == 7 then worn = e end
        end
        assert(worn.name == "Raider's Legguards of the Monkey" and worn.stats.ITEM_MOD_AGILITY_SHORT == 3)
    """)


SAVED = r"""
-- Every OnUpdate's time, in ms.
local Frame = getmetatable(UIParent)
local SetScript, frames, slowest = Frame.SetScript, 0, 0
function Frame:SetScript(name, fn)
    if name == "OnUpdate" and fn then
        local inner = fn
        fn = function(...)
            local started = os.clock()
            inner(...)
            frames, slowest = frames + 1, math.max(slowest, (os.clock() - started) * 1000)
        end
    end
    SetScript(self, name, fn)
end
"""


def test_a_big_scan_is_searched_in_small_chunks(game):
    """20,000 items in the full scan, 5,000 of them gear (every seventh loading late): many frames, none long."""
    game(SAVED + r"""
        client.FillGear(100000, 5000)
        local ids = {}
        for i = 0, 19999 do ids[#ids + 1] = 100000 + i end
        client.Scanned(ids, function(id) return 100 + id % 997 end)
        frames, slowest = 0, 0
        local answer = Find({ perSlot = 5 })
        assert(frames >= 30 and slowest < 5, string.format("%d frames, the slowest %.2f ms", frames, slowest))
        assert(answer.checked >= 20000 and answer.notLoaded == 0, answer.checked)
        -- Leather, cloth, shields and staves; no mail, plate, daggers or bows.
        local types = {}
        for _, slot in ipairs(answer.slots) do
            for _, item in ipairs(slot.items) do
                if item.id >= 100000 then types[item.sub] = true end
                assert(item.req <= 27, item.req)
            end
        end
        assert(types.Leather and types.Cloth and types.Shields and types.Staves)
        assert(not (types.Mail or types.Plate or types.Daggers or types.Bows))
        assert(#Slot(answer, "head").items == 5 and #Slot(answer, "hands").items == 0)

        -- A full scan read while the search walks the table: the walk waits and starts over, and nothing is missed.
        client.FillGear(200000, 300)
        for i = 0, 299 do
            client.items[200000 + i].stats.ITEM_MOD_STRENGTH_SHORT = 100
            client.ah.listings[200000 + i] = { results = { { buyout = 100, quantity = 1 } } }
        end
        WowMCPDB.auctionScan = nil
        client.OpenAuctionHouse()
        local q = { slots = { "back" }, perSlot = 5, sources = { "auction" } }
        local after = Find(q)
        assert(WowMCPDB.auction.Firemaw.current == 3, "the scan didn't finish")
        local ours = 0
        for _, item in ipairs(Slot(after, "back").items) do
            if item.id >= 200000 then ours = ours + 1 end
        end
        assert(ours == 5, Ids(after))
    """)


def test_answers_go_stale(game):
    game("""
        local first = Find({ slots = { "hands" } })
        -- The same query again (every later payload carries it) doesn't search again.
        Receive({ upgradeQuery = { id = first.query, at = time() } })
        assert(Ask() == first)
        -- New gear on: the answer is out of date, so the server asks again.
        client.Fire("PLAYER_EQUIPMENT_CHANGED", 10, false)
        local stale = Ask()
        assert(stale.expired and stale.query == first.query and stale.slots == nil)
        -- Answers are kept two minutes.
        local second = Find({ slots = { "hands" } })
        client.Wait(230)
        assert(Ask().slots and Ask().query == second.query)
        client.Wait(20)
        assert(Ask().expired)

    """)


def test_a_query_from_before_a_reload_isnt_run_again(run_client):
    run_client(WORLD + """
        -- Inbox.lua hands the game the last payload at login, query and all.
        local Fire = client.Fire
        function client.Fire(event, ...)
            if event == "PLAYER_LOGIN" then WowMCP_Receive({ seq = 7, upgradeQuery = { id = 7, at = time() } }) end
            return Fire(event, ...)
        end
        local ns = client.LoadAddon()
        client.Wait()
        local reply
        ns.Stream.Send = function(_, value) reply = value end
        ns.Answer(7, ns.Kinds.ITEM_DETAILS)
        assert(next(reply.upgrades) == nil)
        -- A new one after login is.
        WowMCP_Receive({ seq = 8, upgradeQuery = { id = 8, at = time() } })
        ns.Answer(15, ns.Kinds.ITEM_DETAILS)
        assert(reply.upgrades.query == 8)
    """)


def test_the_character_section_has_the_specialization(run_client):
    run_client("""
        local ns = client.LoadAddon()
        local reply
        ns.Stream.Send = function(_, value) reply = value end
        ns.Answer(0, ns.Kinds.STATE)
        assert(reply.character.class == "Hunter" and reply.character.classFile == "HUNTER" and reply.character.spec == nil)
        client.Shaman(25)
        client.spec = { 2, "Enhancement", "DAMAGER" }
        ns.Answer(8, ns.Kinds.STATE)
        local c = reply.character
        assert(c.class == "Shaman" and c.classFile == "SHAMAN" and c.spec == "Enhancement" and c.role == "DAMAGER")
        assert(c.level == 25)
    """)


def test_the_reply_stays_small(game, tmp_path):
    """Five upgrades in each of the 15 slots, from a scan of 5,000 gear items: a few KB compressed."""
    out = tmp_path / "reply.json"
    game(r"""
        client.FillGear(100000, 5000)
        local ids = {}
        for i = 0, 4999 do ids[#ids + 1] = 100000 + i end
        client.Scanned(ids, function(id) return 1000 + id % 997 end)
        -- A character that can use everything, with nothing on, so every slot has its five.
        local all = {}
        for _, id in ipairs({ 415, 414, 413, 293, 433, 44, 172, 45, 46, 54, 160, 229, 43, 55, 136, 162, 173, 176, 226, 228 }) do
            all[#all + 1] = { skillID = id, name = "Skill " .. id, rank = 1, maxRank = 1 }
        end
        client.armorSkills, client.equipment = all, {}
        local answer = Find({ perSlot = 5 })
        for _, slot in ipairs(answer.slots) do assert(#slot.items == 5, slot.slot .. " " .. #slot.items) end
        Write("OUT", answer)
    """.replace("OUT", str(out)))
    reply = json.loads(out.read_text())
    body = json.dumps(reply, separators=(",", ":")).encode()
    size = len(zlib.compress(body, 9))
    print(f"upgrades: {len(body)} bytes, {size} compressed")
    assert size < 5000, size


# --- The server side ---------------------------------------------------------------------------------------------

def test_default_weights():
    shaman = {"class": "Shaman", "classFile": "SHAMAN", "level": 25}
    used, weights = upgrades._defaults(shaman)
    assert used == ("Shaman (no talent specialization yet): melee (Enhancement) leveling weights; pass weights for "
                    "caster or healer gear")
    assert weights["strength"] == 1 and weights["damage per second"] == 6
    used, weights = upgrades._defaults({**shaman, "spec": "Elemental", "role": "DAMAGER"})
    assert used == "Shaman, Elemental: caster leveling weights" and weights == upgrades.CASTER
    used, _ = upgrades._defaults({**shaman, "spec": "Enhancement"})
    assert used == "Shaman, Enhancement: melee (Enhancement) leveling weights"
    # A specialization by another name: its role decides.
    used, weights = upgrades._defaults({"class": "Druid", "classFile": "DRUID", "spec": "Dreamweaver", "role": "HEALER"})
    assert used == "Druid, Dreamweaver: healer leveling weights" and weights == upgrades.HEALER
    used, _ = upgrades._defaults({"class": "Tinker", "classFile": "TINKER"})
    assert used.startswith("Tinker: balanced")

    # As the game scores: by its stat keys (both of those named "health per 5 sec"), armor and weapon DPS apart.
    assert upgrades._payload_weights({"strength": 1.0, "armor": 0.03, "damage per second": 6.0,
                                      "ranged damage per second": 2.0, "health per 5 sec": 0.5,
                                      "fire resistance": 0.1, "spell power": 0.8}) == {
        "stats": {"ITEM_MOD_STRENGTH_SHORT": 1.0, "ITEM_MOD_HEALTH_REGEN_SHORT": 0.5,
                  "ITEM_MOD_HEALTH_REGENERATION_SHORT": 0.5, "RESISTANCE2_NAME": 0.1, "ITEM_MOD_SPELL_POWER_SHORT": 0.8},
        "armor": 0.03, "melee": 6.0, "ranged": 2.0,
    }
    assert upgrades._clean({" Agility ": 1, "DPS": 4, "ranged dps": 2}) == {
        "agility": 1.0, "damage per second": 4.0, "ranged damage per second": 2.0}
    for bad in ({"str-ength": 1}, {"agility": float("nan")}, {}):
        with pytest.raises(ToolError):
            upgrades._clean(bad)


@pytest.fixture
def saved_reply(game, tmp_path):
    """The game's answer to the default query, as JSON."""
    out = tmp_path / "answer.json"
    game(f'Write("{out}", Find({{}}))')
    return json.loads(out.read_text())


@pytest.fixture
def fake_game(addons, monkeypatch, saved_reply):
    """The tool against the saved answer: the character is the Shaman with 1g 50s; answer(state) gives each reply's
    upgrades section, by default the saved answer for whatever query is published."""
    monkeypatch.setattr(items, "LIST_WAIT", 0)
    monkeypatch.setattr(upgrades, "WORK_WAIT", 0)
    game = {"asked": 0, "answer": None, "character": {"class": "Shaman", "classFile": "SHAMAN", "level": 25}}

    def answer(query):
        return {**saved_reply, "query": query["id"]}

    game["answer"] = answer

    async def fake_request(kind):
        if kind == kinds.STATE:
            return {"character": game["character"], "stats": {"money": 15000, "level": 25}}, "WoW"
        assert kind == kinds.ITEM_DETAILS
        game["asked"] += 1
        return {"upgrades": game["answer"](items.transport().load_state().get("upgradeQuery"))}, "WoW"

    monkeypatch.setattr(items, "request", fake_request)
    monkeypatch.setattr(upgrades, "request", fake_request)
    return game


def find(**kwargs):
    return asyncio.run(upgrades.find_upgrades(**kwargs))


def test_scores_and_explanations(fake_game):
    result = find()
    assert result.character == "level 25 Shaman" and result.money == "1g 50s 0c"
    assert result.weights_used.startswith("Shaman (no talent specialization yet): melee")
    assert result.searched == ["the auction house: full scan 2 hours ago (18 items), and later searches",
                               "rewards of the quests in the log", "vendors the player has visited"]
    by_slot = {s.slot: s for s in result.slots}
    hands = by_slot["hands"]
    assert hands.equipped == [] and [c.item_id for c in hands.upgrades] == [80015, 4253, 80014]
    gauntlets, gloves, wolfhide = hands.upgrades
    # 15 strength and 50 armor against nothing: 15 + 50 * 0.03.
    assert gauntlets.score_change == 16.5 and gauntlets.why == "+15 strength, +50 armor vs an empty slot"
    assert gauntlets.get_it[0].cost == "50g 0s 0c on the auction house (1 listed, full scan 2 hours ago)"
    assert gauntlets.get_it[0].affordable is False and gloves.get_it[0].affordable is True
    assert gloves.score_change == 14.1 and gloves.why == ("+10 attack power, +4 strength, +6 agility, +49 armor vs an "
                                                          "empty slot")
    assert (gloves.required_level, gloves.levels_to_go, gloves.type) == (25, 0, "Leather")
    assert gloves.stats == {"agility": 6, "attack power": 10, "strength": 4} and gloves.armor == 49
    assert wolfhide.levels_to_go == 2 and wolfhide.can_use and not wolfhide.asked

    chest = by_slot["chest"]
    assert [e.name for e in chest.equipped] == ["Fine Leather Tunic"] and chest.equipped[0].score == 3.6
    robe = chest.upgrades[0]
    assert robe.score_change == 1.3 and robe.why == "+10 intellect, -3 stamina, -29 armor vs Fine Leather Tunic"

    staff = by_slot["main hand"].upgrades[0]
    assert staff.why == "+6.1 DPS, -250 armor vs Heavy Spiked Mace and Battered Buckler" and staff.score_change == 29.0
    assert staff.stats == {"damage per second": 11.88}
    finger = by_slot["finger"]
    assert [e.slot for e in finger.equipped] == ["finger 1", "finger 2"]
    assert finger.upgrades[0].why == "+4 strength, +1 stamina vs Copper Band"

    boots = by_slot["feet"].upgrades[0]
    assert boots.get_it[0].cost == "free: quest reward from Blackrock Menace, one of 2 to choose from"
    assert boots.get_it[0].quest_id == 20 and boots.get_it[0].price is None
    belt = by_slot["waist"].upgrades[1]
    assert belt.get_it[0].cost == "25s 0c from Gerik Koen (Trade District, Stormwind City), seen 60 minutes ago"
    legs = by_slot["legs"].upgrades[0]
    assert legs.random_suffix.startswith("unknown: random suffix") and "check the listings" in legs.get_it[0].cost
    assert by_slot["neck"].note == "Nothing better found." and result.note.startswith("Some quests' rewards")

    # Custom weights score with those: the robe's intellect is worth more than the tunic's armor and stamina.
    caster = find(weights={"intellect": 1, "stamina": 0.5})
    assert caster.weights_used == "as asked" and caster.weights == {"intellect": 1.0, "stamina": 0.5}
    robe = {s.slot: s for s in caster.slots}["chest"].upgrades[0]
    assert robe.score_change == 8.5 and robe.why == "+10 intellect, -3 stamina, unweighted: -29 armor vs Fine Leather Tunic"


def test_queries_and_retries(fake_game, monkeypatch):
    def state():
        return items.transport().load_state()

    find(slots=["hands"])
    assert state()["seq"] == 1 and fake_game["asked"] == 1
    query = state()["upgradeQuery"]
    assert query["slots"] == ["hands"] and query["sources"] == ["auction", "quests", "vendors"]
    assert query["weights"]["stats"]["ITEM_MOD_STRENGTH_SHORT"] == 1.0 and query["levelAhead"] == 2
    # The same query again soon: the game still has it, so no new update.
    find(slots=["hands"])
    assert state()["seq"] == 1 and fake_game["asked"] == 2
    # Another one is sent.
    find(slots=["feet"], max_price=5000, item_ids=[4253, 4253])
    assert state()["seq"] == 2 and state()["upgradeQuery"]["maxPrice"] == 5000
    assert state()["upgradeQuery"]["items"] == [4253]
    # Item IDs alone: no sources searched.
    find(item_ids=[4253])
    assert state()["upgradeQuery"]["sources"] == []

    # The game's answer has gone (new gear on, or two minutes): sent again.
    fake_game["asked"] = 0
    answers = iter([{"query": state()["upgradeQuery"]["id"], "expired": True}])
    saved = fake_game["answer"]
    fake_game["answer"] = lambda q: next(answers, None) or saved(q)
    find(item_ids=[4253])
    assert state()["seq"] == 4 and fake_game["asked"] == 2

    # Still searching after every retry: say so.
    fake_game["asked"] = 0
    fake_game["answer"] = lambda q: {"query": q["id"], "status": "working", "checked": 1234}
    with pytest.raises(ToolError, match=r"still searching \(1234 items checked"):
        find(slots=["head"])
    assert fake_game["asked"] == upgrades.WORK_RETRIES + 1
    # ...and asking again picks up the same search.
    fake_game["answer"] = saved
    find(slots=["head"])
    assert state()["seq"] == 5

    fake_game["answer"] = lambda q: {"query": q["id"], "status": "error", "error": "boom"}
    with pytest.raises(ToolError, match="failed in game: boom"):
        find(slots=["neck"])
    # The query never arrives (e.g. a long fight).
    fake_game["answer"] = lambda q: {}
    with pytest.raises(ToolError, match="hasn't received the search yet"):
        find(slots=["shoulder"])

    # An addon from before the upgrade finder.
    fake_game["answer"] = lambda q: None
    async def old(kind):
        return {"itemDetails": {}}, "WoW"
    monkeypatch.setattr(items, "request", old)
    with pytest.raises(ToolError, match="doesn't send upgrades yet"):
        find(slots=["back"])


def test_online_prices(fake_game, monkeypatch):
    """With Blizzard's API set up, auction prices newer than the game's come from the online snapshot; never for one
    random suffix."""
    by_slot = {s.slot: s for s in find().slots}
    legs = by_slot["legs"].upgrades[0].item_id
    now = int(time.time())
    house = online_auctions.House(110, "Alliance Auction House")
    asked = []

    def lookup(items, realm=None, faction=None, seen=(), with_history=False, addons_dir=None, now=None):
        asked.extend(items)
        return online_auctions.Prices(True, house, "matched", now_ - 600, now_ - 86400, notes=["A note."], items={
            80015: online_auctions.ItemPrice(80015, now_ - 600, Listing(400000, 2, 2)),
            4253: online_auctions.ItemPrice(4253, now_ - 5 * 3600, Listing(1, 9, 9)),  # older than the game's
            legs: online_auctions.ItemPrice(legs, now_ - 600, Listing(1234, 9, 9, suffixes=3)),
        })

    now_ = now
    monkeypatch.setattr(online_auctions, "lookup", lookup)
    result = find()
    assert {80015, 4253, legs} <= set(asked)
    by_slot = {s.slot: s for s in result.slots}
    gauntlets, gloves = by_slot["hands"].upgrades[:2]
    assert [o.cost for o in gauntlets.get_it] == ["40g 0s 0c on the auction house (2 listed, online 10 minutes ago)"]
    assert gauntlets.get_it[0].price == 400000 and gauntlets.get_it[0].affordable is False
    assert gloves.get_it[0].cost == "95s 0c on the auction house (1 listed, full scan 2 hours ago)", "the game's is newer"
    offer = by_slot["legs"].upgrades[0].get_it[0]
    assert offer.cost == ("from 12s 34c on the auction house, the cheapest of any random suffix (9 listed, online 10 "
                          "minutes ago): check the listings for the suffix's stats")
    assert "online prices of Alliance Auction House 110 (snapshot 10 minutes ago) for what was found" in result.searched
    assert "A note." in result.note

    # One random suffix's price only comes from the game; a plain item online seen with suffixes is marked random.
    online = {5000: online_auctions.ItemPrice(5000, now - 600, Listing(100, 5, 5, suffixes=4))}
    raw = {"id": 5000, "suffix": "-12:345", "name": "Bandit Gloves of the Monkey", "vs": [],
           "offers": [{"src": "auction", "price": 900, "at": now - 7200, "listed": 1}]}
    suffix = upgrades._candidate(raw, {"agility": 1.0}, {}, 20, 1000, online)
    assert [o.price for o in suffix.get_it] == [900] and suffix.random_suffix.startswith("this exact suffix")
    plain = upgrades._candidate({**raw, "suffix": None}, {"agility": 1.0}, {}, 20, 1000, online)
    assert [o.price for o in plain.get_it] == [100] and plain.random_suffix.startswith("unknown: random suffix")


def test_find_upgrades_live(live_game):
    with live_game(WORLD, seconds=20):
        result = asyncio.run(upgrades.find_upgrades(per_slot=2))
    assert result.game.startswith("World of Warcraft: Forever 1.60.1")
    assert result.character == "level 25 Shaman" and result.money == "12g 34s 56c"
    by_slot = {s.slot: [c.item_id for c in s.upgrades] for s in result.slots}
    assert by_slot["hands"] == [80015, 4253] and by_slot["feet"] == [2232, 80017] and by_slot["waist"] == [3562, 80020]
    assert by_slot["main hand"] == [80030, 80031] and by_slot["neck"] == []
    gloves = next(s for s in result.slots if s.slot == "hands").upgrades[1]
    assert gloves.name == "Toughened Leather Gloves" and gloves.get_it[0].price == 9500
