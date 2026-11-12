"""The player's auction house history, kept in a ledger past the game's 7 days (tools/auction_history.py).

Against replies shaped as the game sends them: section myAuctions of request kind KNOWLEDGE (as test_my_auctions.py
has it) and section mail of ITEMS (as test_mail.py has it).
"""

import asyncio
import json
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wow_mcp import kinds, memory
from wow_mcp.tools import auction_history
from wow_mcp.tools import memory as memory_tools

NOW = int(time.time())
HOUR, DAY = 3600, 86400
MAIL_DAYS = 30 * DAY  # auction house mail expires this long after it came
VERSION = "World of Warcraft: Forever 1.60.1"


def auctions_(**changes):
    data = {
        "seen": NOW - 3 * HOUR, "open": False, "realm": "Firemaw", "faction": "Alliance",
        "auctions": [
            {"id": 7001, "item": 4232, "name": "Medium Hide", "count": 2, "price": 52, "status": "active",
             "first": NOW - 5 * HOUR, "seen": NOW - 3 * HOUR, "expires": NOW + 17 * HOUR, "left": "seconds"},
            {"id": 7002, "item": 4253, "name": "Toughened Leather Gloves", "count": 1, "price": 8000, "bid": 5000,
             "status": "active", "first": NOW - 5 * HOUR, "seen": NOW - 3 * HOUR, "expires": NOW + 45 * HOUR,
             "soonest": NOW + 9 * HOUR, "left": "band"},
            # Its time ran out since the list was read: not listed now.
            {"id": 7003, "item": 4232, "name": "Medium Hide", "count": 1, "price": 55, "status": "active",
             "first": NOW - 30 * HOUR, "seen": NOW - 3 * HOUR, "expires": NOW - HOUR, "left": "seconds"},
            {"id": 7012, "item": 4340, "name": "Gray Dye", "count": 3, "price": 210, "status": "expired",
             "first": NOW - 30 * HOUR, "seen": NOW - 3 * HOUR, "at": NOW - HOUR, "how": "notice"},
            {"id": 7004, "item": 4232, "name": "Medium Hide", "count": 3, "price": 50, "status": "sold",
             "first": NOW - 30 * HOUR, "seen": NOW - 3 * HOUR, "at": NOW - 2 * HOUR, "how": "notice"},
            {"id": 7009, "item": 2589, "name": "Linen Cloth", "count": 20, "price": 10, "status": "sold-pending",
             "first": NOW - 20 * HOUR, "seen": NOW - 3 * HOUR, "at": NOW - 3 * HOUR - 600, "how": "list",
             "due": NOW - 2 * HOUR},
            {"id": 7013, "item": 4232, "name": "Medium Hide", "count": 5, "price": 60, "status": "cancelled",
             "first": NOW - 6 * HOUR, "seen": NOW - 6 * HOUR, "at": NOW - 4 * HOUR, "how": "cancel"},
            {"id": 7010, "item": 2318, "name": "Light Leather", "count": 10, "price": 30, "status": "sold",
             "first": NOW - 40 * HOUR, "seen": NOW - 26 * HOUR, "at": NOW - 25 * HOUR, "how": "mail"},
            # Gone from the list, nothing said why: only that it was listed.
            {"id": 7011, "item": 159, "name": "Refreshing Spring Water", "count": 5, "price": 5, "status": "gone",
             "first": NOW - 60 * HOUR, "seen": NOW - 26 * HOUR, "at": NOW - 3 * HOUR, "how": "list"},
        ],
        "notices": [
            {"kind": "outbid", "name": "Fine Leather Tunic", "at": NOW - 4 * HOUR, "src": "notice"},
            {"kind": "won", "name": "Fine Leather Tunic", "at": NOW - 3 * HOUR, "src": "notice"},
            # A sale of an auction the game never listed (put up before the addon was installed).
            {"kind": "sold", "name": "Wool Cloth", "at": NOW - 50 * HOUR, "src": "chat"},
        ],
        "prices": [], "places": [],
        "mail": {"at": NOW - HOUR, "sales": []},
    }
    data.update(changes)
    return data


def sold_mail(name, count, price, fee, deposit, came):
    sale = {"item": name, "player": "Bob", "price": price, "fee": fee, "deposit": deposit}
    if count > 1:
        sale["count"] = count  # the game leaves out a count of 1
    return {"from": "Auction House", "subject": f"Auction successful: {name}", "money": price - fee + deposit,
            "expires": came + MAIL_DAYS, "ah": "sold", "sale": sale}


