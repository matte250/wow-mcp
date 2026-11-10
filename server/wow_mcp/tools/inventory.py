"""Bags, equipped gear with durability and stats, and the bank and mail as last seen (request kind ITEMS,
Inventory.lua)."""

import re
import time
from datetime import datetime
from typing import Annotated, Any, Literal, get_args

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool
from wow_mcp.tools.stats import format_money

guidance(
    "For questions about the player's items (do they have X and how many, bag space, what to sell to a vendor, "
    "their gear, its stats, item level and durability, whether they need repairs), call get_inventory. Pass "
    "include_bank=true when items they may have stored matter; the bank is as they last saw it, so mention "
    "when that was if it's been a while.",
    order=45,
)
guidance(
    "The player's mail: get_inventory with include_mail=true has their mailbox as they last saw it (it can only be "
    "read at a mailbox): every mail's attachments, money, COD and expiry, and totals. Items in the mail are the "
    "player's (COD items once they pay), but they have to collect them at a mailbox, so say where they are. Before "
    "suggesting the player buy or farm something, check their bags, bank and mail for it. Money waiting in the "
    "mail (auction house sales too) counts toward their money when weighing buying against farming. Mention mail "
    "that expires within a few days, especially what's deleted with its items and money then. When the game shows "
    "new mail (new_mail_from), say who it's from, e.g. 'you have new mail from the Auction House'.",
    order=46,
)

Quality = Literal["poor", "common", "uncommon", "rare", "epic", "legendary", "artifact", "heirloom"]
QUALITIES: tuple[Quality, ...] = get_args(Quality)  # by Enum.ItemQuality

SLOTS = {
    0: "ammo", 1: "head", 2: "neck", 3: "shoulder", 4: "shirt", 5: "chest", 6: "waist", 7: "legs", 8: "feet",
    9: "wrist", 10: "hands", 11: "finger 1", 12: "finger 2", 13: "trinket 1", 14: "trinket 2", 15: "back",
    16: "main hand", 17: "off hand", 18: "ranged", 19: "tabard",
}
KEYRING, BANK, ACCOUNT_BANK = -1, 6, 15  # bag IDs: the keyring, the first bank tab, the first warband bank tab

# GetItemStats names stats by global string; most read well trimmed (ITEM_MOD_STAMINA_SHORT: stamina).
STAT_NAMES = {
    "RESISTANCE0_NAME": "armor",
    "RESISTANCE1_NAME": "holy resistance",
    "RESISTANCE2_NAME": "fire resistance",
    "RESISTANCE3_NAME": "nature resistance",
    "RESISTANCE4_NAME": "frost resistance",
    "RESISTANCE5_NAME": "shadow resistance",
    "RESISTANCE6_NAME": "arcane resistance",
    "ITEM_MOD_MANA_REGENERATION_SHORT": "mana per 5 sec",
    "ITEM_MOD_HEALTH_REGEN_SHORT": "health per 5 sec",
    "ITEM_MOD_HEALTH_REGENERATION_SHORT": "health per 5 sec",
}
Stats = dict[str, int | float]
STATS_NOTE = (
    "Stats as the game reports them for this item, e.g. {'stamina': 5, 'damage per second': 7.4}. Effects the "
    "tooltip only describes in words ('Equip: ...') may be missing; get_item_details has the full tooltip."
)


def stat_name(key: str) -> str:
    if key in STAT_NAMES:
        return STAT_NAMES[key]
    if key.startswith("EMPTY_SOCKET_"):
        return key.removeprefix("EMPTY_SOCKET_").lower() + " socket"
    return re.sub(r"^ITEM_MOD_|_SHORT$", "", key).lower().replace("_", " ")


def stats(value: Any) -> Stats | None:
    """The game's stat table ({ ITEM_MOD_STAMINA_SHORT = 5, ... }) with readable names; None if it's empty."""
    if not isinstance(value, dict):
        return None
    named: Stats = {}
    for key, number in sorted(value.items()):
        if isinstance(number, (int, float)) and not isinstance(number, bool):
            name = stat_name(key)
            total = named.get(name, 0) + number
            named[name] = int(total) if float(total).is_integer() else round(total, 2)
    return named or None


class Item(BaseModel):
    slot: int = Field(description="Slot within the bag.")
    item_id: int
    name: str | None = Field(description="Null if the game hadn't loaded the item yet; look it up by ID or ask again.")
    count: int
    quality: Quality | None
    item_level: int | None = Field(description="Weapons and armor only.")
    sell_price: int | None = Field(description="What a vendor pays for one, in copper. Null if vendors won't buy it "
                                   "or the game hadn't loaded the item yet.")
    armor: int | None = Field(description="Armor value of gear in the bags (not the bank).")
    stats: Stats | None = Field(description=f"Weapons and armor in the bags (not the bank), including random "
                                f"suffixes such as 'of the Bear'. {STATS_NOTE} Null if none.")


