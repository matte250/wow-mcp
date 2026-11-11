"""The player's own auctions (Auction.lua): what they have listed, as the auction house listed it at their last visit,
and what became of it since: sold, expired or cancelled, from the game's notices (they arrive anywhere) and the auction
house mail the mailbox showed. Section "myAuctions" of request kind KNOWLEDGE, which also has the game's own prices of
the listed items and the auction house mail as last seen.

Whether others list an item cheaper comes from the newer of the online snapshot (online_auctions.py) and the game's own
prices. The game leaves the player's own auctions out of its prices. Blizzard's API has no sellers, so the player's
listings are taken out of the snapshot's price points by price and quantity.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds, online_auctions
from wow_mcp.app import addons_dir, as_list, guidance, request, section, tool
from wow_mcp.tools import auction
from wow_mcp.tools.auction import _duration, _money, _when
from wow_mcp.tools.stats import format_money

guidance(
    "For questions about the player's own auctions ('did anything sell?', 'am I undercut?', 'what should I relist?', "
    "'what do I have listed?'), call get_my_auctions first and go by it: never assume what they listed or at what "
    "price, e.g. from prices you suggested earlier. Its listings are as of the player's last auction house visit; "
    "sales and expiries since come from the game's notices and the mail. So say how old it is ('as of your last "
    "visit to the auction house, 3 hours ago'), and that the next visit confirms the rest. For sold money, use its "
    "totals (sale money in the mail, and sales whose money is still coming) with get_inventory(include_mail=True) for "
    "the rest of the mailbox. Before suggesting a new price to relist at, check it with get_auction_prices.",
    order=14,
)

ONLINE_CAVEAT = (
    "Blizzard's API doesn't say who sells what, so the player's own listings were taken out of the online snapshot by "
    "price and quantity: someone else's listing at exactly the same price and size would be taken out too."
)
HOW = {"list": "the auction house's list", "notice": "the game's notice", "chat": "a chat message",
       "mail": "the mail", "cancel": "the player cancelled it"}
Verdict = Literal["cheapest", "matched", "undercut", "only seller", "unknown"]
Status = Literal["active", "may have expired", "probably expired"]
Outcome = Literal["sold", "sold, money on its way", "expired", "cancelled", "no longer listed", "outbid", "won"]
OUTCOMES: dict[str, Outcome] = {"sold": "sold", "sold-pending": "sold, money on its way", "expired": "expired",
                                "cancelled": "cancelled", "gone": "no longer listed", "outbid": "outbid", "won": "won"}


class Competition(BaseModel):
    verdict: Verdict = Field(description="cheapest: nobody else lists it as cheap; matched: someone else lists it at "
                             "the player's price; undercut: others list it cheaper; only seller: nobody else lists it; "
                             "unknown: nothing to compare (the player's has only a bid, or others' have only bids).")
    cheapest_other: int | None = Field(description="The cheapest buyout per item of anyone else's, in copper.")
    cheapest_other_text: str | None
    difference: int | None = Field(description="Copper per item between the player's price and the cheapest other: "
                                   "what they're undercut by, or how much cheaper they are.")
    difference_text: str | None
    cheaper_listed: int | None = Field(description="Undercut: how many items others list cheaper. Null: not known "
                                       "(only the cheapest price was seen).")
    cheaper_at_least: bool = Field(default=False, description="The price points seen all were cheaper, so at least "
                                   "cheaper_listed are.")
    others_listed: int | None = Field(description="How many others list in all.")
    source: Literal["online", "full scan", "search"] = Field(
        description="online: Blizzard's API snapshot, with the player's own taken out; in game (a full scan or a "
        "search), the player's own are left out.")
    seen_at: datetime
    seen_ago: str


class ListedAuction(BaseModel):
    item_id: int
    name: str | None
    count: int
    price: int | None = Field(description="The player's buyout per item, in copper. Null: only a bid.")
    price_text: str | None
    buyout: int | None = Field(description="The buyout for all of them.")
    buyout_text: str | None
    bid: int | None = Field(description="The highest bid on it so far, for all of them.")
    bid_text: str | None
    status: Status = Field(
        description="probably expired: its time ran out since the list was read, so the item would be in the mailbox; "
        "may have expired: the game only gave a range, and the earliest end has passed.")
    time_left: str = Field(description="Counted from when the list was read, e.g. 'about 19 hours', 'between 2 hours "
                           "and 12 hours', 'ran out 3 hours ago: probably expired, the item's in the mailbox'.")
    time_left_from: Literal["seconds", "band"] | None = Field(
        description="What the game gave: the seconds left (exact), or a band (under 30 minutes, 30 minutes to 2 "
        "hours, 2 to 12 hours, 12 to 48 hours), narrowed by every look. Null: it said neither.")
    ends_at: datetime | None = Field(description="When it ends; with a band, the latest it can.")
    ends_earliest: datetime | None = Field(description="With a band: the earliest it can end.")
    seen_at: datetime = Field(description="When the auction house's list last had it.")
    seen_ago: str
    listed_since: datetime | None = Field(description="When the list first had it.")
    competition: Competition | None = Field(description="Others' listings of the item, from the newer of the online "
                                            "snapshot and the game's prices. Null: no price seen yet.")


class EndedAuction(BaseModel):
    item_id: int | None
    name: str | None
    count: int | None
    price: int | None = Field(description="The player's buyout per item, in copper.")
    price_text: str | None
    outcome: Outcome = Field(description="no longer listed: gone from the list, nothing said why (sold, expired or "
                             "cancelled; the mail tells). outbid, won: the player's own bids on others' auctions.")
    known_from: str = Field(description="What said so, e.g. 'the game's notice', 'the mail', 'the auction house's "
                            "list'.")
    at: datetime | None = Field(description="When; for 'no longer listed', when the list no longer had it.")
    ago: str | None
    money_comes: str | None = Field(description="Sold, money on its way: when, if the game said, e.g. 'in 40 minutes'.")
    note: str | None = None


class Totals(BaseModel):
    listed: int = Field(description="Auctions still listed (probably expired ones left out).")
    listed_value: int = Field(description="What they'd bring at the player's buyouts, in copper, before the auction "
                              "house's cut.")
    listed_value_text: str
    sold_in_mail: int = Field(description="Money from auction house sales in the mailbox as last seen, in copper.")
    sold_in_mail_text: str
    sold_coming: int = Field(description="Sales whose money wasn't in the mailbox when it was last seen: pending sales "
                             "there, and sales the game noticed since (at the player's price, before the cut).")
    sold_coming_text: str


class MyAuctions(BaseModel):
    game: str = Field(description="The WoW version being played.")
    realm: str | None
    seen_at: datetime | None = Field(description="When the auction house last listed the player's auctions: their "
                                     "last visit. Null: not since the WowMCP addon was installed.")
    seen_ago: str | None
    at_auction_house: bool
    listed: list[ListedAuction] = Field(description="As the list had them, those ending first first.")
    ended: list[EndedAuction] = Field(description="The last 7 days, newest first: what sold, expired, was cancelled or "
                                      "went from the list, and the game's notices about the player's own bids.")
    totals: Totals
    mail_seen_at: datetime | None = Field(description="When the player last had a mailbox open. Null: not since the "
                                          "addon was installed.")
    mail_seen_ago: str | None
    note: str | None = None


@dataclass
class Others:
    """Other sellers' listings of an item."""

    source: Literal["online", "full scan", "search"]
    at: int
    tiers: list[tuple[int, int]]  # their cheapest price points, (price per item, how many), cheapest first
    cheapest: int | None  # their cheapest buyout per item; None: none with a buyout seen
    listed: int | None  # how many they list in all
    complete: bool  # the price points cover all of them with a buyout
    found_own: bool = True  # online: the player's own listings were in the snapshot


