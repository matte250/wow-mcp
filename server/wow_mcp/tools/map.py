"""Map markers and the game's waypoint (payload key "map", Map.lua)."""

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp.app import guidance, mismatch_error, tool, transport

guidance(
    "When the player asks where something is (an NPC, trainer, vendor, flight master, quest giver, dungeon "
    "entrance, herb or mining area, etc.), find its location and call show_on_map so it's marked on their "
    "in-game map, in addition to answering in text. Markers appear in game within about a second, with no "
    "action from the player.\n\n"
    "Coordinates use the 0-100 zone-map scale that the game and Wowhead show (e.g. \"Orgrimmar 63.2, 45.1\"). "
    "Don't guess coordinates from memory when unsure: look them up (e.g. on Wowhead) first. If there are "
    "several sensible options (e.g. a trainer in each capital city), mark the ones relevant to the player's "
    "faction or current area. For \"closest/nearest\" questions, send every plausible candidate for the "
    "player's faction with nearest_first=true: the game measures real distances, sorts the markers and puts "
    "the waypoint on the nearest one.",
    order=10,
)


class Marker(BaseModel):
    zone: Annotated[
        str,
        Field(
            min_length=1,
            max_length=60,
            description="Zone or city name as shown in-game, e.g. 'Orgrimmar', 'Elwynn Forest', 'Stormwind City'.",
        ),
    ]
    x: Annotated[float, Field(ge=0, le=100, description="X coordinate on that zone's map, 0-100.")]
    y: Annotated[float, Field(ge=0, le=100, description="Y coordinate on that zone's map, 0-100.")]
    label: Annotated[
        str,
        Field(
            min_length=1,
            max_length=60,
            description="Short name shown next to the pin, e.g. 'Karolek - Leatherworking Trainer'.",
        ),
    ]
    note: Annotated[
        str | None,
        Field(
            max_length=300,
            description="Optional detail for the pin tooltip, e.g. 'Upstairs in the tannery in the Drag'.",
        ),
    ] = None
    map_id: Annotated[
        int | None,
        Field(gt=0, description="Optional uiMapID. Only set it if you're sure; otherwise the zone name is used."),
    ] = None


@tool(
    annotations=ToolAnnotations(
        title="Show on the in-game map", read_only_hint=False, destructive_hint=False, open_world_hint=False
    )
)
def show_on_map(
    markers: Annotated[
        list[Marker],
        Field(min_length=1, max_length=20, description="Places to mark. The first one gets the waypoint."),
    ],
    title: Annotated[
        str | None,
        Field(max_length=80, description="Heading printed in the player's chat, e.g. 'Leatherworking trainers'."),
    ] = None,
    open_map: Annotated[bool, Field(description="Open the world map on the first marker.")] = True,
    set_waypoint: Annotated[
        bool, Field(description="Set the game's navigation waypoint (map pin + on-screen arrow) on the first marker.")
    ] = True,
    nearest_first: Annotated[
        bool,
        Field(
            description="Let the game order the markers by distance from the player, so the waypoint and map go "
            "to the closest one. Use for 'closest/nearest' questions."
        ),
    ] = False,
) -> str:
    """Mark one or more locations on the player's World of Warcraft map.

    Replaces any markers sent earlier. Pins, the waypoint and a chat summary show up in
    game within about a second.
    """
    map_payload = {
        "title": title,
        "openMap": open_map,
        "setWaypoint": set_waypoint,
        "nearestFirst": nearest_first,
        "markers": [
            {"zone": m.zone, "x": round(m.x, 2), "y": round(m.y, 2), "label": m.label, "note": m.note, "mapID": m.map_id}
            for m in markers
        ],
    }
    transport().publish(lambda state, seq: state.update(map={"id": seq, **map_payload}))

    places = "; ".join(f"{m.label} ({m.zone} {m.x:.1f}, {m.y:.1f})" for m in markers)
    result = (
        f"Sent {len(markers)} marker(s) to the game: {places}. "
        "If a zone name doesn't match the game's map names, the addon says so in the player's chat."
    )
    if problem := mismatch_error():
        result += f" But: {problem} They'll show when the player is back in that version."
    return result


@tool(
    annotations=ToolAnnotations(
        title="Clear in-game map markers", read_only_hint=False, destructive_hint=True, open_world_hint=False
    )
)
def clear_map(
    all_waypoints: Annotated[
        bool,
        Field(
            description="Also remove the game's waypoint when the player placed it themselves. "
            "Use when they ask to remove all waypoints."
        ),
    ] = False,
) -> str:
    """Remove all markers previously sent with show_on_map, and the waypoint set for them."""
    transport().publish(lambda state, seq: state.update(map={"id": seq, "markers": [], "allWaypoints": all_waypoints}))
    return "Cleared the markers and waypoints." if all_waypoints else "Cleared the markers and their waypoint."
