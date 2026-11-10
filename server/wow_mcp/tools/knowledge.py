"""Recipes known and vendors seen (request kind KNOWLEDGE, Knowledge.lua)."""

from datetime import datetime, timezone
from typing import Annotated, Literal

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool

guidance(
    "For crafting questions (what the player can make, what a recipe needs, which recipes still give skill-ups), "
    "call get_known_recipes. To find where the player can buy something, or what a vendor they've visited sells "
    "and for how much, call get_vendor_items. Both are what the game showed when the player last opened that "
    "window, so say when that was if it matters.",
    order=60,
)


class Reagent(BaseModel):
    name: str | None = Field(description="Null if the game hadn't loaded the item; look it up by item_id.")
    item_id: int
    count: int


class Recipe(BaseModel):
    name: str
    recipe_id: int = Field(description="The recipe's spell ID.")
    color: Literal["orange", "yellow", "green", "gray"] | None = Field(
        description="Skill-up chance when last seen: orange always, yellow usually, green rarely, gray never."
    )
    reagents: list[Reagent]


class ProfessionRecipes(BaseModel):
    profession: str
    rank: int | None = Field(description="Skill level when last seen.")
    max_rank: int | None
    seen_at: datetime = Field(
        description="When the player last had this profession's window open; recipes learned since are missing."
    )
    recipes: list[Recipe]


class KnownRecipes(BaseModel):
    game: str = Field(description="The WoW version being played; look things up for this version.")
    professions: list[ProfessionRecipes]
    note: str | None = None


class VendorItem(BaseModel):
    name: str | None = Field(description="Null if the game hadn't loaded the item; look it up by item_id.")
    item_id: int | None
    price: int = Field(description="In copper (100 copper = 1 silver, 10000 = 1 gold), for one purchase of `stack`.")
    stack: int = Field(description="How many one purchase gives.")
    stock: int | None = Field(description="Limited supply left when seen (0: sold out). Null: unlimited.")
    other_cost: str | None = Field(description="What it costs besides money, e.g. '2 Honor Token'.")
    can_buy: bool = Field(description="False if the vendor wouldn't sell it to the player then (e.g. reputation).")


class Vendor(BaseModel):
    name: str
    npc_id: int | None
    zone: str | None = Field(description="As named on the game's map.")
    subzone: str | None
    x: float | None = Field(description="Where the player stood at the vendor, 0-100 on the zone map.")
    y: float | None
    map_id: int | None = Field(description="The game's uiMapID for the zone.")
    seen_at: datetime = Field(description="When the player last had the vendor's window open.")
    items: list[VendorItem]


class VendorItems(BaseModel):
    game: str = Field(description="The WoW version being played; look things up for this version.")
    vendors: list[Vendor] = Field(description="Newest first.")
    note: str | None = None


def _seen_at(raw: dict) -> datetime:
    return datetime.fromtimestamp(raw.get("at") or 0, timezone.utc)


def _matches(text: str | None, query: str) -> bool:
    return query.casefold() in (text or "").casefold()


def _profession(raw: dict) -> ProfessionRecipes:
    return ProfessionRecipes(
        profession=raw.get("name") or "unknown",
        rank=raw.get("rank"),
        max_rank=raw.get("maxRank"),
        seen_at=_seen_at(raw),
        recipes=[
            Recipe(
                name=r.get("name") or "unknown",
                recipe_id=r["id"],
                color=r.get("color"),
                reagents=[Reagent(name=g.get("name"), item_id=g["item"], count=g.get("count") or 1)
                          for g in as_list(r.get("reagents"))],
            )
            for r in as_list(raw.get("recipes"))
        ],
    )


def _vendor(raw: dict) -> Vendor:
    return Vendor(
        name=raw.get("name") or "unknown",
        npc_id=raw.get("npc"),
        zone=raw.get("zone"),
        subzone=raw.get("subzone"),
        x=raw.get("x"),
        y=raw.get("y"),
        map_id=raw.get("mapID"),
        seen_at=_seen_at(raw),
        items=[
            VendorItem(name=i.get("name"), item_id=i.get("id"), price=i.get("price") or 0, stack=i.get("stack") or 1,
                       stock=i.get("stock"), other_cost=i.get("cost"), can_buy=not i.get("locked"))
            for i in as_list(raw.get("items"))
        ],
    )


@tool(annotations=ToolAnnotations(title="Known recipes", read_only_hint=True, open_world_hint=False))
async def get_known_recipes(
    profession: Annotated[
        str | None, Field(description="A profession, e.g. 'Leatherworking'. Leave out for all of them.")
    ] = None,
) -> KnownRecipes:
    """Get the recipes the player's character knows, per profession, with reagents and skill-up colour, as the
    game listed them when the player last opened that profession's window."""
    reply, version = await request(kinds.KNOWLEDGE)
    data = section(reply, "recipes")
    cached = [_profession(p) for p in as_list(data.get("professions"))]
    chosen = [p for p in cached if not profession or _matches(p.profession, profession)]
    notes = []
    if not chosen:
        have = ", ".join(p.profession for p in cached) or "none"
        notes.append(
            f"No recipes cached for {profession or 'any profession'} on this character (cached: {have}). The game "
            "only lists recipes while a profession window is open: ask the player to open it once; it's "
            "remembered after that."
        )
    if problem := data.get("problem"):
        notes.append(f"Reading the last profession window failed: {problem}")
    return KnownRecipes(game=version, professions=chosen, note=" ".join(notes) or None)


@tool(annotations=ToolAnnotations(title="Vendors seen", read_only_hint=True, open_world_hint=False))
async def get_vendor_items(
    vendor: Annotated[
        str | None,
        Field(description="Part of a vendor's name, or a zone or subzone to list the vendors seen there. Leave out "
                          "for all of them."),
    ] = None,
) -> VendorItems:
    """Get what the vendors the player has visited sell, with prices and where each vendor stands, as seen
    when the player last opened their window. Use it to find where to buy something nearby, or what it costs."""
    reply, version = await request(kinds.KNOWLEDGE)
    data = section(reply, "vendors")
    vendors = [_vendor(v) for v in as_list(data.get("vendors"))]
    if vendor:
        vendors = [v for v in vendors if any(_matches(text, vendor) for text in (v.name, v.zone, v.subzone))]
    notes = []
    if not vendors:
        notes.append(
            f"No vendor{f' matching {vendor!r}' if vendor else ''} seen yet. Vendors are remembered when the "
            "player opens their window (the most recent ones are kept)."
        )
    if problem := data.get("problem"):
        notes.append(f"Reading the last vendor failed: {problem}")
    return VendorItems(game=version, vendors=vendors, note=" ".join(notes) or None)
