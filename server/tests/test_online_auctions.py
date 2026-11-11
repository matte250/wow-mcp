"""Online auction prices (online_auctions.py, and their part of tools/auction.py and bridge.py), against a fake of
Blizzard's API shaped like the real one: five houses (110, 111 and an empty 112 named Alliance, a Horde 113 and a
Blackwater 116), each with its own market of single items (a buyout per auction) and commodities (a unit price), and
304s for unchanged snapshots. House 110 is the player's: the in-game prices are its market."""

import asyncio
import base64
import email.utils
import itertools
import logging
import os
import random
import re
import sqlite3
import threading
import time
from pathlib import Path

import httpx2 as httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from wow_mcp import battlenet, bridge, kinds, online_auctions
from wow_mcp.online_auctions import Listing, Seen, Store
from wow_mcp.tools import auction, items
from wow_mcp.transport import Transport

CLIENT_ID, SECRET, TOKEN = "client-id-123", "s3cret-VALUE-9f2c", "TOKEN-abc-77ab"
REALM = "CAMWOW EU4 Realm 8 PvP"
T0 = 1_800_000_000  # the fake's snapshots, for the tests that set the time
HOUSES = {110: "Alliance Auction House", 111: "Alliance Auction House", 112: "Alliance Auction House",
          113: "Horde Auction House", 116: "Blackwater Auction House"}
RULESETS = {1: "PvP", 2: "Normal", 3: "Roleplay", 15: "Roleplay PvP"}
COMMODITIES = range(3000, 3010)
ITEMS = range(3010, 3030)
SUFFIXED, BIDS = 3030, 3031


def auction_(id, item, buyout=None, quantity=1, bid=None, bonus=None):
    found = {"id": id, "item": {"id": item, "context": 1}, "quantity": quantity, "time_left": "LONG"}
    if bonus:
        found["item"]["bonus_lists"] = bonus
        found["item"]["modifiers"] = [{"type": 28, "value": 5240}]
    if buyout is not None:
        found["buyout"] = buyout
    if bid is not None:
        found["bid"] = bid
    return found


def commodity(id, item, unit, quantity):
    return {"id": id, "item": {"id": item}, "quantity": quantity, "unit_price": unit, "time_left": "SHORT"}


def market(house: int) -> dict[int, tuple[int, int, int]]:
    """Per item on a house: (cheapest price, how many, in how many auctions); each house has its own."""
    if house == 112:
        return {}
    rng, found = random.Random(house), {}
    for item in COMMODITIES:
        found[item] = (rng.randint(10, 900), rng.randint(20, 400), 2)
    for item in ITEMS:
        n = rng.randint(1, 6)
        found[item] = (rng.randint(100, 9000), n, n)
    return found


def listings(house: int) -> tuple[list, list]:
    """The house's single-item auctions and commodity auctions, as the API has them."""
    items, commodities, ids = [], [], itertools.count(37_000_000)
    for item, (price, quantity, _) in market(house).items():
        if item in COMMODITIES:
            commodities += [commodity(next(ids), item, price, quantity - 10), commodity(next(ids), item, price + 7, 10)]
        else:
            items += [auction_(next(ids), item, buyout=price + 100 * k) for k in range(quantity)]
    if house == 110:
        items += [auction_(next(ids), SUFFIXED, buyout=900, bonus=[12730]),
                  auction_(next(ids), SUFFIXED, buyout=700, bonus=[12719]), auction_(next(ids), BIDS, bid=500)]
    return items, commodities


def evidence(house: int, at: int, items=None) -> list[Seen]:
    """Prices as the game saw them on a house."""
    found = market(house)
    return [Seen(i, at, *found[i], "full scan", "Alliance") for i in (items or found)]


