"""The mailbox as last seen, and new mail (Inventory.lua, tools/inventory.py)."""

import asyncio
import json
import time
import zlib
from datetime import datetime

from wow_mcp import kinds
from wow_mcp.tools import inventory

# An inbox, newest first (stub/apis/mail.lua): every kind of mail. Brannoc is another character of the
# player's, whose bank the addon has seen. Medium Leather (2319) takes a moment to load.
INBOX = r"""
    WowMCPDB = { bank = { ["Brannoc-Firemaw"] = { time = 1, bags = {} } } }
    local day = 86400
    for _, mail in ipairs({
        { sender = "Auction House", subject = "Auction successful: Linen Cloth (20)", money = 1960, expires = 30 * day,
          invoice = { type = "seller", item = "Linen Cloth", player = "Bob", bid = 2000, buyout = 2000, deposit = 60,
                      fee = 100, count = 20 } },
        { sender = "Auction House", subject = "Auction expired: Light Leather", expires = 2 * day,
          items = { [1] = { id = 2318, count = 10 } } },
        { sender = "Brannoc", subject = "mats", body = true, player = true, money = 500, expires = 29 * day,
          items = { [1] = { id = 2589, count = 20 }, [3] = { id = 4865, count = 2 } } },
        { sender = "Grimbold", subject = "Your tunic", body = true, player = true, cod = 5000, expires = 3 * day + 3600,
          items = { [1] = { id = 4243 } } },
        { sender = "Kaelith", subject = "linen", body = true, player = true, returned = true, read = true,
          expires = 20 * 3600, items = { [1] = { id = 2589, count = 5 } } },
        { sender = "Jarel Moor", subject = "Your reward", body = true, read = true, expires = 10 * day,
          items = { [1] = { id = 159, count = 5 } } },
        { sender = "Auction House", subject = "Outbid on Fine Leather Tunic", money = 450, expires = 30 * day },
        { sender = "Auction House", subject = "Auction won: Medium Leather (5)", expires = 30 * day,
          invoice = { type = "buyer", item = "Medium Leather", player = "Sella", bid = 900, buyout = 900, count = 5 },
          items = { [1] = { id = 2319, count = 5 } } },
        { sender = "Auction House", subject = "Sale Pending: Wool Cloth", expires = 30 * day,
          invoice = { type = "seller_temp_invoice", item = "Wool Cloth", player = "Bob", bid = 3000, fee = 150,
                      delay = 60, etaHour = 14, etaMin = 5 } },
    }) do
        mail.expires = time() + mail.expires
        client.mail.all[#client.mail.all + 1] = mail
    end
"""

ASK = r"""
    local ns = client.LoadAddon()
    local reply
    local function Ask()
        ns.Stream.Send = function(_, value) reply = value end
        ns.Answer(2, ns.Kinds.ITEMS)
        return reply.mail
    end
    local me = UnitName("player") .. "-" .. GetRealmName()
    local function Saved() return WowMCPDB.mail and WowMCPDB.mail[me] end
"""