def online_others(price: online_auctions.ItemPrice, mine: list[dict]) -> Others:
    """The snapshot less the player's own listings that were up then: each taken out of the price point at its price,
    if that has as many."""
    listing = price.listing
    if listing is None:
        return Others("online", price.at, [], None, 0, True, found_own=not mine)
    tiers = dict(listing.tiers)
    complete = sum(tiers.values()) >= listing.quantity - listing.bid_only
    taken = 0
    for a in mine:
        each, count = a.get("price"), a.get("count") or 1
        if isinstance(each, int) and tiers.get(each, 0) >= count:
            tiers[each] -= count
            taken += count
    points = sorted((p, n) for p, n in tiers.items() if n > 0)
    cheapest = points[0][0] if points else None
    return Others("online", price.at, points, cheapest, listing.quantity - taken, complete, found_own=taken > 0)


def game_others(price: auction.AuctionPrice) -> Others:
    """The game's own price (the player's auctions are left out of it)."""
    tiers = [(p.price, p.quantity) for p in price.cheapest]
    complete = bool(tiers) and sum(n for _, n in tiers) >= price.listed - (price.bid_only or 0)
    return Others(price.source, int(price.seen_at.timestamp()), tiers, price.min_price, price.listed, complete)


def compete(each: int | None, others: Others) -> Competition:
    """How the player's price per item compares with others' listings."""
    seen_at, seen_ago = _when(others.at)
    cheapest, difference, cheaper, at_least = others.cheapest, None, None, False
    if each is None or (cheapest is None and others.listed):
        verdict: Verdict = "unknown"
    elif cheapest is None:
        verdict = "only seller"
    elif each < cheapest:
        verdict, difference = "cheapest", cheapest - each
    elif each == cheapest:
        verdict, difference = "matched", 0
    else:
        verdict, difference = "undercut", each - cheapest
        if others.tiers:
            cheaper = sum(n for p, n in others.tiers if p < each)
            at_least = not others.complete and all(p < each for p, _ in others.tiers)
    return Competition(verdict=verdict, cheapest_other=cheapest, cheapest_other_text=_money(cheapest),
                       difference=difference, difference_text=_money(difference), cheaper_listed=cheaper,
                       cheaper_at_least=at_least, others_listed=others.listed, source=others.source, seen_at=seen_at,
                       seen_ago=seen_ago)


