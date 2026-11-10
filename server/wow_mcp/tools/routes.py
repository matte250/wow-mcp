"""Routes with a navigator arrow (payload key "routes", Routes.lua)."""

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp.app import guidance, mismatch_error, tool, transport

guidance(
    "When the player should visit several places in order (quest turn-ins, a round of trainers or vendors, a "
    "herb, mining or farming loop), call show_route instead of show_on_map. The stops are numbered and joined by "
    "lines on the in-game map, the game's waypoint goes on the current stop, and an on-screen navigator arrow "
    "points the way with the distance in yards, moving on to the next stop when the player gets there. Order the "
    "stops along a sensible path starting near the player (get_player_location tells you where they are), and use "
    "loop=true for circuits they'll repeat. The same coordinate rules as show_on_map apply.",
    order=15,
)


class Stop(BaseModel):
    zone: Annotated[
        str,
        Field(
            min_length=1,
            max_length=60,
            description="Zone or city name as shown in-game, e.g. 'Durotar', 'Orgrimmar', 'Elwynn Forest'.",
        ),
    ]
    x: Annotated[float, Field(ge=0, le=100, description="X coordinate on that zone's map, 0-100.")]
    y: Annotated[float, Field(ge=0, le=100, description="Y coordinate on that zone's map, 0-100.")]
    label: Annotated[
        str,
        Field(min_length=1, max_length=60, description="Short name for the stop, e.g. 'Turn in: Vile Familiars'."),
    ]
    note: Annotated[
        str | None,
        Field(max_length=300, description="Optional detail for the stop's tooltip, e.g. 'Inside the inn, upstairs'."),
    ] = None
    map_id: Annotated[
        int | None,
        Field(gt=0, description="Optional uiMapID. Only set it if you're sure; otherwise the zone name is used."),
    ] = None


@tool(
    annotations=ToolAnnotations(
        title="Show a route in game", read_only_hint=False, destructive_hint=False, open_world_hint=False
    )
)
def show_route(
    stops: Annotated[
        list[Stop],
        Field(min_length=2, max_length=40, description="The stops, in the order the player should visit them."),
    ],
    title: Annotated[
        str | None,
        Field(max_length=80, description="Name shown on the navigator and in chat, e.g. 'Razor Hill turn-ins'."),
    ] = None,
    loop: Annotated[
        bool, Field(description="Go back to the first stop after the last one, for farming or gathering circuits.")
    ] = False,
    open_map: Annotated[bool, Field(description="Open the world map on the first stop.")] = True,
) -> str:
    """Guide the player along a route of stops in World of Warcraft.

    Draws the numbered stops and the path between them on the in-game map, sets the game's waypoint
    on the first stop and shows a navigator arrow with the distance. When the player gets within
    about 12 yards of a stop, the next one becomes current. Replaces any route sent earlier;
    markers from show_on_map stay.
    """
    route = {
        "title": title,
        "loop": loop,
        "openMap": open_map,
        "stops": [
            {
                "zone": s.zone, "x": round(s.x, 2), "y": round(s.y, 2), "label": s.label, "note": s.note,
                "mapID": s.map_id,
            }
            for s in stops
        ],
    }
    transport().publish(lambda state, seq: state.update(routes={"id": seq, **route}))

    path = " -> ".join(f"{s.label} ({s.zone} {s.x:.1f}, {s.y:.1f})" for s in stops)
    result = (
        f"Sent a route with {len(stops)} stops{' (looping)' if loop else ''} to the game: {path}. "
        "The player's navigator now points to the first stop. If a zone name doesn't match the game's "
        "map names, the addon says so in the player's chat."
    )
    if problem := mismatch_error():
        result += f" But: {problem} It will show when the player is back in that version."
    return result


@tool(
    annotations=ToolAnnotations(
        title="Clear the in-game route", read_only_hint=False, destructive_hint=True, open_world_hint=False
    )
)
def clear_route() -> str:
    """Remove the route sent with show_route: its stops and lines on the map, the navigator and its waypoint."""
    transport().publish(lambda state, seq: state.update(routes={"id": seq, "stops": []}))
    return "Cleared the route and its navigator."
