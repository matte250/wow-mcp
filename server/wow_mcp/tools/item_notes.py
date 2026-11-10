"""Notes on item tooltips (payload key "itemNotes", Tooltips.lua)."""

from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp.app import guidance, mismatch_error, tool, transport

# Every update carries all notes, written into every slot file; this keeps that small.
MAX_NOTES = 100

guidance(
    "When your advice is about specific items (vendor it, keep it for a quest or recipe, an upgrade to equip, "
    "a reagent to bank), also call set_item_notes so the advice shows on those items' tooltips in game, where "
    "the player will see it when they hover the item in their bags, at a vendor or in loot. Look up the item "
    "IDs for the player's game version. Keep each note short. Notes stay until replaced or cleared, so clear "
    "ones that no longer apply.",
    order=75,
)


class ItemNote(BaseModel):
    item_id: Annotated[
        int,
        Field(gt=0, description="Item ID for this game version, e.g. 2589 for Linen Cloth (wowhead .../item=2589)."),
    ]
    text: Annotated[
        str,
        Field(min_length=1, max_length=200, description="Shown on the item's tooltip, e.g. 'Keep: 10 for First Aid'."),
    ]


def _payload(notes: list[dict], seq: int) -> dict:
    return {"id": seq, "notes": notes}


@tool(
    annotations=ToolAnnotations(
        title="Add notes to item tooltips", read_only_hint=False, destructive_hint=False, open_world_hint=False
    )
)
def set_item_notes(
    notes: Annotated[list[ItemNote], Field(min_length=1, max_length=MAX_NOTES, description="One note per item.")],
    replace: Annotated[
        bool, Field(description="Remove all earlier notes first. By default, notes are added to the earlier ones.")
    ] = False,
) -> str:
    """Show a short note on the in-game tooltip of each given item, e.g. "Vendor" or "Keep: 10 needed for
    First Aid". A new note for an item replaces its earlier one. Use it to leave advice about the player's
    items where they'll see it while playing."""
    total = dropped = 0

    def change(state: dict, seq: int) -> None:
        nonlocal total, dropped
        merged: dict[int, str] = {}
        if not replace:
            for note in (state.get("itemNotes") or {}).get("notes", []):
                merged[note["item"]] = note["text"]
        for note in notes:
            merged.pop(note.item_id, None)  # an updated note counts as the newest
            merged[note.item_id] = note.text
        items = list(merged.items())
        dropped = max(0, len(items) - MAX_NOTES)
        total = len(items) - dropped
        state["itemNotes"] = _payload([{"item": item, "text": text} for item, text in items[dropped:]], seq)

    transport().publish(change)
    result = f"Set {len(notes)} note(s); {total} item(s) now have a note on their tooltip in game."
    if dropped:
        result += f" Removed the {dropped} oldest note(s) to stay within {MAX_NOTES}."
    if problem := mismatch_error():
        result += f" But: {problem} They'll show when the player is back in that version."
    return result


@tool(
    annotations=ToolAnnotations(
        title="Clear item tooltip notes", read_only_hint=False, destructive_hint=True, open_world_hint=False
    )
)
def clear_item_notes() -> str:
    """Remove all notes previously added to item tooltips with set_item_notes."""
    transport().publish(lambda state, seq: state.update(itemNotes=_payload([], seq)))
    return "Removed all item tooltip notes."
