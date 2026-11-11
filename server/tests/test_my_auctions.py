"""The player's own auctions (Auction.lua's part after the lookups, tools/my_auctions.py).

The stub auction house (stub/apis/auction.lua) has two of the player's auctions: 2 Medium Hide (4232) at 52c each, 20
hours left (the client says the seconds), and Toughened Leather Gloves (4253) at 80s, 30 hours left (only a band: Very
long). It answers QueryOwnedAuctions half a second later, like a search. Its Auctions view asks for them when the
player opens it and after a cancel, and counts an answer the addon asked for while the player has that list as a
disturbance. Sales and expiries come as the game's notice and as a chat line.
"""

import asyncio
import json
import time

import pytest

from wow_mcp import kinds, online_auctions
from wow_mcp.online_auctions import Listing
from wow_mcp.tools import my_auctions

PRELUDE = r"""
    local function plain(text)
        return (tostring(text or ""):gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|r", ""):gsub("|T%d+:0|t ", "")
            :gsub("|Hitem:[^|]*|h%[(.-)%]|h", "%1"))
    end
    local function chat() return plain(table.concat(client.chat, "\n")) end
    local function has(text) return chat():find(text, 1, true) ~= nil end
    local function noErrors() assert(not has("error"), chat()) end
    local function mine() return WowMCPDB.myAuctions["Thorgrim-Firemaw"] end
    local function auction(id)
        for _, a in ipairs(mine().auctions) do
            if a.id == id then return a end
        end
    end
    local function statuses()
        local list = {}
        for _, a in ipairs(mine().auctions) do list[#list + 1] = a.id .. "=" .. a.status end
        table.sort(list)
        return table.concat(list, " ")
    end
    -- The account's full scan was five minutes ago: none at this visit.
    local function scannedRecently() WowMCPDB.auctionScan = { asked = time() - 300 } end
    local ns
    -- What get_my_auctions gets.
    local function Ask()
        local reply
        ns.Stream.Send = function(_, value) reply = value end
        ns.Answer(5, ns.Kinds.KNOWLEDGE)
        return reply.myAuctions
    end
"""


def test_read_at_each_visit_without_disturbing_the_view(run_client):
    run_client(PRELUDE + r"""
        ns = client.LoadAddon()
        client.Wait()
        assert(Ask().seen == nil and #Ask().auctions == 0)
        client.OpenAuctionHouse()
        client.Wait(10)

        -- Asked once, on the Browse view, with the window's own sorts for that list, while the full scan was coming.
        local asked = client.AddonOwnedQueries()
        assert(#asked == 1 and asked[1].mode == "Buy" and asked[1].sorts.mark == "window", #asked)
        assert(#client.AddonScans() == 1 and client.AddonScans()[1].at < asked[1].at)
        -- Kept for this character: item, name, how many, the buyout per item, when seen.
        local hides, gloves = auction(7001), auction(7002)
        assert(hides.item == 4232 and hides.name == "Medium Hide" and hides.count == 2 and hides.price == 52)
        assert(hides.status == "active" and hides.first == hides.seen and mine().seen == hides.seen)
        assert(gloves.item == 4253 and gloves.name == "Toughened Leather Gloves" and gloves.price == 8000)
        assert(gloves.status == "active" and gloves.bid == nil)
        -- The full scan still left the player's own out.
        assert(WowMCPDB.auction.Firemaw.scan[4253] == "9500,3,3,1", WowMCPDB.auction.Firemaw.scan[4253])

        -- The player opens their auctions: Blizzard's list asks, and its answer is read too. Cancelling the gloves
        -- there: the list asks again.
        client.ViewOwnAuctions()
        client.Wait(2)
        assert(AuctionHouseFrame.view.loaded and mine().seen > hides.first)
        client.CancelOwnAuction(7002)
        client.Wait(4)
        assert(auction(7002).status == "cancelled" and auction(7002).how == "cancel")
        assert(#client.ah.ownedQueries == 3 and #client.AddonOwnedQueries() == 1)
        -- Something posted while the player has that list (a multisell finishing): nothing is asked until they're
        -- back on the Browse view, as the answer would clear their selection there.
        client.Fire("AUCTION_HOUSE_AUCTION_CREATED", 7099)
        client.Wait(10)
        assert(#client.AddonOwnedQueries() == 1)
        client.BackToBrowse()
        client.Wait(3)
        assert(#client.AddonOwnedQueries() == 2)

        -- The player sells 5 Light Leather at 33c: asked again once they're back on the Browse view.
        client.SellAuctionItem(2318)
        local posted = client.PostAuction({ item = 2318, quantity = 5, price = 33, hours = 12 })
        client.Wait(10)
        assert(#client.AddonOwnedQueries() == 2, "not while selling")
        client.BackToBrowse()
        client.Wait(3)
        assert(#client.AddonOwnedQueries() == 3)
        local leather = auction(posted)
        assert(leather.item == 2318 and leather.count == 5 and leather.price == 33 and leather.status == "active")
        assert(math.abs(leather.expires - (time() + 12 * 3600)) <= 15 and leather.left == "seconds")

        -- None of it went out anywhere but on the Browse view, or reached what the player looked at.
        for _, q in ipairs(client.AddonOwnedQueries()) do assert(q.mode == "Buy" and q.sorts.mark == "window") end
        assert(#client.ah.disturbed == 0)
        -- Under the game's limit, counted with the searches: one message a visit, and one per post or cancel.
        assert(client.ah.dropped == 0)
        noErrors()
    """)


