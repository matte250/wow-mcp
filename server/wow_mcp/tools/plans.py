"""Plans: step-by-step guides shown in game as a checklist that ticks itself off (payload key "plan",
Plans.lua). The game reports progress in the "plan" section of request kind STATE."""

from typing import Annotated, Literal

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag

from wow_mcp import kinds
from wow_mcp.app import as_list, guidance, request, section, tool, transport
from wow_mcp.tools.areas import Area
from wow_mcp.tools.messages import delivery_note
from wow_mcp.tools.routes import Stop

guidance(
    "When the player wants something done that takes several steps (craft an item, a quest chain, 'plan my next "
    "quests', leveling, gathering or farming materials, getting a recipe, a flight path or a mount), build a plan "
    "with show_plan. It shows in game as a checklist that guides them step by step: the navigator arrow points to "
    "each step's places, and every step ticks itself off as the game sees it done. First check what the player "
    "already has and knows, so the plan only covers what's missing: get_inventory (include_bank=True) for "
    "materials, get_vendor_items for what vendors they've visited sell, get_quest_log and get_quest_status for "
    "quests, get_professions and get_known_recipes, get_flight_paths and get_player_location for travel. Prefer "
    "exact game data for this game version: item IDs for 'have' conditions, quest IDs for quest steps, recipe "
    "spell IDs, real coordinates. Give every step conditions the game can check (have, quest_*, level, skill, "
    "recipe, at, zone) so it ticks off by itself; a step without conditions needs the player to tick it. Put "
    "places on every step the player has to walk to (bank, vendors, trainers, quest givers, flight masters, "
    "farming spots with loop=true) and areas on kill or gathering regions. A plan replaces the route from "
    "show_route. When the player asks about their plan or seems stuck, call get_plan_progress; to re-plan, send "
    "a new plan.",
    order=12,
)


class _Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Have(_Condition):
    """Done when the player has `count` of an item: in their bags, or bags plus bank with bank=true."""

    have: Annotated[int, Field(gt=0, description="Item ID for this game version (e.g. from Wowhead).")]
    count: Annotated[int, Field(ge=1, le=10000, description="How many they need.")] = 1
    bank: Annotated[
        bool,
        Field(description="Also count the bank. Use it for 'take it out of the bank' steps; leave it off for "
              "steps that need the items carried (crafting, turning in)."),
    ] = False
    name: Annotated[
        str | None, Field(max_length=80, description="The item's name, shown until the game has loaded the item.")
    ] = None


class Spot(BaseModel):
    zone: Annotated[str, Field(min_length=1, max_length=60, description="Zone or city name as shown in game.")]
    x: Annotated[float, Field(ge=0, le=100, description="X on that zone's map, 0-100.")]
    y: Annotated[float, Field(ge=0, le=100, description="Y on that zone's map, 0-100.")]
    map_id: Annotated[int | None, Field(gt=0, description="Optional uiMapID, only if you're sure.")] = None


class At(_Condition):
    """Done when the player is within `yards` of a spot. Only checked while the step is the current one."""

    at: Spot
    yards: Annotated[float, Field(ge=3, le=300, description="How close they have to get.")] = 12


class InZone(_Condition):
    """Done when the player is in this zone or city (or a map inside it). Only checked for the current step."""

    zone: Annotated[str, Field(min_length=1, max_length=60, description="As named in game, e.g. 'Ironforge'.")]


class InSubzone(_Condition):
    """Done when the player is in this subzone, e.g. 'Razor Hill'. Only checked for the current step."""

    subzone: Annotated[str, Field(min_length=1, max_length=60)]


class QuestAccepted(_Condition):
    """Done when the quest is in the player's quest log (or already turned in)."""

    quest_accepted: Annotated[int, Field(gt=0, description="Quest ID for this game version.")]
    name: Annotated[str | None, Field(max_length=80, description="Quest title, shown until the game knows it.")] = None


class QuestComplete(_Condition):
    """Done when all the quest's objectives are done (ready to turn in), or it's turned in."""

    quest_complete: Annotated[int, Field(gt=0, description="Quest ID for this game version.")]
    name: Annotated[str | None, Field(max_length=80, description="Quest title, shown until the game knows it.")] = None


