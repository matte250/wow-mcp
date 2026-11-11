"""Auction house prices (Auction.lua, the auction ping in Goals.lua, tools/auction.py).

The stub auction house (stub/apis/auction.lua) answers each search half a second later, one message at a time,
keeps what Blizzard's window shows, and sends a full scan (ReplicateItems) two seconds after it's asked for, at most
once per 15 minutes. Listed: Medium Hide (4232) 30 at 45c, 12 at 52c (2 of them the player's), 40 at 60c; Salt (4289)
200 at 38c; Light Leather (2318) 80 at 30c; Gray Dye (4340) 3 at 2s 10c; no Fine Thread (2321); Light Hide (783) 5 at
20c; and Toughened Leather Gloves (4253) as single auctions: 1g 20s, 95s (bid 40s), a bid-only one and the player's
own at 80s. The player has 10 Light Leather in the bank, none in their bags. What they could sell from their bags
(stub/apis/inventory.lua), most valuable first: Raider's Legguards (9789), 7 Linen Cloth (2589), 2 Ruined Pelts
(4865), 5 Refreshing Spring Water (159).
"""

import asyncio
import json
import sys
import time
import zlib
from datetime import datetime, timezone

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wow_mcp import kinds
from wow_mcp.tools import auction
from wow_mcp.transport import Transport

PUBLISH_SCRIPT = """
import asyncio, json, sys
from wow_mcp import game
from wow_mcp.tools import auction, goals, plans
game.running_folders = lambda proc=None: set()  # the stub game waits for this script: it can't check item IDs
args = json.loads(sys.argv[1])
if "goals" in args:
    goals.set_goals([goals.Goal(**g) for g in args["goals"]])
elif "plan" in args:
    asyncio.run(plans.show_plan(args["plan"]["title"], [plans.Step(**s) for s in args["plan"]["steps"]]))
elif "watch" in args:
    auction.watch_auction_prices(args["watch"], replace=args.get("replace", False))
"""


def lua_json(value):
    return json.dumps(value).replace("'", "\\u0027")


GOALS = {"goals": [
    {"id": "medium-hide", "title": "6 Medium Hide", "target": {"have": 4232, "count": 6, "name": "Medium Hide"}},
    {"id": "salt", "title": "3 Salt", "target": {"have": 4289, "count": 3, "name": "Salt"}},
]}
PLAN = {"plan": {"title": "Dark Leather Belts", "steps": [
    {"text": "Get 2 Fine Thread", "conditions": [{"have": 2321, "count": 2, "name": "Fine Thread"}]},
    {"text": "Get 10 Light Leather", "conditions": [{"have": 2318, "count": 10}]},
    {"text": "Goal: 6 Medium Hide", "conditions": [{"goal": "medium-hide"}]},
]}}
WATCH = {"watch": [4253, 4340]}
# Everything on the priority list, in the order it's searched when nothing has a price yet.
ALL = "4232,4289,2321,2318,4253,4340,9789,2589,4865,159"

PRELUDE = (
    f"local GOALS, PLAN, WATCH, ALL = [[{lua_json(GOALS)}]], [[{lua_json(PLAN)}]], [[{lua_json(WATCH)}]], '{ALL}'\n"
    + r"""
    -- Text as read: no colours, icons or link codes (Core.lua renders {item:...} as icon plus link).
    local function plain(text)
        return (tostring(text or ""):gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|r", ""):gsub("|T%d+:0|t ", "")
            :gsub("|Hitem:[^|]*|h%[(.-)%]|h", "%1"))
    end
    local function chat() return plain(table.concat(client.chat, "\n")) end
    local function has(text) return chat():find(text, 1, true) ~= nil end
    local function searched() return table.concat(client.AddonSearches(), ",") end
    local function noErrors() assert(not has("error"), chat()) end
    local function realm() return WowMCPDB.auction.Firemaw end
    local function prices() return realm().items end
    -- The watch list's times are the server's clock; the game's runs a minute ahead here, so the goals' and the
    -- plan's items (refreshed at each visit) are the newest on the priority list, as they would be.
    client.epoch = client.epoch + 60
    -- The addon asked for a full scan five minutes ago, and its results never came: it won't ask again yet.
    local function scannedRecently() WowMCPDB.auctionScan = { asked = time() - 300 } end
    -- Everything the AI wants priced: two goals, two plan items, two watched items.
    local function setUp()
        local ns = client.LoadAddon()
        client.Wait()
        client.Publish(GOALS)
        client.Wait(4)
        client.Publish(PLAN)
        client.Wait(4)
        client.Publish(WATCH)
        client.Wait(4)
        return ns
    end
    -- Waits until the addon has read a full scan (and stored it), up to a few minutes.
    local function untilScanned(n)
        for _ = 1, 600 do
            if WowMCPDB.auction and realm().current == n then return end
            client.Tick()
        end
        error("no full scan " .. n)
    end
"""
)


@pytest.fixture
def game(addons, run_client, tmp_path, monkeypatch):
    script = tmp_path / "publish_auction.py"
    script.write_text(PUBLISH_SCRIPT)
    monkeypatch.setenv("WOWMCP_PUBLISH", f"{sys.executable} {script}")
    return lambda lua: run_client(PRELUDE + lua)


