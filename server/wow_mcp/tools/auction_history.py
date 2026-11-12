"""The player's own auction house history: what they listed, what sold and for how much after the cut, what expired
or was cancelled, and what they won, kept for good. It's a building block: the AI works out profits and what's worth
flipping from it, with vendor prices (get_vendor_items) and the market now (get_auction_prices).

The game only keeps what became of the player's auctions for 7 days (Auction.lua's myAuctions, request kind KNOWLEDGE),
and the auction house mail only as the mailbox last showed it (Inventory.lua's mail, ITEMS). So every call adds what's
new in them to a ledger in the data folder (auction-history.jsonl in ~/.local/share/wow-mcp, $XDG_DATA_HOME, next to
memory.py's files), one JSON record per line, per character by name, and answers from the ledger: everything since
its first use.

A record is one observation: an auction's own record (by its auction id), an auction house mail, or the game's notice of
an auction it didn't know. One sale can show up all three ways; reading joins them into one event.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds, locking, memory
from wow_mcp.app import as_list, guidance, request, section, tool
from wow_mcp.tools.auction import _installed_version, _money, _when
from wow_mcp.tools.memory import Player, whose
from wow_mcp.tools.stats import format_money

log = logging.getLogger("wow_mcp.auction_history")

guidance(
    "For questions about the player's past auction house dealings (what sold and for how much, what keeps expiring, "
    "their income from it, how their flips went, what doesn't sell), call get_auction_history: their own sales, "
    "expiries, cancellations, wins and listings, kept from its first use on (the game itself forgets them after 7 "
    "days), with totals per item. For flip results ('how are my flips doing?', 'which flips work?'), combine it with "
    "get_vendor_items for the cost (take the cheapest vendor price for the item, and say the cost is an estimate from "
    "vendor prices: purchases aren't recorded) and get_auction_prices for the market now (the cheapest listing and "
    "how many are listed). Work out the rest yourself, per item: the profit (received after the cut, less the cost of "
    "what sold), which flips work and which don't (sold below cost, or expired and cancelled without sales), and "
    "whether each is still worth it or flooded now. A sale whose invoice the mail didn't show has only its price "
    "before the cut: take the cut the other invoices show. Show flip results as a named visual ('Flip results') whose "
    "refresh instructions rebuild it from these three tools.",
    order=14,
)

LEDGER_FILE = "auction-history.jsonl"
DAY = 86400
AH_MAIL_DAYS = 30  # auction house mail lasts this long, so its expiry says when it came (Auction.lua assumes the same)
MAIL_SLACK = 2  # seconds: the game gives a mail's expiry in days left, so each read of the inbox may be a second off
JOIN_WITHIN = DAY  # an auction's record, its mail and the game's notice of it are this close in time
MAX_EVENTS = 200

Kind = Literal["sold", "expired", "cancelled", "won", "listed"]
KINDS = get_args(Kind)
Source = Literal["auctions", "mail", "notice"]
SOURCES = ("auction", "mail", "notice")  # the field a record names its source with
ENDED: dict[str, Kind] = {"sold": "sold", "sold-pending": "sold", "expired": "expired", "cancelled": "cancelled"}
NOTICED = ("sold", "expired", "won")  # outbid isn't a deal
# A pending sale's money comes in a later mail of its own, which records it.
MAILED: dict[str, Kind] = {"sold": "sold", "won": "won", "expired": "expired", "cancelled": "cancelled"}
INVOICED = ("sold", "won")  # their mail names the item in an invoice; the others have it attached


# The ledger ----------------------------------------------------------------------------------


def _mail_key(r: dict) -> tuple:
    """What a mail says, which knows it again with its expiry. An invoice names the item; an attachment's ID would be
    gone once the player takes it."""
    return r["kind"], r.get("name") if r["kind"] in INVOICED else r.get("item"), r.get("count"), r.get("price")


def _key(r: dict) -> tuple:
    if "auction" in r:
        return "auction", r["auction"], r["kind"]
    return "notice", r["kind"], r.get("name"), r["notice"]


def fresh(known: list[dict], seen: list[dict]) -> list[dict]:
    """The records in `seen` that `known` doesn't have yet.

    An auction is known by its id, a notice by its time. Mail has no id: it's known by what it says and when it
    expires, within MAIL_SLACK. Each known mail stands for one mail now, so mails alike (stacks that sold together)
    all count."""
    keys = {_key(r) for r in known if "mail" not in r}
    mails = [r for r in known if "mail" in r]
    claimed: set[int] = set()
    new = []
    for r in seen:
        if "mail" not in r:
            if _key(r) not in keys:
                keys.add(_key(r))
                new.append(r)
            continue
        match = next((i for i, m in enumerate(mails) if i not in claimed and _mail_key(m) == _mail_key(r)
                      and abs(m["mail"] - r["mail"]) <= MAIL_SLACK), None)
        if match is None:
            mails.append(r)
            match = len(mails) - 1
            new.append(r)
        claimed.add(match)
    return new


def _same(player: Any, name: str) -> bool:
    return isinstance(player, str) and player.casefold() == name.strip().casefold()


class Ledger:
    """Every auction house event seen, in auction-history.jsonl; lines are only ever added."""

    def __init__(self, path: Path | None = None):
        self.path = path or memory.data_dir() / LEDGER_FILE

    def records(self, player: str | None = None) -> list[dict]:
        """Every record (of one character), in the file's order; lines that can't be read are left out."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        return [r for r in self._parse(raw) if player is None or _same(r["player"], player)]

    def players(self) -> list[str]:
        return list(dict.fromkeys(r["player"] for r in self.records()))

    def merge(self, player: str, seen: list[dict]) -> int:
        """Add the character's records the ledger doesn't have yet; returns how many."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+b") as f:
            locking.acquire(f)  # the bridge's agent and other sessions may merge at once
            f.seek(0)
            raw = f.read()
            known = [r for r in self._parse(raw) if _same(r["player"], player)]
            new = fresh(known, seen)
            data = b"".join(json.dumps({"player": player, **r}, ensure_ascii=False).encode("utf-8") + b"\n"
                            for r in new)
            if data:
                if raw and not raw.endswith(b"\n"):  # a write cut short: that line stays broken, the next ones whole
                    data = b"\n" + data
                f.write(data)
        return len(new)

    def _parse(self, raw: bytes) -> list[dict]:
        found = []
        for n, line in enumerate(raw.split(b"\n"), 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                if not (isinstance(r["player"], str) and r["kind"] in KINDS and isinstance(r["at"], int)
                        and any(source in r for source in SOURCES)):
                    raise ValueError("not a record")
                found.append(r)
            except (ValueError, TypeError, KeyError, AttributeError):
                log.warning("%s: line %d can't be read, skipping it", self.path, n)
        return found


# What the game shows now, as records ---------------------------------------------------------


def _time(value: Any) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def observed(auctions: dict, inbox: dict | None, now: int) -> list[dict]:
    """The records in the game's own auctions (each listing, and how it ended), its notices of auctions it didn't
    know, and the auction house mail as the mailbox last showed it."""
    seen = []
    for a in as_list(auctions.get("auctions")):
        if not (isinstance(a, dict) and isinstance(a.get("id"), int) and isinstance(a.get("item"), int)):
            continue
        count = a.get("count") or 1
        price = a["price"] * count if isinstance(a.get("price"), int) else None
        same = {"auction": a["id"], "item": a["item"], "name": a.get("name"), "count": count, "price": price}
        seen.append({"kind": "listed", **same, "at": _time(a.get("first")) or _time(a.get("seen")) or now})
        if kind := ENDED.get(a.get("status")):
            seen.append({"kind": kind, **same, "at": _time(a.get("at")) or _time(a.get("seen")) or now})
    for n in as_list(auctions.get("notices")):
        if isinstance(n, dict) and n.get("kind") in NOTICED and _time(n.get("at")) and isinstance(n.get("name"), str):
            seen.append({"kind": n["kind"], "notice": n["at"], "name": n["name"], "at": n["at"]})
    for m in as_list((inbox or {}).get("mails")):
        kind = MAILED.get(m.get("ah")) if isinstance(m, dict) else None
        if not kind or not _time(m.get("expires")):
            continue
        sale = m["sale"] if isinstance(m.get("sale"), dict) else {}
        attached = next((i for i in as_list(m.get("items")) if isinstance(i, dict)), {})
        count = sale.get("count") or attached.get("count") or 1
        record = {"kind": kind, "mail": m["expires"], "item": attached.get("id"),
                  "name": sale.get("item") or attached.get("name"), "count": count, "price": sale.get("price"),
                  "at": min(m["expires"] - AH_MAIL_DAYS * DAY, now)}
        if kind in INVOICED and not isinstance(record["price"], int):
            continue  # no invoice: nothing says what it went for
        if kind not in INVOICED and not isinstance(record["item"], int):
            continue  # its item was taken: nothing says what it was
        if kind == "sold":
            record.update(fee=sale.get("fee") or 0, deposit=sale.get("deposit") or 0)  # the game leaves out zeros
        seen.append(record)
    return seen


# Reading the ledger --------------------------------------------------------------------------


@dataclass
class Event:
    kind: Kind
    at: int
    item: int | None
    name: str | None
    count: int | None
    price: int | None
    fee: int | None = None
    deposit: int | None = None
    auction: int | None = None
    sources: list[Source] = field(default_factory=list)

    @classmethod
    def of(cls, r: dict, source: Source) -> "Event":
        return cls(r["kind"], r["at"], r.get("item"), r.get("name"), r.get("count"), r.get("price"), r.get("fee"),
                   r.get("deposit"), r.get("auction"), [source])

    def join(self, r: dict, source: Source) -> None:
        """Another record of this event."""
        self.sources.append(source)
        if source == "mail":
            # The invoice has what it went for: a bid may have won it below the buyout.
            self.price = r["price"] if r.get("price") is not None else self.price
            self.fee, self.deposit = r.get("fee"), r.get("deposit")
        elif "auctions" not in self.sources:
            self.at = r["at"]  # the notice's time is exact; a mail's is worked out from its expiry
        self.item = self.item or r.get("item")
        self.name = self.name or r.get("name")
        self.count = self.count or r.get("count")

    @property
    def received(self) -> int | None:
        if self.kind == "sold" and "mail" in self.sources and self.price is not None:
            return self.price - (self.fee or 0)
        return None


def _fits(e: Event, r: dict, source: Source) -> bool:
    if e.kind != r["kind"] or source in e.sources or abs(e.at - r["at"]) > JOIN_WITHIN:
        return False
    if e.count and r.get("count") and e.count != r["count"]:
        return False
    if e.item and r.get("item"):
        return e.item == r["item"]
    return bool(e.name and r.get("name")) and e.name.casefold() == r["name"].casefold()


def events(records: list[dict]) -> list[Event]:
    """One character's events: an auction's record, the mail about it and the game's notice of it are one. A record
    joins the event of its kind and item (and count) closest in time, one at the same price first."""
    ids: dict[str, int] = {}  # an invoice names its item: the ID is in the records that have both
    for r in records:
        if isinstance(r.get("item"), int) and isinstance(r.get("name"), str):
            ids.setdefault(r["name"].casefold(), r["item"])
    records = [{**r, "item": r.get("item") or ids.get((r.get("name") or "").casefold())} for r in records]
    found: dict[tuple, Event] = {}
    for r in records:
        if "auction" in r:
            found.setdefault((r["auction"], r["kind"]), Event.of(r, "auctions"))
    merged = list(found.values())
    joining: tuple[Source, ...] = ("mail", "notice")
    for source in joining:
        for r in sorted((r for r in records if source in r), key=lambda r: r["at"]):
            if fits := [e for e in merged if _fits(e, r, source)]:
                min(fits, key=lambda e: (e.price != r.get("price"), abs(e.at - r["at"]))).join(r, source)
            else:
                merged.append(Event.of(r, source))
    return merged


# The tool ------------------------------------------------------------------------------------


class AuctionEvent(BaseModel):
    kind: Kind = Field(description="sold, expired, cancelled: the player's auctions; won: the player bought it on the "
                       "auction house; listed: the auction house's list had the player's auction.")
    item_id: int | None = Field(description="Null: only its name is known (an invoice names the item).")
    name: str | None
    count: int | None = Field(description="How many. Null: only the game's notice said, and it doesn't say.")
    price: int | None = Field(description="In copper, for all of them. sold: what it sold for, before the auction "
                              "house's cut; won: what the player paid; listed, expired, cancelled: the player's "
                              "buyout. Null: not known (only a bid, or only a notice).")
    price_text: str | None
    price_each: int | None = Field(description="price per item, rounded.")
    received: int | None = Field(description="sold: the money after the auction house's cut, from the mail's invoice. "
                                 "Null: the invoice wasn't seen (the mail was taken before the game saw it).")
    received_text: str | None
    deposit: int | None = Field(description="sold: the deposit paid back with the sale, from the invoice.")
    at: datetime = Field(description="When it happened, as the game noticed it, or when its mail came; listed: when "
                         "the auction house's list first had it.")
    ago: str
    known_from: list[Source] = Field(description="auctions: the game's record of the player's auctions; mail: an "
                                     "auction house mail; notice: the game's notice of an auction it didn't know.")
    auction_id: int | None


class Totals(BaseModel):
    sold: int = Field(description="Items sold.")
    sold_for: int = Field(description="What they sold for, before the auction house's cut, in copper (those whose "
                          "price is known).")
    sold_for_text: str
    received: int = Field(description="The money after the cut, for the sales whose invoice the mail showed.")
    received_text: str
    invoiced: int = Field(description="How many of the sold items received covers; for the rest only sold_for is "
                          "known.")
    expired: int = Field(description="Items whose auctions expired.")
    cancelled: int = Field(description="Items whose auctions the player cancelled.")
    won: int = Field(description="Items the player bought on the auction house.")
    spent: int = Field(description="What they paid for those, in copper.")
    spent_text: str
    listed: int = Field(description="Items the player put up in the period (as the auction house's list first had "
                        "them).")
    listed_now: int | None = Field(description="Items listed now, as of the player's last auction house visit. Null: "
                                   "not known (the game didn't answer).")


class ItemTotals(Totals):
    item_id: int | None
    name: str | None


class AuctionHistory(BaseModel):
    game: str = Field(description="The WoW version being played.")
    player: str | None = Field(description="The character it's about.")
    since: datetime = Field(description="The start of the period asked for.")
    kept_since: datetime | None = Field(description="The ledger's first record of this character: the history goes "
                                        "back to there, not further. Null: nothing recorded yet.")
    totals: Totals = Field(description="Over the events in the period (all of them, also those not shown).")
    items: list[ItemTotals] = Field(description="Per item, the one with the newest event first; also items listed "
                                    "now without events in the period.")
    events: list[AuctionEvent] = Field(description=f"Newest first, at most {MAX_EVENTS}.")
    note: str | None = None


def _event(e: Event) -> AuctionEvent:
    at, ago = _when(e.at)
    each = round(e.price / e.count) if e.price is not None and e.count else None
    return AuctionEvent(kind=e.kind, item_id=e.item, name=e.name, count=e.count, price=e.price,
                        price_text=_money(e.price), price_each=each, received=e.received,
                        received_text=_money(e.received), deposit=e.deposit if e.kind == "sold" else None, at=at,
                        ago=ago, known_from=e.sources, auction_id=e.auction)


def _totals(found: list[Event], listed_now: int | None) -> dict:
    def items(kind: str) -> list[Event]:
        return [e for e in found if e.kind == kind]

    sold, won = items("sold"), items("won")
    invoiced = [e for e in sold if e.received is not None]
    sold_for, received = sum(e.price or 0 for e in sold), sum(e.received or 0 for e in invoiced)
    spent = sum(e.price or 0 for e in won)
    # A notice doesn't say how many: one at least.
    return dict(sold=sum(e.count or 1 for e in sold), sold_for=sold_for, sold_for_text=format_money(sold_for),
                received=received, received_text=format_money(received), invoiced=sum(e.count or 1 for e in invoiced),
                expired=sum(e.count or 1 for e in items("expired")),
                cancelled=sum(e.count or 1 for e in items("cancelled")), won=sum(e.count or 1 for e in won),
                spent=spent, spent_text=format_money(spent), listed=sum(e.count or 1 for e in items("listed")),
                listed_now=listed_now)


def _group(e: Event) -> int | str:
    return e.item if e.item is not None else (e.name or "").casefold()


async def _game() -> tuple[dict, dict | None, str, str | None]:
    """The player's auctions (get_my_auctions' request) and the mailbox as last seen (get_inventory's), the game
    version, and why the mail couldn't be read."""
    reply, version = await request(kinds.KNOWLEDGE)
    auctions = section(reply, "myAuctions")
    auctions = auctions if isinstance(auctions, dict) else {}
    try:
        mail = section((await request(kinds.ITEMS))[0], "mail")
    except ToolError as e:
        return auctions, None, version, str(e)
    inbox = mail.get("inbox") if isinstance(mail, dict) and isinstance(mail.get("inbox"), dict) else None
    return auctions, inbox, version, None


@tool(annotations=ToolAnnotations(title="The player's auction house history", read_only_hint=False,
                                  destructive_hint=False, open_world_hint=False))
async def get_auction_history(
    days: Annotated[int, Field(ge=1, le=3650, description="The last this many days.")] = 30,
    item_ids: Annotated[
        list[Annotated[int, Field(gt=0)]] | None, Field(max_length=100, description="Only these items.")
    ] = None,
    kinds: Annotated[
        list[Kind] | None, Field(description="Only these kinds of events: sold, expired, cancelled, won, listed.")
    ] = None,
    player: Player = None,
) -> AuctionHistory:
    """Get the player's own auction house history, newest first: what sold (how many, what for, the money after the
    auction house's cut and the deposit back, from the mail's invoices), what expired or was cancelled, what they won,
    and what they listed, with totals per item (sold and received, expired, cancelled, won, listed now). The game
    forgets these after 7 days; this keeps them from its first use on, so call it for anything about past sales,
    income or flips. It has no costs or profits: get_vendor_items and get_auction_prices have what to weigh them
    against."""
    now = int(time.time())
    trouble, mail_trouble, asking = None, None, None
    auctions: dict = {}
    inbox: dict | None = None
    try:
        auctions, inbox, version, mail_trouble = await _game()
        try:
            asking = await whose(None)  # the game's data is the logged-in character's
        except ToolError:
            pass
    except ToolError as e:
        trouble, version = e, _installed_version()
        asking = os.environ.get(memory.PLAYER_ENV, "").strip() or None
    ledger = Ledger()
    if not trouble and asking:
        try:
            ledger.merge(asking, observed(auctions, inbox, now))
        except OSError as e:
            raise ToolError(f"Couldn't record the auction history in {ledger.path}: {e}") from None
    who = (player or "").strip() or asking
    known = ledger.players()
    who = next((p for p in known if _same(p, who)), who) if who else None  # as recorded: names are typed in any case
    if not who:
        if len(known) > 1:
            raise ToolError("Can't tell which character this is about (the game didn't say); the history has "
                            f"{', '.join(known)}. Pass their name as `player`.")
        who = known[0] if known else None

    records = ledger.records(who) if who else []
    since = now - days * DAY
    found = [e for e in events(records) if e.at >= since and (not item_ids or e.item in item_ids)
             and (not kinds or e.kind in kinds)]
    found.sort(key=lambda e: e.at, reverse=True)
    current = trouble is None and asking is not None and who is not None and _same(asking, who)
    listed: dict[int | str, int] = {}
    names: dict[int | str, str | None] = {}
    if current:
        for a in as_list(auctions.get("auctions")):
            if (isinstance(a, dict) and a.get("status") == "active" and isinstance(a.get("item"), int)
                    and (_time(a.get("expires")) or now + 1) > now and (not item_ids or a["item"] in item_ids)):
                listed[a["item"]] = listed.get(a["item"], 0) + (a.get("count") or 1)
                names.setdefault(a["item"], a.get("name"))

    groups: dict[int | str, list[Event]] = {}
    for e in found:
        groups.setdefault(_group(e), []).append(e)
    if not kinds or "listed" in kinds:
        for key in listed:
            groups.setdefault(key, [])
    items = [ItemTotals(item_id=key if isinstance(key, int) else None,
                        name=next((e.name for e in group if e.name), names.get(key)),
                        **_totals(group, listed.get(key, 0) if current else None)) for key, group in groups.items()]

    notes = []
    if trouble:
        notes.append(f"The game didn't answer ({trouble}), so this is the history as recorded by earlier calls: "
                     "anything since isn't in it, and what's listed now isn't known.")
    elif not asking:
        notes.append("Couldn't tell which character is logged in, so nothing new was recorded.")
    else:
        if auctions.get("seen") is None:
            notes.append("The game hasn't read this character's auctions since the WowMCP addon was installed (it "
                         "does at the auction house): only its notices and the mail say what happened.")
        if mail_trouble:
            notes.append(f"The mail couldn't be read ({mail_trouble}): no invoices from it this time.")
        elif inbox is None:
            notes.append("The mailbox hasn't been seen since the WowMCP addon was installed, so no invoices (the "
                         "money after the cut) yet: it's read at a mailbox.")
    if who and not _same(who, asking or ""):
        notes.append(f"This is {who}'s history as recorded while they played.")
    kept = min((r["at"] for r in records), default=None)
    if kept is None:
        notes.append("Nothing recorded yet" + (f" for {who}." if who else "."))
    elif kept > since:
        notes.append(f"The history only goes back to {_when(kept)[1]}, its first record of {who}; the game itself "
                     "keeps 7 days.")
    uninvoiced = [e for e in found if e.kind == "sold" and e.received is None]
    if uninvoiced:
        notes.append(f"{len(uninvoiced)} sale(s) without an invoice seen: received leaves them out, sold_for has their "
                     "price before the auction house's cut.")
    if any(e.count is None for e in found):
        notes.append("Events known only from the game's notice don't say how many: the totals count one.")
    if len(found) > MAX_EVENTS:
        notes.append(f"{len(found) - MAX_EVENTS} older event(s) not shown (the totals have them): fewer days, "
                     "item_ids or kinds narrow it.")
    return AuctionHistory(
        game=version, player=who, since=_when(since)[0], kept_since=_when(kept)[0] if kept else None,
        totals=Totals(**_totals(found, sum(listed.values()) if current else None)), items=items,
        events=[_event(e) for e in found[:MAX_EVENTS]], note=" ".join(notes) or None,
    )
