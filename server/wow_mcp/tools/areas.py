"""Area highlights on the map (payload key "areas", Areas.lua)."""

import re
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, field_validator

from wow_mcp.app import guidance, mismatch_error, tool, transport

guidance(
    "When the answer is a region rather than a single spot (where a mob, beast, herb, ore or fishing pool is found, "
    "a quest's kill or collection area), call highlight_areas to shade it on the player's map, in addition to "
    "answering in text. Use one circle per spawn region; radius is in percent of the zone map's width (the same "
    "scale as x), about 2-4 for a camp or cave, 5-12 for a large part of a zone. Look the spawn areas up (e.g. on "
    "Wowhead's map for that NPC or object) rather than guessing. Use show_on_map for single NPCs or objects.",
    order=16,
)

COLORS = {
    "red": "ff4d4d",
    "orange": "ff8c26",
    "yellow": "ffcc33",
    "green": "4de64d",
    "blue": "4d99ff",
    "purple": "cc66ff",
    "cyan": "4de6e6",
    "white": "ffffff",
}


class Area(BaseModel):
    zone: Annotated[
        str,
        Field(
            min_length=1,
            max_length=60,
            description="Zone name as shown in-game, e.g. 'Durotar', 'Elwynn Forest'.",
        ),
    ]
    x: Annotated[float, Field(ge=0, le=100, description="X coordinate of the centre on that zone's map, 0-100.")]
    y: Annotated[float, Field(ge=0, le=100, description="Y coordinate of the centre on that zone's map, 0-100.")]
    radius: Annotated[
        float,
        Field(
            gt=0,
            le=50,
            description="Radius in percent of the zone map's width (the x scale): 5 spans x-5 to x+5. "
            "About 2-4 for a camp, 5-12 for a large region.",
        ),
    ]
    label: Annotated[
        str, Field(min_length=1, max_length=60, description="Short text shown in the circle, e.g. 'Kolkar Centaurs'.")
    ]
    color: Annotated[
        str | None,
        Field(
            description=f"One of {', '.join(COLORS)}, or a hex colour like '#ff8800'. "
            "By default each area gets its own colour."
        ),
    ] = None
    map_id: Annotated[
        int | None,
        Field(gt=0, description="Optional uiMapID. Only set it if you're sure; otherwise the zone name is used."),
    ] = None

    @field_validator("color")
    @classmethod
    def _hex(cls, value: str | None) -> str | None:
        """As the addon wants it: 'rrggbb'."""
        if value is None:
            return None
        name = value.strip().lower()
        if name in COLORS:
            return COLORS[name]
        if re.fullmatch(r"#?[0-9a-f]{6}", name):
            return name.lstrip("#")
        raise ValueError(f"use one of {', '.join(COLORS)} or a hex colour like '#ff8800'")


@tool(
    annotations=ToolAnnotations(
        title="Highlight areas on the in-game map", read_only_hint=False, destructive_hint=False, open_world_hint=False
    )
)
def highlight_areas(
    areas: Annotated[list[Area], Field(min_length=1, max_length=30, description="The regions to shade.")],
    title: Annotated[
        str | None, Field(max_length=80, description="Heading printed in the player's chat, e.g. 'Kolkar camps'.")
    ] = None,
    open_map: Annotated[bool, Field(description="Open the world map on the first area.")] = True,
) -> str:
    """Shade regions on the player's World of Warcraft map, e.g. where a mob, herb or ore is found.

    Each area is a labelled, coloured circle on its zone map, also shown on the continent map.
    Replaces areas sent earlier; markers from show_on_map and the route stay.
    """
    payload = {
        "title": title,
        "openMap": open_map,
        "areas": [
            {
                "zone": a.zone, "x": round(a.x, 2), "y": round(a.y, 2), "radius": round(a.radius, 2),
                "label": a.label, "color": a.color, "mapID": a.map_id,
            }
            for a in areas
        ],
    }
    transport().publish(lambda state, seq: state.update(areas={"id": seq, **payload}))

    shaded = "; ".join(f"{a.label} ({a.zone} {a.x:.1f}, {a.y:.1f}, radius {a.radius:g})" for a in areas)
    result = (
        f"Highlighted {len(areas)} area(s) on the player's map: {shaded}. "
        "If a zone name doesn't match the game's map names, the addon says so in the player's chat."
    )
    if problem := mismatch_error():
        result += f" But: {problem} They'll show when the player is back in that version."
    return result


@tool(
    annotations=ToolAnnotations(
        title="Clear in-game area highlights", read_only_hint=False, destructive_hint=True, open_world_hint=False
    )
)
def clear_areas() -> str:
    """Remove the areas highlighted with highlight_areas from the player's map."""
    transport().publish(lambda state, seq: state.update(areas={"id": seq, "areas": []}))
    return "Cleared the highlighted areas."