def test_inbox_is_saved_at_the_mailbox(run_client):
    run_client(INBOX + ASK + r"""
        local day = 86400
        -- Never seen, and unreadable away from a mailbox.
        local mail = Ask()
        assert(mail.inbox == nil and mail.open == false and mail.newMail == nil)
        client.Fire("MAIL_INBOX_UPDATE")
        assert(Saved() == nil)

        -- Opening it: nothing yet, until the game has fetched the inbox.
        client.OpenMailbox()
        assert(Ask().open == true and Ask().inbox == nil and Saved() == nil)
        client.Tick()
        local saved = Saved()
        assert(saved.time == time() and saved.total == 9 and #saved.mails == 9)
        local m = saved.mails

        -- A sale: the invoice, and the money (sale price + deposit - the auction house's cut).
        assert(m[1].from == "Auction House" and m[1].ah == "sold" and m[1].money == 1960 and not m[1].returns)
        local sale = m[1].sale
        assert(sale.item == "Linen Cloth" and sale.count == 20 and sale.player == "Bob" and sale.price == 2000)
        assert(sale.deposit == 60 and sale.fee == 100 and sale.eta == nil)
        -- Expiry: days left as an absolute time.
        assert(math.abs(m[1].expires - (time() + 30 * day)) <= 1 and math.abs(m[2].expires - (time() + 2 * day)) <= 1)

        -- An expired auction: by its subject; it's deleted when it expires.
        assert(m[2].ah == "expired" and m[2].sale == nil and m[2].returns == nil and m[2].player == nil)
        assert(#m[2].items == 1 and m[2].items[1].id == 2318 and m[2].items[1].name == "Light Leather")
        assert(m[2].items[1].count == 10 and m[2].items[1].quality == 1)

        -- From one of the player's characters: two attachments with a gap; it goes back when it expires.
        assert(m[3].player and m[3].alt and m[3].ah == nil and m[3].returns and m[3].money == 500)
        assert(#m[3].items == 2 and m[3].items[1].id == 2589 and m[3].items[1].count == 20)
        assert(m[3].items[2].id == 4865 and m[3].items[2].count == 2 and m[3].items[2].quality == 0)

        -- COD from another player; one that came back (it can't go back again); one from the game.
        assert(m[4].player and not m[4].alt and m[4].cod == 5000 and m[4].items[1].count == nil and m[4].returns)
        assert(m[5].returned and m[5].read and m[5].player and not m[5].returns)
        assert(not m[6].player and m[6].ah == nil and m[6].read and not m[6].returns and m[6].items[1].id == 159)

        -- Outbid, won (with its purchase invoice) and a sale whose money comes later.
        assert(m[7].ah == "outbid" and m[7].money == 450 and m[7].items == nil)
        assert(m[8].ah == "won" and m[8].sale.price == 900 and m[8].sale.player == "Sella" and m[8].sale.count == 5)
        assert(m[8].sale.deposit == nil and m[8].sale.fee == nil and m[8].items[1].id == 2319)
        assert(m[9].ah == "pending" and m[9].money == nil and m[9].sale.price == 3000 and m[9].sale.eta == "14:05")
        assert(m[9].sale.fee == 150)

        -- An attachment the game hadn't loaded has no name yet; a request while it's open reads it again.
        assert(m[8].items[1].name == nil)
        client.Tick()
        assert(Ask().inbox.mails[8].items[1].name == "Medium Leather")

        -- The player takes the linen from Brannoc's mail, deletes the reward mail and takes the outbid money:
        -- the auction house's mail goes once it's empty.
        TakeInboxItem(3, 1)
        client.Tick()
        assert(#Saved().mails == 9 and #Saved().mails[3].items == 1 and Saved().mails[3].items[1].id == 4865)
        assert(client.bags[0].slots[7].id == 2589 and client.bags[0].slots[7].count == 20)
        DeleteInboxItem(6)
        TakeInboxMoney(6)
        client.Wait(2)
        saved = Saved()
        assert(#saved.mails == 7 and saved.total == 7)
        assert(saved.mails[6].ah == "won" and saved.mails[7].ah == "pending")
        assert(client.stats.money > 450)

        -- After closing, the cache is what was left.
        client.CloseMailbox()
        assert(Ask().open == false)
        client.Deliver({ sender = "Brannoc", subject = "more", player = true, body = true })
        client.Fire("MAIL_INBOX_UPDATE")
        mail = Ask()
        assert(#mail.inbox.mails == 7 and mail.inbox.time == saved.time and mail.newMail[1] == "Brannoc")

        -- Saved variables are account-wide: another character hasn't seen its mailbox.
        local Name = UnitName
        UnitName = function() return "Alt" end
        assert(Ask().inbox == nil)
        UnitName = Name

        -- /reload keeps it.
        wipe(client.tickers); wipe(client.timers); wipe(client.frames)
        ASK_AGAIN
        assert(#Ask().inbox.mails == 7)
    """.replace("ASK_AGAIN", ASK))


def test_more_mail_than_the_inbox_shows(run_client):
    """The inbox shows at most 50; the total says how many there are."""
    run_client(r"""
        for i = 1, 60 do
            client.mail.all[i] = { sender = "Auction House", subject = "Auction expired: Light Leather",
                expires = time() + 86400 * 20, items = { [1] = { id = 2318, count = i } } }
        end
    """ + ASK + r"""
        client.OpenMailbox()
        client.Tick()
        local saved = Saved()
        assert(#saved.mails == 50 and saved.total == 60 and saved.mails[50].items[1].count == 50)

        -- Taking one: the game shows the next ones at its next check, not now.
        TakeInboxItem(1, 1)
        client.Tick()
        assert(#Saved().mails == 49 and Saved().total == 59 and Saved().mails[1].items[1].count == 2)
        CheckInbox()
        client.Tick()
        assert(#Saved().mails == 50 and Saved().total == 59 and Saved().mails[50].items[1].count == 51)
        client.CloseMailbox()
    """)