def _span(seconds: float) -> str:
    return _duration(seconds) if seconds >= 60 else "under a minute"


def _time_left(raw: dict, now: float) -> tuple[Status, str]:
    """The status and time left of a listed auction, from its estimated end."""
    expires, soonest = raw.get("expires"), raw.get("soonest")
    if not isinstance(expires, int):
        return "active", "not known"
    if expires <= now:
        return "probably expired", f"ran out {_span(now - expires)} ago: probably expired, the item's in the mailbox"
    if raw.get("left") == "band" and isinstance(soonest, int):
        if soonest <= now:
            return "may have expired", f"up to {_span(expires - now)}"
        return "active", f"between {_span(soonest - now)} and {_span(expires - now)}"
    return "active", f"about {_span(expires - now)}"


def _dt(at: Any) -> datetime | None:
    return _when(at)[0] if isinstance(at, int) else None


def _listed(raw: dict, competition: Competition | None, now: float) -> ListedAuction:
    status, left = _time_left(raw, now)
    each, count = raw.get("price"), raw.get("count") or 1
    buyout = each * count if isinstance(each, int) else None
    seen_at, seen_ago = _when(raw.get("seen"))
    return ListedAuction(
        item_id=raw["item"], name=raw.get("name"), count=count, price=each, price_text=_money(each), buyout=buyout,
        buyout_text=_money(buyout), bid=raw.get("bid"), bid_text=_money(raw.get("bid")), status=status,
        time_left=left, time_left_from=raw.get("left") if raw.get("left") in ("seconds", "band") else None,
        ends_at=_dt(raw.get("expires")), ends_earliest=_dt(raw.get("soonest")), seen_at=seen_at, seen_ago=seen_ago,
        listed_since=_dt(raw.get("first")), competition=competition)


def _ended(raw: dict, now: float) -> EndedAuction:
    at = raw.get("at") if isinstance(raw.get("at"), int) else raw.get("seen")
    status, note = raw.get("status"), None
    due = raw.get("due")
    comes = None
    if status == "sold-pending" and isinstance(due, int):
        comes = f"in {_span(due - now)}" if due > now else "by now"
    if status == "gone" and isinstance(raw.get("expires"), int) and raw["expires"] <= (at or now):
        note = "Its time had run out, so it probably expired: the item would be in the mailbox."
    return EndedAuction(
        item_id=raw.get("item"), name=raw.get("name"), count=raw.get("count"), price=raw.get("price"),
        price_text=_money(raw.get("price")), outcome=OUTCOMES.get(status, "no longer listed"),
        known_from=HOW.get(raw.get("how"), "the auction house's list"), at=_dt(at),
        ago=_when(at)[1] if isinstance(at, int) else None, money_comes=comes, note=note)