def test_the_panel_says_so_briefly(run_client):
    run_client(PRELUDE + r"""
        local function line() return WowMCPAuctionPanel and plain(WowMCPAuctionPanel.Text:GetText()) or "" end
        client.Scanned({ 4232, 4253, 9789, 2589, 4865, 159 }, 100) -- every price is fresh: nothing else to do
        ns = client.LoadAddon()
        client.Wait()
        scannedRecently()
        client.ah.ownedLatency = 2 -- a slow answer
        client.OpenAuctionHouse()
        client.Wait(3)
        assert(line() == "Reading your auctions…", line())
        client.Wait(4)
        assert(line():find("^Prices up to date"), line())
        -- It doesn't bring the panel back once it has faded.
        client.Wait(20)
        assert(not WowMCPAuctionPanel:IsShown())
        client.SellAuctionItem(2318)
        client.PostAuction({ item = 2318, quantity = 5, price = 33 })
        client.Wait(4)
        client.BackToBrowse()
        client.Wait(3)
        assert(#client.AddonOwnedQueries() == 2 and not WowMCPAuctionPanel:IsShown())
        noErrors()
    """)


def test_time_left(run_client):
    run_client(PRELUDE + r"""
        ns = client.LoadAddon()
        client.Wait()
        scannedRecently()
        client.OpenAuctionHouse()
        client.Wait(4)
        client.CloseAuctionHouse()
        local t1 = mine().seen
        -- The hides: the seconds left, so exactly when.
        local hides, gloves = auction(7001), auction(7002)
        assert(hides.left == "seconds" and not hides.soonest)
        assert(math.abs(hides.expires - (client.epoch + 20 * 3600)) <= 2)
        -- The gloves: only a band (Very long, 12 to 48 hours, from the client), so between.
        assert(gloves.left == "band" and gloves.band == 3)
        assert(gloves.soonest == t1 + 12 * 3600 and gloves.expires == t1 + 48 * 3600, gloves.expires - t1)

        -- An hour later 10 hours are left: Long (2 to 12 hours). Both looks hold: it ends 12 to 13 hours after the
        -- first.
        client.epoch = client.epoch + 3600
        client.ah.owned[2].ends = client.now + 10 * 3600
        client.OpenAuctionHouse()
        client.Wait(4)
        client.CloseAuctionHouse()
        local t2 = mine().seen
        gloves = auction(7002)
        assert(t2 - t1 >= 3600 and gloves.band == 2)
        assert(gloves.soonest == t1 + 12 * 3600 and gloves.expires == t2 + 12 * 3600, gloves.expires - t2)

        -- A client that can't say what a band is: retail's bands.
        C_AuctionHouse.GetTimeLeftBandInfo = nil
        client.epoch = client.epoch + 9 * 3600
        client.ah.owned[2].ends = client.now + 3600
        client.OpenAuctionHouse()
        client.Wait(4)
        local t3 = mine().seen
        gloves = auction(7002)
        assert(gloves.band == 1 and gloves.soonest == t1 + 12 * 3600 and gloves.expires == t3 + 7200)
        assert(Ask().auctions[1].id == 7002, "listed first: it ends first")
        noErrors()
    """)


