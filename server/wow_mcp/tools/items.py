"""Any item's tooltip, stats and item level as the game shows them, by ID (payload key "itemLookup",
request kind ITEM_DETAILS, Inventory.lua).

Requests carry no parameters, so the item IDs go to the game first, as a published payload (one live-update
slot), and the request then asks for their details. The reply names the lookup it answers; if the game
answered before the list arrived, ask again. Items the client hasn't loaded yet come back as loading, so
ask again a moment later: the IDs are already in the game, so that costs no slot.
"""

import asyncio
import re
from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool, transport
from wow_mcp.tools.inventory import Quality, Stats, quality, stats

MAX_ITEMS = 20
LIST_RETRIES, LIST_WAIT = 4, 1.0  # the game hasn't got the item list yet (e.g. in combat)
LOAD_RETRIES, LOAD_WAIT = 3, 1.0  # items the client is still loading

guidance(
    "For item stats and gear comparisons (is X an upgrade, how much DPS or armor would it add, what does an item "
    "do), use the game's own data: get_item_details for any item by ID, also ones the player doesn't have, and the "
    "stats of their equipped gear from get_inventory. Never take item stats from websites: WoW: Forever changed "
    "many items, so Wowhead and wikis can be wrong for the player's game. Websites are still where to find an "
    "item's ID, where it drops, who sells it and which quest rewards it.",
    order=46,
)

Status = Literal["ok", "loading", "unknown", "error"]
STATUSES: tuple[Status, ...] = get_args(Status)

EQUIP_SLOTS = {
    "INVTYPE_HEAD": "head", "INVTYPE_NECK": "neck", "INVTYPE_SHOULDER": "shoulder", "INVTYPE_BODY": "shirt",
    "INVTYPE_CHEST": "chest", "INVTYPE_ROBE": "chest", "INVTYPE_WAIST": "waist", "INVTYPE_LEGS": "legs",
    "INVTYPE_FEET": "feet", "INVTYPE_WRIST": "wrist", "INVTYPE_HAND": "hands", "INVTYPE_FINGER": "finger",
    "INVTYPE_TRINKET": "trinket", "INVTYPE_CLOAK": "back", "INVTYPE_WEAPON": "one-hand", "INVTYPE_2HWEAPON": "two-hand",
    "INVTYPE_WEAPONMAINHAND": "main hand", "INVTYPE_WEAPONOFFHAND": "off hand", "INVTYPE_SHIELD": "off hand (shield)",
    "INVTYPE_HOLDABLE": "held in off hand", "INVTYPE_RANGED": "ranged", "INVTYPE_RANGEDRIGHT": "ranged",
    "INVTYPE_THROWN": "thrown", "INVTYPE_RELIC": "relic", "INVTYPE_AMMO": "ammo", "INVTYPE_TABARD": "tabard",
    "INVTYPE_BAG": "bag", "INVTYPE_QUIVER": "quiver",
}

# UI escape sequences in tooltip text: || is a literal |, |n a line break; colours, textures, atlases
# and hyperlinks are formatting.
ESCAPE = re.compile(r"\|(\||n|c[0-9a-fA-F]{8}|cn[^:|]*:|r|T[^|]*\|t|A[^|]*\|a|H[^|]*\|h|h)")


class ItemDetails(BaseModel):
    item_id: int
    status: Status = Field(
        description="ok; loading: the game was still loading the item from the server, ask again in a moment; "
        "unknown: the game doesn't know this item ID (not in this game version?); error: see error."
    )
    name: str | None
    quality: Quality | None
    item_level: int | None = Field(description="Weapons and armor only.")
    required_level: int | None
    slot: str | None = Field(description="Where it's worn, e.g. 'hands', 'one-hand', 'two-hand'. Null if it isn't.")
    type: str | None = Field(description="Item class as the game names it, e.g. 'Armor', 'Weapon'.")
    subtype: str | None = Field(description="E.g. 'Leather', 'Two-Handed Axes'.")
    armor: int | None
    stats: Stats | None = Field(
        description="Stats as the game reports them, e.g. {'agility': 6, 'damage per second': 7.4}. Effects the "
        "tooltip only describes in words ('Equip: ...') may be missing here; they're in tooltip."
    )
    tooltip: list[str] = Field(
        description="The tooltip lines as the game shows them, top to bottom (formatting removed; a right-hand "
        "column follows a tab): weapon damage and speed, 'Equip:' and 'Use:' effects, set bonuses, requirements."
    )
    shown_in_red: list[str] = Field(
        description="Tooltip lines the game shows in red: what the player doesn't meet, e.g. a level, class, "
        "skill, or an armor or weapon type they can't use."
    )
    error: str | None


