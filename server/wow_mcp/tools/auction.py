"""Auction house prices (Auction.lua). While the player has the auction house open, the game takes a full scan of
it whenever the game allows one (every 15 minutes): the cheapest buyout, how many are listed and in how many auctions,
for every item, kept per realm with the scan's time; an item that's gone keeps its last price. Items on its priority
list (what the AI tracks, then what the player could sell from their bags and bank) also get their cheapest price
points, and item searches when the last full scan is over 10 minutes old.

Replies stream at about 1 KB/s, so the game only sends what's asked for: the item IDs go to the game first, as the
"lookup" of payload key "auctionWatch" (one live-update slot; none if it's the same list as last time), then section
"auction" of request kind KNOWLEDGE answers them. With no IDs, it answers with the priority list.

When Blizzard's API is set up (online_auctions.py), each item's price is the online one, a snapshot of the player's
auction house at most about an hour old, with the game's own alongside when that's newer; the game's prices also
go to online_auctions as the evidence for which auction house is the player's.
"""

import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import game, kinds, online_auctions
from wow_mcp.app import addons_dir, as_list, guidance, request, section, tool, transport
from wow_mcp.tools.messages import delivery_note
from wow_mcp.tools.stats import format_money

guidance(
    "When a plan or goal needs materials, weigh buying them against farming them before you build it: "
    "get_auction_prices has what they cost on the player's realm, get_vendor_items what vendors sell, "
    "get_character_stats the player's money; compare with where farming them would be and how long it would take. "
    "When it's a real choice and the player hasn't said which they prefer, ask them in your answer before building "
    "the plan (they can reply with /ai). Prices are online when Blizzard's API is set up (source 'online': a "
    "snapshot of the player's auction house, at most about an hour old), otherwise the game's own as of the player's "
    "last auction house visit (its full scan or a later search); say which and how old (seen_ago), e.g. 'online, 20 "
    "minutes ago' or 'when you were last at the auction house, 3 days ago'. An item that wasn't listed has "
    "last_listed, e.g. 'last listed 3 days ago at 45c'. history=True adds the usual price (the median of the last 7 "
    "days), so you can say whether something is cheaper than usual. Online prices of items the game hasn't named "
    "come with the item ID only: name them with get_item_details. Any item listed at a full scan has a baseline "
    "price; asking about an item, or watch_auction_prices, puts it on the priority list the game checks at every "
    "auction house visit (goal and plan items are on it anyway).",
    order=14,
)

MAX_WATCH = 100
MAX_LOOKUP = 100
USUAL_SNAPSHOTS = 6  # online snapshots (hours) before a price is compared with the usual one
LIST_RETRIES, LIST_WAIT = 4, 1.0  # the game hasn't got the item list yet (e.g. in combat)
TRACKED = Literal["goal", "plan", "watch", "asked", "bags", "bank"]


class PricePoint(BaseModel):
    price: int = Field(description="Copper per item.")
    quantity: int


class LastListed(BaseModel):
    min_price: int | None = Field(description="Cheapest buyout per item then, in copper. Null: only bids.")
    min_price_text: str | None
    listed: int
    seen_at: datetime
    seen_ago: str


class InGamePrice(BaseModel):
    min_price: int | None = Field(description="Cheapest buyout per item, in copper. Null: none listed, or only bids.")
    min_price_text: str | None
    listed: int = Field(description="How many were listed by others (the player's own are left out).")
    auctions: int | None
    cheapest: list[PricePoint]
    source: Literal["full scan", "search"]
    seen_at: datetime
    seen_ago: str
    where: str | None


class UsualPrice(BaseModel):
    min_price: int | None = Field(description="The median of the cheapest buyout per item over the last 7 days of "
                                  "online snapshots. Null: none listed with a buyout then.")
    min_price_text: str | None
    listed: int = Field(description="The median of how many were listed.")
    snapshots: int = Field(description="How many snapshots it's from (about one an hour).")
    since: datetime
    now_vs_usual: str | None = Field(description="e.g. '25% cheaper than usual', 'about usual', '40% more expensive "
                                     "than usual'. Null until there are 6 snapshots.")