def test_a_sale_is_noticed_once(run_client):
    run_client(PRELUDE + r"""
        -- Three lots of Medium Hide: 2 at 52c, 5 at 48c, 3 at 50c; and the gloves.
        table.insert(client.ah.owned, { id = 7003, item = 4232, quantity = 5, buyout = 48, ends = 20 * 3600 })
        table.insert(client.ah.owned, { id = 7004, item = 4232, quantity = 3, buyout = 50, ends = 20 * 3600 })
        ns = client.LoadAddon()
        client.Wait()
        scannedRecently()
        client.OpenAuctionHouse()
        client.Wait(4)
        client.CloseAuctionHouse()

        -- Away from the auction house, the 2 at 52c sell: the game's notice and a chat line both say so. Once.
        client.SellOwnAuction(7001)
        local hides = auction(7001)
        assert(hides.status == "sold" and hides.how == "notice" and hides.at == time())
        assert(#mine().notices == 1 and mine().notices[1].paired and mine().notices[1].auction == 7001)
        assert(statuses() == "7001=sold 7002=active 7003=active 7004=active", statuses())

        -- The chat line first: it only names the item, so it takes the cheapest; the notice then names the auction.
        client.Wait(30)
        client.SellOwnAuction(7004, { notice = false, chat = false })
        client.Fire("CHAT_MSG_SYSTEM", "A buyer has been found for your auction of Medium Hide.")
        assert(statuses() == "7001=sold 7002=active 7003=sold 7004=active", statuses())
        client.Fire("AUCTION_HOUSE_SHOW_FORMATTED_NOTIFICATION", Enum.AuctionHouseNotification.AuctionSold,
            "Medium Hide", 7004)
        assert(statuses() == "7001=sold 7002=active 7003=active 7004=sold", statuses())
        assert(auction(7004).how == "notice" and #mine().notices == 2)

        -- Two sales the chat alone says, one after the other: two sales. The second is of an auction the game hadn't
        -- listed.
        client.Wait(30)
        client.SellOwnAuction(7003, { notice = false })
        client.Fire("CHAT_MSG_SYSTEM", "A buyer has been found for your auction of Medium Hide.")
        assert(statuses() == "7001=sold 7002=active 7003=sold 7004=sold", statuses())
        assert(#mine().notices == 4 and mine().notices[4].auction == nil)

        -- The gloves expire: the notice, the chat line and the expiry event are one.
        client.ExpireOwnAuction(7002, { mail = false })
        client.Fire("AUCTION_HOUSE_AUCTIONS_EXPIRED", 7002)
        assert(auction(7002).status == "expired" and auction(7002).how == "notice" and #mine().notices == 5)
        -- Outbid on something the player bid on: kept as news, about no auction of theirs.
        client.Fire("CHAT_MSG_SYSTEM", "You have been outbid on Gray Dye.")
        client.Fire("CHAT_MSG_SYSTEM", "Someone says hello.")
        assert(#mine().notices == 6 and mine().notices[6].kind == "outbid" and mine().notices[6].name == "Gray Dye")

        -- What the AI gets: every auction, the latest ended first, and the news that's about none of them.
        local reply = Ask()
        assert(#reply.auctions == 4 and reply.seen == mine().seen and reply.open == false)
        local notices = {}
        for _, n in ipairs(reply.notices) do notices[#notices + 1] = n.kind .. ":" .. n.name end
        assert(table.concat(notices, ",") == "sold:Medium Hide,outbid:Gray Dye", table.concat(notices, ","))
        -- The next visit: the sold ones are listed as sold until their money goes out.
        client.OpenAuctionHouse()
        client.Wait(4)
        assert(statuses() == "7001=sold-pending 7002=expired 7003=sold-pending 7004=sold-pending", statuses())
        assert(auction(7001).how == "notice" and math.abs(auction(7001).due - (hides.at + 3600)) <= 2)
        noErrors()
    """)


