"""The AI's long-term memory: earlier /ai conversations and its notes about the player (wow_mcp/memory.py).

Both are files on this computer, not game data. The notes are per character: the one the bridge says
is asking (memory.PLAYER_ENV), or else the one logged in, which the game is asked for.
"""

import os
import time
from datetime import datetime, timedelta
from typing import Annotated

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import item_text, kinds, memory
from wow_mcp.app import guidance, request, section, tool
from wow_mcp.online_auctions import ago

GUIDANCE = (
    "You remember earlier conversations with the player, also from earlier days: search_conversations searches "
    "every one of them (questions asked in game with /ai, and the answers), back to the first. A question asked in "
    "game only comes with the last few exchanges of this conversation. So when the player refers to an earlier "
    "conversation ('remember…', 'like we said', 'yesterday', 'last time', 'our flipping plan') or the question "
    "looks like part of an ongoing project, search before you answer: by its words or items, or by date, or "
    "without a query for the most recent exchanges. Never say you can't remember or can't see an earlier "
    "conversation without having searched."
)
NOTES_GUIDANCE = (
    "Notes are what you know about the player for good, per character: remember saves one, forget_note removes "
    "one and list_notes shows them; questions asked in game come with them all, as 'What you know about <name>'. "
    "Save lasting facts and decisions, not chit-chat: their preferences (e.g. \"Doesn't want to sell their "
    "leather; keeps it for leatherworking.\"), ongoing projects with their numbers (e.g. \"Flipping Finespindle "
    "patterns: Heavy Leather Ball 18s -> ~63s; watch whether listings pile up.\"), the character's plans and goals, "
    "and things they corrected you on. Keep each note short and complete on its own. When one becomes wrong or "
    "out of date, update it (remember with replaces) or forget it."
)
guidance(GUIDANCE, order=17)
guidance(NOTES_GUIDANCE, order=18)

SHOWN = 600  # characters of each answer in search results; `exchange` gives all of one
MAX_RESULTS = 30
DAY = 86400

Player = Annotated[
    str | None,
    Field(max_length=60, description="The character's name. Leave it out for the one asking in game, or else the "
          "one logged in now."),
]


class PastExchange(BaseModel):
    id: int = Field(description="Pass it as `exchange` to read this exchange whole.")
    when: str = Field(description="Local time of the answer, and how long ago; ~ marks an estimated time.")
    player: str | None = Field(description="The character who asked.")
    question: str
    answer: str = Field(description="Your answer then; … marks a part left out.")
    answer_length: int | None = Field(default=None, description="The whole answer's length, when it's cut here.")


class PastConversations(BaseModel):
    now: str = Field(description="The local time now.")
    matches: int = Field(description="How many exchanges match in all.")
    exchanges: list[PastExchange] = Field(description="Newest first.")
    note: str | None = None


class SavedNote(BaseModel):
    id: int
    text: str
    saved: str = Field(description="The local date it was saved or last updated.")


class PlayerNotes(BaseModel):
    player: str
    notes: list[SavedNote] = Field(description="Oldest first; when there's no room, the oldest go.")
    room: str


def when(entry: memory.Entry, now: float) -> str:
    at = datetime.fromtimestamp(entry.at)
    if entry.approx:
        return f"{at:%a %Y-%m-%d} ~{at:%H:%M} (about {ago(entry.at, now)})"
    return f"{at:%a %Y-%m-%d %H:%M} ({ago(entry.at, now)})"


def excerpt(text: str, looked_for: list[str], size: int = SHOWN) -> str:
    """At most `size` characters of an answer: its start, or the part around the first word looked
    for when that's further in. "…" marks what's left out; items are never cut."""
    if len(text) <= size:
        return text
    hits = [m.start() for w in looked_for if (m := memory.word(w).search(text))]
    if not hits or min(hits) <= size // 2:
        return item_text.shorten(text, size)
    target = min(min(hits) - size // 4, len(text) - size + 1)  # some of what comes before it, and fill the room
    start = max(text.rfind(" ", 0, target), text.rfind("\n", 0, target)) + 1
    for match in item_text.ITEM.finditer(text):
        if match.start() < start < match.end():
            start = match.start()
    return "…" + item_text.shorten(text[start:], size - 1)


def shown(entry: memory.Entry, looked_for: list[str], now: float, whole: bool = False) -> PastExchange:
    answer = entry.answer if whole else excerpt(entry.answer, looked_for)
    return PastExchange(id=entry.id, when=when(entry, now), player=entry.player, question=entry.question,
                        answer=answer, answer_length=len(entry.answer) if answer != entry.answer else None)


@tool(annotations=ToolAnnotations(title="Search earlier conversations", read_only_hint=True, open_world_hint=False))
def search_conversations(
    query: Annotated[
        str | None,
        Field(max_length=200, description="Words that must all be in the question or the answer, in any case; a "
              "word also finds longer ones (flip: flipping). Items are found by name or ID. Leave it out for the "
              "most recent exchanges."),
    ] = None,
    days: Annotated[int | None, Field(ge=1, le=3650, description="Only from the last this many days.")] = None,
    date: Annotated[
        str | None, Field(pattern=r"^\d{4}-\d\d-\d\d$", description="Only from this day (YYYY-MM-DD, local time).")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=MAX_RESULTS, description="How many exchanges to return.")] = 10,
    offset: Annotated[int, Field(ge=0, description="Skip this many of the newest matches, for the next ones.")] = 0,
    exchange: Annotated[
        int | None, Field(ge=1, description="Instead of searching, read this exchange (its id) whole.")
    ] = None,
) -> PastConversations:
    """Search your earlier conversations with the player: every question they asked in game (/ai) and your
    answer, kept for good, also from earlier days. Newest first, each with its date and how long ago it was.

    Use it whenever the player refers to an earlier conversation ("remember…", "like we said", "yesterday",
    "our plan") or seems to continue a project, before answering. Long answers are cut around what was
    searched for; `exchange` gives one whole, `offset` the next matches.
    """
    now = time.time()
    archive = memory.Archive()
    stamp = f"{datetime.fromtimestamp(now):%a %Y-%m-%d %H:%M}"
    if exchange is not None:
        found = [e for e in archive.entries() if e.id == exchange]
        if not found:
            raise ToolError(f"There's no exchange {exchange}; search_conversations gives their ids.")
        return PastConversations(now=stamp, matches=1, exchanges=[shown(found[0], [], now, whole=True)])
    since = now - days * DAY if days else None
    until = None
    if date:
        try:
            day = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise ToolError(f"{date!r} isn't a date; use YYYY-MM-DD.") from None
        since = max(since or 0, day.timestamp())
        until = (day + timedelta(days=1)).timestamp()
    found = archive.search(query, since, until)
    entries = found[offset : offset + limit]
    page = [shown(e, memory.words(query), now) for e in entries]
    notes = []
    if not found:
        notes.append("No earlier conversations have been kept yet." if not archive.entries() else
                     "Nothing matches. Try fewer or other words, or a longer time.")
    elif not page:
        notes.append(f"Only {len(found)} match; the offset is past them.")
    if len(found) > offset + limit:
        notes.append(f"{len(found) - offset - limit} older match(es): offset={offset + limit} gives the next ones.")
    if any(e.answer_length for e in page):
        notes.append("Answers with answer_length are cut: exchange=<id> gives one whole.")
    if any(e.approx for e in entries):
        notes.append("Times with ~ are estimated: those exchanges came from a log without times.")
    return PastConversations(now=stamp, matches=len(found), exchanges=page, note=" ".join(notes) or None)


