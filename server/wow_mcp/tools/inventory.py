"""Bags, equipped gear with durability, and the bank as last seen (request kind ITEMS, Inventory.lua)."""

import time
from datetime import datetime
from typing import Annotated, Any, Literal, get_args

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool

guidance(
    "For questions about the player's items (do they have X and how many, bag space, what to sell to a vendor, "
    "their gear, its item level and durability, whether they need repairs), call get_inventory. Pass "
    "include_bank=true when items they may have stored matter; the bank is as they last saw it, so mention "
    "when that was if it's been a while.",
    order=45,
)

Quality = Literal["poor", "common", "uncommon", "rare", "epic", "legendary", "artifact", "heirloom"]
QUALITIES: tuple[Quality, ...] = get_args(Quality)  # by Enum.ItemQuality

SLOTS = {
    0: "ammo", 1: "head", 2: "neck", 3: "shoulder", 4: "shirt", 5: "chest", 6: "waist", 7: "legs", 8: "feet",
    9: "wrist", 10: "hands", 11: "finger 1", 12: "finger 2", 13: "trinket 1", 14: "trinket 2", 15: "back",
    16: "main hand", 17: "off hand", 18: "ranged", 19: "tabard",
}
KEYRING, BANK, ACCOUNT_BANK = -1, 6, 15  # bag IDs: the keyring, the first bank tab, the first warband bank tab


class Item(BaseModel):
    slot: int = Field(description="Slot within the bag.")
    item_id: int
    name: str | None = Field(description="Null if the game hadn't loaded the item yet; look it up by ID or ask again.")
    count: int
    quality: Quality | None
    item_level: int | None = Field(description="Weapons and armor only.")
    sell_price: int | None = Field(description="What a vendor pays for one, in copper. Null if vendors won't buy it "
                                   "or the game hadn't loaded the item yet.")


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


class Bank(BaseModel):
    seen_at: datetime = Field(description="When the player last had the bank open (local time).")
    hours_ago: float
    free_slots: int
    bags: list[Bag] = Field(description="Bag 6 is the bank itself, 7 and up the bank bags; 15 and up the warband bank.")


class Inventory(BaseModel):
    game: str = Field(description="The WoW version being played; item IDs are for this version.")
    free_bag_slots: int = Field(description="Free slots in the regular (not special) bags.")
    bags: list[Bag]
    equipment: list[EquippedItem]
    bank: Bank | None = Field(description="Null unless include_bank was set, or if the player hasn't opened their "
                              "bank since the WowMCP addon was installed.")


def _quality(value: Any) -> Quality | None:
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
                 quality=_quality(i.get("quality")), item_level=i.get("ilvl"), sell_price=i.get("sell"))
            for i in as_list(raw.get("items"))
        ]
        bags.append(Bag(bag=raw["bag"], name=raw.get("name") or _bag_name(raw["bag"]), slots=raw["size"],
                        free=raw["size"] - len(items), special=bool(raw.get("special")) or raw["bag"] == KEYRING,
                        items=items))
    return bags


def _equipment(value: Any) -> list[EquippedItem]:
    return [
        EquippedItem(slot=SLOTS.get(e["slot"], str(e["slot"])), item_id=e["id"], name=e.get("name"),
                     quality=_quality(e.get("quality")), item_level=e.get("ilvl"), durability=e.get("durability"),
                     max_durability=e.get("maxDurability"), count=e.get("count"))
        for e in sorted(as_list(value), key=lambda e: e["slot"])
    ]


def _bank(value: Any) -> Bank | None:
    if not (isinstance(value, dict) and value.get("time")):
        return None  # false: never seen
    bags = _bags(value.get("bags"))
    return Bank(seen_at=datetime.fromtimestamp(value["time"]).astimezone(),
                hours_ago=max(0.0, round((time.time() - value["time"]) / 3600, 1)),
                free_slots=sum(b.free for b in bags), bags=bags)


@tool(
    annotations=ToolAnnotations(title="Player's bags, gear and bank", read_only_hint=True, open_world_hint=False)
)
async def get_inventory(
    include_bank: Annotated[bool, Field(description="Also return the bank, as the player last saw it.")] = False,
) -> Inventory:
    """Get what the player carries: every item in their bags (count, quality, item level for gear, vendor price),
    their equipped gear with item level and durability, and optionally their bank as last seen. Use it for
    "do I have X", "what can I vendor", "do I need repairs", bag space and gear questions."""
    reply, version = await request(kinds.ITEMS)
    bags = _bags(section(reply, "bags"))
    return Inventory(
        game=version,
        free_bag_slots=sum(b.free for b in bags if not b.special),
        bags=bags,
        equipment=_equipment(section(reply, "equipment")),
        bank=_bank(section(reply, "bank")) if include_bank else None,
    )