def test_the_mail_settles_what_the_notices_missed(run_client):
    run_client(PRELUDE + r"""
        -- Two lots of 5 Light Hide at 20c each, alike.
        table.insert(client.ah.owned, { id = 7005, item = 783, quantity = 5, buyout = 20, ends = 20 * 3600 })
        table.insert(client.ah.owned, { id = 7006, item = 783, quantity = 5, buyout = 20, ends = 20 * 3600 })
        ns = client.LoadAddon()
        client.Wait()
        scannedRecently()
        client.OpenAuctionHouse()
        client.Wait(4)
        client.CloseAuctionHouse()

        -- A sale mail from two days ago, for an earlier lot of hides alike: older than what the list showed, so it
        -- settles nothing.
        client.Deliver({ sender = "Auction House", subject = "Auction successful: Medium Hide", money = 99,
            expires = time() + 28 * 86400, invoice = { type = "seller", item = "Medium Hide", player = "Bob", bid = 104,
            buyout = 104, fee = 5, count = 2 } })
        client.OpenMailbox()
        client.Wait(2)
        client.CloseMailbox()
        assert(statuses() == "7001=active 7002=active 7005=active 7006=active", statuses())

        -- An hour later, with no notice seen (the player was offline): the hides sold, the gloves expired, one lot of
        -- Light Hide sold.
        client.epoch = client.epoch + 3600
        client.SaleMail(7001)
        client.ExpireOwnAuction(7002, { notice = false, chat = false })
        client.SaleMail(7005)
        client.OpenMailbox()
        client.Wait(2)
        assert(auction(7001).status == "sold" and auction(7001).how == "mail" and auction(7001).at <= time())
        assert(auction(7002).status == "expired" and auction(7002).how == "mail")
        -- A mail settles one auction.
        assert(statuses() == "7001=sold 7002=expired 7005=sold 7006=active", statuses())

        -- The reply has the auction house's mail: what's in it, and what came of it.
        local mail = Ask().mail
        assert(mail.at == WowMCPDB.mail["Thorgrim-Firemaw"].time and #mail.sales == 4)
        local kinds = {}
        for _, s in ipairs(mail.sales) do kinds[#kinds + 1] = s.kind .. ":" .. s.name .. ":" .. s.count end
        assert(table.concat(kinds, ",") == "sold:Light Hide:5,expired:Toughened Leather Gloves:1,sold:Medium Hide:2,"
            .. "sold:Medium Hide:2", table.concat(kinds, ","))
        assert(mail.sales[1].money == 95 and mail.sales[1].price == 100 and mail.sales[1].fee == 5)
        assert(mail.sales[2].item == 4253)
        noErrors()
    """)


def test_prices_come_along(run_client):
    run_client(PRELUDE + r"""
        ns = client.LoadAddon()
        client.Wait()
        client.OpenAuctionHouse()
        for _ = 1, 40 do
            if WowMCPDB.auction and WowMCPDB.auction.Firemaw and WowMCPDB.auction.Firemaw.current then break end
            client.Tick()
        end
        client.Wait(2)
        -- The game's price of each listed item, as get_auction_prices has it (the player's own left out).
        local reply = Ask()
        assert(reply.open and reply.realm == "Firemaw" and reply.faction == "Alliance" and #reply.prices == 2)
        local hides, gloves = reply.prices[1], reply.prices[2]
        if hides.item ~= 4232 then hides, gloves = gloves, hides end
        assert(hides.item == 4232 and hides.price == 45 and hides.listed == 80 and hides.src == "scan")
        assert(gloves.item == 4253 and gloves.price == 9500 and gloves.listed == 3)
        assert(reply.places[1].zone == "Durotar")
        noErrors()
    """)