def test_full_scan_covers_everything_and_leaves_the_players_view_alone(game):
    game(r"""
        local ns = setUp()
        client.ah.favorites = { 783 }
        client.ah.missingInfo = { [4253] = true } -- the client hasn't loaded the gloves yet
        client.OpenAuctionHouse()
        client.Wait(20)

        -- One full scan, asked for on the Browse view once the window's favourites search was answered; it covered
        -- every item, so nothing was searched.
        local scans = client.AddonScans()
        assert(#scans == 1 and scans[1].mode == "Buy" and scans[1].at >= 1, #scans)
        assert(searched() == "", searched())
        -- Every item listed: the cheapest buyout per item, how many and in how many auctions, as of scan 1; the
        -- player's own left out. The gloves were read before their data loaded.
        local r = realm()
        assert(r.current == 1 and #r.scans == 1 and r.scans[1].items == 6 and r.scans[1].zone == "Durotar")
        assert(r.scan[4232] == "45,80,3,1" and r.scan[4253] == "9500,3,3,1" and r.scan[783] == "20,5,1,1", r.scan[4253])
        assert(r.scan[2321] == nil)
        -- The priority items' cheapest price points come from the scan too, as a search would have had them.
        local hides, gloves = prices()[4232], prices()[4253]
        assert(hides.src == "scan" and hides.price == 45 and hides.listed == 80 and hides.n == 3 and #hides.tiers == 3)
        assert(hides.tiers[2].price == 52 and hides.tiers[2].count == 10 and hides.name == "Medium Hide")
        assert(gloves.price == 9500 and gloves.bidOnly == 1 and gloves.tiers[1].price == 9500 and gloves.tiers[2].price == 12000)
        assert(hides.at == r.scans[1].at and hides.zone == "Durotar" and hides.faction == "Alliance")
        -- Light Hide isn't a priority item: just the scan's entry. Fine Thread: none listed at the scan.
        assert(prices()[783] == nil)
        local light, thread = ns.Auction.Price(783), ns.Auction.Price(2321)
        assert(light.price == 20 and light.listed == 5 and light.src == "scan" and not light.tiers)
        assert(thread.price == nil and thread.listed == 0 and thread.src == "scan" and thread.at == r.scans[1].at)

        -- What the goals and the plan need is pointed out, with what buying it costs.
        assert(has("2 of your goal items are listed here: Medium Hide 80 listed from 45c, 6 for 2s 70c; "
            .. "Salt 200 listed from 38c, 3 for 1s 14c."), chat())
        assert(has("For your plan: Light Leather 80 listed from 30c, 10 for 3s."), chat())
        assert(has("Noted the auction prices of 6 items for your AI (a full scan of the auction house)."), chat())

        -- The window: its Browse view still lists the favourites; nothing reached another view. The scan's event
        -- came again once the gloves loaded: read again into the same scan.
        local browse = AuctionHouseFrame.browse
        assert(#browse == 1 and browse[1].itemKey.itemID == 783 and #client.ah.disturbed == 0)
        assert(client.ah.replicate.reads == 2 * 12 and #r.scans == 1 and r.scans[1].items == 6, client.ah.replicate.reads)
        noErrors()
    """)


def test_searches_leave_the_players_view_alone(game):
    game(r"""
        setUp()
        client.ah.favorites = { 783 }
        client.ah.replicate.at = time() - 300 -- another addon took the account's full scan five minutes ago
        client.OpenAuctionHouse()
        client.Wait(60)

        -- The full scan was asked for, on the Browse view, but the game didn't send it. So one search per item: the
        -- goals', the plan's, the watch list, then what the player could sell from their bags (not the Hearthstone,
        -- the quest item or the arrows); one at a time, all on the Browse view, none sent while another message was
        -- out (the window's favourites search).
        local scans = client.AddonScans()
        assert(#scans == 1 and scans[1].mode == "Buy" and client.ah.replicate.refused == 1)
        assert(searched() == ALL, searched())
        for _, s in ipairs(client.ah.searches) do
            if s.by == "addon" then assert(s.mode == "Buy" and not s.queued, s.item) end
        end
        -- Item searches use the window's own sorts, so its later refresh of one looks the same.
        assert(client.ah.searches[5].item == 4253 and client.ah.searches[5].sorts.mark == "window")
        -- The Browse view still lists the player's favourites; nothing reached another view.
        local browse = AuctionHouseFrame.browse
        assert(#browse == 1 and browse[1].itemKey.itemID == 783 and #client.ah.disturbed == 0)

        -- Cached for this realm: the player's own auctions left out.
        local hides, gloves, thread = prices()[4232], prices()[4253], prices()[2321]
        assert(hides.price == 45 and hides.listed == 80 and hides.commodity and #hides.tiers == 3, hides.listed)
        assert(hides.tiers[2].price == 52 and hides.tiers[2].count == 10 and hides.name == "Medium Hide")
        assert(hides.zone == "Durotar" and hides.faction == "Alliance" and time() - hides.at < 30 and hides.src == "search")
        assert(gloves.price == 9500 and gloves.listed == 3 and gloves.n == 3 and gloves.bidOnly == 1 and not gloves.commodity)
        assert(gloves.tiers[1].price == 9500 and gloves.tiers[2].price == 12000 and #gloves.tiers == 2)
        assert(thread.price == nil and thread.listed == 0)

        -- What the goals and the plan need is pointed out, with what buying it costs.
        assert(has("2 of your goal items are listed here: Medium Hide 80 listed from 45c, 6 for 2s 70c; "
            .. "Salt 200 listed from 38c, 3 for 1s 14c."), chat())
        assert(has("For your plan: Light Leather 80 listed from 30c, 10 for 3s."), chat())
        assert(has("Noted the auction prices of 10 items for your AI."))
        noErrors()
    """)