class ItemDetailsList(BaseModel):
    game: str = Field(description="The WoW version being played; the details are this version's.")
    items: list[ItemDetails] = Field(description="In the order asked for.")


def _plain(text: str) -> str:
    return ESCAPE.sub(lambda m: {"|": "|", "n": "\n"}.get(m[1], ""), text).strip()


def _money(copper: int) -> str:
    parts = [(copper // 10000, "g"), (copper // 100 % 100, "s"), (copper % 100, "c")]
    return " ".join(f"{n}{unit}" for n, unit in parts if n) or "0c"


def _line(raw: dict) -> str:
    text = _plain(str(raw.get("left") or ""))
    if right := _plain(str(raw.get("right") or "")):
        text = f"{text}\t{right}"
    if isinstance(price := raw.get("price"), int) and price > 0:
        text = f"{text} {_money(price)}"
    return text.strip()


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _details(item_id: int, raw: Any) -> ItemDetails:
    if not isinstance(raw, dict):
        raw = {"status": "error", "error": "The game didn't report this item."}
    tooltip, red = [], []
    for line in as_list(raw.get("lines")):
        if isinstance(line, dict) and (text := _line(line)):
            tooltip.append(text)
            if line.get("red"):
                red.append(text)
    status = raw.get("status", "ok")
    slot = _text(raw.get("slot"))
    return ItemDetails(
        item_id=item_id,
        status=status if status in STATUSES else "error",
        name=_text(raw.get("name")),
        quality=quality(raw.get("quality")),
        item_level=raw.get("ilvl"),
        required_level=raw.get("minLevel"),
        slot=EQUIP_SLOTS.get(slot, slot.removeprefix("INVTYPE_").lower()) if slot else None,
        type=_text(raw.get("type")),
        subtype=_text(raw.get("subtype")),
        armor=raw.get("armor"),
        stats=stats(raw.get("stats")),
        tooltip=tooltip,
        shown_in_red=red,
        error=raw.get("error") or (None if status in STATUSES else f"unexpected status {status!r}"),
    )


def _publish(ids: list[int]) -> int:
    return transport().publish(lambda state, seq: state.update(itemLookup={"id": seq, "items": ids}))


def _published(ids: list[int]) -> int | None:
    """The lookup for exactly these items, if it's the latest one sent: then asking costs no slot."""
    current = transport().load_state().get("itemLookup") or {}
    return current.get("id") if current.get("items") == ids else None


async def look_up(item_ids: list[int]) -> ItemDetailsList:
    """The game's details of up to MAX_ITEMS items, without repeats; a ToolError if the game can't answer."""
    ids = list(dict.fromkeys(item_ids))
    lookup, sent = _published(ids), False
    if lookup is None:
        lookup, sent = _publish(ids), True
    list_waits = load_waits = 0
    while True:
        reply, version = await request(kinds.ITEM_DETAILS)
        details = section(reply, "itemDetails")
        details = details if isinstance(details, dict) else {}
        answered = details.get("lookup")
        if answered != lookup:
            if list_waits >= LIST_RETRIES:
                raise ToolError(
                    "The game hasn't received the list of items yet. Updates wait while the player is in combat, "
                    "so try again after the fight; if it keeps happening, call get_status."
                )
            list_waits += 1
            if not sent or (isinstance(answered, int) and answered > lookup):
                # The game never got this list, or another session's lookup replaced it.
                lookup, sent = _publish(ids), True
            await asyncio.sleep(LIST_WAIT)
            continue
        found = {i.get("id"): i for i in as_list(details.get("items")) if isinstance(i, dict)}
        if load_waits < LOAD_RETRIES and any(i.get("status") == "loading" for i in found.values()):
            load_waits += 1
            await asyncio.sleep(LOAD_WAIT)
            continue
        return ItemDetailsList(game=version, items=[_details(i, found.get(i)) for i in ids])


@tool(annotations=ToolAnnotations(title="Item details from the game", read_only_hint=True, open_world_hint=False))
async def get_item_details(
    item_ids: Annotated[
        list[Annotated[int, Field(gt=0)]],
        Field(min_length=1, max_length=MAX_ITEMS,
              description="Item IDs for this game version, e.g. 4253 (from wowhead.com/forever/item=4253)."),
    ],
) -> ItemDetailsList:
    """Get items' details from the game itself, for any item ID, also ones the player doesn't have: the tooltip
    lines as the game shows them, stats, armor, item level, quality, slot, required level, and which lines the
    game shows in red (requirements the player doesn't meet). Use it for gear comparisons and "what does this
    item do" instead of website data, which can be wrong for this game version. An ID gives the plain item: for
    a random-suffix item ("of the Bear") the player has, use the stats from get_inventory."""
    return await look_up(item_ids)