# get_my_auctions, against a reply shaped as the game sends it.

NOW = int(time.time())
HOUR = 3600


def reply_(**changes):
    data = {
        "seen": NOW - 3 * HOUR, "open": False, "realm": "Firemaw", "faction": "Alliance",
        "auctions": [
            {"id": 7001, "item": 4232, "name": "Medium Hide", "count": 2, "price": 52, "status": "active",
             "first": NOW - 5 * HOUR, "seen": NOW - 3 * HOUR, "expires": NOW + 17 * HOUR + 600, "left": "seconds"},
            {"id": 7003, "item": 4232, "name": "Medium Hide", "count": 5, "price": 60, "status": "active",
             "first": NOW - 5 * HOUR, "seen": NOW - 3 * HOUR, "expires": NOW + 17 * HOUR + 600, "left": "seconds"},
            {"id": 7002, "item": 4253, "name": "Toughened Leather Gloves", "count": 1, "price": 8000, "bid": 5000,
             "status": "active", "first": NOW - 5 * HOUR, "seen": NOW - 3 * HOUR, "expires": NOW + 45 * HOUR + 600,
             "soonest": NOW + 9 * HOUR + 600, "left": "band"},
            {"id": 7007, "item": 4340, "name": "Gray Dye", "count": 3, "price": 210, "status": "active",
             "first": NOW - 30 * HOUR, "seen": NOW - 3 * HOUR, "expires": NOW - HOUR, "left": "seconds"},
            {"id": 7008, "item": 2319, "name": "Medium Leather", "count": 10, "price": 40, "status": "active",
             "first": NOW - 5 * HOUR, "seen": NOW - 3 * HOUR, "expires": NOW + 9 * HOUR + 600, "soonest": NOW - HOUR,
             "left": "band"},
            {"id": 7004, "item": 4232, "name": "Medium Hide", "count": 3, "price": 50, "status": "sold",
             "first": NOW - 5 * HOUR, "seen": NOW - 3 * HOUR, "at": NOW - 2 * HOUR, "how": "notice"},
            {"id": 7009, "item": 2589, "name": "Linen Cloth", "count": 20, "price": 10, "status": "sold-pending",
             "first": NOW - 5 * HOUR, "seen": NOW - 3 * HOUR, "at": NOW - 3 * HOUR - 600, "how": "list",
             "due": NOW - 3 * HOUR + 3000},
            {"id": 7010, "item": 2318, "name": "Light Leather", "count": 10, "price": 30, "status": "sold",
             "first": NOW - 30 * HOUR, "seen": NOW - 26 * HOUR, "at": NOW - 25 * HOUR, "how": "mail"},
            {"id": 7011, "item": 159, "name": "Refreshing Spring Water", "count": 5, "price": 5, "status": "gone",
             "first": NOW - 60 * HOUR, "seen": NOW - 26 * HOUR, "at": NOW - 3 * HOUR, "expires": NOW - 10 * HOUR,
             "how": "list"},
        ],
        "notices": [{"kind": "outbid", "name": "Fine Leather Tunic", "at": NOW - 4 * HOUR}],
        "places": [{"zone": "Ironforge", "faction": "Alliance"}],
        "prices": [
            # The full scan three hours ago; the player's own left out.
            {"item": 4232, "name": "Medium Hide", "price": 45, "listed": 80, "n": 3, "commodity": True,
             "at": NOW - 3 * HOUR, "src": "scan", "place": 1, "tiers": [[45, 30], [52, 10], [60, 40]]},
            # A search just now at the auction house: newer than the online snapshot.
            {"item": 4253, "name": "Toughened Leather Gloves", "price": 9500, "listed": 3, "n": 3, "bidOnly": 1,
             "at": NOW - 60, "src": "search", "place": 1, "tiers": [[9500, 1], [12000, 1]]},
            # Only the full scan's cheapest price.
            {"item": 2319, "name": "Medium Leather", "price": 35, "listed": 40, "n": 4, "at": NOW - 3 * HOUR,
             "src": "scan", "place": 1},
        ],
        "mail": {"at": NOW - 24 * HOUR, "sales": [
            {"kind": "sold", "name": "Light Leather", "count": 10, "money": 285, "price": 300, "fee": 15,
             "expires": NOW + 29 * 86400},
            {"kind": "pending", "name": "Wool Cloth", "count": 1, "price": 3000, "fee": 150, "eta": "14:05",
             "expires": NOW + 29 * 86400},
            {"kind": "expired", "name": "Gray Dye", "item": 4340, "count": 3, "expires": NOW + 29 * 86400},
        ]},
    }
    data.update(changes)
    return data


