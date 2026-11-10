"""The player's location, character and professions (request kind STATE, Player.lua)."""

from typing import Literal

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool

guidance(
    "When the answer depends on where the player is or who they are (the closest X, what's nearby, "
    "directions, anything faction- or level-specific), call get_player_location first. For questions about "
    "their professions (skill levels, what to train or craft next), call get_professions.",
    order=20,
)


class PlayerLocation(BaseModel):
    game: str = Field(description="The WoW version being played; look things up for this version.")
    zone: str = Field(description="Zone or city the player is in, as named on the game's map.")
    subzone: str | None = Field(description="Area within the zone, e.g. 'Kharanos'.")
    continent: str | None
    x: float | None = Field(description="0-100 on the zone map. Null where the game hides positions (instances).")
    y: float | None
    map_id: int | None = Field(description="The game's uiMapID for the zone.")
    faction: str | None
    race: str | None
    player_class: str | None
    level: int | None
    in_instance: bool


class Profession(BaseModel):
    name: str
    rank: int
    max_rank: int = Field(
        description="The cap at the player's training level (75 Apprentice, 150 Journeyman, 225 Expert, 300 Artisan); "
        "reaching it means they need to train the next rank."
    )
    kind: Literal["primary", "secondary"]


class Professions(BaseModel):
    game: str = Field(description="The WoW version being played; look things up for this version.")
    professions: list[Profession]


@tool(
    annotations=ToolAnnotations(title="Where is the player", read_only_hint=True, open_world_hint=False)
)
async def get_player_location() -> PlayerLocation:
    """Get the player's current zone, coordinates, faction, race, class and level from the running game."""
    reply, version = await request(kinds.STATE)
    location, character = section(reply, "location"), section(reply, "character")
    return PlayerLocation(
        game=version,
        zone=location.get("zone") or "unknown",
        subzone=location.get("subzone") or None,
        continent=location.get("continent") or None,
        x=location.get("x"),
        y=location.get("y"),
        map_id=location.get("mapID"),
        faction=character.get("faction"),
        race=character.get("race"),
        player_class=character.get("class"),
        level=character.get("level"),
        in_instance=bool(location.get("inInstance")),
    )


@tool(
    annotations=ToolAnnotations(title="Player's professions", read_only_hint=True, open_world_hint=False)
)
async def get_professions() -> Professions:
    """Get the player's professions and secondary skills (cooking, first aid, fishing) with their skill levels."""
    reply, version = await request(kinds.STATE)
    entries = as_list(section(reply, "professions"))
    return Professions(
        game=version,
        professions=[
            Profession(name=p["name"], rank=p["rank"], max_rank=p["maxRank"],
                       kind="primary" if p.get("primary") else "secondary")
            for p in entries
        ],
    )