class QuestObjective(_Condition):
    """Done when one objective of a quest in the log is done (e.g. the kills, before the items)."""

    quest_objective: Annotated[int, Field(gt=0, description="Quest ID for this game version.")]
    index: Annotated[int, Field(ge=1, le=10, description="Which objective, 1 = the first the quest log lists.")]
    name: Annotated[str | None, Field(max_length=80, description="Quest title, shown until the game knows it.")] = None


class QuestTurnedIn(_Condition):
    """Done when the quest is turned in."""

    quest_turned_in: Annotated[int, Field(gt=0, description="Quest ID for this game version.")]
    name: Annotated[str | None, Field(max_length=80, description="Quest title, shown until the game knows it.")] = None


class Level(_Condition):
    """Done when the character reaches this level."""

    level: Annotated[int, Field(ge=2, le=100)]


class Skill(_Condition):
    """Done when a profession or skill reaches this rank."""

    skill: Annotated[str, Field(min_length=1, max_length=60, description="As named in game, e.g. 'Leatherworking'.")]
    rank: Annotated[int, Field(ge=1, le=1000)]


class Recipe(_Condition):
    """Done when the player knows a recipe (learned from a trainer or a recipe item)."""

    recipe: Annotated[
        int | str,
        Field(description="The recipe's spell ID (best; from Wowhead's spell page for this version) or its exact "
              "name as the profession window shows it."),
    ]


class Manual(_Condition):
    """Never done by itself: the player ticks the step off. Only for what the game can't see."""

    manual: Literal[True]


GOAL_ID = r"^[a-z0-9][a-z0-9_-]{0,39}$"


class GoalDone(_Condition):
    """Done when a goal set with set_goals is done (and stays done, like a step). For gathering or anything else
    done alongside other steps: the step links the goal instead of describing it."""

    goal: Annotated[str, Field(pattern=GOAL_ID, description="The goal's id, as given to set_goals.")]


CONDITIONS = {
    "have": Have, "at": At, "zone": InZone, "subzone": InSubzone, "quest_accepted": QuestAccepted,
    "quest_complete": QuestComplete, "quest_objective": QuestObjective, "quest_turned_in": QuestTurnedIn,
    "level": Level, "skill": Skill, "recipe": Recipe, "manual": Manual, "goal": GoalDone,
}


def _kind(value: object) -> str | None:
    """Which kind of condition this is: its one kind key (None if it has none or several)."""
    if isinstance(value, _Condition):
        return next(k for k, cls in CONDITIONS.items() if isinstance(value, cls))
    if isinstance(value, dict):
        found = [k for k in value if k in CONDITIONS]
        return found[0] if len(found) == 1 else None
    return None


Condition = Annotated[
    Annotated[Have, Tag("have")] | Annotated[At, Tag("at")] | Annotated[InZone, Tag("zone")]
    | Annotated[InSubzone, Tag("subzone")] | Annotated[QuestAccepted, Tag("quest_accepted")]
    | Annotated[QuestComplete, Tag("quest_complete")] | Annotated[QuestObjective, Tag("quest_objective")]
    | Annotated[QuestTurnedIn, Tag("quest_turned_in")] | Annotated[Level, Tag("level")]
    | Annotated[Skill, Tag("skill")] | Annotated[Recipe, Tag("recipe")] | Annotated[Manual, Tag("manual")]
    | Annotated[GoalDone, Tag("goal")],
    Discriminator(
        _kind,
        custom_error_type="condition_kind",
        custom_error_message="A condition needs exactly one of these keys: " + ", ".join(CONDITIONS),
    ),
]

LUA_KEYS = {
    "quest_accepted": "questAccepted", "quest_complete": "questComplete", "quest_objective": "questObjective",
    "quest_turned_in": "questTurnedIn", "map_id": "mapID",
}


class Step(BaseModel):
    text: Annotated[
        str,
        Field(min_length=1, max_length=300, description="The instruction, e.g. 'Buy 2 Coarse Thread and 1 Salt from "
              "Duokna'. Say what to do and where; the checklist shows it as written."),
    ]
    places: Annotated[
        list[Stop],
        Field(max_length=40, description="Where to go for this step. One place: the navigator arrow points there. "
              "Several: a route through them in this order (e.g. farming spots, with loop=true)."),
    ] = []
    loop: Annotated[bool, Field(description="Go round the places until the step is done (farming, gathering).")] = False
    areas: Annotated[
        list[Area],
        Field(max_length=10, description="Regions shaded on the map while this step is current (where the mobs or "
              "herbs are)."),
    ] = []
    conditions: Annotated[
        list[Condition],
        Field(max_length=8, description="All must hold for the step to be done; the game checks them live. Each is "
              "an object with exactly one kind. Item, quest, level, skill and recipe conditions count even before the "
              "step is current; position ones (at, zone, subzone) only while it is. Empty: the player ticks it off."),
    ] = []