class Blizzard:
    """Blizzard's API as checked live, small: the token, rulesets, the houses (only with the trailing slash), each
    house's auctions and commodities with Last-Modified, and 304s for If-Modified-Since."""

    def __init__(self, at: float):
        self.at = {house: at for house in HOUSES}
        self.data = {house: listings(house) for house in HOUSES}
        self.requests: list[httpx.Request] = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("Name or service not known")
        if request.url.host == "oauth.battle.net":
            basic = "Basic " + base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
            assert request.headers["authorization"] == basic
            return httpx.Response(200, json={"access_token": TOKEN, "token_type": "bearer", "expires_in": 86399})
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["battlenet-namespace"] == "dynamic-forever-eu"
        path = request.url.path
        if path == "/data/wow/ruleset/index":
            return httpx.Response(200, json={"rulesets": [{"key": {"href": "..."}, "id": r} for r in (1, 15, 3, 2)]})
        if m := re.fullmatch(r"/data/wow/ruleset/(\d+)", path):
            return httpx.Response(200, json={"id": int(m[1]), "name": RULESETS[int(m[1])], "status": {"type": "UP"}})
        if re.fullmatch(r"/data/wow/ruleset/\d+/auctions/", path):
            return httpx.Response(200, json={"auctions": [{"key": {"href": "..."}, "name": name, "id": house}
                                                          for house, name in HOUSES.items()]})
        m = re.fullmatch(r"/data/wow/ruleset/\d+/auctions/(\d+)(/commodities)?", path)
        if not m or int(m[1]) not in HOUSES:
            return httpx.Response(404)
        house = int(m[1])
        modified = email.utils.formatdate(self.at[house], usegmt=True)
        if request.headers.get("if-modified-since") == modified:
            return httpx.Response(304, headers={"Last-Modified": modified})
        items, commodities = self.data[house]
        if m[2]:
            body = {"auctions": commodities} if commodities else {}
        else:
            body = {"id": house, "name": HOUSES[house], "commodities": {"href": "..."}, "ruleset": {"href": "..."}}
            if items:
                body["auctions"] = items
        return httpx.Response(200, json=body, headers={"Last-Modified": modified})

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests if r.url.host != "oauth.battle.net"]


def setup(text: str) -> Path:
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "wow-mcp" / "battlenet.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def api(monkeypatch):
    """Credentials (EU) and the fake API, its snapshots taken at T0."""
    setup(f"BATTLENET_CLIENT_ID={CLIENT_ID}\nBATTLENET_CLIENT_SECRET={SECRET}\nBATTLENET_REGION=eu\n")
    fake = Blizzard(T0)
    monkeypatch.setattr(online_auctions, "make_client", lambda config, region: battlenet.Client(
        config, region, transport=httpx.MockTransport(fake), sleep=lambda s: None))
    return fake


def online(store: Store, now: float, addons_dir=None) -> online_auctions.Online:
    return online_auctions.Online(store, battlenet.load_config(), "eu", addons_dir, now)


def no_secrets(*texts: str) -> None:
    for text in texts:
        assert SECRET not in text and TOKEN not in text


def test_summarize_single_items_and_commodities():
    rows = online_auctions.summarize([
        auction_(1, 10, buyout=1000, quantity=3),  # 333.3 each
        auction_(2, 10, buyout=101, quantity=2),  # 50.5 each: rounded as the game does
        auction_(3, 10, bid=500),  # only a bid
        auction_(4, 20, buyout=900, bonus=[12730]),
        auction_(5, 20, buyout=700, bonus=[12719]),
        auction_(6, 20, buyout=800, bonus=[12730]),
        auction_(7, 30, bid=40, quantity=4),
        {"id": 8, "item": {}}, {"id": 9, "item": {"id": 40}, "quantity": 0}, "junk",
    ], commodities=False)
    assert rows[(10, "")] == Listing(51, 6, 3, bid_only=1, suffixes=0, tiers=[(51, 2), (333, 3)])
    # Random suffixes: the item as a whole (the cheapest of any), and each suffix on its own.
    assert rows[(20, "")] == Listing(700, 3, 3, suffixes=2, tiers=[(700, 1), (800, 1), (900, 1)])
    assert rows[(20, "12730")] == Listing(800, 2, 2) and rows[(20, "12719")] == Listing(700, 1, 1)
    assert rows[(30, "")] == Listing(None, 4, 1, bid_only=4)
    assert set(rows) == {(10, ""), (20, ""), (20, "12730"), (20, "12719"), (30, "")}
    # Commodities: a unit price per auction.
    rows = online_auctions.summarize([commodity(1, 2589, 24, 20), commodity(2, 2589, 26, 264),
                                      commodity(3, 2589, 24, 5), commodity(4, 2592, 60, 15934)], commodities=True)
    assert rows == {(2589, ""): Listing(24, 289, 3, tiers=[(24, 25), (26, 264)]),
                    (2592, ""): Listing(60, 15934, 1, tiers=[(60, 15934)])}
    assert online_auctions.summarize(None, True) == {}


