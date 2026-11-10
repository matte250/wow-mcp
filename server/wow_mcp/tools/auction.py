"""Auction house prices (Auction.lua). While the player has the auction house open, the game takes a full scan of
it whenever the game allows one (every 15 minutes): the cheapest buyout, how many are listed and in how many auctions,
for every item, kept per realm with the scan's time; an item that's gone keeps its last price. Items on its priority
list (what the AI tracks, then what the player could sell from their bags and bank) also get their cheapest price
points, and item searches when the last full scan is over 10 minutes old.

Replies stream at about 1 KB/s, so the game only sends what's asked for: the item IDs go to the game first, as the
"lookup" of payload key "auctionWatch" (one live-update slot; none if it's the same list as last time), then section
"auction" of request kind KNOWLEDGE answers them. With no IDs, it answers with the priority list.
"""

import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool, transport
from wow_mcp.tools.messages import delivery_note
from wow_mcp.tools.stats import format_money

guidance(
    "When a plan or goal needs materials, weigh buying them against farming them before you build it: "
    "get_auction_prices has what they cost on the player's realm, get_vendor_items what vendors sell, "
    "get_character_stats the player's money; compare with where farming them would be and how long it would take. "
    "When it's a real choice and the player hasn't said which they prefer, ask them in your answer before building "
    "the plan (they can reply with /ai). Prices come from the game's last full scan of the auction house or a later "
    "search, so say how old they are (seen_ago); an item that wasn't listed then has last_listed, e.g. 'last listed "
    "3 days ago at 45c'. Any item listed at a full scan has a baseline price; asking about an item, or "
    "watch_auction_prices, puts it on the priority list the game checks at every auction house visit (goal and plan "
    "items are on it anyway).",
    order=14,
)

MAX_WATCH = 100
MAX_LOOKUP = 100
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


class AuctionPrice(BaseModel):
    item_id: int
    name: str | None = Field(description="Null if the game hadn't loaded the item; look it up by item_id.")
    min_price: int | None = Field(
        description="Cheapest buyout per item, in copper (100 copper = 1 silver, 10000 = 1 gold). Null: none "
        "listed (see last_listed), or only bids."
    )
    min_price_text: str | None = Field(description="The same, e.g. '1s 45c'.")
    listed: int = Field(description="How many were listed by others when seen (the player's own are left out).")
    auctions: int | None = Field(description="In how many auctions.")
    cheapest: list[PricePoint] = Field(
        description="The cheapest prices, cheapest first, with how many at each: what buying several would cost. "
        "Empty for items off the priority list, which only have the full scan's cheapest price."
    )
    bid_only: int | None = Field(description="How many were listed with a bid but no buyout.")
    commodity: bool | None = Field(description="Sold by quantity from a shared pool, rather than as single auctions.")
    source: Literal["full scan", "search"] = Field(
        description="A full scan sees every auction on the auction house at once; a search looks at one item."
    )
    seen_at: datetime
    seen_ago: str = Field(description="e.g. '12 minutes ago'; prices change, so say how old they are.")
    where: str | None = Field(description="Where the auction house was (faction and neutral ones can differ).")
    last_listed: LastListed | None = Field(
        description="None were listed when last seen: the last time some were, if the game saw that."
    )
    tracked: TRACKED | None = Field(
        description="Why it's on the priority list the game checks at every visit: the AI's goal, plan, watch list "
        "or question, or the player's bags or bank. Null: not on it; it only has full scan prices."
    )


class FullScan(BaseModel):
    seen_at: datetime
    seen_ago: str
    items: int = Field(description="Different items listed.")
    auctions: int
    where: str | None


class AuctionPrices(BaseModel):
    game: str = Field(description="The WoW version being played.")
    realm: str | None
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


@tool(annotations=ToolAnnotations(title="Auction house prices", read_only_hint=True, open_world_hint=False))
async def get_auction_prices(
    item_ids: Annotated[
        list[Annotated[int, Field(gt=0)]] | None,
        Field(max_length=MAX_LOOKUP, description="Item IDs for this game version. Leave out for the priority list."),
    ] = None,
) -> AuctionPrices:
    """Get auction house prices on the player's realm as the game last saw them: the cheapest buyout per item, how
    many were listed in how many auctions, the cheapest price points (to work out what buying several costs), where
    they came from (the game's full scan of the auction house, or a search) and how old they are. Any item listed at
    the last full scan has a price; one that wasn't says when it was last listed and at what. The items asked about
    go on the priority list the game checks at every auction house visit. Without item IDs: the priority list (goal,
    plan, watched and asked-about items, then what the player could sell from their bags and bank) and when the
    last full scan was."""
    ids = list(dict.fromkeys(item_ids or []))
    data, version = await _ask(ids)
    places = as_list(data.get("places"))
    prices = [_price(p, places) for p in as_list(data.get("prices"))
              if isinstance(p, dict) and isinstance(p.get("item"), int)]
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
    return AuctionPrices(
        game=version, realm=data.get("realm"), prices=prices, not_seen=not_seen, last_full_scan=last_full_scan,
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