class Bag(BaseModel):
    bag: int = Field(description="Container ID: 0 backpack, 1-4 bags, -1 keyring; 6 and up in the bank.")
    name: str | None
    slots: int
    free: int
    special: bool = Field(description="Holds only certain items (quiver, ammo pouch, soul bag, keyring...), so its "
                          "free slots aren't general bag space.")
    items: list[Item]


class EquippedItem(BaseModel):
    slot: str
    item_id: int
    name: str | None
    quality: Quality | None
    item_level: int | None
    durability: int | None = Field(description="Null for items without durability. At 0 the item is broken and "
                                   "gives no stats until repaired.")
    max_durability: int | None
    count: int | None = Field(description="Ammo only: how much is left in the bags.")
    armor: int | None
    stats: Stats | None = Field(description=f"{STATS_NOTE} Includes random suffixes. Null if none.")


class Bank(BaseModel):
    seen_at: datetime = Field(description="When the player last had the bank open (local time).")
    hours_ago: float
    free_slots: int
    bags: list[Bag] = Field(description="Bag 6 is the bank itself, 7 and up the bank bags; 15 and up the warband bank.")


class MailItem(BaseModel):
    item_id: int
    name: str | None = Field(description="Null if the game hadn't loaded the item; look it up by ID.")
    count: int
    quality: Quality | None


MailKind = Literal[
    "auction sale", "auction sale pending", "auction won", "auction expired", "auction cancelled", "auction outbid",
    "from your character", "from a player", "from a game master", "from the game",
]
AUCTION_KINDS: dict[str, MailKind] = {
    "sold": "auction sale", "pending": "auction sale pending", "won": "auction won", "expired": "auction expired",
    "cancelled": "auction cancelled", "outbid": "auction outbid",
}


class Invoice(BaseModel):
    item: str | None = Field(description="What was sold or bought, as the invoice names it.")
    count: int
    player: str | None = Field(description="Who bought it (a sale) or sold it (a purchase); null if several.")
    price: str = Field(description="What it sold or was bought for, all of it.")
    deposit: str | None = Field(description="Sales: the deposit, paid back with the sale.")
    fee: str | None = Field(description="Sales: the auction house's cut, taken off.")
    money_arrives: str | None = Field(description="A pending sale: when its money comes, in realm time.")


class Mail(BaseModel):
    sender: str | None
    subject: str | None
    kind: MailKind = Field(description="'from your character': one the addon has seen on this account.")
    money: str | None = Field(description="Money attached, e.g. '12g 5s 0c'.")
    cod: str | None = Field(description="Cash on delivery: what the player pays to take the items.")
    items: list[MailItem]
    expires_in: str = Field(description="e.g. '28 days', '20 hours'.")
    on_expiry: Literal["deleted", "returned to sender"] = Field(description="Deleted takes what's attached with it.")
    read: bool
    returned: bool = Field(description="The player sent it and it came back.")
    invoice: Invoice | None = Field(description="Auction house sales and purchases: what, to whom, for how much.")


class Mailbox(BaseModel):
    seen_at: datetime | None = Field(description="When the player last had a mailbox open (local time). Null: not "
                                     "since the WowMCP addon was installed.")
    hours_ago: float | None
    at_mailbox: bool = Field(description="The player has a mailbox open right now, so this is current.")
    mails: list[Mail] = Field(description="Newest first. Mail that has expired since it was seen is left out.")
    not_shown: int = Field(description="Mail on the server the inbox doesn't show yet (it shows at most 50); it "
                           "shows once the player takes or deletes some.")
    money_waiting: int = Field(description="Copper attached to all of it, auction house sales included.")
    money_waiting_text: str
    items: list[MailItem] = Field(description="Everything attached, by item ID with counts added up. COD mail is "
                                  "left out: the player has to pay for those items first.")
    new_mail_from: list[str] | None = Field(description="The game's new-mail notice (the minimap's envelope): mail "
                                            "has arrived since, from these senders (the game names up to three, "
                                            "maybe none). Null: no new mail.")
    note: str | None


class Inventory(BaseModel):
    game: str = Field(description="The WoW version being played; item IDs are for this version.")
    free_bag_slots: int = Field(description="Free slots in the regular (not special) bags.")
    bags: list[Bag]
    equipment: list[EquippedItem]
    bank: Bank | None = Field(description="Null unless include_bank was set, or if the player hasn't opened their "
                              "bank since the WowMCP addon was installed.")
    mail: Mailbox | None = Field(description="Null unless include_mail was set.")