def mails_():
    return [
        sold_mail("Light Leather", 10, 300, 15, 12, NOW - 25 * HOUR),
        sold_mail("Linen Cloth", 20, 200, 10, 6, NOW - 2 * HOUR),  # the money an hour after the sale
        sold_mail("Wool Cloth", 1, 3000, 150, 0, NOW - 49 * HOUR),
        {"from": "Auction House", "subject": "Auction expired: Gray Dye", "expires": NOW - HOUR + MAIL_DAYS,
         "ah": "expired", "items": [{"id": 4340, "name": "Gray Dye", "count": 3, "quality": 1}]},
        {"from": "Auction House", "subject": "Auction won: Fine Leather Tunic",
         "expires": NOW - 3 * HOUR + 60 + MAIL_DAYS, "ah": "won",
         "sale": {"item": "Fine Leather Tunic", "player": "Sella", "price": 4500},
         "items": [{"id": 4243, "name": "Fine Leather Tunic", "quality": 2}]},
        # Not deals of the player's own, or told by another mail.
        {"from": "Auction House", "subject": "Sale Pending: Wool Cloth", "expires": NOW + MAIL_DAYS, "ah": "pending",
         "sale": {"item": "Wool Cloth", "player": "Bob", "price": 3000, "fee": 150, "eta": "14:05"}},
        {"from": "Auction House", "subject": "Outbid on Fine Leather Tunic", "money": 450,
         "expires": NOW + MAIL_DAYS, "ah": "outbid"},
        {"from": "Brannoc", "subject": "mats", "money": 500, "expires": NOW + 29 * DAY, "player": True, "alt": True,
         "items": [{"id": 2589, "name": "Linen Cloth", "count": 20, "quality": 1}]},
        # An expired auction whose item the player took: nothing says what it was.
        {"from": "Auction House", "subject": "Auction expired: Ruined Pelt", "expires": NOW + 29 * DAY,
         "ah": "expired"},
    ]


def mail_(mails=None, **changes):
    data = {"open": False, "inbox": {"time": NOW - HOUR, "total": 9, "mails": mails_() if mails is None else mails}}
    data.update(changes)
    return data


@pytest.fixture
def game(monkeypatch):
    """The game as the tool asks it: get_my_auctions' request, get_inventory's and, for the character, STATE."""
    state = {"now": NOW, "auctions": auctions_(), "mail": mail_(), "name": "Thorgrim", "running": True, "asked": []}
    monkeypatch.setattr(time, "time", lambda: state["now"])

    async def request(kind):
        if not state["running"]:
            raise ToolError("World of Warcraft: Forever isn't running.")
        state["asked"].append(kind)
        if kind == kinds.KNOWLEDGE:
            return {"myAuctions": state["auctions"], "recipes": {}, "vendors": {"vendors": []}}, VERSION
        if kind == kinds.ITEMS:
            return {"bags": [], "equipment": [], "bank": False, "mail": state["mail"]}, VERSION
        assert kind == kinds.STATE
        return {"character": {"name": state["name"]}}, VERSION

    monkeypatch.setattr(auction_history, "request", request)
    monkeypatch.setattr(memory_tools, "request", request)  # whose() asks the game who's logged in
    return state


def get(**args):
    return asyncio.run(auction_history.get_auction_history(**args))


def ledger_lines():
    return (memory.data_dir() / auction_history.LEDGER_FILE).read_text().splitlines()


