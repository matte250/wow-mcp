"""Money, XP, rested XP, hearthstone location and /played (request kind STATE, Stats.lua)."""

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds
from wow_mcp.app import guidance, request, section, tool

guidance(
    "For questions about the player's money, leveling (XP, rested XP, time to level, time played) or where "
    "their hearthstone is set, call get_character_stats.",
    order=40,
)


class CharacterStats(BaseModel):
    game: str = Field(description="The WoW version being played; look things up for this version.")
    money: str = Field(description="Gold, silver and copper, e.g. '12g 34s 56c'.")
    money_copper: int = Field(description="The same in copper (1g = 100s = 10000c).")
    level: int
    max_level: int | None = Field(description="The level cap in this game version.")
    xp: int | None = Field(
        description="XP into the current level. Null at the level cap or when XP gain is turned off."
    )
    xp_max: int | None = Field(description="XP the current level takes in total.")
    xp_to_level: int | None = Field(description="XP still needed for the next level.")
    rested_xp: int | None = Field(
        description="Rested bonus left: kills give double XP until it's used up. The rested marker on the XP bar "
        "sits at xp + rested_xp."
    )
    resting: bool = Field(description="In an inn or a city, where rested XP builds up.")
    xp_disabled: bool = Field(description="The player has turned off XP gain.")
    hearthstone: str | None = Field(description="Where the hearthstone is set, as the game names the place.")
    played_total_seconds: int | None = Field(
        description="/played on this character. Null for the first seconds after login or /reload, until the game "
        "has answered the addon's /played request."
    )
    played_this_level_seconds: int | None
    played: str | None = Field(description="The same, readable, e.g. '3d 4h 12m total, 2h 5m this level'.")


def format_money(copper: int) -> str:
    gold, silver, copper = copper // 10000, copper // 100 % 100, copper % 100
    if gold:
        return f"{gold}g {silver}s {copper}c"
    return f"{silver}s {copper}c" if silver else f"{copper}c"


def format_duration(seconds: int) -> str:
    days, hours, minutes = seconds // 86400, seconds // 3600 % 24, seconds // 60 % 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


@tool(annotations=ToolAnnotations(title="Character stats", read_only_hint=True, open_world_hint=False))
async def get_character_stats() -> CharacterStats:
    """Get the player's money, level, XP and rested XP, where their hearthstone is set and their /played time
    from the running game."""
    reply, version = await request(kinds.STATE)
    stats = section(reply, "stats")
    xp, xp_max = stats.get("xp"), stats.get("xpMax")
    played = stats.get("played") or {}
    total, this_level = played.get("total"), played.get("level")
    return CharacterStats(
        game=version,
        money=format_money(stats["money"]),
        money_copper=stats["money"],
        level=stats["level"],
        max_level=stats.get("maxLevel"),
        xp=xp,
        xp_max=xp_max,
        xp_to_level=xp_max - xp if xp is not None and xp_max is not None else None,
        rested_xp=stats.get("rested"),
        resting=bool(stats.get("resting")),
        xp_disabled=bool(stats.get("xpDisabled")),
        hearthstone=stats.get("hearth") or None,
        played_total_seconds=total,
        played_this_level_seconds=this_level,
        played=f"{format_duration(total)} total, {format_duration(this_level)} this level"
        if total is not None and this_level is not None else None,
    )
