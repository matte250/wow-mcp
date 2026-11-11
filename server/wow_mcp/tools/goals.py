"""Goals: long-running targets that track themselves in the background, next to the one guided plan, and
point out chances to make progress (payload key "goals", Goals.lua). The game reports progress in the "goals"
section of request kind STATE."""

from datetime import datetime, timezone
from typing import Annotated

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool, transport
from wow_mcp.tools.messages import delivery_note
from wow_mcp.tools.plans import (
    GOAL_ID, Have, Level, QuestAccepted, QuestComplete, QuestObjective, QuestTurnedIn, Recipe, Skill,
    _lua, describe, sent_plans,
)

guidance(
    "One plan guides the player at a time, but goals run in the background, any number of them: set_goals. Make "
    "open-ended gathering, farming or anything done alongside other activity (e.g. '6 more Medium Hide while "
    "questing in the Wetlands', 'Leatherworking 125', 'Cooking 150') a goal instead of a vague plan step, and have "
    "the plan wait for it with a {goal: '<id>'} step condition (e.g. step 'Goal: 6 Medium Hide'). A goal tracks "
    "its target itself (the same conditions as plan steps: have, skill, level, recipe, quest_*), shows in a small "
    "list under the plan window, and pings the player when there's a chance to make progress: give it the zones "
    "and areas where it's found and the mobs to kill, skin or loot for it (with a note per mob, e.g. 'skin'). "
    "Vendors and the auction house that sell a goal's item are pointed out without being listed. get_goals shows "
    "progress; clear_goals removes finished or abandoned ones.",
    order=13,
)

MAX_GOALS = 10

TARGETS = {
    "have": Have, "skill": Skill, "level": Level, "recipe": Recipe, "quest_accepted": QuestAccepted,
    "quest_complete": QuestComplete, "quest_objective": QuestObjective, "quest_turned_in": QuestTurnedIn,
}


def _target_kind(value: object) -> str | None:
    if isinstance(value, BaseModel):
        return next((k for k, cls in TARGETS.items() if isinstance(value, cls)), None)
    if isinstance(value, dict):
        found = [k for k in value if k in TARGETS]
        return found[0] if len(found) == 1 else None
    return None


Target = Annotated[
    Annotated[Have, Tag("have")] | Annotated[Skill, Tag("skill")] | Annotated[Level, Tag("level")]
    | Annotated[Recipe, Tag("recipe")] | Annotated[QuestAccepted, Tag("quest_accepted")]
    | Annotated[QuestComplete, Tag("quest_complete")] | Annotated[QuestObjective, Tag("quest_objective")]
    | Annotated[QuestTurnedIn, Tag("quest_turned_in")],
    Discriminator(
        _target_kind,
        custom_error_type="target_kind",
        custom_error_message="A goal's target needs exactly one of these keys: " + ", ".join(TARGETS),
    ),
]