def _noticed(raw: dict) -> EndedAuction:
    """A notice the game gave about an auction it hadn't listed (or about the player's bids)."""
    kind = raw.get("kind")
    return EndedAuction(item_id=None, name=raw.get("name"), count=None, price=None, price_text=None,
                        outcome=OUTCOMES.get(kind, "no longer listed"),
                        known_from=HOW.get(raw.get("src"), "the game's notice"), at=_dt(raw.get("at")),
                        ago=_when(raw["at"])[1] if isinstance(raw.get("at"), int) else None, money_comes=None,
                        note=None if kind in ("outbid", "won") else "None of the listings the game read matches it.")


def _mail_money(mail: dict | None, ended: list[dict], now: float) -> tuple[int, int]:
    """Sale money in the mail as last seen, and sales whose money wasn't there yet."""
    sales = [s for s in as_list((mail or {}).get("sales")) if isinstance(s, dict)]
    seen = (mail or {}).get("at")
    in_mail = sum(s["money"] for s in sales if s.get("kind") == "sold" and isinstance(s.get("money"), int)
                  and (s.get("expires") or now + 1) > now)
    coming = sum(s["price"] + (s.get("deposit") or 0) - (s.get("fee") or 0) for s in sales
                 if s.get("kind") == "pending" and isinstance(s.get("price"), int))
    for a in ended:
        if not isinstance(a.get("price"), int):
            continue
        noticed_since = a.get("status") == "sold" and a.get("how") != "mail" and (
            not isinstance(seen, int) or (a.get("at") or 0) > seen)
        if a.get("status") == "sold-pending" or noticed_since:
            coming += a["price"] * (a.get("count") or 1)
    return in_mail, coming


def _undercut_note(listed: list[ListedAuction]) -> str | None:
    undercut = [a for a in listed if a.competition and a.competition.verdict == "undercut"]
    if not undercut:
        return None
    return f"{len(undercut)} listing(s) undercut: " + "; ".join(
        f"{a.name or a.item_id} by {a.competition.difference_text}" for a in undercut[:5]  # type: ignore[union-attr]
    ) + ("; and more" if len(undercut) > 5 else "") + "."


