"""Glow in the game's own windows (payload key "glow", Glow.lua): items wherever they show up (bags, bank, a
vendor's items, loot, quest rewards, the character's gear, action bars), spells on the action bars, and named
UI frames get a glow like the game's proc glow until replaced or cleared."""

import re
from typing import Annotated, Literal

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, model_validator

from wow_mcp.app import guidance, mismatch_error, tool, transport

# Every update carries all marks, written into every slot file; this keeps that small.
MAX_MARKS = 50
FRAME_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

guidance(
    "To point the player at something in the game's own windows, make it glow with highlight_in_ui: the items to "
    "buy at a vendor, a gear upgrade in their bags or among quest rewards, what to sell or keep, the loot worth "
    "taking, the spell to train or use (any rank of it glows at the trainer, in the spellbook and on action bars). "
    "Mark what you recommend when you recommend it, even if the player isn't at the vendor or trainer yet: it glows "
    "when they get there. Use green for buy/equip/keep, red for sell/destroy/avoid, gold for 'look here'. It "
    "adds to what already glows; pass replace when the earlier marks no longer apply, and call "
    "clear_ui_highlights when they're done. Combine it with set_item_notes when the reason matters.",
    order=76,
)

Color = Literal["gold", "green", "red", "blue", "purple"]


class Highlight(BaseModel):
    item_id: Annotated[
        int | None,
        Field(gt=0, description="An item for this game version: glows in bags, bank, at vendors, in loot, quest "
                                "rewards, the character's gear and on action bars."),
    ] = None
    spell_id: Annotated[
        int | None, Field(gt=0, description="A spell: any rank of it glows in a trainer's list, the spellbook and on action bars."),
    ] = None
    frame: Annotated[
        str | None,
        Field(max_length=100, description="A frame of the game's UI by its global name, optionally with keys after "
                                          "dots, e.g. 'MerchantRepairAllButton' or 'CharacterMicroButton'. Glows "
                                          "while it's shown."),
    ] = None
    color: Annotated[Color, Field(description="green: buy, equip or keep. red: sell or avoid. gold: look here.")] = (
        "gold"
    )

    @model_validator(mode="after")
    def one_target(self) -> "Highlight":
        if sum(x is not None for x in (self.item_id, self.spell_id, self.frame)) != 1:
            raise ValueError("give exactly one of item_id, spell_id or frame")
        if self.frame is not None and not FRAME_PATH.match(self.frame):
            raise ValueError("frame must be a global frame name, optionally with .keys")
        return self

    def key(self) -> str:
        if self.item_id is not None:
            return f"item:{self.item_id}"
        if self.spell_id is not None:
            return f"spell:{self.spell_id}"
        return f"frame:{self.frame}"

    def encode(self) -> dict:
        if self.item_id is not None:
            return {"item": self.item_id, "color": self.color}
        if self.spell_id is not None:
            return {"spell": self.spell_id, "color": self.color}
        return {"frame": self.frame, "color": self.color}


def _key(mark: dict) -> str:
    for kind in ("item", "spell", "frame"):
        if kind in mark:
            return f"{kind}:{mark[kind]}"
    return ""


@tool(
    annotations=ToolAnnotations(
        title="Make things glow in the game's windows", read_only_hint=False, destructive_hint=False,
        open_world_hint=False,
    )
)
def highlight_in_ui(
    highlights: Annotated[
        list[Highlight], Field(min_length=1, max_length=MAX_MARKS, description="What to make glow.")
    ],
    replace: Annotated[
        bool, Field(description="Stop the earlier glow first. By default, these are added to what already glows.")
    ] = False,
) -> str:
    """Make items, spells or UI frames glow in the game's own windows, like the game's proc glow: an item glows
    wherever it shows up (bags, bank, a vendor's items, loot, quest rewards, the character frame, a trainer's
    recipes, action bars; BetterBags' bags too), a spell (any rank) at trainers, in the spellbook and on action
    bars, a frame by its global name. It keeps
    glowing until replaced or cleared. Only the game's own windows (and BetterBags) are covered, not other
    addons' windows."""
    total = dropped = 0

    def change(state: dict, seq: int) -> None:
        nonlocal total, dropped
        merged: dict[str, dict] = {}
        if not replace:
            for mark in (state.get("glow") or {}).get("marks", []):
                merged[_key(mark)] = mark
        for highlight in highlights:
            merged.pop(highlight.key(), None)  # a changed mark counts as the newest
            merged[highlight.key()] = highlight.encode()
        marks = list(merged.values())
        dropped = max(0, len(marks) - MAX_MARKS)
        total = len(marks) - dropped
        state["glow"] = {"id": seq, "marks": marks[dropped:]}

    transport().publish(change)
    result = f"Marked {len(highlights)} thing(s); {total} now glow in game wherever they show up."
    if dropped:
        result += f" Stopped the {dropped} oldest to stay within {MAX_MARKS}."
    if problem := mismatch_error():
        result += f" But: {problem} They'll glow when the player is back in that version."
    return result


@tool(
    annotations=ToolAnnotations(
        title="Clear the glow in the game's windows", read_only_hint=False, destructive_hint=True,
        open_world_hint=False,
    )
)
def clear_ui_highlights() -> str:
    """Stop everything highlight_in_ui made glow."""
    transport().publish(lambda state, seq: state.update(glow={"id": seq, "marks": []}))
    return "Nothing glows in game any more."