def test_new_mail_away_from_the_mailbox(run_client):
    run_client(ASK + r"""
        assert(Ask().newMail == nil)
        client.Deliver({ sender = "Auction House", subject = "Auction successful: Linen Cloth", money = 100 })
        client.Deliver({ sender = "Brannoc", subject = "hi", player = true, body = true })
        local mail = Ask()
        assert(mail.inbox == nil and #mail.newMail == 2 and mail.newMail[1] == "Brannoc" and mail.newMail[2] == "Auction House")

        -- The game names at most three, newest first.
        for _, sender in ipairs({ "A", "B", "C" }) do client.Deliver({ sender = sender, subject = "x", player = true }) end
        mail = Ask()
        assert(#mail.newMail == 3 and mail.newMail[1] == "C" and mail.newMail[3] == "A")

        -- A notice that names nobody.
        local Senders = GetLatestThreeSenders
        GetLatestThreeSenders = function() end
        assert(next(Ask().newMail) == nil)
        GetLatestThreeSenders = Senders

        -- Opening the mailbox: it's in the inbox now, and no longer new.
        client.OpenMailbox()
        client.Tick()
        mail = Ask()
        assert(mail.newMail == nil and #mail.inbox.mails == 5 and mail.inbox.mails[5].ah == "sold")
        assert(mail.inbox.mails[5].sale == nil, "no invoice: known by its subject")
    """)


def test_full_inbox_stays_compact(run_client, tmp_path):
    """50 mails: auction house sales, expired auctions, mail from alts with several items, COD: the reply
    should still stream in a second or two."""
    out = tmp_path / "reply.json"
    run_client(r"""
        local words = { "Linen", "Wool", "Silk", "Heavy", "Light", "Leather", "Scale", "Copper", "Bronze", "Iron",
            "Healing", "Mana", "Minor", "Lesser", "Greater", "Band", "Boots", "Gloves", "Cloak", "Tunic", "Pants" }
        local n = 0
        local function Item()
            n = n + 1
            local id = 30000 + n * 7
            client.items[id] = { name = words[n % #words + 1] .. " " .. words[n * 7 % #words + 1] .. " of the "
                .. words[n * 13 % #words + 1], quality = n % 5, level = 10 + n % 50, class = 7, sell = n * 37 % 5000 }
            return id
        end
        local day = 86400
        for i = 1, 50 do
            local mail
            if i % 4 == 0 then
                local id = Item()
                local name = client.items[id].name
                mail = { sender = "Auction House", subject = "Auction successful: " .. name .. " (20)", money = 9000 + i,
                    invoice = { type = "seller", item = name, player = "Buyer" .. i, bid = 9500 + i, buyout = 9500 + i,
                        deposit = 120, fee = 620, count = 20 } }
            elseif i % 4 == 1 then
                local id = Item()
                mail = { sender = "Auction House", subject = "Auction expired: " .. client.items[id].name,
                    items = { [1] = { id = id, count = 20 } } }
            elseif i % 4 == 2 then
                mail = { sender = "Brannoc", subject = "stuff " .. i, player = true, body = true, money = i * 100, items = {} }
                for slot = 1, 6 do mail.items[slot] = { id = Item(), count = slot * 3 } end
            else
                mail = { sender = "Trader" .. i, subject = "COD " .. i, player = true, body = true, cod = i * 1000,
                    items = { [1] = { id = Item(), count = 5 }, [2] = { id = Item() } } }
            end
            mail.expires = time() + (i % 10 + 1) * day - i * 600
            client.mail.all[i] = mail
        end
        for i = 51, 70 do client.mail.all[i] = { sender = "Later", subject = "x", expires = time() + day } end
    """ + ASK + r"""
        client.OpenMailbox()
        client.Tick()
        local mail = Ask()
        assert(#mail.inbox.mails == 50 and mail.inbox.total == 70 and #mail.inbox.mails[2].items == 6)
        local f = assert(io.open("OUT", "w"))
        f:write(C_EncodingUtil.SerializeJSON(reply))
        f:close()
    """.replace("OUT", str(out)))
    body = out.read_bytes()
    assert len(json.loads(body)["mail"]["inbox"]["mails"]) == 50
    size = len(zlib.compress(body, 9))
    print(f"full inbox: {len(body)} bytes, {size} compressed")
    assert size < 5000, size

    # What the agent gets: every mail, and notes that name only the first few expiring ones.
    mail = inventory._mailbox(json.loads(body)["mail"])
    assert len(mail.mails) == 50 and mail.not_shown == 20 and len(mail.items) == 13 + 13 * 6
    assert "Expiring soon (15): " in mail.note and "; and 10 more." in mail.note
    assert "12 COD mail(s): the player pays 30g 0s 0c in all" in mail.note
    assert len(mail.note) < 1000, mail.note


def reply_with(mail):
    return {"bags": [], "equipment": [], "bank": False, "mail": mail}