def test_lookups_wait_while_the_player_looks_at_an_item_or_sells(game):
    game(r"""
        setUp()
        scannedRecently()
        client.OpenAuctionHouse()
        client.Wait(3)
        local before = #client.AddonSearches()
        assert(before >= 1)

        -- The player opens an item from the browse list: no more lookups until they're back.
        client.ViewAuctionItem(783)
        client.Wait(10)
        local n = #client.AddonSearches()
        assert(n <= before + 1, "at most the one already out")
        client.Wait(10)
        assert(#client.AddonSearches() == n and AuctionHouseFrame.view.loaded)
        client.BackToBrowse()
        client.Wait(3)
        assert(#client.AddonSearches() > n)

        -- Selling: nothing either.
        client.SellAuctionItem(4232)
        client.Wait(4)
        n = #client.AddonSearches()
        client.Wait(10)
        assert(#client.AddonSearches() == n)
        client.BackToBrowse()
        client.Wait(20)
        assert(searched() == ALL, searched())
        assert(#client.ah.disturbed == 0 and #client.AddonScans() == 0)
        for _, s in ipairs(client.ah.searches) do
            if s.by == "addon" then assert(s.mode == "Buy") end
        end
        -- The player's own searches are noted too.
        assert(prices()[783].price == 20 and prices()[783].listed == 5)

        -- A full scan that comes due while the player is selling waits for the Browse view too.
        client.epoch = client.epoch + 16 * 60
        client.SellAuctionItem(4232)
        client.Wait(10)
        assert(#client.AddonScans() == 0)
        client.BackToBrowse()
        client.Wait(4)
        assert(#client.AddonScans() == 1 and client.AddonScans()[1].mode == "Buy")
        noErrors()
    """)


