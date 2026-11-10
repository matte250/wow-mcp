"""Items in text sent to the game: {item:<id>}, or {item:<id>:<name>}, shows in game as the item's icon
and the game's own name for it in its quality colour, and in chat as an item link (Core.lua's
ns.ItemText). The given name is only shown until the game has loaded the item, or if it doesn't know
the ID. Shared by the tools and the bridge."""

import re

# As Core.lua reads a token: an ID, and maybe a name without braces, bars or line breaks.
ITEM = re.compile(r"\{item:\d+(?::[^{}|\n]*)?\}")

HINT = "Write items as {item:<id>} or {item:<id>:<name>}: the game shows them with their icon."

GUIDANCE = (
    "When you mention an item in any text you send to the game (show_message, show_plan titles and steps, "
    "labels and notes of show_route, show_on_map and highlight_areas, set_reminder, set_item_notes), write it as "
    "{item:<id>}, or {item:<id>:<name>}, with the item's ID for this game version, e.g. 'Craft 5 {item:2318:Light "
    "Leather}'. The game shows it with the item's icon and its own name for it, in the item's quality colour; in "
    "chat it's an item link the player can click or shift-click. The name is shown only until the game has loaded "
    "the item, so add it when you know it. Item IDs come with the player's bags and bank (get_inventory), recipes "
    "and their reagents (get_known_recipes), vendor items (get_vendor_items), quest rewards (get_quest_log) and "
    "get_item_details; otherwise look them up for this game version. Write items you have no ID for as plain text."
)


def shorten(text: str, limit: int) -> str:
    """text in at most `limit` characters, ending in "…" if it was cut; never cut inside an item."""
    if len(text) <= limit:
        return text
    cut = limit - 1
    for match in ITEM.finditer(text):
        if match.start() >= cut:
            break
        if match.end() > cut:
            cut = match.start()
    return text[:cut] + "…"