def test_mail_in_the_tool(monkeypatch):
    now = int(time.time())
    day = 86400
    mail = {
        "open": False,
        "newMail": ["Auction House"],
        "inbox": {"time": now - 5 * 3600, "total": 12, "mails": [
            {"from": "Auction House", "subject": "Auction successful: Linen Cloth (20)", "money": 1960,
             "expires": now + 30 * day - 60, "ah": "sold",
             "sale": {"item": "Linen Cloth", "count": 20, "player": "Bob", "price": 2000, "deposit": 60, "fee": 100}},
            {"from": "Auction House", "subject": "Auction expired: Light Leather", "expires": now + 2 * day - 60,
             "ah": "expired", "items": [{"id": 2318, "name": "Light Leather", "count": 10, "quality": 1}]},
            {"from": "Brannoc", "subject": "mats", "money": 500, "expires": now + 29 * day + 60, "player": True,
             "alt": True, "returns": True, "items": [{"id": 2589, "name": "Linen Cloth", "count": 20, "quality": 1},
                                                     {"id": 2318, "name": "Light Leather", "count": 5, "quality": 1}]},
            {"from": "Grimbold", "subject": "Your tunic", "cod": 5000, "expires": now + 3 * day + 3600,
             "player": True, "returns": True, "items": [{"id": 4243, "name": "Fine Leather Tunic", "quality": 2}]},
            {"from": "Kaelith", "subject": "linen", "expires": now + 20 * 3600 + 60, "player": True, "returned": True,
             "read": True, "items": [{"id": 2589, "name": "Linen Cloth", "count": 5, "quality": 1}]},
            {"from": "Jarel Moor", "subject": "Your reward", "expires": now + 10 * day + 60, "read": True,
             "items": [{"id": 159, "count": 5}]},
            {"from": "Auction House", "subject": "Sale Pending: Wool Cloth", "expires": now + 30 * day + 60,
             "ah": "pending",
             "sale": {"item": "Wool Cloth", "player": "Bob", "price": 3000, "fee": 150, "eta": "14:05"}},
            {"from": "Gamemaster", "subject": "Hi", "expires": now + 30 * day + 60, "gm": True},
            {"from": "Auction House", "subject": "Outbid on Fine Leather Tunic", "money": 450,
             "expires": now + 40 * 60 + 30, "ah": "outbid"},
            # Expired since the player saw it.
            {"from": "Auction House", "subject": "Auction expired: Ruined Pelt", "expires": now - 60, "ah": "expired",
             "items": [{"id": 4865, "count": 2}]},
        ]},
    }

    async def fake_request(kind):
        assert kind == kinds.ITEMS
        return reply_with(mail), "WoW"

    monkeypatch.setattr(inventory, "request", fake_request)
    assert asyncio.run(inventory.get_inventory(include_bank=True)).mail is None, "only when asked for"
    result = asyncio.run(inventory.get_inventory(include_mail=True)).mail

    assert result.hours_ago == 5.0 and result.seen_at == datetime.fromtimestamp(now - 5 * 3600).astimezone()
    assert not result.at_mailbox and result.not_shown == 2 and result.new_mail_from == ["Auction House"]
    m = result.mails
    assert len(m) == 9, "the expired one is gone"
    assert [x.kind for x in m] == [
        "auction sale", "auction expired", "from your character", "from a player", "from a player", "from the game",
        "auction sale pending", "from a game master", "auction outbid"]
    assert m[0].model_dump() == {
        "sender": "Auction House", "subject": "Auction successful: Linen Cloth (20)", "kind": "auction sale",
        "money": "19s 60c", "cod": None, "items": [], "expires_in": "29 days", "on_expiry": "deleted",
        "read": False, "returned": False,
        "invoice": {"item": "Linen Cloth", "count": 20, "player": "Bob", "price": "20s 0c", "deposit": "60c",
                    "fee": "1s 0c", "money_arrives": None},
    }
    assert [x.expires_in for x in m] == [
        "29 days", "47 hours", "29 days", "3 days", "20 hours", "10 days", "30 days", "30 days", "40 minutes"]
    assert [x.on_expiry for x in m[1:5]] == ["deleted", "returned to sender", "returned to sender", "deleted"]
    assert m[3].cod == "50s 0c" and m[3].items[0].count == 1 and m[3].items[0].quality == "uncommon"
    assert m[4].returned and m[4].read and not m[3].read
    assert m[5].items[0].model_dump() == {"item_id": 159, "name": None, "count": 5, "quality": None}
    assert m[6].money is None and m[6].invoice.money_arrives == "14:05" and m[6].invoice.deposit is None
    assert m[6].invoice.count == 1

    # Totals: all the money; items by ID, without the COD mail's.
    assert result.money_waiting == 1960 + 500 + 450 and result.money_waiting_text == "29s 10c"
    assert [(i.item_id, i.count) for i in result.items] == [(2318, 15), (2589, 25), (159, 5)]
    assert m[1].items[0].count == 10, "the mail's own count stays"

    note = result.note
    assert "As the player last saw it 5 hours ago; mail may have arrived since." in note
    assert "1 mail(s) expired since then" in note
    assert "2 more mail(s) wait on the server" in note
    assert ("Expiring soon (3): 'Auction expired: Light Leather' from Auction House in 47 hours (deleted); 'linen' "
            "from Kaelith in 20 hours (deleted); 'Outbid on Fine Leather Tunic' from Auction House in 40 minutes "
            "(deleted).") in note, note
    assert "1 COD mail(s): the player pays 50s 0c in all to take their items." in note
    assert "1 auction sale(s) pending" in note
    assert ("The game shows new mail from Auction House; what arrived since the player last opened a mailbox isn't "
            "listed yet.") in note

    # At the mailbox it's current; a notice without senders.
    mail["open"], mail["inbox"]["time"], mail["newMail"] = True, now, {}
    result = asyncio.run(inventory.get_inventory(include_mail=True)).mail
    assert result.at_mailbox and result.new_mail_from == [] and "last saw" not in result.note
    assert "The game shows new mail; what arrived" in result.note