def test_snapshots_and_history():
    hour = online_auctions.HOUR
    now = T0 + 10 * online_auctions.DAY
    with Store() as store:
        assert store.path == Path(os.environ["XDG_CACHE_HOME"]) / "wow-mcp" / "auctions.sqlite3"
        indexes = {r[1] for r in store.db.execute("PRAGMA index_list(prices)")}
        assert "prices_by_item" in indexes
        # Hourly snapshots over 9 days; the hide is gone from the last two, and was cheaper on day 9.
        for k in range(9 * 24):
            at = now - (9 * 24 - k) * hour
            price = 400 if k < 8 * 24 else 300
            rows = {} if k >= 9 * 24 - 2 else {(4232, ""): Listing(price, 20 + k % 5, 3, tiers=[(price, 2)])}
            rows[(2589, "")] = Listing(24, 100, 4)
            _, new = store.add_snapshot("eu", 110, "commodities", 1, at, at, rows, 1000)
            assert new
        snapshot, new = store.add_snapshot("eu", 110, "commodities", 1, now - hour, now, {}, 1)
        assert not new and snapshot.at == now - hour, "a snapshot is kept once"
        store.add_snapshot("eu", 110, "items", 1, now - 30 * 60, now, {(9805, ""): Listing(1000, 1, 1)}, 10)
        latest = store.latest("eu", 110, "commodities")
        assert latest.at == now - hour and latest.items == 1 and latest.auctions == 4
        assert store.nearest("eu", 110, "commodities", now - 50 * hour + 600).at == now - 50 * hour
        assert store.nearest("eu", 111, "items", now) is None
        assert store.listings(latest.id, [4232, 2589]) == {2589: Listing(24, 100, 4)}
        last_at, last = store.last_listed("eu", 110, 4232)
        assert last_at == now - 3 * hour and last.price == 300 and last.tiers == [(300, 2)]
        series = store.series("eu", 110, 4232, now - online_auctions.HISTORY_DAYS * online_auctions.DAY)
        assert len(series) == 9 * 24 and series[-1] == (now - hour, None), "not listed in the last snapshots"
        assert store.series("eu", 110, 7777, 0) == []

        usual, points = online_auctions.history(series, now)
        assert usual.price == 400 and usual.snapshots == 7 * 24 and usual.since == now - 7 * online_auctions.DAY
        assert usual.quantity == 22
        recent = [p for p in points if p.span == "snapshot"]
        days = [p for p in points if p.span == "day"]
        assert len(recent) == 24 and recent[-1].price is None and recent[-1].quantity == 0
        assert recent[0].price == 300 and recent[0].at == now - 24 * hour
        assert [p.at for p in days] == sorted(p.at for p in days) and days[0].price == 400
        assert all(p.at % online_auctions.DAY == 0 for p in days) and days[-1].snapshots <= 24