@pytest.fixture
def fake_game(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)  # the reply's times are NOW's
    state = {"data": reply_(), "online": None, "asked": []}

    async def request(kind):
        assert kind == kinds.KNOWLEDGE
        return {"myAuctions": state["data"]}, "World of Warcraft: Forever 1.60.1"

    def lookup(items, realm=None, faction=None, seen=(), with_history=False, addons_dir=None, now=None):
        if items:
            state["asked"].append((list(items), realm, faction, len(list(seen))))
        return state["online"] or online_auctions.Prices(notes=["Online prices are off."])

    monkeypatch.setattr(my_auctions, "request", request)
    monkeypatch.setattr(online_auctions, "lookup", lookup)
    return state


def get():
    return asyncio.run(my_auctions.get_my_auctions())


def test_my_auctions_against_the_game_and_the_online_snapshot(fake_game):
    house = online_auctions.House(110, "Alliance Auction House")
    # An hour-old snapshot: newer than the game's full scan of the hides, older than its search of the gloves. It has
    # the player's own: 2 at 52c and 5 at 60c.
    fake_game["online"] = online_auctions.Prices(True, house, "matched", NOW - HOUR, NOW - 86400, items={
        4232: online_auctions.ItemPrice(4232, NOW - HOUR, Listing(45, 92, 5, tiers=[(45, 35), (52, 12), (60, 45)])),
        4253: online_auctions.ItemPrice(4253, NOW - HOUR, Listing(7000, 2, 2, tiers=[(7000, 1), (8000, 1)])),
    })
    result = get()
    assert result.game.startswith("World of Warcraft: Forever") and result.realm == "Firemaw"
    assert result.seen_ago == "3 hours ago" and not result.at_auction_house
    # Asked online about what's listed, with the game's prices as the evidence of which house.
    assert fake_game["asked"] == [([4232, 4253, 4340, 2319], "Firemaw", "Alliance", 3)]

    two, five, gloves, dye, leather = result.listed
    # The hides: online, less the player's own (2 at 52c, 5 at 60c), so 35 at 45c, 10 at 52c, 40 at 60c are others'.
    c = two.competition
    assert (two.price_text, two.buyout, two.status, two.time_left, two.time_left_from) == (
        "52c", 104, "active", "about 17 hours", "seconds")
    assert (c.verdict, c.cheapest_other, c.difference_text, c.cheaper_listed, c.cheaper_at_least) == (
        "undercut", 45, "7c", 35, False)
    assert (c.source, c.seen_ago, c.others_listed) == ("online", "60 minutes ago", 85)
    assert (five.competition.verdict, five.competition.difference, five.competition.cheaper_listed) == (
        "undercut", 15, 45)
    # The gloves: the game's search is newer, and has the player's left out already; 80s is the cheapest.
    c = gloves.competition
    assert (c.verdict, c.cheapest_other_text, c.difference_text, c.source, c.others_listed) == (
        "cheapest", "95s 0c", "15s 0c", "search", 3)
    assert gloves.bid_text == "50s 0c" and gloves.time_left == "between 9 hours and 45 hours"
    assert gloves.time_left_from == "band" and gloves.ends_earliest is not None
    # Its time ran out since: probably expired, in the mailbox; none of its prices were seen.
    assert (dye.status, dye.competition) == ("probably expired", None)
    assert dye.time_left == "ran out 60 minutes ago: probably expired, the item's in the mailbox"
    # A band whose start has passed; only the full scan's cheapest price: undercut, by how many not known.
    assert (leather.status, leather.time_left) == ("may have expired", "up to 9 hours")
    c = leather.competition
    assert (c.verdict, c.difference, c.cheaper_listed, c.source) == ("undercut", 5, None, "full scan")

    # Ended, newest first, with what said so; the news about the player's bids too.
    ended = [(e.name, e.outcome, e.known_from) for e in result.ended]
    assert ended == [
        ("Medium Hide", "sold", "the game's notice"),
        ("Refreshing Spring Water", "no longer listed", "the auction house's list"),
        ("Linen Cloth", "sold, money on its way", "the auction house's list"),
        ("Fine Leather Tunic", "outbid", "the game's notice"),
        ("Light Leather", "sold", "the mail"),
    ], ended
    water, linen = result.ended[1], result.ended[2]
    assert water.note.startswith("Its time had run out, so it probably expired") and linen.money_comes == "by now"

    # Totals: listings at the player's prices (the dye left out); sale money in the mail; sales whose money wasn't
    # there yet: the pending sale (3000 less the 150 fee), the linen and the hides the notice said sold since.
    t = result.totals
    assert (t.listed, t.listed_value, t.listed_value_text) == (4, 104 + 300 + 8000 + 400, "88s 4c")
    assert (t.sold_in_mail, t.sold_coming) == (285, 2850 + 200 + 150)
    assert result.mail_seen_ago == "24 hours ago"
    note = result.note
    assert note.startswith("As of the player's last visit to the auction house, 3 hours ago: since then, only what")
    assert "1 listing(s) probably expired since (their time ran out): the items would be in the mailbox." in note
    assert "3 listing(s) undercut: Medium Hide by 7c; Medium Hide by 15c; Medium Leather by 5c." in note
    assert my_auctions.ONLINE_CAVEAT in note and "No price seen yet for item(s) 4340" in note
    assert "Sale money as of the player's last mailbox visit, 24 hours ago." in note
    assert "Online prices are off" not in note


