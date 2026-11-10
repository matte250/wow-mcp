"""Reminders triggered by time or place (payload key "reminders", Reminders.lua).

The server keeps the list in its state; the addon fires each one once and remembers that
itself, so firing costs no live-update slot. The game reports which have fired (section
"reminders" of request kind STATE).
"""

import re
import time
from datetime import datetime, timedelta
from typing import Annotated, Literal

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool, transport
from wow_mcp.tools.messages import delivery_note

guidance(
    "When the player asks to be reminded of something, call set_reminder: after some minutes or at a time "
    "(in_minutes / at_time), or when they reach a place (zone and/or subzone as named in game, or near a "
    "spot given by coordinates, e.g. a quest giver). It fires once in game, on screen and in chat with an "
    "alarm sound, and survives /reloads and restarts. list_reminders shows them and whether they've fired; "
    "cancel_reminder removes one.",
    order=70,
)

MAX_REMINDERS = 25
STALE = 24 * 3600  # time reminders this overdue are dropped at the next change; they'd be no use now
MAX_DAYS = 30


class Near(BaseModel):
    zone: Annotated[str, Field(min_length=1, max_length=60, description="Zone or city name as shown in game.")]
    x: Annotated[float, Field(ge=0, le=100, description="X on that zone's map, 0-100 (as on Wowhead).")]
    y: Annotated[float, Field(ge=0, le=100, description="Y on that zone's map, 0-100.")]
    yards: Annotated[float, Field(ge=5, le=500, description="How close the player has to get.")] = 30


def parse_time(text: str, now: datetime) -> datetime:
    """'HH:MM' (the next time it comes round) or an ISO date and time, in local time."""
    text = text.strip()
    if match := re.fullmatch(r"(\d{1,2})[:.](\d{2})", text):
        hour, minute = int(match[1]), int(match[2])
        if hour > 23 or minute > 59:
            raise ToolError(f"{text!r} isn't a time of day.")
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return due if due > now else due + timedelta(days=1)
    try:
        due = datetime.fromisoformat(text)
    except ValueError:
        raise ToolError(f"Can't read at_time {text!r}; use 'HH:MM' or 'YYYY-MM-DD HH:MM'.") from None
    return due.astimezone() if due.tzinfo else due


def clock(at: int) -> str:
    due = datetime.fromtimestamp(at)
    return due.strftime("%H:%M" if due.date() == datetime.now().date() else "%Y-%m-%d %H:%M")


def describe(r: dict) -> str:
    if r.get("at"):
        return f"at {clock(r['at'])} local time"
    if near := r.get("near"):
        return f"within {near['yards']:g} yards of {near['zone']} {near['x']:.1f}, {near['y']:.1f}"
    place = ", ".join(p for p in (r.get("subzone"), r.get("zone")) if p)
    return f"when the player is in {place}"


def current(state: dict) -> list[dict]:
    """The reminders in the state, without time reminders that are long overdue."""
    cutoff = time.time() - STALE
    return [r for r in state.get("reminders") or [] if isinstance(r, dict) and not (r.get("at") and r["at"] < cutoff)]


async def game_reminders() -> tuple[dict[int, dict] | None, str | None]:
    """What the game knows: reminder id -> {fired: unix time if it has}; or None and why not."""
    try:
        reply, _ = await request(kinds.STATE)
        entries = as_list(section(reply, "reminders"))
    except ToolError as e:
        return None, str(e)
    return {int(e["id"]): e for e in entries if isinstance(e, dict) and isinstance(e.get("id"), (int, float))}, None