def quality(value: Any) -> Quality | None:
    return QUALITIES[value] if isinstance(value, int) and 0 <= value < len(QUALITIES) else None


def _bag_name(bag: int) -> str | None:
    if bag == KEYRING:
        return "Keyring"
    if bag == BANK:
        return "Bank"
    if BANK < bag < ACCOUNT_BANK:
        return f"Bank bag {bag - BANK}"
    if bag >= ACCOUNT_BANK:
        return f"Warband bank tab {bag - ACCOUNT_BANK + 1}"
    return None


def _bags(value: Any) -> list[Bag]:
    bags = []
    for raw in as_list(value):
        items = [
            Item(slot=i["slot"], item_id=i["id"], name=i.get("name"), count=i.get("count", 1),
                 quality=quality(i.get("quality")), item_level=i.get("ilvl"), sell_price=i.get("sell"),
                 armor=i.get("armor"), stats=stats(i.get("stats")))
            for i in as_list(raw.get("items"))
        ]
        bags.append(Bag(bag=raw["bag"], name=raw.get("name") or _bag_name(raw["bag"]), slots=raw["size"],
                        free=raw["size"] - len(items), special=bool(raw.get("special")) or raw["bag"] == KEYRING,
                        items=items))
    return bags


def _equipment(value: Any) -> list[EquippedItem]:
    return [
        EquippedItem(slot=SLOTS.get(e["slot"], str(e["slot"])), item_id=e["id"], name=e.get("name"),
                     quality=quality(e.get("quality")), item_level=e.get("ilvl"), durability=e.get("durability"),
                     max_durability=e.get("maxDurability"), count=e.get("count"), armor=e.get("armor"),
                     stats=stats(e.get("stats")))
        for e in sorted(as_list(value), key=lambda e: e["slot"])
    ]


def _bank(value: Any) -> Bank | None:
    if not (isinstance(value, dict) and value.get("time")):
        return None  # false: never seen
    bags = _bags(value.get("bags"))
    return Bank(seen_at=datetime.fromtimestamp(value["time"]).astimezone(),
                hours_ago=max(0.0, round((time.time() - value["time"]) / 3600, 1)),
                free_slots=sum(b.free for b in bags), bags=bags)


EXPIRES_SOON = 3 * 86400  # seconds: mail expiring sooner is pointed out
MAX_LISTED = 5  # mails named in the note