class _Source(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoalZone(_Source):
    zone: Annotated[str, Field(min_length=1, max_length=60, description="Zone or city name as shown in game.")]
    note: Annotated[
        str | None,
        Field(max_length=120, description="Said when the player enters, e.g. 'Beasts here skin into Medium Hide'."),
    ] = None


class GoalArea(_Source):
    zone: Annotated[str, Field(min_length=1, max_length=60, description="Zone or city name as shown in game.")]
    x: Annotated[float, Field(ge=0, le=100, description="Center X on that zone's map, 0-100.")]
    y: Annotated[float, Field(ge=0, le=100, description="Center Y on that zone's map, 0-100.")]
    radius: Annotated[float, Field(ge=0.5, le=50, description="Radius in percent of the map's width.")] = 4
    label: Annotated[
        str,
        Field(min_length=1, max_length=120, description="Said when the player gets there, e.g. 'Mottled Raptors "
              "here skin into Medium Hide'."),
    ]
    map_id: Annotated[int | None, Field(gt=0, description="Optional uiMapID, only if you're sure.")] = None


class GoalMob(_Source):
    name: Annotated[str, Field(min_length=1, max_length=60, description="As named in game, e.g. 'Mottled Raptor'.")]
    npc_id: Annotated[int | None, Field(gt=0, description="Its NPC ID for this game version, if known.")] = None
    note: Annotated[
        str | None,
        Field(max_length=40, description="What to do with it: 'skin', 'kill', 'loot', 'pickpocket'. Shown on its "
              "tooltip as e.g. 'Skin for Medium Hide: goal 2/6'."),
    ] = None


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[
        str,
        Field(pattern=GOAL_ID, description="Short, lowercase, e.g. 'medium-hide'. Setting a goal with the same id "
              "again updates it; a plan step waits for it with {goal: id}."),
    ]
    title: Annotated[str, Field(min_length=1, max_length=60, description="e.g. '6 Medium Hide'.")]
    target: Annotated[
        Target,
        Field(description="When it's done, checked live by the game like a plan step condition, e.g. {have: 4232, "
              "count: 6} (counts the bags; bank: true counts the bank too) or {skill: 'Leatherworking', rank: 125}."),
    ]
    zones: Annotated[list[GoalZone], Field(max_length=10, description="Zones where it can be worked on.")] = []
    areas: Annotated[
        list[GoalArea], Field(max_length=10, description="Spots where it's found; more precise than zones.")
    ] = []
    mobs: Annotated[list[GoalMob], Field(max_length=20, description="Mobs that give it.")] = []
    note: Annotated[
        str | None, Field(max_length=200, description="Shown with the goal, e.g. how to go about it.")
    ] = None


def _payload_goal(g: Goal) -> dict:
    return {
        "id": g.id,
        "title": g.title,
        "target": _lua(g.target.model_dump()),
        "zones": [{"zone": z.zone, "note": z.note} for z in g.zones],
        "areas": [{"zone": a.zone, "x": round(a.x, 2), "y": round(a.y, 2), "radius": round(a.radius, 2),
                   "label": a.label, "mapID": a.map_id} for a in g.areas],
        "mobs": [{"name": m.name, "npc": m.npc_id, "note": m.note} for m in g.mobs],
        "note": g.note,
    }


def _same(a: dict, b: dict) -> bool:
    """Whether a goal is unchanged (its rev aside): an unchanged goal keeps its progress in game."""
    return {k: v for k, v in a.items() if k != "rev"} == {k: v for k, v in b.items() if k != "rev"}


def _sources(g: Goal) -> str:
    where = [f"in {z.zone}" for z in g.zones] + [f"at {a.label!r} ({a.zone} {a.x:.1f}, {a.y:.1f})" for a in g.areas]
    where += [f"on {m.name}" + (f" ({m.note})" if m.note else "") for m in g.mobs]
    if isinstance(g.target, Have):
        where.append("at vendors and the auction house selling it")
    return "; ".join(where) or "nowhere in particular (no zones, areas or mobs given)"


@tool(
    annotations=ToolAnnotations(
        title="Set background goals in game", read_only_hint=False, destructive_hint=False, open_world_hint=False
    )
)
def set_goals(
    goals: Annotated[list[Goal], Field(min_length=1, max_length=MAX_GOALS)],
    replace: Annotated[
        bool, Field(description="Remove all earlier goals first. By default goals are added to (or update) them.")
    ] = False,
) -> str:
    """Set long-running goals the player works on in the background, alongside the one guided plan: open-ended
    gathering or farming, a skill level, anything done while doing other things. Each tracks itself in game,
    shows in a small list under the plan window, and points out chances to make progress: entering its zones
    or areas, hovering its mobs (a tooltip line), a vendor or the auction house selling its item, and progress
    as it comes in. Completion plays a sound; the goal stays shown as done until cleared. A plan step can wait
    for a goal with the condition {goal: id}. Goals merge by id: the same id again updates that goal."""
    ids = [g.id for g in goals]
    if len(set(ids)) != len(ids):
        raise ToolError("Each goal needs its own id.")
    total = 0

    def change(state: dict, seq: int) -> None:
        nonlocal total
        before = {g["id"]: g for g in (state.get("goals") or {}).get("goals") or [] if isinstance(g, dict)}
        merged = {} if replace else dict(before)
        for g in goals:
            new = _payload_goal(g)
            old = before.get(g.id)
            new["rev"] = old["rev"] if old and _same(old, new) else seq
            merged[g.id] = new
        if len(merged) > MAX_GOALS:
            raise ToolError(f"That makes {len(merged)} goals; at most {MAX_GOALS}. clear_goals removes some first.")
        total = len(merged)
        state["goals"] = {"id": seq, "goals": list(merged.values())}

    transport().publish(change)
    lines = [f"- {g.id} {g.title!r}: done when {describe(g.target)}; pings {_sources(g)}" for g in goals]
    return (
        f"Set {len(goals)} goal(s); {total} in game now:\n" + "\n".join(lines)
        + "\nA plan step waits for one with the condition {goal: '<id>'}. get_goals shows their progress."
        + delivery_note()
    )


class GoalProgress(BaseModel):
    id: str
    title: str | None = Field(description="Null if this server didn't set it (an older goal in game).")
    target: str | None = Field(description="When it's done, in words.")
    progress: str = Field(description="As the game shows it, e.g. 'Medium Hide 2/6'.")
    done: bool = Field(description="Done stays done, e.g. after crafting with the gathered items.")
    done_at: datetime | None = None
    met_now: bool = Field(description="Whether the target holds right now.")
    muted: bool = Field(description="The player muted it: it doesn't point anything out.")
    cleared: bool = Field(description="The player removed it from the list in game.")


class GoalsProgress(BaseModel):
    game: str = Field(description="The WoW version being played.")
    goals: list[GoalProgress]
    hidden: bool = Field(default=False, description="The player hid the goals window; they're still tracked.")
    note: str | None = None


@tool(annotations=ToolAnnotations(title="Progress on the in-game goals", read_only_hint=True, open_world_hint=False))
async def get_goals() -> GoalsProgress:
    """Get the background goals set with set_goals and the player's progress on each, live from the game (e.g.
    'Medium Hide 2/6'), with which are done, muted or cleared in game."""
    reply, version = await request(kinds.STATE)
    game = section(reply, "goals")
    game = game if isinstance(game, dict) else {}
    sent = transport().load_state().get("goals") or {}
    sent_goals = {g["id"]: g for g in sent.get("goals") or [] if isinstance(g, dict)}
    result = []
    for entry in as_list(game.get("goals")):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        mine = sent_goals.get(entry["id"])
        done = entry.get("done")
        result.append(GoalProgress(
            id=entry["id"],
            title=mine.get("title") if mine else None,
            target=_described(mine.get("target")) if mine else None,
            progress=str(entry.get("progress") or ""),
            done=bool(done),
            done_at=datetime.fromtimestamp(done, timezone.utc) if isinstance(done, (int, float)) else None,
            met_now=bool(entry.get("met")),
            muted=bool(entry.get("muted")),
            cleared=bool(entry.get("cleared")),
        ))
    notes = []
    if sent.get("id") and game.get("id") != sent["id"]:
        notes.append("The game hasn't received the newest goals yet (live updates off?): the player should /reload.")
    if not result:
        notes.append("There are no goals in game. set_goals sets some.")
    if game.get("problem"):
        notes.append(f"A goal couldn't be checked in game: {game['problem']}")
    return GoalsProgress(game=version, goals=result, hidden=bool(game.get("hidden")), note=" ".join(notes) or None)


def _described(target: dict | None) -> str | None:
    """A target as sent (Lua keys) in words."""
    if not isinstance(target, dict):
        return None
    keys = {"questAccepted": "quest_accepted", "questComplete": "quest_complete", "questObjective": "quest_objective",
            "questTurnedIn": "quest_turned_in"}
    raw = {keys.get(k, k): v for k, v in target.items()}
    kind = _target_kind(raw)
    try:
        return describe(TARGETS[kind](**raw)) if kind else None
    except ValueError:
        return None


@tool(
    annotations=ToolAnnotations(
        title="Clear in-game goals", read_only_hint=False, destructive_hint=True, open_world_hint=False
    )
)
def clear_goals(
    ids: Annotated[list[str] | None, Field(description="The goals to remove, by id.")] = None,
    all: Annotated[bool, Field(description="Remove every goal.")] = False,
) -> str:
    """Remove goals set with set_goals: finished ones the player no longer needs to see, or ones they gave up."""
    if not ids and not all:
        raise ToolError("Give the ids of the goals to remove, or all=True.")
    removed: list[str] = []
    linked: list[str] = []

    def change(state: dict, seq: int) -> None:
        current = [g for g in (state.get("goals") or {}).get("goals") or [] if isinstance(g, dict)]
        known = {g["id"] for g in current}
        if not all and (unknown := [i for i in ids or [] if i not in known]):
            raise ToolError(f"No goal {', '.join(map(repr, unknown))}; get_goals lists them.")
        removed[:] = [g["id"] for g in current if all or g["id"] in (ids or [])]
        state["goals"] = {"id": seq, "goals": [g for g in current if g["id"] not in removed]}
        plans = sent_plans(state)
        for plan in plans:
            for n, step in enumerate(plan.get("steps") or [], 1):
                if any(isinstance(c, dict) and c.get("goal") in removed for c in step.get("conditions") or []):
                    linked.append(str(n) if len(plans) == 1 else f"{n} of {plan.get('tab')!r}")

    transport().publish(change)
    result = f"Removed {len(removed)} goal(s): {', '.join(removed) or 'none'}."
    if linked:
        result += (f" The plan's step(s) {', '.join(linked)} waited for them and can now only be ticked off by hand; "
                   "send a new plan if that's not what the player wants.")
    return result