def test_mailbox_never_seen(monkeypatch):
    """The real client sends empty Lua tables as {}; a mailbox never seen is no inbox."""
    replies = iter([{"open": False}, {"open": False, "newMail": {}},
                    {"open": True, "inbox": {"time": int(time.time()), "total": 0, "mails": {}}}])

    async def fake_request(kind):
        return reply_with(next(replies)), "WoW"

    monkeypatch.setattr(inventory, "request", fake_request)
    result = asyncio.run(inventory.get_inventory(include_mail=True)).mail
    assert result.seen_at is None and result.hours_ago is None and result.mails == [] and result.items == []
    assert result.money_waiting == 0 and result.money_waiting_text == "0c" and result.new_mail_from is None
    assert result.note == ("The game hasn't seen this character's mailbox since the WowMCP addon was installed; it "
                           "can only be read at a mailbox.")
    result = asyncio.run(inventory.get_inventory(include_mail=True)).mail
    assert result.new_mail_from == [] and "The game shows new mail;" in result.note
    result = asyncio.run(inventory.get_inventory(include_mail=True)).mail
    assert result.at_mailbox and result.mails == [] and result.not_shown == 0 and result.note is None


def test_mail_live(live_game):
    # The player opens the mailbox, takes Brannoc's linen, deletes the reward mail and closes it again.
    setup = INBOX + r"""
        C_Timer.After(1, client.OpenMailbox)
        C_Timer.After(2, function()
            TakeInboxItem(3, 1)
            DeleteInboxItem(6)
        end)
        C_Timer.After(4, function()
            client.CloseMailbox()
            client.Deliver({ sender = "Auction House", subject = "Auction successful: Wool Cloth", money = 1000 })
        end)
    """
    with live_game(setup):
        time.sleep(0.5)
        result = asyncio.run(inventory.get_inventory(include_bank=True, include_mail=True))

    assert result.game.startswith("World of Warcraft: Forever 1.60.1")
    assert [i.name for i in result.bags[1].items][-1] == "Linen Cloth", "taken into the backpack"
    mail = result.mail
    assert not mail.at_mailbox and mail.hours_ago == 0 and abs(mail.seen_at.timestamp() - time.time()) < 600
    assert [(m.sender, m.kind) for m in mail.mails] == [
        ("Auction House", "auction sale"), ("Auction House", "auction expired"), ("Brannoc", "from your character"),
        ("Grimbold", "from a player"), ("Kaelith", "from a player"), ("Auction House", "auction outbid"),
        ("Auction House", "auction won"), ("Auction House", "auction sale pending")]
    assert [(i.name, i.count) for i in mail.mails[2].items] == [("Ruined Pelt", 2)]
    assert mail.mails[6].items[0].name == "Medium Leather" and mail.mails[6].invoice.price == "9s 0c"
    assert [(i.name, i.count) for i in mail.items] == [
        ("Light Leather", 10), ("Ruined Pelt", 2), ("Linen Cloth", 5), ("Medium Leather", 5)]
    assert mail.money_waiting == 1960 + 500 + 450
    assert mail.new_mail_from == ["Auction House"] and "Expiring soon" in mail.note