def test_events_from_the_auctions_the_mail_and_the_notices(game, tmp_path):
    result = get()
    assert game["asked"] == [kinds.KNOWLEDGE, kinds.ITEMS, kinds.STATE]
    assert result.game == VERSION and result.player == "Thorgrim"
    # Kept with the player's data, away from the game's folder ($XDG_DATA_HOME, which the tests point at tmp).
    assert (tmp_path / "data" / "wow-mcp" / "auction-history.jsonl").is_file()

    ended = [(e.kind, e.name, e.count, e.price, e.received, e.ago, e.known_from)
             for e in result.events if e.kind != "listed"]
    assert ended == [
        # The auction's record and its mail are one event; an expiry's mail has no money.
        ("expired", "Gray Dye", 3, 630, None, "60 minutes ago", ["auctions", "mail"]),
        # Sold, its mail not seen (yet): the price before the cut only.
        ("sold", "Medium Hide", 3, 150, None, "2 hours ago", ["auctions"]),
        # At its time, not the mail's: the invoice has the money after the cut, and the deposit back.
        ("won", "Fine Leather Tunic", 1, 4500, None, "3 hours ago", ["mail", "notice"]),
        ("sold", "Linen Cloth", 20, 200, 190, "3 hours ago", ["auctions", "mail"]),
        ("cancelled", "Medium Hide", 5, 300, None, "4 hours ago", ["auctions"]),
        ("sold", "Light Leather", 10, 300, 285, "25 hours ago", ["auctions", "mail"]),
        # An auction the game never listed: its notice and its mail.
        ("sold", "Wool Cloth", 1, 3000, 2850, "2 days ago", ["mail", "notice"]),
    ], ended
    by_kind = {(e.kind, e.name): e for e in result.events}
    linen = by_kind["sold", "Linen Cloth"]
    assert (linen.item_id, linen.auction_id, linen.deposit, linen.price_each, linen.received_text) == (
        2589, 7009, 6, 10, "1s 90c")
    # The won mail has the item attached; the sale of Wool Cloth only names it.
    assert by_kind["won", "Fine Leather Tunic"].item_id == 4243 and by_kind["sold", "Wool Cloth"].item_id is None
    assert by_kind["sold", "Light Leather"].deposit == 12 and by_kind["sold", "Medium Hide"].deposit is None
    # Every auction was listed, the one gone without a word too.
    listed = [(e.auction_id, e.count, e.price) for e in result.events if e.kind == "listed"]
    assert sorted(listed) == [(7001, 2, 104), (7002, 1, 8000), (7003, 1, 55), (7004, 3, 150), (7009, 20, 200),
                              (7010, 10, 300), (7011, 5, 25), (7012, 3, 630), (7013, 5, 300)]
    assert [e.at for e in result.events] == sorted((e.at for e in result.events), reverse=True)

    t = result.totals
    assert (t.sold, t.sold_for, t.received, t.invoiced) == (34, 3650, 190 + 285 + 2850, 31)
    assert (t.expired, t.cancelled, t.won, t.spent, t.spent_text) == (3, 5, 1, 4500, "45s 0c")
    assert (t.listed, t.listed_now) == (2 + 1 + 1 + 3 + 3 + 20 + 5 + 10 + 5, 3)
    items = {i.name: i for i in result.items}
    hide = items["Medium Hide"]
    assert (hide.item_id, hide.sold, hide.sold_for, hide.received, hide.invoiced) == (4232, 3, 150, 0, 0)
    assert (hide.cancelled, hide.expired, hide.listed, hide.listed_now) == (5, 0, 2 + 1 + 3 + 5, 2)
    assert (items["Linen Cloth"].received, items["Wool Cloth"].item_id, items["Gray Dye"].expired) == (190, None, 3)
    assert items["Toughened Leather Gloves"].listed_now == 1
    assert result.items[0].name == "Gray Dye", "the item with the newest event first"
    assert "1 sale(s) without an invoice seen: received leaves them out" in result.note
    assert "The history only goes back to 2 days ago, its first record of Thorgrim" in result.note


def test_the_ledger_keeps_each_event_once_and_past_the_games_7_days(game):
    get()
    lines = ledger_lines()
    # The same again: nothing new. Nor when each read of the inbox puts the mail's expiry a second off.
    get()
    for m in game["mail"]["inbox"]["mails"]:
        m["expires"] += 1
    get()
    assert ledger_lines() == lines
    assert all(json.loads(line)["player"] == "Thorgrim" for line in lines)

    # Two stacks alike that sold together: two mails, two sales, however often they're read; a third arrives.
    twin = sold_mail("Medium Hide", 1, 60, 3, 2, NOW - HOUR)
    game["mail"]["inbox"]["mails"] += [dict(twin), dict(twin)]
    get()
    get()
    assert len(ledger_lines()) == len(lines) + 2
    game["mail"]["inbox"]["mails"].append(dict(twin))
    assert sum(e.price == 60 for e in get(kinds=["sold"]).events) == 3

    # A week and a day later the game has forgotten them all and the mail's been taken: the ledger hasn't.
    game["now"] = NOW + 8 * DAY
    game["auctions"] = auctions_(auctions=[], notices=[], seen=NOW + 8 * DAY - HOUR)
    game["mail"] = mail_(mails=[])
    result = get()
    sold = [(e.name, e.received) for e in result.events if e.kind == "sold"]
    assert ("Light Leather", 285) in sold and ("Wool Cloth", 2850) in sold and len(sold) == 7
    assert result.totals.listed_now == 0 and result.totals.received == 190 + 285 + 2850 + 3 * 57
    # Only the last day: nothing happened in it.
    assert get(days=1).events == []


def test_filters(game):
    get()
    hides = get(item_ids=[4232])
    assert {e.name for e in hides.events} == {"Medium Hide"} and [i.item_id for i in hides.items] == [4232]
    assert (hides.totals.sold, hides.totals.cancelled, hides.totals.listed_now) == (3, 5, 2)
    sales = get(kinds=["sold", "won"])
    assert {e.kind for e in sales.events} == {"sold", "won"} and sales.totals.listed == 0
    # Items only listed now don't show among sales.
    assert "Toughened Leather Gloves" not in {i.name for i in sales.items}
    recent = get(days=1)
    assert {e.name for e in recent.events if e.kind == "sold"} == {"Medium Hide", "Linen Cloth"}
    assert recent.totals.sold == 23