class HistoryPoint(BaseModel):
    at: datetime
    span: Literal["snapshot", "day"] = Field(description="One online snapshot, or a day: the median of its snapshots.")
    min_price: int | None = Field(description="The cheapest buyout per item then. Null: none listed.")
    min_price_text: str | None
    listed: int


class AuctionPrice(BaseModel):
    item_id: int
    name: str | None = Field(description="Null if the game hasn't named the item (online prices come with the item "
                             "ID only): look it up with get_item_details.")
    min_price: int | None = Field(
        description="Cheapest buyout per item, in copper (100 copper = 1 silver, 10000 = 1 gold). Null: none "
        "listed (see last_listed), or only bids."
    )
    min_price_text: str | None = Field(description="The same, e.g. '1s 45c'.")
    listed: int = Field(description="How many were listed when seen (in game, the player's own are left out).")
    auctions: int | None = Field(description="In how many auctions.")
    cheapest: list[PricePoint] = Field(
        description="The cheapest prices, cheapest first, with how many at each: what buying several would cost. "
        "In game, only priority items have them; others only have the full scan's cheapest price."
    )
    bid_only: int | None = Field(description="How many were listed with a bid but no buyout.")
    commodity: bool | None = Field(description="Sold by quantity from a shared pool, rather than as single auctions.")
    source: Literal["online", "full scan", "search"] = Field(
        description="online: Blizzard's API, a snapshot of the player's auction house (taken about once an hour). "
        "In game: a full scan sees every auction at once at the player's visit; a search looks at one item."
    )
    seen_at: datetime
    seen_ago: str = Field(description="e.g. '12 minutes ago'; prices change, so say how old they are.")
    where: str | None = Field(description="Which auction house (faction and neutral ones can differ).")
    last_listed: LastListed | None = Field(
        description="None were listed when last seen: the last time some were, if that was seen."
    )
    tracked: TRACKED | None = Field(
        description="Why it's on the priority list the game checks at every visit: the AI's goal, plan, watch list "
        "or question, or the player's bags or bank. Null: not on it; in game it only has full scan prices."
    )
    random_suffixes: int | None = Field(
        default=None, description="Random-suffix gear ('of the Bear') online: how many different suffixes are listed. "
        "min_price is then the cheapest of any suffix, not of one with particular stats. Null: no suffixes seen."
    )
    in_game: InGamePrice | None = Field(
        default=None, description="What the game saw itself, when that's newer than the online snapshot (e.g. a "
        "search at the auction house just now)."
    )
    usual: UsualPrice | None = Field(default=None, description="history=True: the usual online price.")
    history: list[HistoryPoint] | None = Field(
        default=None, description="history=True: online snapshots of the last 24 hours, then a point per day for "
        "14 days, oldest first."
    )


class FullScan(BaseModel):
    seen_at: datetime
    seen_ago: str
    items: int = Field(description="Different items listed.")
    auctions: int
    where: str | None


class OnlineSource(BaseModel):
    house: str = Field(description="The player's auction house in Blizzard's API, e.g. 'Alliance Auction House 110'.")
    chosen: str = Field(description="How it was picked: set by the player, or matched to the prices the game saw.")
    snapshot_at: datetime | None
    snapshot_ago: str | None = Field(description="The newest snapshot; Blizzard takes one about every hour.")
    history_since: datetime | None = Field(description="The oldest snapshot kept: how far history goes back.")


