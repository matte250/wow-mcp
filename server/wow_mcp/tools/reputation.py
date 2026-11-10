"""Faction standings (request kind REPUTATION, Reputation.lua)."""

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool

guidance(
    "For reputation questions (standing with a faction, how far to the next standing, whether they can use a "
    "faction's vendor, quests or reputation-gated rewards), call get_reputations.",
    order=50,
)


class Faction(BaseModel):
    name: str
    faction_id: int = Field(description="The game's faction ID, as in Wowhead faction URLs.")
    group: str | None = Field(
        description="Where the Reputation window lists it, e.g. 'Alliance', 'Other > Steamwheedle Cartel' or "
        "'Inactive' (the player moved it there)."
    )
    standing: str | None = Field(description="As the game shows it, e.g. 'Friendly'.")
    standing_rank: int = Field(
        description="1 Hated, 2 Hostile, 3 Unfriendly, 4 Neutral, 5 Friendly, 6 Honored, 7 Revered, 8 Exalted."
    )
    progress: int = Field(description="Points into the current standing.")
    progress_max: int = Field(description="Points the current standing spans; the next standing starts there.")
    at_war: bool = Field(description="At war with the faction, so its members can be attacked.")


class Reputations(BaseModel):
    game: str = Field(description="The WoW version being played; look things up for this version.")
    factions: list[Faction] = Field(description="Every faction the character has met, in the game's order.")


@tool(annotations=ToolAnnotations(title="Player's reputations", read_only_hint=True, open_world_hint=False))
async def get_reputations() -> Reputations:
    """Get the player's standing with every faction they've met (e.g. Friendly 1200/6000), as in the game's
    Reputation window."""
    reply, version = await request(kinds.REPUTATION)
    return Reputations(
        game=version,
        factions=[
            Faction(
                name=f["name"],
                faction_id=f["id"],
                group=" > ".join(g for g in (f.get("header"), f.get("subheader")) if g) or None,
                standing=f.get("standing"),
                standing_rank=f["reaction"],
                progress=f["value"],
                progress_max=f["max"],
                at_war=bool(f.get("atWar")),
            )
            for f in as_list(section(reply, "factions"))
            if isinstance(f, dict)
        ],
    )