def _left(seconds: float) -> str:
    if seconds >= 2 * 86400:
        return f"{int(seconds // 86400)} days"
    if seconds >= 2 * 3600:
        return f"{int(seconds // 3600)} hours"
    minutes = max(1, int(seconds // 60))
    return f"{minutes} minute{'s' if minutes > 1 else ''}"


def _money(copper: Any) -> str | None:
    return format_money(copper) if isinstance(copper, int) and copper > 0 else None


def _mail_kind(raw: dict) -> MailKind:
    if raw.get("ah") in AUCTION_KINDS:
        return AUCTION_KINDS[raw["ah"]]
    if raw.get("gm"):
        return "from a game master"
    if raw.get("player"):
        return "from your character" if raw.get("alt") else "from a player"
    return "from the game"


def _invoice(raw: Any) -> Invoice | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("price"), int):
        return None
    return Invoice(item=raw.get("item"), count=raw.get("count", 1), player=raw.get("player"),
                   price=format_money(raw["price"]), deposit=_money(raw.get("deposit")), fee=_money(raw.get("fee")),
                   money_arrives=raw.get("eta"))


def _mail(raw: dict, now: float) -> Mail:
    return Mail(
        sender=raw.get("from"), subject=raw.get("subject"), kind=_mail_kind(raw), money=_money(raw.get("money")),
        cod=_money(raw.get("cod")),
        items=[MailItem(item_id=i["id"], name=i.get("name"), count=i.get("count", 1), quality=quality(i.get("quality")))
               for i in as_list(raw.get("items"))],
        expires_in=_left(raw["expires"] - now), on_expiry="returned to sender" if raw.get("returns") else "deleted",
        read=bool(raw.get("read")), returned=bool(raw.get("returned")), invoice=_invoice(raw.get("sale")),
    )


def _mail_notes(mailbox: Mailbox, raw: list[dict], gone: int, now: float) -> str | None:
    """raw: the mails as the game sent them, in the order of mailbox.mails."""
    notes = []
    if mailbox.seen_at is None:
        notes.append("The game hasn't seen this character's mailbox since the WowMCP addon was installed; it can "
                     "only be read at a mailbox.")
    elif not mailbox.at_mailbox and mailbox.hours_ago >= 1:
        notes.append(f"As the player last saw it {_left(now - mailbox.seen_at.timestamp())} ago; mail may have "
                     "arrived since.")
    if gone:
        notes.append(f"{gone} mail(s) expired since then: returned to their senders or deleted.")
    if mailbox.not_shown:
        notes.append(f"{mailbox.not_shown} more mail(s) wait on the server; the inbox shows them once the player "
                     "takes or deletes some.")
    soon = [m for m, r in zip(mailbox.mails, raw) if r["expires"] - now < EXPIRES_SOON and (m.items or m.money)]
    if soon:
        more = f"; and {len(soon) - MAX_LISTED} more" if len(soon) > MAX_LISTED else ""
        notes.append(f"Expiring soon ({len(soon)}): " + "; ".join(
            f"'{m.subject or m.kind}' from {m.sender or 'unknown'} in {m.expires_in} ({m.on_expiry})"
            for m in soon[:MAX_LISTED]
        ) + more + ".")
    if cod := [r["cod"] for r in raw if isinstance(r.get("cod"), int) and r["cod"] > 0]:
        notes.append(f"{len(cod)} COD mail(s): the player pays {format_money(sum(cod))} in all to take their items.")
    if pending := [m for m in mailbox.mails if m.kind == "auction sale pending"]:
        notes.append(f"{len(pending)} auction sale(s) pending: their money comes in a later mail.")
    if mailbox.new_mail_from is not None:
        senders = f" from {', '.join(mailbox.new_mail_from)}" if mailbox.new_mail_from else ""
        notes.append(f"The game shows new mail{senders}; what arrived since the player last opened a mailbox isn't "
                     "listed yet.")
    return " ".join(notes) or None


def _mailbox(value: Any) -> Mailbox:
    """The mail section: { inbox = { time, total, mails } as last seen, open, newMail = senders }."""
    value = value if isinstance(value, dict) else {}
    inbox = value["inbox"] if isinstance(value.get("inbox"), dict) else {}
    seen, now = inbox.get("time"), time.time()
    seen_mails = as_list(inbox.get("mails"))
    raw = [m for m in seen_mails if m.get("expires", 0) > now]  # the rest are gone
    mails = [_mail(m, now) for m in raw]
    new_mail = value.get("newMail")

    totals: dict[int, MailItem] = {}
    for item in (i for m in mails if not m.cod for i in m.items):
        if item.item_id in totals:
            totals[item.item_id].count += item.count
        else:
            totals[item.item_id] = item.model_copy()
    money = sum(m["money"] for m in raw if isinstance(m.get("money"), int))

    mailbox = Mailbox(
        seen_at=datetime.fromtimestamp(seen).astimezone() if seen else None,
        hours_ago=max(0.0, round((now - seen) / 3600, 1)) if seen else None,
        at_mailbox=bool(value.get("open")),
        mails=mails,
        not_shown=max(0, inbox.get("total", 0) - len(seen_mails)),
        money_waiting=money,
        money_waiting_text=format_money(money),
        items=list(totals.values()),
        new_mail_from=[s for s in as_list(new_mail) if isinstance(s, str)] if new_mail is not None else None,
        note=None,
    )
    mailbox.note = _mail_notes(mailbox, raw, len(seen_mails) - len(raw), now)
    return mailbox


@tool(
    annotations=ToolAnnotations(title="Player's bags, gear, bank and mail", read_only_hint=True,
                                open_world_hint=False)
)
async def get_inventory(
    include_bank: Annotated[bool, Field(description="Also return the bank, as the player last saw it.")] = False,
    include_mail: Annotated[bool, Field(description="Also return the mailbox, as the player last saw it, and "
                                        "whether new mail has arrived.")] = False,
) -> Inventory:
    """Get what the player carries: every item in their bags (count, quality, vendor price; item level, armor and
    stats for gear), their equipped gear with item level, durability, armor and stats as the game reports them,
    and optionally their bank and their mail as last seen (the mail with money, COD, expiry, auction house sales
    and totals). Use it for "do I have X", "what can I vendor", "do I need repairs", "any mail / did it sell",
    bag space, and as the baseline for gear comparisons (with get_item_details for the other item)."""
    reply, version = await request(kinds.ITEMS)
    bags = _bags(section(reply, "bags"))
    return Inventory(
        game=version,
        free_bag_slots=sum(b.free for b in bags if not b.special),
        bags=bags,
        equipment=_equipment(section(reply, "equipment")),
        bank=_bank(section(reply, "bank")) if include_bank else None,
        mail=_mailbox(section(reply, "mail")) if include_mail else None,
    )
