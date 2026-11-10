"""Known flight paths (request kind TRAVEL, Travel.lua)."""

from datetime import datetime, timezone
from typing import Literal

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool

guidance(
    "For travel questions (the fastest way somewhere, where to fly, whether the player has a flight path), call "
    "get_flight_paths together with get_player_location. The player can only fly to flight masters they've "
    "discovered; an undiscovered one on the way is worth a stop.",
    order=55,
)


class FlightPath(BaseModel):
    name: str = Field(description="As the game names it, usually 'Place, Zone'.")
    zone: str | None = Field(description="Zone or city the flight master is in, as named on the game's map.")
    x: float | None = Field(description="0-100 on that zone's map.")
    y: float | None
    faction: Literal["Alliance", "Horde", "Neutral"] | None = Field(
        description="Who can use it. Null for paths only known from a flight map."
    )


class FlightMapSeen(BaseModel):
    continent: str | None
    flight_master: str | None = Field(description="The flight path where the player opened the flight map.")
    seen_at: datetime
    unreachable: list[str] = Field(
        description="Known flight paths with no route from there at the time: a flight master in between is "
        "still undiscovered."
    )


class FlightPaths(BaseModel):
    game: str = Field(description="The WoW version being played; look things up for this version.")
    known: list[FlightPath] = Field(description="Flight masters this character has discovered and can fly to.")
    not_yet_discovered: list[FlightPath] = Field(
        description="Flight masters for the player's faction (or neutral) they haven't talked to yet. Talking to "
        "one discovers it."
    )
    flight_masters: list[FlightPath] = Field(
        description="Every flight master for the player's faction (or neutral), discovered or not. Given when "
        "the game's world map doesn't say which ones the character has discovered (as on Forever); then "
        "`known` only has the ones seen on a flight master's map."
    )
    flight_maps_seen: list[FlightMapSeen] = Field(
        description="The flight map last opened at a flight master on each continent, newest first."
    )
    note: str | None = None


def _path(raw: dict) -> FlightPath:
    return FlightPath(name=raw.get("name") or "unknown", zone=raw.get("zone"), x=raw.get("x"), y=raw.get("y"),
                      faction=raw.get("faction"))


def _real(raw: dict) -> bool:
    """Not one of the client's leftover "zzOLD..." flight points."""
    return bool(raw.get("name")) and not str(raw["name"]).startswith("zzOLD")


def _seen_at(raw: dict) -> datetime:
    return datetime.fromtimestamp(raw.get("at") or 0, timezone.utc)


@tool(annotations=ToolAnnotations(title="Known flight paths", read_only_hint=True, open_world_hint=False))
async def get_flight_paths() -> FlightPaths:
    """Get the flight paths (flight masters) the player's character has discovered, with zone and coordinates,
    plus those of their faction still to discover. Use it to plan travel: which flight master to fly to, and
    which ones to pick up on the way."""
    reply, version = await request(kinds.TRAVEL)
    data = section(reply, "flightPaths")
    known = [_path(p) for p in as_list(data.get("known")) if _real(p)]
    undiscovered = [_path(p) for p in as_list(data.get("undiscovered")) if _real(p)]
    masters = [_path(p) for p in as_list(data.get("flightMasters")) if _real(p)]
    # Without discovery on the world map (Forever), the flight map is the source: there it lists
    # every flight master of the continent and shows the undiscovered ones as unreachable. Its
    # positions are on the continent map, so take zone and coordinates from the world map's list.
    no_world_discovery = "flightMasters" in data
    by_name = {p.name: p for p in masters}
    # Otherwise the world map's data is current; flight maps only add paths it didn't mention.
    listed = {p.name for p in known + undiscovered}
    maps = []
    for seen in as_list(data.get("flightMaps")):
        nodes = [n for n in as_list(seen.get("nodes")) if _real(n)]
        for node in nodes:
            if node["name"] not in listed:
                listed.add(node["name"])
                path = by_name.get(node["name"]) or _path(node)
                if no_world_discovery and node.get("state") == "unreachable":
                    undiscovered.append(path)
                else:
                    known.append(path)
        maps.append(FlightMapSeen(
            continent=seen.get("map"), flight_master=seen.get("from"), seen_at=_seen_at(seen),
            unreachable=[n["name"] for n in nodes if n.get("state") == "unreachable"],
        ))
    notes = []
    if "flightMasters" in data and not maps:
        notes.append(
            "This game's world map doesn't say which flight paths the character has discovered, and no flight "
            "master's map has been seen yet, so `known` is empty and `flight_masters` lists them all. Ask the "
            "player to talk to any flight master once on each continent; their flight map records what's known."
        )
    if no_world_discovery and maps:
        seen_on = ", ".join(sorted({m.continent for m in maps if m.continent})) or "one continent"
        notes.append(
            f"What's discovered is only known for the continents whose flight map has been seen ({seen_on}). "
            "Elsewhere `flight_masters` lists them all; the player has to talk to a flight master there once."
        )
    if problem := data.get("worldMapProblem"):
        notes.append(
            f"The game's world map data wasn't readable ({problem}), so the known paths only come from the "
            "flight maps seen; paths discovered since are missing."
        )
    if problem := data.get("flightMapProblem"):
        notes.append(f"The last flight map couldn't be cached: {problem}")
    return FlightPaths(game=version, known=known, not_yet_discovered=undiscovered, flight_masters=masters,
                       flight_maps_seen=maps, note=" ".join(notes) or None)