class AuctionPrices(BaseModel):
    game: str = Field(description="The WoW version being played.")
    realm: str | None
    online: OnlineSource | None = Field(default=None, description="Where the online prices come from. Null: none "
                                        "(the note says why).")
    prices: list[AuctionPrice] = Field(description="In the order asked for; without item IDs, the priority list's.")
    not_seen: list[int] = Field(description="Items asked for that no full scan or search has seen yet.")
    last_full_scan: FullScan | None = Field(description="Null: the game hasn't taken one yet.")
    next_full_scan: str = Field(description="When the game may take the next one (at the auction house).")
    priority_list: int = Field(description="Items the game checks at every auction house visit (at most 100).")
    ai_tracked: int = Field(description="How many of those are the AI's (goals, plan, watched, asked about).")
    at_auction_house: bool = Field(description="The player has the auction house open right now.")
    searches_left: int | None = Field(description="At the auction house: searches still to come this visit.")
    note: str | None = None


def _ago(at: int) -> str:
    minutes = max(0, int(time.time() - at)) // 60
    if minutes < 1:
        return "just now"
    if minutes < 120:
        return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} hours ago"
    return f"{minutes // 1440} days ago"


def _when(at: Any) -> tuple[datetime, str]:
    at = int(at or 0)
    return datetime.fromtimestamp(at, timezone.utc), _ago(at)


def _where(places: list, number: Any) -> str | None:
    place = places[number - 1] if isinstance(number, int) and 0 < number <= len(places) else None
    if not isinstance(place, dict):
        return None
    where = ", ".join(p for p in (place.get("subzone"), place.get("zone")) if isinstance(p, str) and p)
    if where and place.get("faction"):
        where += f" (seen as {place['faction']})"
    return where or None


def _money(copper: Any) -> str | None:
    return format_money(copper) if isinstance(copper, int) else None


def _cheapest(tiers: Any) -> list[PricePoint]:
    return [PricePoint(price=t[0], quantity=t[1]) for t in as_list(tiers)
            if isinstance(t, list) and len(t) == 2 and all(isinstance(n, int) for n in t)]


def _price(raw: dict, places: list) -> AuctionPrice:
    seen_at, seen_ago = _when(raw.get("at"))
    last = raw.get("last") if isinstance(raw.get("last"), dict) else None
    last_listed = None
    if last:
        last_at, last_ago = _when(last.get("at"))
        last_listed = LastListed(min_price=last.get("price"), min_price_text=_money(last.get("price")),
                                 listed=int(last.get("listed") or 0), seen_at=last_at, seen_ago=last_ago)
    why = raw.get("why")
    return AuctionPrice(
        item_id=raw["item"],
        name=raw.get("name") if isinstance(raw.get("name"), str) else None,
        min_price=raw.get("price"),
        min_price_text=_money(raw.get("price")),
        listed=int(raw.get("listed") or 0),
        auctions=raw.get("n"),
        cheapest=_cheapest(raw.get("tiers")),
        bid_only=raw.get("bidOnly"),
        commodity=raw.get("commodity"),
        source="full scan" if raw.get("src") == "scan" else "search",
        seen_at=seen_at,
        seen_ago=seen_ago,
        where=_where(places, raw.get("place")),
        last_listed=last_listed,
        tracked=why if why in ("goal", "plan", "watch", "asked", "bags", "bank") else None,
    )


def _watch(state: dict) -> dict:
    """The auctionWatch payload as it is, with every part there."""
    watch = dict(state.get("auctionWatch") or {})
    watch.setdefault("items", [])
    return watch


def _published(ids: list[int]) -> int | None | bool:
    """The lookup the game already has for exactly these items (its id; None for the priority list when there's
    never been a lookup), or False if one must be published."""
    current = (transport().load_state().get("auctionWatch") or {}).get("lookup")
    if not current:
        return None if not ids else False
    return current.get("id") if current.get("items") == ids else False


def _publish(ids: list[int]) -> int:
    def change(state: dict, seq: int) -> None:
        watch = _watch(state)
        watch.update(id=seq, lookup={"id": seq, "at": int(time.time()), "items": ids})
        state["auctionWatch"] = watch

    return transport().publish(change)