async def whose(player: str | None) -> str:
    """Whose notes: the character named, else the one asking in game (the bridge says), else the one logged in."""
    if player and player.strip():
        return player.strip()
    if asking := os.environ.get(memory.PLAYER_ENV, "").strip():
        return asking
    try:
        reply, _ = await request(kinds.STATE)
        name = section(reply, "character").get("name")
    except ToolError as e:
        raise ToolError(f"Can't tell which character this is about: {e} Pass their name as `player`.") from None
    if not isinstance(name, str) or not name:
        raise ToolError("The game didn't say which character is logged in. Pass their name as `player`.")
    return name


def room(notes: list[memory.Note]) -> str:
    return (f"{len(notes)} of {memory.MAX_NOTES} notes, {sum(len(n.text) for n in notes):,} of "
            f"{memory.MAX_NOTES_CHARS:,} characters")


@tool(
    annotations=ToolAnnotations(
        title="Remember something about the player", read_only_hint=False, destructive_hint=False,
        open_world_hint=False,
    )
)
async def remember(
    note: Annotated[
        str,
        Field(min_length=1, max_length=memory.MAX_NOTE, description="One lasting fact, complete on its own, in a "
              "sentence or two, e.g. \"Doesn't want to sell their leather; keeps it for leatherworking.\""),
    ],
    replaces: Annotated[
        int | None, Field(description="The id of a note this one corrects or updates; that one goes.")
    ] = None,
    player: Player = None,
) -> str:
    """Save a note about the player that you'll know in every later conversation: a preference, a decision,
    an ongoing project with its numbers, the character's plans and goals, or something they corrected you
    on. Not chit-chat. Questions they ask in game (/ai) come with all their notes; list_notes shows them.
    Each character has room for 30 notes (3,000 characters); when it's full, the oldest go."""
    name = await whose(player)
    notes = memory.Notes()
    try:
        saved, already, cut = notes.add(name, note, time.time(), replaces)
    except LookupError:
        raise ToolError(f"{name} has no note #{replaces}; list_notes shows them.") from None
    except (OSError, ValueError) as e:
        raise ToolError(f"Couldn't save the note: {e}") from None
    if already:
        result = f"Already noted for {name} as #{saved.id}; it now counts as the newest."
    elif replaces is not None:
        result = f"Note #{saved.id} for {name} updated."
    else:
        result = f"Noted for {name} as #{saved.id}."
    result += f" {room(notes.about(name))}."
    if cut:
        result += (" To make room, the oldest were removed: " + "; ".join(f"#{n.id} {n.text!r}" for n in cut)
                   + ". If one still matters, save it again, shorter or merged into another note.")
    return result


@tool(
    annotations=ToolAnnotations(
        title="Forget a note about the player", read_only_hint=False, destructive_hint=True, open_world_hint=False
    )
)
async def forget_note(
    id: Annotated[int, Field(description="The note's id, from list_notes or the prompt.")],
    player: Player = None,
) -> str:
    """Remove a note about the player that's wrong or no longer applies."""
    name = await whose(player)
    try:
        gone = memory.Notes().forget(name, id)
    except (OSError, ValueError) as e:
        raise ToolError(f"Couldn't change the notes: {e}") from None
    if not gone:
        raise ToolError(f"{name} has no note #{id}; list_notes shows them.")
    return f"Forgot note #{id} for {name}: {gone.text!r}."


@tool(annotations=ToolAnnotations(title="Notes about the player", read_only_hint=True, open_world_hint=False))
async def list_notes(player: Player = None) -> PlayerNotes:
    """List what you know about the player: your notes about this character, with their ids, oldest first.
    Questions asked in game already come with them."""
    name = await whose(player)
    notes = memory.Notes().about(name)
    return PlayerNotes(player=name, room=room(notes), notes=[
        SavedNote(id=n.id, text=n.text, saved=f"{datetime.fromtimestamp(n.at):%Y-%m-%d}") for n in notes
    ])