@tool(annotations=ToolAnnotations(title="The player's own auctions", read_only_hint=True, open_world_hint=True))
async def get_my_auctions() -> MyAuctions:
    """Get the player's own auction house listings: each item, how many, their price per item, time left and when the
    game saw it listed, and whether others list it cheaper (cheapest, matched or undercut, by how much and how many,
    from the newer of the online snapshot and the game's own prices). Also what sold, expired or was cancelled in the
    last 7 days (the game's notices arrive anywhere, and the mail settles the rest), and totals: what the listings
    are worth, sale money in the mail, and sales whose money is still coming. The listings are as of the player's last
    auction house visit; the reply says how old. Use it for "did anything sell", "am I undercut", "what should I
    relist", "what do I have listed"."""
    # A newer snapshot is fetched meanwhile, if one is due.
    warming = asyncio.create_task(asyncio.to_thread(online_auctions.warm_up, addons_dir()))
    try:
        reply, version = await request(kinds.KNOWLEDGE)
    finally:
        await warming
    data = section(reply, "myAuctions")
    data = data if isinstance(data, dict) else {}
    now = time.time()
    auctions = [a for a in as_list(data.get("auctions")) if isinstance(a, dict) and isinstance(a.get("item"), int)]
    active = [a for a in auctions if a.get("status") == "active"]
    ended = [a for a in auctions if a.get("status") != "active"]
    places = as_list(data.get("places"))
    raw_prices = [p for p in as_list(data.get("prices")) if isinstance(p, dict) and isinstance(p.get("item"), int)]
    in_game = {p["item"]: auction._price(p, places) for p in raw_prices}
    items = list(dict.fromkeys(a["item"] for a in active))
    online = online_auctions.Prices()
    if items:
        online = await auction._online(items, data.get("realm"), raw_prices, places, False)

    used_online, not_in_snapshot, never_priced = False, [], []
    competition: dict[int, Others] = {}
    for item in items:
        snapshot = online.items.get(item) if online.house else None
        game = in_game.get(item)
        if snapshot and not (game and game.seen_at.timestamp() > snapshot.at):
            # The player's listings that were up when the snapshot was taken.
            up = [a for a in auctions if a["item"] == item
                  and (a.get("status") == "active" or (a.get("at") or 0) > snapshot.at)]
            competition[item] = online_others(snapshot, up)
            used_online = True
            if not competition[item].found_own:
                not_in_snapshot.append(item)
        elif game:
            competition[item] = game_others(game)
        else:
            never_priced.append(item)

    listed = [_listed(a, compete(a.get("price"), competition[a["item"]]) if a["item"] in competition else None, now)
              for a in active]
    gone = [_ended(a, now) for a in ended]
    gone += [_noticed(n) for n in as_list(data.get("notices")) if isinstance(n, dict)]
    gone.sort(key=lambda e: e.at.timestamp() if e.at else 0, reverse=True)

    current = [a for a in listed if a.status != "probably expired"]
    value = sum(a.buyout or 0 for a in current)
    mail = data.get("mail") if isinstance(data.get("mail"), dict) else None
    in_mail, coming = _mail_money(mail, ended, now)
    totals = Totals(listed=len(current), listed_value=value, listed_value_text=format_money(value),
                    sold_in_mail=in_mail, sold_in_mail_text=format_money(in_mail), sold_coming=coming,
                    sold_coming_text=format_money(coming))

    seen = data.get("seen") if isinstance(data.get("seen"), int) else None
    open_ = bool(data.get("open"))
    notes = []
    if seen is None:
        notes.append("The game hasn't listed this character's auctions since the WowMCP addon was installed: it reads "
                     "them when the player opens the auction house."
                     + (" Sales and expiries it noticed since are under ended." if gone else ""))
    elif open_:
        notes.append(f"Read at the auction house {_when(seen)[1]}; the player is there now.")
    else:
        notes.append(f"As of the player's last visit to the auction house, {_when(seen)[1]}: since then, only what the "
                     "game noticed (its notices arrive anywhere) or the mail showed is known; the next visit tells "
                     "the rest. Time left is estimated from what the list said then.")
    if data.get("reading"):
        notes.append("The game is reading them right now; ask again in a moment for the newest.")
    if expired := [a for a in listed if a.status == "probably expired"]:
        notes.append(f"{len(expired)} listing(s) probably expired since (their time ran out): the items would be in "
                     "the mailbox.")
    if note := _undercut_note(listed):
        notes.append(note)
    if never_priced:
        notes.append("No price seen yet for item(s) " + ", ".join(map(str, never_priced)) + ", so no undercut check: "
                     "get_auction_prices puts them on the list the game checks at the next visit.")
    if used_online:
        notes.append(ONLINE_CAVEAT)
    if not_in_snapshot:
        notes.append("The player's listing of item(s) " + ", ".join(map(str, not_in_snapshot)) + " wasn't in the "
                     "online snapshot (listed after it was taken?), so nothing was taken out of it.")
    if mail is None:
        notes.append("The mailbox hasn't been seen since the addon was installed, so sale money waiting there isn't "
                     "known.")
    elif isinstance(mail.get("at"), int):
        notes.append(f"Sale money as of the player's last mailbox visit, {_when(mail['at'])[1]}.")
    if data.get("more"):
        notes.append(f"{data['more']} more auction(s) not shown.")
    if data.get("problem"):
        notes.append(f"Reading them failed in game: {data['problem']}")
    if items:
        notes += online.notes
    mail_at = mail.get("at") if mail and isinstance(mail.get("at"), int) else None
    return MyAuctions(
        game=version, realm=data.get("realm"), seen_at=_dt(seen), seen_ago=_when(seen)[1] if seen else None,
        at_auction_house=open_, listed=listed, ended=gone, totals=totals, mail_seen_at=_dt(mail_at),
        mail_seen_ago=_when(mail_at)[1] if mail_at else None, note=" ".join(notes) or None,
    )