async def _ask(ids: list[int]) -> tuple[dict, str]:
    """The game's answer for these items (the priority list if none): the section and the game version."""
    lookup, sent = _published(ids), False
    if lookup is False:
        lookup, sent = _publish(ids), True
    waits = 0
    while True:
        reply, version = await request(kinds.KNOWLEDGE)
        data = section(reply, "auction")
        data = data if isinstance(data, dict) else {}
        answered = data.get("lookup")
        if answered == lookup:
            return data, version
        if waits >= LIST_RETRIES:
            raise ToolError(
                "The game hasn't received the list of items yet. Updates wait while the player is in combat, "
                "so try again after the fight; if it keeps happening, call get_status."
            )
        waits += 1
        if not sent or (isinstance(answered, int) and isinstance(lookup, int) and answered > lookup):
            # The game never got this list, or another session's lookup replaced it.
            lookup, sent = _publish(ids), True
        await asyncio.sleep(LIST_WAIT)


def _faction(places: list, number: Any) -> str | None:
    place = places[number - 1] if isinstance(number, int) and 0 < number <= len(places) else None
    return place.get("faction") if isinstance(place, dict) and isinstance(place.get("faction"), str) else None


def _evidence(raw: list[dict], places: list) -> list[online_auctions.Seen]:
    """The game's prices, for working out which online auction house is the player's."""
    seen = []
    for p in raw:
        if p.get("src") in ("scan", "search") and isinstance(p.get("at"), int) and p["at"] > 0:
            seen.append(online_auctions.Seen(
                p["item"], p["at"], p.get("price") if isinstance(p.get("price"), int) else None,
                int(p.get("listed") or 0), p.get("n") if isinstance(p.get("n"), int) else None,
                "full scan" if p["src"] == "scan" else "search", _faction(places, p.get("place"))))
    return seen


def _in_game(price: AuctionPrice) -> InGamePrice:
    return InGamePrice(min_price=price.min_price, min_price_text=price.min_price_text, listed=price.listed,
                       auctions=price.auctions, cheapest=price.cheapest, source=price.source,  # type: ignore[arg-type]
                       seen_at=price.seen_at, seen_ago=price.seen_ago, where=price.where)


def _usual(usual: online_auctions.Usual | None, now: int | None) -> UsualPrice | None:
    if not usual:
        return None
    compared = None
    if usual.price and now and usual.snapshots >= USUAL_SNAPSHOTS:
        change = now / usual.price - 1
        compared = "about usual" if abs(change) < 0.05 else (
            f"{abs(change):.0%} {'cheaper' if change < 0 else 'more expensive'} than usual")
    return UsualPrice(min_price=usual.price, min_price_text=_money(usual.price), listed=usual.quantity,
                      snapshots=usual.snapshots, since=_when(usual.since)[0], now_vs_usual=compared)


def _online_price(item: int, online: online_auctions.ItemPrice, house: online_auctions.House,
                  in_game: AuctionPrice | None, with_history: bool) -> AuctionPrice:
    """An item's online price, with the game's alongside when that's newer."""
    listing = online.listing
    seen_at, seen_ago = _when(online.at)
    last_listed = None
    if listing is None and online.last:
        last_at, last = online.last
        when = _when(last_at)
        last_listed = LastListed(min_price=last.price, min_price_text=_money(last.price), listed=last.quantity,
                                 seen_at=when[0], seen_ago=when[1])
    history = None
    if with_history:
        history = [HistoryPoint(at=_when(p.at)[0], span=p.span, min_price=p.price,  # type: ignore[arg-type]
                                min_price_text=_money(p.price), listed=p.quantity) for p in online.history]
    return AuctionPrice(
        item_id=item, name=in_game.name if in_game else None,
        min_price=listing.price if listing else None, min_price_text=_money(listing.price if listing else None),
        listed=listing.quantity if listing else 0, auctions=listing.auctions if listing else None,
        cheapest=[PricePoint(price=p, quantity=n) for p, n in listing.tiers] if listing else [],
        bid_only=(listing.bid_only or None) if listing else None, commodity=online.commodity, source="online",
        seen_at=seen_at, seen_ago=seen_ago, where=f"{house.label} (online)", last_listed=last_listed,
        tracked=in_game.tracked if in_game else None,
        random_suffixes=(listing.suffixes or None) if listing else None,
        in_game=_in_game(in_game) if in_game and in_game.seen_at > seen_at else None,
        usual=_usual(online.usual, listing.price if listing else None) if with_history else None, history=history,
    )