def test_refresh_uses_conditional_gets(api):
    with Store() as store:
        now = T0 + 600
        fetched = online(store, now).refresh([110], REALM)
        assert [(f.kind, f.status) for f in fetched] == [("items", "new"), ("commodities", "new")]
        items, commodities = (f.snapshot for f in fetched)
        assert (items.at, items.ruleset, items.items, items.auctions) == (T0, 1, 22, sum(
            q for i, (_, q, _) in market(110).items() if i in ITEMS) + 3)
        assert (commodities.items, commodities.auctions) == (10, 20)
        assert api.paths() == ["/data/wow/ruleset/index", "/data/wow/ruleset/1", "/data/wow/ruleset/15",
                               "/data/wow/ruleset/3", "/data/wow/ruleset/2", "/data/wow/ruleset/1/auctions/110",
                               "/data/wow/ruleset/1/auctions/110/commodities"]
        listed = store.listings(items.id, [SUFFIXED, BIDS, 3010])
        assert listed[SUFFIXED] == Listing(700, 2, 2, suffixes=2, tiers=[(700, 1), (900, 1)])
        assert listed[BIDS] == Listing(None, 1, 1, bid_only=1) and listed[3010].price == market(110)[3010][0]
        assert set(store.suffixes(items.id, SUFFIXED)) == {"12730", "12719"}

        # Fresh for an hour: not asked again.
        before = len(api.requests)
        assert {f.status for f in online(store, T0 + 50 * 60).refresh([110], REALM)} == {"fresh"}
        assert len(api.requests) == before
        # Then a conditional GET: unchanged, so a 304 and no new snapshot...
        fetched = online(store, T0 + 3700).refresh([110], REALM)
        assert [f.status for f in fetched] == ["unchanged", "unchanged"]
        assert api.requests[-1].headers["if-modified-since"] == email.utils.formatdate(T0, usegmt=True)
        # ...and not asked again for a few minutes.
        before = len(api.requests)
        online(store, T0 + 3700 + 60).refresh([110], REALM)
        assert len(api.requests) == before
        # Blizzard has a new one: kept next to the old one.
        api.at[110] = T0 + 3600
        fetched = online(store, T0 + 4100).refresh([110], REALM)
        assert [f.status for f in fetched] == ["new", "new"]
        assert [s.at for s in store.snapshots("eu", 110)] == [T0, T0, T0 + 3600, T0 + 3600]


def test_ruleset_by_realm_type():
    rulesets = [{"id": i, "name": n} for i, n in RULESETS.items()]
    assert online_auctions.ruleset_for(REALM, rulesets) == 1
    assert online_auctions.ruleset_for("Some Realm RP PvP", rulesets) == 15
    assert online_auctions.ruleset_for("Some Realm RP", rulesets) == 3
    assert online_auctions.ruleset_for("Firemaw", rulesets) == 2
    assert online_auctions.ruleset_for(None, [{"id": 9, "name": "Odd"}]) == 9
    with pytest.raises(battlenet.BattleNetError):
        online_auctions.ruleset_for(REALM, [])


def test_matching_the_players_house(api):
    with Store() as store:
        # The game's full scan was ten minutes before Blizzard's snapshot.
        store.record(REALM, evidence(110, T0 - 600))
        now = T0 + 900
        match = online(store, now).match(REALM, "Alliance")
        assert match.clear and match.house == 110 and match.items == 30 and match.confidence > 0.999
        assert match.scores[110] == 1.0 and set(match.scores) == {110, 111, 116}, "the empty and Horde ones aren't"
        assert match.note.startswith("Alliance Auction House 110 matches the 30 in-game prices best (over 99.99% sure,"
                                     " with snapshots up to 10 minutes from when the game saw them; how alike")
        assert not any("/auctions/113" in p for p in api.paths()), "the Horde's houses aren't the player's"
        # Kept: choosing again needs nothing new.
        before = len(api.requests)
        choice = online(store, now + 60).choose(REALM, "Alliance")
        assert choice.house.label == "Alliance Auction House 110" and choice.how.startswith("matched to the in-game")
        assert len(api.requests) == before and store.match(REALM).clear


