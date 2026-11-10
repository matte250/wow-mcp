"""Auction house prices (Auction.lua, the auction ping in Goals.lua, tools/auction.py).

The stub auction house (stub/apis/auction.lua) answers each search half a second later, one message at a time,
and keeps what Blizzard's window shows. Listed: Medium Hide (4232) 30 at 45c, 12 at 52c (2 of them the player's),
40 at 60c; Salt (4289) 200 at 38c; Light Leather (2318) 80 at 30c; Gray Dye (4340) 3 at 2s 10c; no Fine Thread
(2321); Light Hide (783) 5 at 20c; and Toughened Leather Gloves (4253) as single auctions: 1g 20s, 95s (bid 40s),
a bid-only one and the player's own at 80s. The player has 10 Light Leather in the bank, none in their bags.
"""

import asyncio
import json
import sys
import time
from datetime import datetime, timezone

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wow_mcp.tools import auction, goals, plans
from wow_mcp.transport import Transport

PUBLISH_SCRIPT = """
import json, sys
from wow_mcp.tools import auction, goals, plans
args = json.loads(sys.argv[1])
if "goals" in args:
    goals.set_goals([goals.Goal(**g) for g in args["goals"]])
elif "plan" in args:
    plans.show_plan(args["plan"]["title"], [plans.Step(**s) for s in args["plan"]["steps"]])
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

PRELUDE = (
    f"local GOALS, PLAN, WATCH = [[{lua_json(GOALS)}]], [[{lua_json(PLAN)}]], [[{lua_json(WATCH)}]]\n"
    + r"""
    local function plain(text) return (tostring(text or ""):gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|r", "")) end
    local function chat() return plain(table.concat(client.chat, "\n")) end
    local function has(text) return chat():find(text, 1, true) ~= nil end
    local function searched() return table.concat(client.AddonSearches(), ",") end
    local function noErrors() assert(not has("error"), chat()) end
    local function prices() return WowMCPDB.auction.Firemaw end
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
"""
)


@pytest.fixture
def game(addons, run_client, tmp_path, monkeypatch):
    script = tmp_path / "publish_auction.py"
    script.write_text(PUBLISH_SCRIPT)
    monkeypatch.setenv("WOWMCP_PUBLISH", f"{sys.executable} {script}")
    return lambda lua: run_client(PRELUDE + lua)


def test_prices_at_the_auction_house_leave_the_players_view_alone(game):
    game(r"""
        setUp()
        client.ah.favorites = { 783 }
        client.OpenAuctionHouse()
        client.Wait(20)

        -- One search per item: the goals', the plan's, then the watch list; one at a time, all while the window
        -- showed the Browse view, none sent while another message (the window's favourites search) was out.
        assert(searched() == "4232,4289,2321,2318,4253,4340", searched())
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
        assert(hides.zone == "Durotar" and hides.faction == "Alliance" and time() - hides.at < 15)
        assert(gloves.price == 9500 and gloves.listed == 3 and gloves.bidOnly == 1 and not gloves.commodity)
        assert(gloves.tiers[1].price == 9500 and gloves.tiers[2].price == 12000 and #gloves.tiers == 2)
        assert(thread.price == nil and thread.listed == 0)

        -- What the goals and the plan need is pointed out, with what buying it costs.
        assert(has("2 of your goal items are listed here: Medium Hide 80 listed from 45c, 6 for 2s 70c; "
            .. "Salt 200 listed from 38c, 3 for 1s 14c."), chat())
        assert(has("For your plan: Light Leather 80 listed from 30c, 10 for 3s."), chat())
        assert(has("Noted the auction prices of 6 items for your AI."))
        noErrors()
    """)


def test_lookups_wait_while_the_player_looks_at_an_item_or_sells(game):
    game(r"""
        setUp()
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
        assert(searched() == "4232,4289,2321,2318,4253,4340", searched())
        assert(#client.ah.disturbed == 0)
        for _, s in ipairs(client.ah.searches) do
            if s.by == "addon" then assert(s.mode == "Buy") end
        end
        -- The player's own searches are noted too.
        assert(prices()[783].price == 20 and prices()[783].listed == 5)
        noErrors()
    """)


def test_throttle_and_dropped_searches(game):
    game(r"""
        client.LoadAddon()
        client.Wait()
        local many = {}
        for i = 1, 45 do many[i] = 50000 + i end
        client.Publish('{"watch": [' .. table.concat(many, ",") .. ']}')
        client.Wait(4)
        client.OpenAuctionHouse()
        client.Wait(90)
        assert(#client.AddonSearches() == 40, "at most 40 a visit")
        assert(has("Noted the auction prices of 40 items for your AI."))

        -- Again: never more than 50 a minute, so the player's searches never wait for the limit.
        SlashCmdList.WOWMCP("prices")
        assert(has("Looking up the auction prices of 40 items."), chat())
        client.Wait(300)
        local addon = {}
        for _, s in ipairs(client.ah.searches) do
            if s.by == "addon" then table.insert(addon, s.at) end
        end
        assert(#addon == 80, #addon)
        for i, at in ipairs(addon) do
            local within = 0
            for j = i, #addon do
                if addon[j] - at < 60 then within = within + 1 end
            end
            assert(within <= 50, within)
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
        noErrors()
    """)


def test_recent_prices_and_the_prices_command(game):
    game(r"""
        setUp()
        local mcp = SlashCmdList.WOWMCP
        mcp("prices")
        assert(has("No auction prices for what your goals, plan and AI need yet"), chat())
        client.OpenAuctionHouse()
        client.Wait(20)
        client.CloseAuctionHouse()
        local n = #client.AddonSearches()

        -- Back within ten minutes: nothing to look up again, but the goals' items are still pointed out.
        client.OpenAuctionHouse()
        client.Wait(10)
        assert(#client.AddonSearches() == n)
        local said = 0
        for _, line in ipairs(client.chat) do
            if plain(line):find("2 of your goal items are listed here", 1, true) then said = said + 1 end
        end
        assert(said == 2, said)
        -- /mcp prices looks them up anyway.
        mcp("prices")
        assert(has("Looking up the auction prices of 6 items."))
        client.Wait(20)
        assert(#client.AddonSearches() == n + 6)
        client.CloseAuctionHouse()

        -- Away from the auction house it lists them.
        mcp("prices")
        assert(has("Auction prices last seen:") and has("Medium Hide: 45c each, 80 listed"), chat())
        assert(has("Fine Thread: none listed"), chat())

        -- Later, they're looked up again; an auction house addon's own window means no lookups at all.
        client.epoch = client.epoch + 3600
        client.OpenAuctionHouse()
        AuctionHouseFrame:Hide()
        client.Wait(10)
        assert(#client.AddonSearches() == n + 6)
        mcp("prices")
        assert(has("(the game's auction house window isn't showing)"), chat())
        AuctionHouseFrame:Show()
        client.Wait(20)
        assert(#client.AddonSearches() == n + 12)
        noErrors()
    """)


def test_watch_auction_prices_payload(addons):
    result = auction.watch_auction_prices([4232, 4253, 4232])
    assert result.startswith("Watching 3 item(s) (2 in all): the game looks up their prices"), result
    state = Transport(addons).load_state()
    assert state["auctionWatch"] == {"id": state["seq"], "items": [{"item": 4232}, {"item": 4253}]}
    auction.watch_auction_prices([4340, 4232])
    assert [w["item"] for w in Transport(addons).load_state()["auctionWatch"]["items"]] == [4253, 4340, 4232]
    auction.watch_auction_prices(list(range(1, 51)))
    items = [w["item"] for w in Transport(addons).load_state()["auctionWatch"]["items"]]
    assert len(items) == 50 and items[0] == 1, "the oldest go first"
    assert auction.watch_auction_prices([], replace=True).startswith("Stopped watching auction prices.")
    assert Transport(addons).load_state()["auctionWatch"]["items"] == []
    with pytest.raises(ToolError, match="Give the item IDs"):
        auction.watch_auction_prices([])
    with pytest.raises(ToolError, match="positive"):
        auction.watch_auction_prices([0])


def run(coro):
    return asyncio.run(coro)


def test_get_auction_prices_reads_the_cache(addons, monkeypatch):
    reply = {}

    async def request(kind):
        return reply, "World of Warcraft: Forever 1.60.1"

    monkeypatch.setattr(auction, "request", request)
    now = int(time.time())
    reply["auction"] = {"realm": "Firemaw", "watch": [4340], "open": True, "waiting": "waiting for the Browse view", "prices": [
        {"item": 4232, "name": "Medium Hide", "price": 145, "listed": 80, "commodity": True, "at": now - 720,
         "tiers": [{"price": 145, "count": 30}, {"price": 152, "count": 10}], "zone": "Ironforge", "faction": "Alliance"},
        {"item": 2321, "name": "Fine Thread", "listed": 0, "commodity": True, "at": now - 7200, "tiers": {}},
    ]}
    result = run(auction.get_auction_prices([4232, 2321, 4340, 9999]))
    assert result.realm == "Firemaw" and result.at_auction_house and result.watching == [4340]
    hides, thread = result.prices
    assert (hides.min_price, hides.min_price_text, hides.listed, hides.seen_ago) == (145, "1s 45c", 80, "12 minutes ago")
    assert [p.model_dump() for p in hides.cheapest] == [{"price": 145, "quantity": 30}, {"price": 152, "quantity": 10}]
    assert hides.where == "Ironforge (seen as Alliance)" and hides.seen_at.tzinfo == timezone.utc
    assert thread.min_price is None and thread.cheapest == [] and thread.seen_ago == "2 hours ago"
    assert result.not_seen == [4340, 9999]
    assert result.note == ("No price yet for item(s) 4340, 9999: call watch_auction_prices with them so the game "
                           "looks them up the next time the player opens the auction house. The player is at the "
                           "auction house; lookups are waiting for the Browse view.")
    everything = run(auction.get_auction_prices())
    assert len(everything.prices) == 2 and everything.not_seen == []

    reply["auction"] = {"realm": "Firemaw", "prices": {}, "watch": {}, "open": False}
    assert run(auction.get_auction_prices()).note == ("No prices yet: the game looks them up when the player opens "
                                                      "the auction house.")
    del reply["auction"]
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
        assert prices.watching == [4232, 4253] and not prices.at_auction_house

        visit.write_text("")
        for _ in range(30):
            prices = run(auction.get_auction_prices([4232, 4253, 9999]))
            if len(prices.prices) == 2:
                break
            time.sleep(0.3)
        by_id = {p.item_id: p for p in prices.prices}
        hides, gloves = by_id[4232], by_id[4253]
        assert (hides.name, hides.min_price, hides.listed, hides.commodity) == ("Medium Hide", 45, 80, True)
        assert (gloves.min_price, gloves.listed, gloves.bid_only, gloves.commodity) == (9500, 3, 1, False)
        assert hides.where == "Razor Hill, Durotar (seen as Alliance)"
        assert (datetime.now(timezone.utc) - hides.seen_at).total_seconds() < 60
        assert prices.not_seen == [9999] and prices.at_auction_house and prices.realm == "Firemaw"
