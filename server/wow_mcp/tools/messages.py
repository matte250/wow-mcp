"""Text from the AI shown in game (payload key "messages", Messages.lua)."""

import time
from typing import Annotated, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from wow_mcp import game, item_text
from wow_mcp.app import addons_dir, guidance, mismatch_error, tool, transport

guidance(
    "To tell the player something inside the game, call show_message: e.g. a short answer when they asked "
    "from in game or asked to be told there, or a heads-up while they play. It shows in their chat within "
    "about a second; style 'center' or 'both' also puts it in big letters in the middle of the screen, for "
    "short alerts. Keep in-game messages to a few lines. Questions the player asked in game (with /ai, or in "
    "the game's AI chat tab) already get your final answer shown there, so don't send it again.",
    order=65,
)
guidance(item_text.GUIDANCE, order=66)  # items in any text sent to the game, this tool's and the others'

STYLES = ("chat", "center", "both")
STATUSES = ("ack", "progress")
KEEP = 10  # the payload carries only the newest; the addon shows the ids it hasn't shown yet
MAX_TEXT = 4000
MAX_SENDER = 40
MAX_STATUS = 80


def _add(message: dict, question: tuple[int, int] | None) -> int:
    """Publish a message, keeping the newest. A question's status notes go once a newer note or its answer is
    there, so they don't crowd out answers the game hasn't shown yet (e.g. while it waits out a fight)."""
    if question:
        message["session"], message["post"] = question

    def change(state: dict, seq: int) -> None:
        kept = [m for m in state.get("messages") or [] if isinstance(m, dict)]
        if question:
            kept = [m for m in kept if not (m.get("status") and (m.get("session"), m.get("post")) == tuple(question))]
        state["messages"] = [*kept[-(KEEP - 1) :], {"id": seq, **message}]

    return transport().publish(change)


def publish_message(text: str, style: str = "chat", sender: str = "Claude", reply: bool = False,
                    default_model: str | None = None, default_effort: str | None = None,
                    question: tuple[int, int] | None = None, took: float | None = None,
                    tokens: int | None = None) -> int:
    """Show text in the player's game and return the message's id (other features use this too).

    style: "chat" (printed with the sender's name), "center" (big text in the middle of the
    screen) or "both". reply: it answers what the player asked in game (/ai, the bridge), so it
    shows in the game's AI chat tab with their question. default_model (with reply): the model
    the bridge answers with when the player hasn't picked one, "" if it doesn't know, for the
    game's model picker; default_effort likewise how hard it thinks. question (with reply): the
    post the question came in, (session, id), so the game knows which one is answered. took and
    tokens (with reply): the seconds the answer took and the tokens the agent wrote for it, for a
    line under it in the AI tab. Messages sent while the game is closed show at the next login.
    """
    if style not in STYLES:
        raise ValueError(f"style must be one of {', '.join(STYLES)}, not {style!r}")
    text = text.strip()
    if not text:
        raise ValueError("the message is empty")
    text = item_text.shorten(text, MAX_TEXT)
    sender = (sender or "").strip()[:MAX_SENDER] or "Claude"
    message = {"text": text, "style": style, "from": sender, "at": int(time.time())}
    if reply:
        message["reply"] = True
        if default_model is not None:
            message["defaultModel"] = default_model
        if default_effort is not None:
            message["defaultEffort"] = default_effort
        if took is not None:
            message["took"] = round(took)
        if tokens is not None:
            message["tokens"] = int(tokens)
    return _add(message, question if reply else None)


def publish_status(status: str, text: str = "", question: tuple[int, int] | None = None,
                   quiet: float | None = None, tokens: int | None = None, rate: float | None = None) -> int:
    """Tell the game how a question the player asked in game (/ai, the bridge) is coming along, for the
    AI chat tab's status line; it's never shown as a message. Returns its id.

    status: "ack" (the question arrived) or "progress" (text: the step the agent is on, e.g. "checking
    auction prices"; "" for none). question: the post it came in, (session, id). quiet: the most
    seconds the bridge goes without telling the game anything while it works on it; longer, and the
    game says it seems to have stopped. tokens: how many the agent has written so far, if they're
    counted; rate: how many a second lately, which the game counts on with until the next note.
    """
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}, not {status!r}")
    message = {"text": " ".join(text.split())[:MAX_STATUS], "status": status, "at": int(time.time())}
    if quiet is not None:
        message["quiet"] = quiet
    if tokens is not None:
        message["tokens"] = int(tokens)
        message["rate"] = round(max(0.0, float(rate or 0)), 1)
    return _add(message, question)


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
            description="What to tell the player. Plain text; newlines start new chat lines. " + item_text.HINT,
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