def test_ambiguous_matches_pick_no_house(api):
    api.data[111] = api.data[110]  # two houses alike
    with Store() as store:
        store.record(REALM, evidence(110, T0 - 600)[:5])
        choice = online(store, T0 + 900).choose(REALM, "Alliance")
        assert choice.house is None and "Only 5 in-game price(s) so far" in choice.note
        assert not api.paths(), "nothing fetched without enough to compare"
        store.record(REALM, evidence(110, T0 - 600))
        choice = online(store, T0 + 900).choose(REALM, "Alliance")
        assert choice.house is None and choice.match.confidence == 0.5
        assert choice.note.startswith(
            "No online prices: which auction house is the player's isn't clear. Alliance Auction House 110 and "
            "Alliance Auction House 111 match the in-game prices about equally well (50% sure;")
        assert "set BATTLENET_AUCTION_HOUSE=<id> in" in choice.note
        prices = online_auctions.lookup([3010], REALM, "Alliance", now=T0 + 900)
        assert prices.house is None and not prices.items and "isn't clear" in prices.notes[0]
    # In-game prices no house has.
    with Store() as store:
        store.db.execute("DELETE FROM matches")
        store.db.commit()
        other = [Seen(s.item, s.at, (s.price or 0) * 7 + 1, s.quantity * 9 + 3, 40, s.source, s.faction)
                 for s in evidence(110, T0 - 600)]
        store.db.execute("DELETE FROM seen")
        store.record(REALM, other)
        match = online(store, T0 + 900).match(REALM, "Alliance")
        assert not match.clear and match.note.startswith("No auction house matches the in-game prices well")


def test_the_house_can_be_set(api):
    setup(f"BATTLENET_CLIENT_ID={CLIENT_ID}\nBATTLENET_CLIENT_SECRET={SECRET}\nBATTLENET_REGION=eu\n"
          "BATTLENET_AUCTION_HOUSE=111\n")
    prices = online_auctions.lookup([3010, 9999], now=T0 + 600)  # no realm, no evidence
    assert prices.house.label == "Alliance Auction House 111" and prices.how.startswith("set in ")
    assert prices.items[3010].listing.price == market(111)[3010][0] and prices.items[9999].listing is None
    assert prices.at == T0 and prices.since == T0


SAVED = """
WowMCPDB = {
["auction"] = {
["CAMWOW EU4 Realm 8 PvP"] = {
["scan"] = {
[3000] = "123,50,2,2",
[3001] = "",
[3002] = "77,10,2,1",
[3010] = ",3,3,2",
},
["scans"] = {
{
["at"] = 1799990000,
["faction"] = "Alliance",
}, -- [1]
{
["at"] = 1799996400,
["zone"] = "Stormwind City",
["faction"] = "Alliance",
["items"] = 3,
}, -- [2]
},
["current"] = 2,
},
},
["note"] = "a \\"quoted\\" line\\\\with\\nescapes\\065",
["flags"] = { true, false, nil, 1.5, -3, 0x10, },
}
WowMCPOther = nil
"""


def test_the_saved_full_scan(wow_root):
    parsed = online_auctions.parse_saved_variables(SAVED)
    assert parsed["WowMCPDB"]["note"] == 'a "quoted" line\\with\nescapesA'
    assert parsed["WowMCPDB"]["flags"] == [True, False, None, 1.5, -3, 16] and parsed["WowMCPOther"] is None
    with pytest.raises(online_auctions.LuaError):
        online_auctions.parse_saved_variables("WowMCPDB = { [1] = }")
    addons_dir = wow_root / "_forever_" / "Interface" / "AddOns"
    saved = wow_root / "_forever_" / "WTF" / "Account" / "PLAYER" / "SavedVariables" / "WowMCP.lua"
    saved.parent.mkdir(parents=True)
    saved.write_text(SAVED)
    realm, seen = online_auctions.saved_scan(addons_dir)
    assert realm == REALM and {s.item: (s.at, s.price, s.quantity, s.auctions) for s in seen} == {
        3000: (1799996400, 123, 50, 2), 3002: (1799996400, None, 0, 0), 3010: (1799996400, None, 3, 3)}
    assert online_auctions.saved_scan(addons_dir, "Firemaw") is None
    with Store() as store:
        assert online_auctions.import_saved_scan(store, addons_dir, REALM) == 3
        assert online_auctions.import_saved_scan(store, addons_dir, REALM) == 0, "once per version of the file"
        assert len(store.evidence(REALM, 0)) == 3


