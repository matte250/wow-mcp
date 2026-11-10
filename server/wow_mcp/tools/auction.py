"""Auction house prices (Auction.lua): looked up in game while the player has the auction house open, for what
their goals and plan need plus a watch list (payload key "auctionWatch"). They're cached per realm in game and
read back from the "auction" section of request kind KNOWLEDGE."""

import time
from datetime import datetime, timezone
from typing import Annotated

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool, transport
from wow_mcp.tools.messages import delivery_note
from wow_mcp.tools.stats import format_money

guidance(
    "When a plan or goal needs materials, weigh buying them against farming them before you build it: "
    "get_auction_prices has what they cost on the player's realm (when that was seen and how many were listed), "
    "get_vendor_items what vendors sell, get_character_stats the player's money; compare with where farming them "
    "would be and how long it would take. When it's a real choice and the player hasn't said which they prefer, "
    "ask them in your answer before building the plan (they can reply with /ai). Without recent prices, call "
    "watch_auction_prices with the item IDs, so the game looks them up the next time the player opens the auction "
    "house (what goals and the plan need is looked up anyway), and say so.",
    order=14,
)

MAX_WATCH = 50


class PricePoint(BaseModel):
    price: int = Field(description="Copper per item.")
    quantity: int


class AuctionPrice(BaseModel):
    item_id: int
    name: str | None = Field(description="Null if the game hadn't loaded the item; look it up by item_id.")
    min_price: int | None = Field(
        description="Cheapest buyout per item, in copper (100 copper = 1 silver, 10000 = 1 gold). Null: none "
        "listed, or only bids."
    )
    min_price_text: str | None = Field(description="The same, e.g. '1s 45c'.")
    listed: int = Field(description="How many were listed by others when seen (the player's own are left out).")
    cheapest: list[PricePoint] = Field(
        description="The cheapest prices, cheapest first, with how many at each: what buying several would cost."
    )
    bid_only: int | None = Field(description="How many were listed with a bid but no buyout.")
    commodity: bool = Field(description="Sold by quantity from a shared pool, rather than as single auctions.")
    seen_at: datetime
    seen_ago: str = Field(description="e.g. '12 minutes ago'; prices change, so say how old they are.")
    where: str | None = Field(description="Where the auction house was (faction and neutral ones can differ).")


class AuctionPrices(BaseModel):
    game: str = Field(description="The WoW version being played.")
    realm: str | None
    prices: list[AuctionPrice] = Field(description="Newest first.")
    not_seen: list[int] = Field(description="Items asked for with no price yet.")
    watching: list[int] = Field(description="Items the game looks up at the next auction house visit.")
    at_auction_house: bool = Field(description="The player has the auction house open right now.")
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


def _price(raw: dict) -> AuctionPrice:
    price = raw.get("price")
    at = int(raw.get("at") or 0)
    where = ", ".join(p for p in (raw.get("subzone"), raw.get("zone")) if p)
    if where and raw.get("faction"):
        where += f" (seen as {raw['faction']})"
    return AuctionPrice(
        item_id=raw["item"],
        name=raw.get("name"),
        min_price=price,
        min_price_text=format_money(price) if isinstance(price, int) else None,
        listed=int(raw.get("listed") or 0),
        cheapest=[PricePoint(price=t["price"], quantity=t["count"]) for t in as_list(raw.get("tiers"))
                  if isinstance(t, dict) and "price" in t and "count" in t],
        bid_only=raw.get("bidOnly"),
        commodity=bool(raw.get("commodity")),
        seen_at=datetime.fromtimestamp(at, timezone.utc),
        seen_ago=_ago(at),
        where=where or None,
    )


@tool(annotations=ToolAnnotations(title="Auction house prices", read_only_hint=True, open_world_hint=False))
async def get_auction_prices(
    item_ids: Annotated[
        list[int] | None,
        Field(max_length=100, description="Item IDs for this game version. Leave out for every price the game has."),
    ] = None,
) -> AuctionPrices:
    """Get auction house prices on the player's realm as the game last saw them: the cheapest buyout per item,
    how many were listed, the cheapest price points (to work out what buying several costs) and when that was.
    The game looks prices up while the player has the auction house open: for what their goals and plan need,
    items added with watch_auction_prices, and whatever the player searched for themselves."""
    reply, version = await request(kinds.KNOWLEDGE)
    data = section(reply, "auction")
    data = data if isinstance(data, dict) else {}
    prices = [_price(p) for p in as_list(data.get("prices")) if isinstance(p, dict) and isinstance(p.get("item"), int)]
    not_seen: list[int] = []
    if item_ids:
        wanted = list(dict.fromkeys(item_ids))
        prices = [p for p in prices if p.item_id in wanted]
        not_seen = [i for i in wanted if i not in {p.item_id for p in prices}]
    watching = [int(i) for i in as_list(data.get("watch")) if isinstance(i, (int, float))]
    notes = []
    if not_seen:
        missing = [i for i in not_seen if i not in watching]
        notes.append(
            "No price yet for item(s) " + ", ".join(map(str, not_seen)) + ": "
            + ("call watch_auction_prices with them so " if missing else "")
            + "the game looks them up the next time the player opens the auction house."
        )
    elif not prices:
        notes.append("No prices yet: the game looks them up when the player opens the auction house.")
    if data.get("open") and data.get("waiting"):
        notes.append(f"The player is at the auction house; lookups are {data['waiting']}.")
    elif data.get("open") and data.get("left"):
        notes.append(f"The player is at the auction house; {data['left']} lookup(s) still to come.")
    if data.get("problem"):
        notes.append(f"Reading a result failed in game: {data['problem']}")
    return AuctionPrices(
        game=version, realm=data.get("realm"), prices=prices, not_seen=not_seen, watching=watching,
        at_auction_house=bool(data.get("open")), note=" ".join(notes) or None,
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
    """Have the game look up the auction house prices of these items the next time the player opens the auction
    house (and every visit after, once the price is 10 minutes old). get_auction_prices reads them afterwards.
    Items the player's goals and plan need are looked up anyway."""
    if not item_ids and not replace:
        raise ToolError("Give the item IDs to watch (or replace=True with none to stop watching all).")
    if any(i <= 0 for i in item_ids):
        raise ToolError("Item IDs are positive numbers.")
    total = 0

    def change(state: dict, seq: int) -> None:
        nonlocal total
        before = [] if replace else [w["item"] for w in (state.get("auctionWatch") or {}).get("items") or []]
        merged = [i for i in before if i not in item_ids] + list(dict.fromkeys(item_ids))
        merged = merged[-MAX_WATCH:]  # the oldest go first
        total = len(merged)
        state["auctionWatch"] = {"id": seq, "items": [{"item": i} for i in merged]}

    transport().publish(change)
    if not item_ids:
        return "Stopped watching auction prices." + delivery_note()
    return (
        f"Watching {len(item_ids)} item(s) ({total} in all): the game looks up their prices the next time the player "
        "opens the auction house, without changing what they're looking at there; get_auction_prices reads them."
        + delivery_note()
    )