def _installed_version() -> str:
    folder = addons_dir()
    try:
        return game.situation(folder).installed.describe() if folder else "unknown"
    except (OSError, IndexError):
        return "unknown"


async def _online(items: list[int], realm: Any, raw: list[dict], places: list,
                  with_history: bool) -> online_auctions.Prices:
    faction = next((p.get("faction") for p in places if isinstance(p, dict) and isinstance(p.get("faction"), str)),
                   None)
    try:
        return await asyncio.to_thread(online_auctions.lookup, items, realm if isinstance(realm, str) else None,
                                       faction, _evidence(raw, places), with_history, addons_dir())
    except Exception as e:  # noqa: BLE001 (online prices never stop the game's)
        return online_auctions.Prices(notes=[f"Online prices failed ({type(e).__name__})."])


@tool(annotations=ToolAnnotations(title="Auction house prices", read_only_hint=True, open_world_hint=True))
async def get_auction_prices(
    item_ids: Annotated[
        list[Annotated[int, Field(gt=0)]] | None,
        Field(max_length=MAX_LOOKUP, description="Item IDs for this game version. Leave out for the priority list."),
    ] = None,
    history: Annotated[
        bool,
        Field(description="Also each item's usual online price (the median of the last 7 days) and its history: the "
              "snapshots of the last 24 hours, then a point per day for 14 days."),
    ] = False,
) -> AuctionPrices:
    """Get auction house prices on the player's realm: the cheapest buyout per item, how many are listed in how many
    auctions, the cheapest price points (to work out what buying several costs), where they came from and how old
    they are. Online prices (Blizzard's API, when set up) are a snapshot of the player's auction house at most about
    an hour old; the game's own are as of the player's last auction house visit (its full scan, or a search) and come
    along when they're newer or there's no online price. An item not listed says when it last was and at what.
    history=True adds the usual price and price history. The items asked about go on the priority list the game
    checks at every auction house visit. Without item IDs: the priority list (goal, plan, watched and asked-about
    items, then what the player could sell from their bags and bank) and when the last full scan was."""
    ids = list(dict.fromkeys(item_ids or []))
    # A newer snapshot is fetched meanwhile, if one is due.
    warming = asyncio.create_task(asyncio.to_thread(online_auctions.warm_up, addons_dir()))
    try:
        data, version = await _ask(ids)
        trouble = None
    except ToolError as e:  # the game isn't there: online prices may still be
        data, version, trouble = {}, _installed_version(), e
    await warming
    places = as_list(data.get("places"))
    raw = [p for p in as_list(data.get("prices")) if isinstance(p, dict) and isinstance(p.get("item"), int)]
    in_game = {p["item"]: _price(p, places) for p in raw}
    asked = ids or list(in_game)
    if trouble and not asked:
        raise trouble
    online = await _online(asked, data.get("realm"), raw, places, history)
    if trouble and not online.items:
        raise ToolError(f"{trouble} {' '.join(online.notes)}".strip()) from None
    prices = []
    for item in asked:
        if online.house and item in online.items:
            prices.append(_online_price(item, online.items[item], online.house, in_game.get(item), history))
        elif item in in_game:
            prices.append(in_game[item])
    not_seen = [i for i in ids if i not in {p.item_id for p in prices}]
    scan = data.get("scan") if isinstance(data.get("scan"), dict) else None
    last_full_scan = None
    if scan:
        seen_at, seen_ago = _when(scan.get("at"))
        last_full_scan = FullScan(seen_at=seen_at, seen_ago=seen_ago, items=int(scan.get("items") or 0),
                                  auctions=int(scan.get("listings") or 0), where=_where(places, scan.get("place")))
    wait = int(data.get("nextScan") or 0) - int(time.time())
    next_full_scan = f"in {math.ceil(wait / 60)} minutes" if wait > 0 else "at the player's next auction house visit"
    if data.get("open") and wait <= 0:
        next_full_scan = "now"
    notes = []
    if not_seen:
        notes.append(
            "No price yet for item(s) " + ", ".join(map(str, not_seen)) + ": the game checks them at the player's "
            "next auction house visit (they're on its priority list now)."
        )
    elif not prices:
        notes.append("No prices yet: the game looks them up when the player opens the auction house.")
    if data.get("reading"):
        notes.append("The game is reading a new full scan right now; ask again in a few seconds for newer prices.")
    if data.get("open") and data.get("waiting"):
        notes.append(f"The player is at the auction house; lookups are {data['waiting']}.")
    elif data.get("open") and data.get("left"):
        notes.append(f"The player is at the auction house; {data['left']} search(es) still to come.")
    if data.get("problem"):
        notes.append(f"Reading a result failed in game: {data['problem']}")
    if trouble:
        notes.append(f"Online prices only: the game didn't answer ({trouble})")
    notes += online.notes
    source = None
    if online.house and online.how:
        snapshot_at, snapshot_ago = _when(online.at) if online.at else (None, None)
        source = OnlineSource(house=online.house.label, chosen=online.how, snapshot_at=snapshot_at,
                              snapshot_ago=snapshot_ago,
                              history_since=_when(online.since)[0] if online.since else None)
    return AuctionPrices(
        game=version, realm=data.get("realm"), online=source, prices=prices, not_seen=not_seen,
        last_full_scan=last_full_scan,
        next_full_scan=next_full_scan, priority_list=int(data.get("priority") or 0),
        ai_tracked=int(data.get("tracked") or 0), at_auction_house=bool(data.get("open")),
        searches_left=data.get("left") if data.get("open") else None, note=" ".join(notes) or None,
    )