def test_searches_stay_under_the_games_limit(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        scannedRecently()
        local many = {}
        for i = 1, 45 do many[i] = 50000 + i end
        client.Publish('{"watch": [' .. table.concat(many, ",") .. ']}')
        client.Wait(4)
        client.OpenAuctionHouse()
        client.Wait(120)
        -- The whole priority list in one visit: the 45 watched items and the four from the bags.
        assert(#client.AddonSearches() == 49, #client.AddonSearches())
        assert(has("Noted the auction prices of 49 items for your AI."), chat())

        -- Again, while the player searches too: never more than 80 messages a minute, counting the player's, so
        -- theirs never hit the game's limit of 100.
        SlashCmdList.WOWMCP("prices")
        assert(has("Looking up the auction prices of 49 items."), chat())
        assert(has("Priority list: 49 items (45 your AI tracks, 4 from your bags and bank); 49 searches left this visit."), chat())
        client.Wait(20)
        for i = 1, 30 do client.PlayerBrowse({ 783 }) end
        client.Wait(300)
        assert(#client.AddonSearches() == 98, #client.AddonSearches())
        for _, m in ipairs(client.ah.log) do
            if m.by == "addon" then
                local within = 0
                for _, other in ipairs(client.ah.log) do
                    if other.at <= m.at and m.at - other.at < 60 then within = within + 1 end
                end
                assert(within <= 80, within)
            end
        end
        assert(client.ah.dropped == 0)

        -- A search the game drops is tried once more, a little later.
        client.CloseAuctionHouse()
        client.Publish('{"watch": [4232], "replace": true}')
        client.Wait(4)
        client.ah.dropNext = true
        client.OpenAuctionHouse()
        client.Wait(30)
        assert(client.ah.dropped == 1 and prices()[4232].price == 45)
        local tries = 0
        for _, item in ipairs(client.AddonSearches()) do
            if item == 4232 then tries = tries + 1 end
        end
        assert(tries == 2, tries)
        -- The watch list was replaced: the 45 others are off the priority list.
        assert(#client.AddonSearches() == 100, #client.AddonSearches())
        noErrors()
    """)


def test_full_scans_come_at_most_every_15_minutes(game):
    game(r"""
        local ns = setUp()
        client.OpenAuctionHouse()
        client.Wait(20)
        client.CloseAuctionHouse()
        assert(#client.AddonScans() == 1 and searched() == "")
        local first = realm().scans[1].at

        -- Five minutes later: no full scan allowed, and the last one's prices are recent, so nothing at all.
        client.epoch = client.epoch + 300
        client.OpenAuctionHouse()
        client.Wait(30)
        assert(#client.AddonScans() == 1 and searched() == "", searched())
        SlashCmdList.WOWMCP("prices")
        assert(has("Auction prices: last full scan 5 min ago (6 items), next one possible in 10 min. Priority list: "
            .. "10 items (6 your AI tracks, 4 from your bags and bank); 10 searches left this visit."), chat())
        client.CloseAuctionHouse()

        -- Twelve minutes after the scan: still none allowed, but the priority items not searched since are looked
        -- up again (the scan is over ten minutes old).
        client.ah.listings[4340] = { commodity = true, results = { { price = 199, quantity = 2 } } }
        client.epoch = client.epoch + 420
        client.OpenAuctionHouse()
        client.Wait(40)
        assert(#client.AddonScans() == 1 and searched() == ALL, searched())
        assert(prices()[4340].price == 199 and prices()[4340].src == "search")
        client.CloseAuctionHouse()

        -- Sixteen minutes after the first: a new full scan, and nothing to search. Gray Dye and Light Hide have
        -- gone since: they keep their last price, as not listed at the latest scan.
        client.ah.listings[4340], client.ah.listings[783] = nil, nil
        client.epoch = client.epoch + 240
        client.OpenAuctionHouse()
        untilScanned(2)
        client.Wait(10)
        assert(#client.AddonScans() == 2 and searched() == ALL, searched())
        local r = realm()
        assert(r.scan[783] == "20,5,1,1" and r.scan[4340] == "210,3,1,1" and r.scan[4232] == "45,80,3,2", r.scan[4340])
        local dye, light = ns.Auction.Price(4340), ns.Auction.Price(783)
        assert(dye.listed == 0 and dye.price == nil and dye.at == r.scans[2].at and dye.src == "scan")
        assert(dye.last.price == 199 and dye.last.listed == 2 and dye.last.at < r.scans[2].at, "the later search")
        assert(light.listed == 0 and light.last.price == 20 and light.last.at == first)
        client.CloseAuctionHouse()
        SlashCmdList.WOWMCP("prices")
        assert(has("Gray Dye: none listed, last "), chat())
        noErrors()
    """)


def test_another_addons_full_scan(game):
    game(r"""
        local ns = setUp()
        -- Another auction addon took the account's full scan a minute ago: the game doesn't answer the addon's, so
        -- after ten seconds it searches instead.
        client.ah.replicate.at = time() - 60
        client.OpenAuctionHouse()
        client.Wait(12)
        assert(#client.AddonScans() == 1 and searched() == "")
        client.Wait(40)
        assert(searched() == ALL and client.ah.replicate.refused == 1, searched())
        client.CloseAuctionHouse()

        -- Fourteen minutes later the other addon scans again as the player arrives: it's read as well, and it's
        -- the account's full scan for the next 15 minutes.
        client.epoch = client.epoch + 14 * 60
        client.ah.replicate.latency = 0.5
        client.OpenAuctionHouse()
        assert(client.OtherAddonScan())
        untilScanned(1)
        client.Wait(10)
        assert(#client.AddonScans() == 1 and searched() == ALL, searched())
        assert(realm().scan[4232] == "45,80,3,1" and time() - WowMCPDB.auctionScan.asked < 15)
        client.CloseAuctionHouse()
        client.epoch = client.epoch + 10 * 60
        client.OpenAuctionHouse()
        client.Wait(10)
        assert(#client.AddonScans() == 1)
        client.CloseAuctionHouse()

        -- Closed before the addon's next full scan arrived: the empty list isn't taken for an empty auction house.
        client.ah.replicate.latency = 2
        client.epoch = client.epoch + 6 * 60
        client.OpenAuctionHouse()
        client.Wait(3)
        assert(#client.AddonScans() == 2)
        client.CloseAuctionHouse()
        client.Wait(10)
        assert(#realm().scans == 1 and realm().current == 1 and ns.Auction.Price(4232).listed == 80)
        noErrors()
    """)


def test_prices_cached_before_full_scans_are_kept(game):
    game(r"""
        WowMCPDB = { auction = { Firemaw = { [4232] = { price = 44, listed = 10, tiers = { { price = 44, count = 10 } },
            at = time() - 60, name = "Medium Hide", zone = "Durotar" } } } }
        local ns = client.LoadAddon()
        local hides = ns.Auction.Price(4232)
        assert(hides.price == 44 and hides.listed == 10 and hides.src == "search" and hides.name == "Medium Hide")
        assert(realm()[4232] == nil and realm().items[4232].price == 44)
    """)


SAVED = r"""
    -- WowMCPDB as the game writes SavedVariables: a key per line, tab-indented, array entries numbered.
    local function Saved(value, depth)
        depth = depth or 0
        if type(value) == "string" then return string.format("%q", value) end
        if type(value) ~= "table" then return tostring(value) end
        local lines, pad, n = { "{" }, string.rep("\t", depth + 1), #value
        for i = 1, n do lines[#lines + 1] = pad .. Saved(value[i], depth + 1) .. ", -- [" .. i .. "]" end
        for k, v in pairs(value) do
            if not (math.type(k) == "integer" and k >= 1 and k <= n) then
                local key = type(k) == "string" and string.format("[%q]", k) or "[" .. tostring(k) .. "]"
                lines[#lines + 1] = pad .. key .. " = " .. Saved(v, depth + 1) .. ","
            end
        end
        lines[#lines + 1] = string.rep("\t", depth) .. "}"
        return table.concat(lines, "\n")
    end
    local function SavedSize() return #("WowMCPDB = " .. Saved(WowMCPDB) .. "\n") end

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

    -- What the full scan should make of a FillAuctionHouse item: "price,listed,auctions".
    local function Expected(itemID)
        local cheapest, listed = nil, 0
        local listing = client.ah.listings[itemID]
        for _, r in ipairs(listing.results) do
            local each = r.price or r.buyout / r.quantity
            cheapest = math.min(cheapest or each, each)
            listed = listed + r.quantity
        end
        return string.format("%d,%d,%d", math.floor(cheapest + 0.5), listed, #listing.results)
    end
"""


def test_a_big_full_scan_is_read_in_small_chunks(game, tmp_path):
    """5,000 items in 20,000 auctions: read over many frames, none long; every item kept, also once it's gone."""
    out = tmp_path / "saved.json"
    game(SAVED + r"""
        client.FillAuctionHouse(100000, 5000, 4)
        local ns = setUp()
        local watch = {}
        for i = 0, 99 do watch[#watch + 1] = { item = 100000 + i * 50, at = time() - 3600 } end
        WowMCP_Receive({ seq = 1000, auctionWatch = { items = watch } })
        client.OpenAuctionHouse()
        untilScanned(1)
        client.Wait(4)
        local r = realm()
        assert(frames >= 20 and slowest < 5, string.format("%d frames, the slowest %.2f ms", frames, slowest))
        local timing = string.format('"frames": %d, "slowest": %.3f', frames, slowest)
        assert(r.scans[1].items == 5006 and r.scans[1].listings == 20000 + 12, r.scans[1].items)
        for _, i in ipairs({ 0, 1, 2, 1234, 4999 }) do
            assert(r.scan[100000 + i] == Expected(100000 + i) .. ",1", r.scan[100000 + i] .. " " .. Expected(100000 + i))
        end
        -- The watched items have their price points; no searches were needed.
        local watched = prices()[100000 + 50]
        assert(watched.src == "scan" and #watched.tiers == 4 and watched.tiers[1].price <= watched.tiers[4].price)
        assert(searched() == "", searched())
        local first = SavedSize()
        local before = Expected(100000)
        client.CloseAuctionHouse()

        -- A day later 1,000 items are gone and 500 are new: every item is still there, the gone ones with their last
        -- price, from the first scan.
        for i = 0, 999 do client.ah.listings[100000 + i] = nil end
        client.FillAuctionHouse(200000, 500, 4)
        client.epoch = client.epoch + 86400
        frames, slowest = 0, 0
        client.OpenAuctionHouse()
        untilScanned(2)
        assert(frames >= 20 and slowest < 5, string.format("%d frames, the slowest %.2f ms", frames, slowest))
        local count = 0
        for _ in pairs(r.scan) do count = count + 1 end
        assert(count == 5506 and r.scans[2].items == 4506, count)
        local gone = ns.Auction.Price(100000)
        assert(gone.listed == 0 and gone.src == "scan" and gone.at == r.scans[2].at)
        assert(gone.last.at == r.scans[1].at and string.format("%d,%d,4", gone.last.price, gone.last.listed) == before)
        local second = SavedSize()
        client.CloseAuctionHouse()

        -- Closed while a scan was being read: it's lost (the list went with the window), nothing half-read is kept,
        -- and the next one has to wait the 15 minutes.
        client.epoch = client.epoch + 16 * 60
        client.OpenAuctionHouse()
        local reads = client.ah.replicate.reads
        for _ = 1, 20 do
            if client.ah.replicate.reads > reads then break end
            client.Tick()
        end
        client.Tick()
        assert(client.ah.replicate.reads > reads and client.ah.replicate.reads - reads < #client.ah.replicate.list,
            "still reading")
        client.CloseAuctionHouse()
        client.Wait(10)
        assert(r.current == 2 and #r.scans == 2 and #client.AddonScans() == 3)
        client.epoch = client.epoch + 5 * 60
        client.OpenAuctionHouse()
        client.Wait(10)
        assert(#client.AddonScans() == 3)

        local f = assert(io.open("OUT", "w"))
        f:write(string.format('{"first": %d, "second": %d, "items": %d, %s}', first, second, count, timing))
        f:close()
        noErrors()
    """.replace("OUT", out.as_posix()))
    sizes = json.loads(out.read_text())
    print(f"SavedVariables: {sizes}")
    # Size isn't capped (every item is kept); this just catches a format that grew by accident.
    assert sizes["second"] / sizes["items"] < 60, sizes


def test_the_priority_list(game):
    game(r"""
        -- Bags: a soulbound item, a quest item and one that bound when equipped (all with a vendor price) aren't for
        -- sale; an unbound one that binds when equipped is. The bank as last seen: another, and one that's bound.
        client.items[90001] = { name = "Bound Band", quality = 2, level = 20, class = 4, sell = 900, bind = "Binds when picked up" }
        client.items[90002] = { name = "Quest Thing", quality = 1, level = 20, class = 12, sell = 800 }
        client.items[90003] = { name = "Worn Cloak", quality = 2, level = 20, class = 4, sell = 700, bind = "Binds when equipped" }
        client.items[90004] = { name = "Fine Cloak", quality = 2, level = 20, class = 4, sell = 500, bind = "Binds when equipped" }
        client.items[90005] = { name = "Banked Ring", quality = 2, level = 20, class = 4, sell = 400, bind = "Binds when equipped" }
        client.items[90006] = { name = "Banked Bound", quality = 2, level = 20, class = 4, sell = 600, bind = "Binds when equipped" }
        client.bags[1].slots = { [1] = { id = 90001 }, [2] = { id = 90002 }, [3] = { id = 90003, bound = true }, [4] = { id = 90004 } }
        client.bags[6].slots[5], client.bags[6].slots[6] = { id = 90005 }, { id = 90006, bound = true }
        local ns = client.LoadAddon()
        client.Wait()
        client.OpenBank()
        client.CloseBank()
        scannedRecently()
        local now = time()
        WowMCP_Receive({ seq = 1000, auctionWatch = { items = { { item = 61001, at = now - 300 }, { item = 61002, at = now - 300 },
            { item = 61003, at = now - 300 } } } })

        -- The AI's three, then what the player could sell, most valuable first by what a vendor pays: the bags'
        -- unbound cloak, the bank's ring, Linen Cloth (7 in the bags, 20 in the bank), the legguards, the bank's
        -- Light Leather, and the rest.
        client.OpenAuctionHouse()
        client.Wait(40)
        local first = "61001,61002,61003,90004,90005,2589,9789,2318,4865,159"
        assert(searched() == first, searched())
        client.CloseAuctionHouse()

        -- The AI watches 100 more: past 100, the oldest go, and the bags' and bank's items have no room.
        local more = {}
        for i = 1, 100 do more[i] = { item = 62000 + i, at = now } end
        WowMCP_Receive({ seq = 1001, auctionWatch = { items = more } })
        SlashCmdList.WOWMCP("prices")
        assert(has("Priority list: 100 items (100 your AI tracks, 0 from your bags and bank)."), chat())
        -- Watching one of the first three again brings it back; the last of the 100 makes room.
        table.insert(more, { item = 61002, at = now + 10 })
        WowMCP_Receive({ seq = 1002, auctionWatch = { items = more } })
        local tracked = realm().tracked
        assert(tracked[61002] and not tracked[61001] and not tracked[61003] and tracked[62001] and not tracked[62100])

        -- Eleven minutes later the prices are stale: the never-priced first, then 61002; nothing from the bags.
        client.epoch = client.epoch + 660
        scannedRecently()
        client.OpenAuctionHouse()
        client.Wait(300)
        local second = {}
        for i = 1, 99 do second[i] = 62000 + i end
        second[100] = 61002
        assert(searched() == first .. "," .. table.concat(second, ","), searched())
        client.CloseAuctionHouse()

        -- Replacing the watch list drops what the AI watched before.
        WowMCP_Receive({ seq = 1003, auctionWatch = { items = {}, cleared = now + 20 } })
        assert(next(realm().tracked) == nil)
        noErrors()
    """)


def test_the_reply_stays_small(game, tmp_path):
    """The priority list's 100 items, or 100 items asked for, from a full scan of 5,000 items: a few KB at most."""
    out = tmp_path / "replies.json"
    game(SAVED + r"""
        local words = { "Heavy", "Light", "Silk", "Wool", "Iron", "Mithril", "Elixir", "Potion", "Leather", "Scale",
            "Gloves", "Boots", "Belt", "Cloak", "Bracers", "of the Owl", "of the Bear", "of the Monkey", "Ore", "Bar" }
        client.FillAuctionHouse(100000, 5000, 6)
        local ns = client.LoadAddon()
        client.Wait()
        local watch = {}
        for i = 0, 99 do
            local itemID = 100000 + i * 7
            watch[#watch + 1] = { item = itemID, at = time() - i }
            client.itemNames[itemID] = words[i % 20 + 1] .. " " .. words[(i * 7) % 20 + 1] .. " " .. words[(i * 3) % 20 + 1]
            client.itemsLoaded[itemID] = true
        end
        WowMCP_Receive({ seq = 1, auctionWatch = { items = watch } })
        client.OpenAuctionHouse()
        untilScanned(1)
        client.Wait(4)
        local reply
        ns.Stream.Send = function(_, value) reply = value end
        ns.Answer(5, ns.Kinds.KNOWLEDGE)
        local list = reply.auction
        assert(#list.prices == 100 and list.priority == 100 and list.tracked == 100 and list.scan.items == 5006)
        assert(#list.prices[1].tiers == 6 and list.prices[1].why == "watch" and list.lookup == nil)

        -- 100 asked for: 50 on the list, 40 only in the scan, 10 the scan didn't see.
        local asked = {}
        for i = 0, 49 do asked[#asked + 1] = 100000 + i * 7 end
        for i = 0, 39 do asked[#asked + 1] = 103001 + i end
        for i = 0, 9 do asked[#asked + 1] = 990000 + i end
        WowMCP_Receive({ seq = 2, auctionWatch = { items = watch, lookup = { id = 2, at = time(), items = asked } } })
        ns.Answer(13, ns.Kinds.KNOWLEDGE)
        local lookup = reply.auction
        assert(lookup.lookup == 2 and #lookup.prices == 100 and lookup.prices[51].item == 103001)
        assert(lookup.prices[100].listed == 0 and lookup.prices[100].price == nil and lookup.prices[100].src == "scan")
        local f = assert(io.open("OUT", "w"))
        f:write(C_EncodingUtil.SerializeJSON({ list = list, lookup = lookup }))
        f:close()
    """.replace("OUT", out.as_posix()))
    replies = json.loads(out.read_text())
    for name, reply in replies.items():
        body = json.dumps(reply, separators=(",", ":")).encode()
        size = len(zlib.compress(body, 9))
        print(f"{name}: {len(body)} bytes, {size} compressed")
        assert size < 5000, (name, size)
        result = auction.AuctionPrices(
            game="x", realm="Firemaw", prices=[auction._price(p, reply["places"]) for p in reply["prices"]], not_seen=[],
            last_full_scan=None, next_full_scan="now", priority_list=100, ai_tracked=100, at_auction_house=False,
            searches_left=None)
        assert len(result.prices) == 100


def test_watch_auction_prices_payload(addons):
    result = auction.watch_auction_prices([4232, 4253, 4232])
    assert result.startswith("Watching 3 item(s) (2 in all): they're on the priority list"), result
    state = Transport(addons).load_state()
    watch = state["auctionWatch"]
    assert watch["id"] == state["seq"] and [w["item"] for w in watch["items"]] == [4232, 4253]
    assert all(abs(w["at"] - time.time()) < 5 for w in watch["items"]) and "cleared" not in watch
    auction.watch_auction_prices([4340, 4232])
    assert [w["item"] for w in Transport(addons).load_state()["auctionWatch"]["items"]] == [4253, 4340, 4232]
    auction.watch_auction_prices(list(range(1, 101)))
    items = [w["item"] for w in Transport(addons).load_state()["auctionWatch"]["items"]]
    assert len(items) == 100 and items[0] == 1, "the oldest go first"
    # A lookup from get_auction_prices stays.
    auction._publish([4232])
    assert auction.watch_auction_prices([], replace=True).startswith("Stopped watching auction prices.")
    watch = Transport(addons).load_state()["auctionWatch"]
    assert watch["items"] == [] and abs(watch["cleared"] - time.time()) < 5 and watch["lookup"]["items"] == [4232]
    with pytest.raises(ToolError, match="Give the item IDs"):
        auction.watch_auction_prices([])
    with pytest.raises(ToolError, match="positive"):
        auction.watch_auction_prices([0])


def run(coro):
    return asyncio.run(coro)


def test_get_auction_prices_asks_for_what_it_needs(addons, monkeypatch):
    now = int(time.time())
    asked = []

    def answer(lookup):
        places = [{"zone": "Ironforge", "faction": "Alliance"}]
        base = {"realm": "Firemaw", "places": places, "nextScan": now + 300, "priority": 12, "tracked": 5,
                "scan": {"at": now - 720, "items": 4812, "listings": 19022, "place": 1}, "open": False}
        if lookup and lookup["items"]:
            prices = [
                {"item": 4232, "name": "Medium Hide", "price": 145, "listed": 80, "n": 3, "commodity": True,
                 "at": now - 720, "src": "scan", "place": 1, "why": "asked",
                 "tiers": [[145, 30], [152, 10]]},
                {"item": 2321, "listed": 0, "at": now - 720, "src": "scan", "place": 1,
                 "last": {"price": 450, "listed": 3, "at": now - 3 * 86400}},
                {"item": 783, "name": "Light Hide", "price": 20, "listed": 5, "n": 1, "at": now - 720, "src": "scan",
                 "place": 1},
            ]
            return {**base, "lookup": lookup["id"], "prices": prices}
        return {**base, **({"lookup": lookup["id"]} if lookup else {}),
                "prices": [{"item": 9789, "price": 4000, "listed": 1, "at": now - 60, "src": "search", "why": "bags",
                            "tiers": {}}]}

    async def request(kind):
        assert kind == kinds.KNOWLEDGE
        lookup = (Transport(addons).load_state().get("auctionWatch") or {}).get("lookup")
        asked.append(lookup)
        return {"auction": answer(lookup)}, "World of Warcraft: Forever 1.60.1"

    monkeypatch.setattr(auction, "request", request)
    monkeypatch.setattr(auction, "LIST_WAIT", 0)

    # The priority list: nothing asked before, so nothing to publish.
    result = run(auction.get_auction_prices())
    assert asked == [None] and Transport(addons).load_state()["seq"] == 0
    (legs,) = result.prices
    assert (legs.item_id, legs.source, legs.tracked, legs.cheapest, legs.where) == (9789, "search", "bags", [], None)
    scan = result.last_full_scan
    assert (scan.items, scan.auctions, scan.seen_ago, scan.where) == (4812, 19022, "12 minutes ago",
                                                                      "Ironforge (seen as Alliance)")
    assert result.next_full_scan == "in 5 minutes" and (result.priority_list, result.ai_tracked) == (12, 5)

    # Items asked for go to the game first; the reply has just those.
    result = run(auction.get_auction_prices([4232, 2321, 783, 4232, 9999]))
    lookup = Transport(addons).load_state()["auctionWatch"]["lookup"]
    assert lookup["items"] == [4232, 2321, 783, 9999] and abs(lookup["at"] - now) < 5
    hides, thread, light = result.prices
    assert (hides.min_price, hides.min_price_text, hides.listed, hides.auctions, hides.seen_ago) == (
        145, "1s 45c", 80, 3, "12 minutes ago")
    assert [p.model_dump() for p in hides.cheapest] == [{"price": 145, "quantity": 30}, {"price": 152, "quantity": 10}]
    assert hides.source == "full scan" and hides.tracked == "asked" and hides.seen_at.tzinfo == timezone.utc
    assert thread.min_price is None and thread.listed == 0 and thread.where == "Ironforge (seen as Alliance)"
    last = thread.last_listed
    assert (last.min_price, last.min_price_text, last.listed, last.seen_ago) == (450, "4s 50c", 3, "3 days ago")
    assert light.cheapest == [] and light.tracked is None
    assert result.not_seen == [9999]
    assert result.note.startswith("No price yet for item(s) 9999: the game checks them at the player's next")
    seq = Transport(addons).load_state()["seq"]

    # The same items again: no new list, so no live-update slot.
    run(auction.get_auction_prices([4232, 2321, 783, 9999]))
    assert Transport(addons).load_state()["seq"] == seq
    # The priority list now needs the lookup emptied.
    run(auction.get_auction_prices())
    assert Transport(addons).load_state()["auctionWatch"]["lookup"]["items"] == []

    # The game keeps answering an older list (e.g. in combat): asked again, then a clear error.
    async def stale(kind):
        return {"auction": {"lookup": 1, "prices": []}}, "World of Warcraft: Forever 1.60.1"

    monkeypatch.setattr(auction, "request", stale)
    with pytest.raises(ToolError, match="hasn't received the list"):
        run(auction.get_auction_prices([4340]))

    async def old(kind):
        return {}, "World of Warcraft: Forever 1.60.1"

    monkeypatch.setattr(auction, "request", old)
    with pytest.raises(ToolError, match="doesn't send auction yet"):
        run(auction.get_auction_prices())


def test_auction_tools_live(live_game, tmp_path):
    visit = tmp_path / "visit"
    setup = f"""
        -- The player goes to the auction house once the test says so.
        C_Timer.NewTicker(0.5, function()
            local f = io.open([[{visit}]])
            if f and not client.visited then
                f:close()
                client.visited = true
                client.OpenAuctionHouse()
            end
        end)
    """
    with live_game(setup, seconds=20):
        result = auction.watch_auction_prices([4232, 4253])
        assert result.startswith("Watching 2 item(s) (2 in all)"), result
        time.sleep(0.5)
        prices = run(auction.get_auction_prices([4232]))
        assert prices.game.startswith("World of Warcraft: Forever") and prices.prices == [] and prices.not_seen == [4232]
        assert prices.last_full_scan is None and not prices.at_auction_house

        visit.write_text("")
        for _ in range(30):
            prices = run(auction.get_auction_prices([4232, 4253, 783, 9999]))
            if prices.last_full_scan:
                break
            time.sleep(0.3)
        by_id = {p.item_id: p for p in prices.prices}
        hides, gloves, light, nothing = by_id[4232], by_id[4253], by_id[783], by_id[9999]
        assert (hides.name, hides.min_price, hides.listed, hides.source, len(hides.cheapest)) == (
            "Medium Hide", 45, 80, "full scan", 3)
        assert (gloves.min_price, gloves.listed, gloves.bid_only, gloves.tracked) == (9500, 3, 1, "watch")
        assert (light.min_price, light.listed, light.tracked) == (20, 5, "asked")
        assert nothing.listed == 0 and nothing.min_price is None and nothing.last_listed is None
        assert hides.where == "Razor Hill, Durotar (seen as Alliance)"
        assert abs((datetime.now(timezone.utc) - hides.seen_at).total_seconds()) < 600
        assert prices.not_seen == [] and prices.at_auction_house and prices.realm == "Firemaw"
        assert prices.last_full_scan.items == 6 and prices.next_full_scan.startswith("in ")

        everything = run(auction.get_auction_prices())
        listed = [p.item_id for p in everything.prices]
        assert listed == [4232, 4253, 783, 9999, 9789, 2589, 4865, 159], listed
        assert everything.priority_list == 8 and everything.ai_tracked == 4, everything
        assert [p.tracked for p in everything.prices[:5]] == ["watch", "watch", "asked", "asked", "bags"]