def _lua(value: object) -> object:
    """A model dump with the keys the addon reads (Lua identifiers, camelCase)."""
    if isinstance(value, dict):
        return {LUA_KEYS.get(k, k): _lua(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_lua(v) for v in value]
    return value


def _place(p: Stop) -> dict:
    return {"zone": p.zone, "x": round(p.x, 2), "y": round(p.y, 2), "label": p.label, "note": p.note, "mapID": p.map_id}


def _area(a: Area) -> dict:
    return {"zone": a.zone, "x": round(a.x, 2), "y": round(a.y, 2), "radius": round(a.radius, 2), "label": a.label,
            "color": a.color, "mapID": a.map_id}


def describe(c: _Condition) -> str:
    """A condition in words, for the tool's summary."""
    match c:
        case Have():
            return f"{c.count} x {c.name or f'item {c.have}'}{' (bags + bank)' if c.bank else ''}"
        case At():
            return f"within {c.yards:g} yd of {c.at.zone} {c.at.x:.1f}, {c.at.y:.1f}"
        case InZone():
            return f"in {c.zone}"
        case InSubzone():
            return f"in {c.subzone}"
        case QuestAccepted():
            return f"quest {c.quest_accepted} accepted"
        case QuestComplete():
            return f"quest {c.quest_complete} ready to turn in"
        case QuestObjective():
            return f"objective {c.index} of quest {c.quest_objective} done"
        case QuestTurnedIn():
            return f"quest {c.quest_turned_in} turned in"
        case Level():
            return f"level {c.level}"
        case Skill():
            return f"{c.skill} {c.rank}"
        case Recipe():
            return f"recipe {c.recipe} known"
        case GoalDone():
            return f"goal {c.goal!r} done"
    return "ticked by the player"


def _check_goals(steps: list["Step"]) -> None:
    """A step can only wait for a goal the game has, or it never ticks off."""
    goals = {g.get("id") for g in (transport().load_state().get("goals") or {}).get("goals") or []}
    for n, s in enumerate(steps, 1):
        for c in s.conditions:
            if isinstance(c, GoalDone) and c.goal not in goals:
                raise ToolError(f"Step {n} waits for goal {c.goal!r}, which isn't set: call set_goals first.")


@tool(
    annotations=ToolAnnotations(
        title="Show a plan in game", read_only_hint=False, destructive_hint=False, open_world_hint=False
    )
)
def show_plan(
    title: Annotated[str, Field(min_length=1, max_length=80, description="e.g. 'Toughened Leather Gloves'.")],
    steps: Annotated[list[Step], Field(min_length=1, max_length=50, description="In the order to do them.")],
) -> str:
    """Show the player a step-by-step plan in World of Warcraft: a checklist window that guides them through
    each step and ticks it off by itself as the game sees it done, then moves on.

    Use it for anything that takes several steps: crafting (bank, vendors, farming, trainer, crafting), quest
    chains and leveling routes, gathering. The current step's places drive the navigator arrow and the game's
    waypoint (a looping route for farming spots) and its areas are shaded on the map. Replaces any plan sent
    earlier and the route from show_route. The player can also tick steps off, go back, or hide the window.
    """
    _check_goals(steps)
    plan = {
        "title": title,
        "steps": [
            {
                "text": s.text,
                "places": [_place(p) for p in s.places],
                "loop": s.loop,
                "areas": [_area(a) for a in s.areas],
                "conditions": [_lua(c.model_dump()) for c in s.conditions],
            }
            for s in steps
        ],
    }
    plan_id = transport().publish(lambda state, seq: state.update(plan={"id": seq, **plan}))

    lines = []
    for n, s in enumerate(steps, 1):
        done = "; ".join(describe(c) for c in s.conditions) if s.conditions else "ticked by the player"
        lines.append(f"{n}. {s.text} [done when: {done}]")
    manual = sum(1 for s in steps if not any(not isinstance(c, Manual) for c in s.conditions))
    result = f"Sent plan #{plan_id} {title!r} with {len(steps)} steps to the game:\n" + "\n".join(lines)
    if manual:
        result += f"\n{manual} step(s) have nothing the game can check, so the player ticks them off."
    return result + "\nSteps already done in game tick off right away; get_plan_progress shows where they are." \
        + delivery_note()


class ConditionProgress(BaseModel):
    kind: str = Field(description="have, at, zone, subzone, questAccepted, questTurnedIn, questComplete, "
                      "questObjective, level, skill, recipe or manual.")
    progress: str = Field(description="As the checklist shows it, e.g. \"Spider's Silk 1/2\" or 'Go to Ironforge'.")
    met: bool


class StepProgress(BaseModel):
    step: int
    text: str | None
    done: bool = Field(description="Done stays done, e.g. a farming step after the items were crafted with.")
    ticked_by_hand: bool = Field(description="The player ticked it off rather than the game seeing it done.")
    current: bool
    needs_click: bool = Field(description="Not done, and nothing the game checks will tick it: the player has to.")
    progress: list[ConditionProgress] = Field(
        description="Live, for steps not done. Position conditions only count for the current step."
    )


class PlanProgress(BaseModel):
    game: str = Field(description="The WoW version being played.")
    active: bool = Field(description="Whether the game has a plan it's tracking.")
    plan_id: int | None = None
    title: str | None = None
    current_step: int | None = Field(default=None, description="Null when all steps are done.")
    steps_done: int = 0
    total_steps: int = 0
    complete: bool = False
    hidden: bool = Field(default=False, description="The player hid the window; progress is still tracked.")
    steps: list[StepProgress] = []
    note: str | None = None


@tool(annotations=ToolAnnotations(title="Progress on the in-game plan", read_only_hint=True, open_world_hint=False))
async def get_plan_progress() -> PlanProgress:
    """Get how far the player is with the plan sent with show_plan: which steps are done, the current step, and
    live progress on each step's conditions (e.g. "Spider's Silk 1/2"). Use it when the player asks about their
    plan or says they're stuck, and before re-planning."""
    reply, version = await request(kinds.STATE)
    game = section(reply, "plan")
    sent = transport().load_state().get("plan") or {}
    if not isinstance(game, dict) or not game.get("id"):
        note = "There's no plan in game. show_plan sends one."
        if isinstance(game, dict) and game.get("cleared"):
            note = "The player removed the plan (/mcp plan clear). show_plan sends a new one."
        elif sent.get("steps"):
            note = "The game hasn't received the plan yet (live updates off?): the player should type /reload."
        return PlanProgress(game=version, active=False, note=note)

    texts = [s.get("text") for s in sent.get("steps") or []] if sent.get("id") == game["id"] else []
    current = game.get("current")
    steps = []
    for n, entry in enumerate(as_list(game.get("steps")), 1):
        entry = entry if isinstance(entry, dict) else {}
        steps.append(StepProgress(
            step=n,
            text=texts[n - 1] if n <= len(texts) else None,
            done=bool(entry.get("done")),
            ticked_by_hand=bool(entry.get("byHand")),
            current=n == current,
            needs_click=bool(entry.get("needsClick")),
            progress=[
                ConditionProgress(kind=str(c.get("kind")), progress=str(c.get("text") or ""), met=bool(c.get("met")))
                for c in as_list(entry.get("progress")) if isinstance(c, dict)
            ],
        ))
    notes = []
    if sent.get("id") and sent["id"] != game["id"]:
        notes.append("The game still shows an older plan: the newest hasn't arrived (the player should /reload).")
    if game.get("problem"):
        notes.append(f"A condition couldn't be checked in game: {game['problem']}")
    return PlanProgress(
        game=version,
        active=True,
        plan_id=game["id"],
        title=game.get("title"),
        current_step=current,
        steps_done=sum(s.done for s in steps),
        total_steps=len(steps),
        complete=bool(game.get("complete")),
        hidden=bool(game.get("hidden")),
        steps=steps,
        note=" ".join(notes) or None,
    )


@tool(
    annotations=ToolAnnotations(
        title="Clear the in-game plan", read_only_hint=False, destructive_hint=True, open_world_hint=False
    )
)
def clear_plan() -> str:
    """Remove the plan sent with show_plan: its checklist window, its navigator directions and highlighted areas."""
    transport().publish(lambda state, seq: state.update(plan={"id": seq, "steps": []}))
    return "Cleared the plan."