@tool(
    annotations=ToolAnnotations(
        title="Watch auction prices", read_only_hint=False, destructive_hint=False, open_world_hint=False
    )
)
def watch_auction_prices(
    item_ids: Annotated[
        list[int],
        Field(max_length=MAX_WATCH, description="Item IDs for this game version, e.g. 4232 for Medium Hide."),
    ],
    replace: Annotated[bool, Field(description="Drop the items watched before (an empty list clears them).")] = False,
) -> str:
    """Put items on the priority list the game checks at every auction house visit (at most 100, the oldest go
    first; watching an item again keeps it on). Its items get their cheapest price points and are searched when the
    last full scan is over 10 minutes old. get_auction_prices reads them. Goal and plan items are on it anyway, and
    so is anything you ask get_auction_prices about."""
    if not item_ids and not replace:
        raise ToolError("Give the item IDs to watch (or replace=True with none to stop watching all).")
    if any(i <= 0 for i in item_ids):
        raise ToolError("Item IDs are positive numbers.")
    total = 0

    def change(state: dict, seq: int) -> None:
        nonlocal total
        watch, now = _watch(state), int(time.time())
        before = [] if replace else [w for w in watch["items"] if w.get("item") not in item_ids]
        merged = (before + [{"item": i, "at": now} for i in dict.fromkeys(item_ids)])[-MAX_WATCH:]  # oldest go first
        total = len(merged)
        watch.update(id=seq, items=merged)
        if replace:
            watch["cleared"] = now
        state["auctionWatch"] = watch

    transport().publish(change)
    if not item_ids:
        return "Stopped watching auction prices." + delivery_note()
    return (
        f"Watching {len(item_ids)} item(s) ({total} in all): they're on the priority list the game checks at every "
        "auction house visit, without changing what the player is looking at there; get_auction_prices reads them."
        + delivery_note()
    )