def test_my_auctions_in_game_only(fake_game):
    """No online prices: the game's own. Listed only by the player, or matched; what's never been seen, and old data."""
    data = reply_()
    data["prices"][0]["tiers"] = [[52, 10], [60, 40]]
    data["prices"][0]["price"] = 52
    data["prices"].append({"item": 4340, "listed": 0, "at": NOW - 3 * HOUR, "src": "scan", "place": 1})
    data["auctions"][3]["expires"] = NOW + HOUR  # the dye: still up
    fake_game["data"] = data
    result = get()
    two, five, gloves, dye, _ = result.listed
    assert (two.competition.verdict, two.competition.difference, two.competition.source) == ("matched", 0, "full scan")
    assert (five.competition.verdict, five.competition.cheaper_listed) == ("undercut", 10)
    assert (dye.competition.verdict, dye.competition.cheapest_other, dye.status) == ("only seller", None, "active")
    assert my_auctions.ONLINE_CAVEAT not in result.note and "Online prices are off." in result.note
    assert result.totals.listed == 5

    # At the auction house now, reading them.
    fake_game["data"] = reply_(open=True, seen=NOW - 30, reading=True)
    result = get()
    assert result.at_auction_house and result.note.startswith("Read at the auction house just now; the player is "
                                                              "there now. The game is reading them right now")

    # Never listed since the addon was installed, and no mailbox either: only the notices.
    fake_game["asked"].clear()
    fake_game["data"] = reply_(seen=None, auctions={}, prices={}, places={}, mail=None,
                               notices=[{"kind": "sold", "name": "Linen Cloth", "at": NOW - 600}])
    result = get()
    assert result.listed == [] and result.seen_at is None and result.mail_seen_at is None
    (sold,) = result.ended
    assert (sold.name, sold.outcome, sold.ago, sold.item_id) == ("Linen Cloth", "sold", "10 minutes ago", None)
    assert result.note.startswith("The game hasn't listed this character's auctions since the WowMCP addon was "
                                  "installed: it reads them when the player opens the auction house. Sales and "
                                  "expiries it noticed since are under ended.")
    assert "The mailbox hasn't been seen since the addon was installed" in result.note
    assert fake_game["asked"] == [], "nothing listed: nothing to look up online"
    assert result.totals.sold_coming == 0 and result.totals.listed_value_text == "0c"