@tool(
    annotations=ToolAnnotations(
        title="Set an in-game reminder", read_only_hint=False, destructive_hint=False, open_world_hint=False
    )
)
async def set_reminder(
    text: Annotated[str, Field(min_length=1, max_length=300, description="What to remind the player of.")],
    in_minutes: Annotated[
        float | None, Field(gt=0, le=MAX_DAYS * 24 * 60, description="Fire this many minutes from now.")
    ] = None,
    at_time: Annotated[
        str | None,
        Field(
            max_length=40,
            description="Fire at this local time on the player's computer: 'HH:MM' (24-hour, the next time it "
            "comes round) or 'YYYY-MM-DD HH:MM'.",
        ),
    ] = None,
    zone: Annotated[
        str | None,
        Field(
            max_length=60,
            description="Fire when the player is in this zone or city (name as shown in game, e.g. 'Orgrimmar'). "
            "Also matches places inside it on the map.",
        ),
    ] = None,
    subzone: Annotated[
        str | None,
        Field(
            max_length=60,
            description="Fire when the player is in this subzone (e.g. 'Razor Hill'); with zone, both must match.",
        ),
    ] = None,
    near: Annotated[Near | None, Field(description="Fire when the player gets within `yards` of this spot.")] = None,
) -> str:
    """Set a reminder that pops up in the player's game (on screen and in chat, with an alarm sound) at a
    time, or when they reach a place.

    Give exactly one trigger: in_minutes, at_time, zone and/or subzone, or near. Place reminders fire as
    soon as the player is there, so right away if they already are. Each reminder fires once.
    """
    triggers = [in_minutes is not None, at_time is not None, bool(zone or subzone), near is not None]
    if sum(triggers) != 1:
        raise ToolError("Give exactly one trigger: in_minutes, at_time, zone/subzone, or near.")
    if not text.strip():
        raise ToolError("The reminder text is empty.")
    now = datetime.now().astimezone()
    reminder: dict = {"text": text.strip(), "created": int(now.timestamp())}
    if in_minutes is not None:
        reminder["at"] = int(now.timestamp() + in_minutes * 60)
    elif at_time is not None:
        due = parse_time(at_time, now.replace(tzinfo=None)).astimezone()
        if due <= now:
            raise ToolError(f"{at_time} has already passed (it's {now:%Y-%m-%d %H:%M} now).")
        if due - now > timedelta(days=MAX_DAYS):
            raise ToolError(f"That's more than {MAX_DAYS} days away.")
        reminder["at"] = int(due.timestamp())
    elif near is not None:
        reminder["near"] = {"zone": near.zone, "x": round(near.x, 2), "y": round(near.y, 2), "yards": near.yards}
    else:
        reminder["zone"], reminder["subzone"] = zone or None, subzone or None

    # Make room by dropping the ones that have fired, if the game can tell us.
    fired: set[int] = set()
    if len(current(transport().load_state())) >= MAX_REMINDERS:
        known, _ = await game_reminders()
        fired = {i for i, e in (known or {}).items() if e.get("fired")}

    def change(state: dict, seq: int) -> None:
        kept = [r for r in current(state) if r.get("id") not in fired]
        if len(kept) >= MAX_REMINDERS:
            raise ToolError(f"There are already {len(kept)} reminders; cancel some first (see list_reminders).")
        state["reminders"] = [*kept, {"id": seq, **reminder}]

    reminder_id = transport().publish(change)
    result = f"Reminder #{reminder_id} set: {reminder['text']!r}, {describe(reminder)}."
    if in_minutes is not None or at_time is not None:
        result += f" (It's {now:%H:%M} now.)"
    return result + " The game confirms it in the player's chat." + delivery_note()


class Reminder(BaseModel):
    id: int
    text: str
    trigger: str = Field(description="When it fires.")
    status: Literal["waiting", "fired", "not in game yet", "unknown"] = Field(
        description="'not in game yet': the game hasn't received it (live updates off: the player needs to "
        "/reload). 'unknown': the game couldn't be asked (see note)."
    )
    fired_at: str | None = Field(default=None, description="Local time it fired.")


class Reminders(BaseModel):
    reminders: list[Reminder]
    note: str | None = Field(default=None, description="Why the status is unknown, if it is.")


@tool(annotations=ToolAnnotations(title="List in-game reminders", read_only_hint=True, open_world_hint=False))
async def list_reminders() -> Reminders:
    """List the reminders set with set_reminder, and whether each has fired in game yet."""
    reminders = current(transport().load_state())
    known, problem = await game_reminders() if reminders else ({}, None)

    def info(r: dict) -> Reminder:
        entry = known.get(r["id"]) if known is not None else None
        fired = entry.get("fired") if entry else None
        if known is None:
            status = "unknown"
        elif entry is None:
            status = "not in game yet"
        else:
            status = "fired" if fired else "waiting"
        return Reminder(id=r["id"], text=r["text"], trigger=describe(r), status=status,
                        fired_at=clock(int(fired)) if fired else None)

    return Reminders(reminders=[info(r) for r in reminders], note=problem and f"Couldn't ask the game: {problem}")


@tool(
    annotations=ToolAnnotations(
        title="Cancel an in-game reminder", read_only_hint=False, destructive_hint=True, open_world_hint=False
    )
)
def cancel_reminder(
    id: Annotated[int, Field(description="The reminder's id, from set_reminder or list_reminders.")],
) -> str:
    """Cancel a reminder set with set_reminder (or remove one that has already fired)."""

    def change(state: dict, seq: int) -> None:
        kept = current(state)
        if not any(r.get("id") == id for r in kept):
            raise ToolError(f"There's no reminder #{id}; list_reminders shows them.")
        state["reminders"] = [r for r in kept if r.get("id") != id]

    transport().publish(change)
    return f"Reminder #{id} cancelled."