def reply_for(addons, asked_items, scan_at, newer=None):
    """The game's auction section for whatever lookup is published: house 110's market as of a full scan."""
    found = market(110)

    async def request(kind):
        assert kind == kinds.KNOWLEDGE
        lookup = (Transport(addons).load_state().get("auctionWatch") or {}).get("lookup")
        prices = [{"item": i, "name": f"Item {i}", "price": found[i][0], "listed": found[i][1], "n": found[i][2],
                   "at": scan_at, "src": "scan", "place": 1} for i in asked_items if i in found]
        if newer:
            prices = [p if p["item"] != newer["item"] else {**p, **newer} for p in prices]
        data = {"realm": REALM, "places": [{"zone": "Stormwind City", "subzone": "Trade District",
                                            "faction": "Alliance"}],
                "scan": {"at": scan_at, "items": 30, "listings": 400, "place": 1}, "nextScan": 0, "priority": 30,
                "tracked": 3, "open": False, "lookup": lookup and lookup["id"], "prices": prices}
        return {"auction": data}, "World of Warcraft: Forever 1.60.1"

    return request


def test_get_auction_prices_merges_online_and_in_game(addons, api, monkeypatch, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    now = int(time.time())
    for house in HOUSES:
        api.at[house] = now - 600  # Blizzard's snapshot, ten minutes old
    asked = [*range(3000, 3025), SUFFIXED, 9999]
    # The game's full scan is three hours old, but it searched Item 3012 a minute ago and saw it cheaper.
    newer = {"item": 3012, "price": 5, "listed": 1, "n": 1, "at": now - 60, "src": "search"}
    monkeypatch.setattr(auction, "request", reply_for(addons, asked, now - 3 * 3600, newer))
    # Item 9999, which no snapshot has, binds when picked up: the game says so when asked (get_item_details).
    looked_up = []

    async def details(kind):
        lookup = Transport(addons).load_state()["itemLookup"]
        looked_up.append(lookup["items"])
        return {"itemDetails": {"lookup": lookup["id"], "items": [
            {"id": i, "name": f"Item {i}", "bind": 1, "lines": []} for i in lookup["items"]]}}, "WoW"

    monkeypatch.setattr(items, "request", details)
    result = asyncio.run(auction.get_auction_prices(asked, history=True))
    assert result.online.house == "Alliance Auction House 110" and result.online.snapshot_ago == "10 minutes ago"
    assert result.online.chosen.startswith("matched to the in-game prices (")
    by_id = {p.item_id: p for p in result.prices}
    assert [p.item_id for p in result.prices] == asked and result.not_seen == []
    hide = by_id[3001]
    price, quantity, _ = market(110)[3001]
    assert (hide.source, hide.min_price, hide.listed, hide.auctions, hide.commodity, hide.name) == (
        "online", price, quantity, 2, True, "Item 3001")
    assert [(p.price, p.quantity) for p in hide.cheapest] == [(price, quantity - 10), (price + 7, 10)]
    assert hide.in_game is None, "the game's full scan is older"
    assert hide.where == "Alliance Auction House 110 (online)" and hide.seen_ago == "10 minutes ago"
    assert hide.usual.snapshots == 1 and hide.usual.now_vs_usual is None and len(hide.history) == 1
    searched = by_id[3012]
    assert searched.source == "online" and searched.in_game.source == "search" and searched.in_game.min_price == 5
    gear = by_id[SUFFIXED]
    assert gear.random_suffixes == 2 and gear.min_price == 700 and gear.name is None, "the game didn't name it"
    missing = by_id[9999]
    assert (missing.source, missing.listed, missing.min_price, missing.last_listed) == ("online", 0, None, None)
    assert (missing.never_listed, missing.history_covers, missing.binds, missing.can_be_auctioned, missing.name) == (
        True, "one online snapshot", "on pickup", False, "Item 9999")
    assert [i for i, p in by_id.items() if p.never_listed] == [9999] and looked_up == [[9999]]
    assert "Never listed at any point in the price history (one online snapshot): item(s) 9999." in result.note

    # The game isn't there: online prices still are, with the house matched before.
    async def gone(kind):
        raise ToolError("World of Warcraft: Forever isn't running.")

    monkeypatch.setattr(auction, "request", gone)
    result = asyncio.run(auction.get_auction_prices([3001, 3012]))
    assert [p.source for p in result.prices] == ["online", "online"] and result.prices[0].name is None
    assert "Online prices only: the game didn't answer (World of Warcraft: Forever isn't running.)" in result.note
    # Nor can it say what an item never listed binds to.
    result = asyncio.run(auction.get_auction_prices([9999]))
    assert result.prices[0].never_listed and result.prices[0].binds is None and looked_up == [[9999]]
    assert "Check binds and can_be_auctioned (get_item_details)" in result.note
    # Without item IDs the priority list comes from the game, so that fails as before.
    with pytest.raises(ToolError, match="isn't running"):
        asyncio.run(auction.get_auction_prices())
    # Blizzard's API is down: the snapshots kept still serve.
    api.down = True
    with Store() as store:
        store.db.execute("DELETE FROM checks")
        store.db.commit()
    for house in HOUSES:
        api.at[house] = now - 7000
    with Store() as store:
        store.db.execute("UPDATE snapshots SET at = ?", (now - 7000,))
        store.db.commit()
    monkeypatch.setattr(auction, "request", reply_for(addons, [3001], now - 3 * 3600))
    result = asyncio.run(auction.get_auction_prices([3001]))
    assert result.prices[0].source == "online" and result.prices[0].seen_ago == "116 minutes ago"
    assert "Couldn't reach Blizzard's API for newer prices: Blizzard's API didn't answer" in result.note
    out = capsys.readouterr()
    no_secrets(caplog.text, out.out, out.err, result.model_dump_json())
    cache = Path(os.environ["XDG_CACHE_HOME"]) / "wow-mcp"
    for file in cache.iterdir():
        assert SECRET.encode() not in file.read_bytes() and TOKEN.encode() not in file.read_bytes()


def test_without_credentials(addons, monkeypatch):
    asked = [3001]
    monkeypatch.setattr(auction, "request", reply_for(addons, asked, int(time.time()) - 60))
    result = asyncio.run(auction.get_auction_prices(asked))
    assert result.online is None and result.prices[0].source == "full scan"
    assert result.note.startswith("Online prices are off: to have them, set up ") and "battlenet.env with " \
        "BATTLENET_CLIENT_ID=... and BATTLENET_CLIENT_SECRET=..." in result.note
    assert not (Path(os.environ["XDG_CACHE_HOME"]) / "wow-mcp").exists(), "no cache without online prices"
    assert online_auctions.refresh_once(addons) == online_auctions.REFRESH_INTERVAL


def test_the_bridge_refreshes_in_the_background(addons, api, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    # Off without credentials or with --no-auction-refresh; on with them.
    assert bridge.auction_refresh(addons, off=True) == []
    assert len(bridge.auction_refresh(addons)) == 1
    started, stop = threading.Event(), None

    def job(event):
        nonlocal stop
        stop = event
        started.set()
        event.wait()

    service = bridge.Bridge(addons, lambda prompt: "", publish=lambda *a, **k: 0, history=bridge.History(None),
                            background=[job])
    thread = threading.Thread(target=service.serve, daemon=True)
    thread.start()
    assert started.wait(5) and stop is service.stop
    service.stop.set()
    thread.join(5)
    assert not thread.is_alive()

    # The loop goes on through failures and stops when asked.
    calls, stop = [], threading.Event()

    def refresh(addons_dir):
        calls.append(addons_dir)
        if len(calls) == 1:
            raise battlenet.BattleNetError("Blizzard's API didn't answer for /x (HTTP 503; tried 3 times).")
        if len(calls) == 2:
            raise RuntimeError("something else")
        if len(calls) == 4:
            stop.set()
        return 0.01

    monkeypatch.setattr(online_auctions, "RECHECK", 0.01)
    online_auctions.refresh_loop(addons, stop, refresh)
    assert calls == [addons] * 4
    assert "online auction prices: Blizzard's API didn't answer" in caplog.text
    assert "online auction prices: refreshing failed" in caplog.text

    # What it fetches: nothing until the player's realm is known...
    monkeypatch.setattr(online_auctions, "RECHECK", 300)
    assert online_auctions.refresh_once(addons, now=T0 + 600) == online_auctions.REFRESH_INTERVAL and not api.paths()
    # ...the houses it could be while it's unknown...
    with Store() as store:
        store.set_facts(realm=REALM, faction="Alliance")
    assert online_auctions.refresh_once(addons, now=T0 + 600) == 3600 - 600 + 120
    assert {p.rsplit("/", 2)[-1] for p in api.paths() if "/auctions/1" in p} >= {"110", "111", "112", "116"}
    # ...then only the player's, each new snapshot soon after it's due.
    with Store() as store:
        store.record(REALM, evidence(110, T0 - 600))
    api.requests.clear()
    assert online_auctions.refresh_once(addons, now=T0 + 700) == 3600 - 700 + 120
    assert online_auctions.refresh_once(addons, now=T0 + 3800) == 300, "a 304: ask again in a few minutes"
    assert {p for p in api.paths() if "/auctions/1" in p} == {
        "/data/wow/ruleset/1/auctions/110", "/data/wow/ruleset/1/auctions/110/commodities"}
    api.at[110] = T0 + 3600
    assert online_auctions.refresh_once(addons, now=T0 + 4200) == 3600 + 3600 - 4200 + 120
    with Store() as store:
        assert len(store.snapshots("eu", 110)) == 4
    no_secrets(caplog.text)


def test_wow_mcp_auctions(api, capsys, wow_root, caplog):
    caplog.set_level(logging.DEBUG)
    now = int(time.time())
    for house in HOUSES:
        api.at[house] = now - 1200

    def run(*args):
        with pytest.raises(SystemExit) as e:
            online_auctions.main(list(args))
        out = capsys.readouterr()
        no_secrets(out.out, out.err)
        return e.value.code, out.out

    code, out = run("status")
    assert code == 0 and "Credentials: set (" in out and "Region: eu; realm: not known yet" in out
    assert "Auction house: not known yet" in out and not api.requests
    code, out = run("houses")
    assert code == 0 and "Rulesets: 1 PvP, 2 Normal, 3 Roleplay, 15 Roleplay PvP" in out
    assert "\n110  Alliance Auction House\n" in out and "\n116  Blackwater Auction House\n" in out
    with Store() as store:
        store.set_facts(realm=REALM, faction="Alliance")
        store.record(REALM, evidence(110, now - 1800))
    code, out = run("refresh")
    assert code == 0 and "realm: CAMWOW EU4 Realm 8 PvP (Alliance)" in out
    assert re.search(r"110 items: not due yet; the latest is from .* \(20 minutes ago\)", out), out
    code, out = run("status")
    assert code == 0 and "Ruleset: 1 (of 1 PvP, 2 Normal, 3 Roleplay, 15 Roleplay PvP; they all list" in out
    assert "Auction house: Alliance Auction House 110, matched to the in-game prices (over 99.99% sure)" in out
    assert "  items: " in out and "22 items" in out and "history: 1 snapshot time(s)" in out
    assert "30 in-game prices kept as evidence" in out
    code, out = run("refresh", "--house", "111", "--force")
    assert code == 0 and "111 items: no newer snapshot yet (304)" in out
    code, out = run("match")
    assert code == 0 and out.strip().endswith("The player's auction house: 110.")
    code, out = run("houses")
    assert "110  Alliance Auction House: " in out and "<- the player's, matched to the in-game prices" in out
    # Without credentials: what's there, and how to set them up.
    setup("BATTLENET_REGION=eu\n")
    code, out = run("status")
    assert code == 0 and "Credentials: missing" in out
    with pytest.raises(SystemExit) as e:
        online_auctions.main(["refresh"])
    assert "No Blizzard API credentials" in str(e.value.code) and "battlenet.env" in str(e.value.code)
    no_secrets(caplog.text)
    database = sqlite3.connect(Path(os.environ["XDG_CACHE_HOME"]) / "wow-mcp" / "auctions.sqlite3")
    assert database.execute("SELECT count(*) FROM matches WHERE house = 110 AND clear").fetchone() == (1,)