def test_own_listings_come_out_of_the_snapshot():
    snapshot = online_auctions.ItemPrice(4232, NOW - 600, Listing(45, 47, 3, tiers=[(45, 30), (52, 12), (60, 5)]))
    mine = [{"price": 52, "count": 2}, {"price": 60, "count": 5}]
    others = my_auctions.online_others(snapshot, mine)
    assert (others.tiers, others.cheapest, others.listed, others.complete, others.found_own) == (
        [(45, 30), (52, 10)], 45, 40, True, True)
    # Not in the snapshot (listed after it): nothing comes out.
    others = my_auctions.online_others(snapshot, [{"price": 55, "count": 2}])
    assert others.tiers == [(45, 30), (52, 12), (60, 5)] and not others.found_own
    # Only the player's: nobody else lists it.
    alone = online_auctions.ItemPrice(4232, NOW - 600, Listing(52, 2, 1, tiers=[(52, 2)]))
    others = my_auctions.online_others(alone, [{"price": 52, "count": 2}])
    assert my_auctions.compete(52, others).verdict == "only seller"
    # The six cheapest price points all cheaper than the player's: at least that many are.
    many = online_auctions.ItemPrice(4232, NOW - 600, Listing(10, 100, 9, tiers=[(p, 5) for p in range(10, 16)]))
    c = my_auctions.compete(52, my_auctions.online_others(many, [{"price": 52, "count": 2}]))
    assert (c.verdict, c.cheaper_listed, c.cheaper_at_least, c.difference_text) == ("undercut", 30, True, "42c")
    # Only a bid on the player's: nothing to compare.
    assert my_auctions.compete(None, others).verdict == "unknown"


def test_agents_are_told_to_look_first(addons):
    from wow_mcp import server

    instructions = server.create_server().instructions
    for said in ("call get_my_auctions first", "never assume what they listed", "as of your last visit to the auction "
                 "house", "get_inventory(include_mail=True)"):
        assert said in instructions, said
    assert instructions.index("call get_my_auctions first") > instructions.index("weigh buying them against farming")


def test_my_auctions_live(live_game):
    setup = """
        C_Timer.After(1, function() client.OpenAuctionHouse() end)
        -- The hides sell while the player is there, after the list was read.
        C_Timer.After(5, function() client.SellOwnAuction(7001) end)
    """
    with live_game(setup, seconds=20):
        for _ in range(40):
            result = get_live()
            if result.ended and result.listed and result.listed[0].competition:
                break
            time.sleep(0.3)
    assert result.at_auction_house and result.seen_ago == "just now"
    (gloves,) = result.listed
    assert (gloves.item_id, gloves.name, gloves.price, gloves.time_left_from) == (
        4253, "Toughened Leather Gloves", 8000, "band")
    c = gloves.competition
    assert (c.verdict, c.cheapest_other, c.source) == ("cheapest", 9500, "full scan")
    (hides,) = result.ended
    assert (hides.item_id, hides.outcome, hides.known_from, hides.count) == (4232, "sold", "the game's notice", 2)
    assert result.totals.listed_value == 8000 and result.totals.sold_coming == 104
    assert json.loads(result.model_dump_json())["listed"][0]["competition"]["verdict"] == "cheapest"


def get_live():
    return asyncio.run(my_auctions.get_my_auctions())