def test_per_character(game, monkeypatch):
    get()
    # Another character logs in, with auctions of their own: asked in game, the bridge says who.
    monkeypatch.setenv(memory.PLAYER_ENV, "Brannoc")
    game["asked"].clear()
    game["auctions"] = auctions_(auctions=[
        {"id": 9001, "item": 2592, "name": "Wool Cloth", "count": 20, "price": 40, "status": "sold",
         "first": NOW - 10 * HOUR, "seen": NOW - 9 * HOUR, "at": NOW - 8 * HOUR, "how": "notice"},
    ], notices=[])
    game["mail"] = mail_(mails=[])
    brannoc = get()
    assert game["asked"] == [kinds.KNOWLEDGE, kinds.ITEMS], "the bridge said who: no need to ask"
    assert brannoc.player == "Brannoc" and [(e.kind, e.auction_id) for e in brannoc.events] == [
        ("sold", 9001), ("listed", 9001)]
    # Thorgrim's, by name in any case, while Brannoc plays: as recorded, nothing listed now known.
    thorgrim = get(player="thorgrim")
    assert thorgrim.player == "Thorgrim" and thorgrim.totals.sold == 34 and thorgrim.totals.listed_now is None
    assert all(e.auction_id != 9001 for e in thorgrim.events)
    assert "This is Thorgrim's history as recorded while they played." in thorgrim.note
    players = {json.loads(line)["player"] for line in ledger_lines()}
    assert players == {"Thorgrim", "Brannoc"}


def test_the_game_not_running(game, monkeypatch):
    game["running"] = False
    result = get()
    assert result.player is None and result.events == [] and result.items == []
    assert result.note.startswith("The game didn't answer (World of Warcraft: Forever isn't running.)")
    assert "Nothing recorded yet." in result.note

    game["running"] = True
    get()
    game["running"] = False
    result = get()  # the only character in the ledger
    assert result.player == "Thorgrim" and result.totals.sold == 34 and result.totals.received == 3325
    assert result.totals.listed_now is None and {i.listed_now for i in result.items} == {None}
    assert "so this is the history as recorded by earlier calls" in result.note

    monkeypatch.setenv(memory.PLAYER_ENV, "Brannoc")
    game["running"] = True
    get()
    monkeypatch.delenv(memory.PLAYER_ENV)
    game["running"] = False
    with pytest.raises(ToolError, match="the history has Thorgrim, Brannoc. Pass their name as `player`"):
        get()
    assert get(player="Thorgrim").totals.sold == 34
    # Asked in game, the bridge says who.
    monkeypatch.setenv(memory.PLAYER_ENV, "Thorgrim")
    assert get().player == "Thorgrim"


def test_the_mail_not_seen(game):
    game["mail"] = {"open": False}
    result = get()
    assert "The mailbox hasn't been seen since the WowMCP addon was installed" in result.note
    sold = {e.name: e.received for e in result.events if e.kind == "sold"}
    assert sold == {"Medium Hide": None, "Linen Cloth": None, "Light Leather": None, "Wool Cloth": None}
    assert "4 sale(s) without an invoice seen" in result.note
    # Seen later: the invoices join the sales already recorded.
    game["mail"] = mail_()
    assert {e.name: e.received for e in get().events if e.kind == "sold"} == {
        "Medium Hide": None, "Linen Cloth": 190, "Light Leather": 285, "Wool Cloth": 2850}


def test_a_broken_line_is_skipped(game):
    get()
    path = memory.data_dir() / auction_history.LEDGER_FILE
    path.write_bytes(path.read_bytes() + b'{"player": "Thorgrim", "kind": "sold", "at"')  # cut short
    game["auctions"]["auctions"].append(
        {"id": 7020, "item": 2589, "name": "Linen Cloth", "count": 5, "price": 12, "status": "sold",
         "first": NOW - 2 * HOUR, "seen": NOW - 2 * HOUR, "at": NOW - HOUR, "how": "notice"})
    result = get()
    assert any(e.auction_id == 7020 and e.kind == "sold" for e in result.events)
    assert json.loads(ledger_lines()[-1])["auction"] == 7020


def test_agents_are_told_how_to_combine_it():
    from wow_mcp import server

    instructions = server.create_server().instructions
    for said in ("call get_auction_history", "get_vendor_items for the cost", "the cost is an estimate from vendor "
                 "prices", "get_auction_prices for the market now", "still worth it or flooded now",
                 "named visual ('Flip results')", "refresh instructions"):
        assert said in instructions, said
    assert instructions.index("call get_auction_history") < instructions.index("call get_my_auctions first")
