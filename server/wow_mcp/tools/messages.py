"""Text from the AI shown in game (payload key "messages", Messages.lua)."""

import time
from typing import Annotated, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from wow_mcp import game
from wow_mcp.app import addons_dir, guidance, mismatch_error, tool, transport

guidance(
    "To tell the player something inside the game, call show_message: e.g. a short answer when they asked "
    "from in game or asked to be told there, or a heads-up while they play. It shows in their chat within "
    "about a second; style 'center' or 'both' also puts it in big letters in the middle of the screen, for "
    "short alerts. Keep in-game messages to a few lines. Questions the player typed with /ai in game already "
    "get your final answer shown there, so don't send it again.",
    order=65,
)

STYLES = ("chat", "center", "both")
KEEP = 10  # the payload carries only the newest; the addon shows the ids it hasn't shown yet
MAX_TEXT = 4000
MAX_SENDER = 40


def publish_message(text: str, style: str = "chat", sender: str = "Claude") -> int:
    """Show text in the player's game and return the message's id (other features use this too).

    style: "chat" (printed with the sender's name), "center" (big text in the middle of the
    screen) or "both". Messages sent while the game is closed show at the next login.
    """
    if style not in STYLES:
        raise ValueError(f"style must be one of {', '.join(STYLES)}, not {style!r}")
    text = text.strip()
    if not text:
        raise ValueError("the message is empty")
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 1] + "…"
    sender = (sender or "").strip()[:MAX_SENDER] or "Claude"

    def change(state: dict, seq: int) -> None:
        kept = [m for m in state.get("messages") or [] if isinstance(m, dict)][-(KEEP - 1) :]
        kept.append({"id": seq, "text": text, "style": style, "from": sender, "at": int(time.time())})
        state["messages"] = kept

    return transport().publish(change)


def delivery_note() -> str:
    """Why something sent to the game won't show right away, if it won't."""
    if problem := mismatch_error():
        return f" But: {problem} It will show when the player is back in that version."
    folder = addons_dir()
    if folder and not game.situation(folder).running:
        return " The game isn't running, so it will show when the player next logs in."
    return ""


@tool(
    annotations=ToolAnnotations(
        title="Show a message in game", read_only_hint=False, destructive_hint=False, open_world_hint=False
    )
)
def show_message(
    text: Annotated[
        str,
        Field(
            min_length=1,
            max_length=2000,
            description="What to tell the player. Plain text; newlines start new chat lines.",
        ),
    ],
    style: Annotated[
        Literal["chat", "center", "both"],
        Field(
            description="'chat': in the chat window, with your name. 'center': big letters in the middle of the "
            "screen for about 10 seconds, with a raid-warning sound; for alerts of a sentence or so. 'both': "
            "on screen and in chat, so it can be read again."
        ),
    ] = "chat",
) -> str:
    """Show a message to the player inside World of Warcraft while they play.

    Use it when they asked to be told something in game, or for a short heads-up. It arrives
    within about a second, with a sound. The player can repeat recent ones with /mcp messages.
    """
    message_id = publish_message(text, style)
    where = {"chat": "in the chat window", "center": "in the middle of the screen", "both": "on screen and in chat"}
    return f"Message #{message_id} sent to the game, shown {where[style]}.{delivery_note()}"
